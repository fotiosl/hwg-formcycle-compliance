"""
Wiederholter Benchmark der semantischen Prüfstufe (Stufe 2) mit vollständiger Rohdatenablage.

Für jedes Modell × Formular × Wiederholung wird die komplette Pipeline (Stufe 1 + Stufe 2) über
die konfigurierte OpenAI-kompatible API ausgeführt. Gespeichert werden je Lauf das vollständige
Ergebnis inklusive Rohantwort des Modells, Prompt-Hash, Latenz und Versionen unter
data/evaluation/llm_runs/<Zeitstempel>/. Zusätzlich entstehen eine Zusammenfassung (summary.json)
und die LaTeX-Tabellen thesis/generated/tab_modellvergleich.tex und tab_hybrid.tex.

Voraussetzung: LLM_API_KEY (bzw. GWDG_API_KEY) in der Umgebung oder .env; für Ollama
--api-base http://localhost:11434/v1.

Beispiel:
    python scripts/run_llm_benchmark.py --runs 5
    python scripts/run_llm_benchmark.py --models qwen3.8:27b --api-base http://localhost:11434/v1 --runs 5 --no-tex
"""
import argparse
import collections
import json
import os
import statistics
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
os.chdir(REPO)

import urllib.request  # noqa: E402

from rule_engine import (  # noqa: E402
    DEFAULT_API_BASE,
    PROMPT_VERSION,
    SEMANTIC_RULE_IDS,
    _load_dotenv_if_present,
    evaluate_form,
    load_rules,
)

# Übertragungsfehler (Rate-Limit, Serverfehler, Zeitüberschreitung) werden wiederholt, weil sie nichts
# über das Modell aussagen. Ungültige Modellantworten (kein JSON, falsche Statuswerte) werden NICHT
# wiederholt; sie gehen als Messergebnis in die Auswertung ein.
TRANSPORT_MARKERS = ("HTTP-Fehler 429", "HTTP-Fehler 500", "HTTP-Fehler 502", "HTTP-Fehler 503", "HTTP-Fehler 504",
                     "(URLError)", "(TimeoutError)", "(timeout)", "(ConnectionResetError)", "(RemoteDisconnected)",
                     "(IncompleteRead)", "(ConnectionAbortedError)")
RETRY_WAIT_S = (20, 60, 180)


def is_transport_error(res: dict) -> bool:
    ev = res.get("llm_evaluation") or {}
    return ev.get("status") == "error" and any(m in str(ev.get("reason", "")) for m in TRANSPORT_MARKERS)


def resolve_endpoint(api_base_arg):
    _load_dotenv_if_present()
    server_base = (os.environ.get("LLM_API_BASE") or os.environ.get("LLM_BASE_URL")
                   or os.environ.get("GWDG_API_BASE") or DEFAULT_API_BASE).strip().rstrip("/")
    base = (api_base_arg or server_base).strip().rstrip("/")
    key = (os.environ.get("LLM_API_KEY") or os.environ.get("GWDG_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if base.lower() != server_base.lower():
        key = ""
    return base, key


def available_models(base: str, key: str):
    """Fragt /models ab. Rückgabe: Menge der Modell-IDs oder None, wenn die Liste nicht abrufbar ist."""
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(f"{base}/models", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return {m.get("id") for m in data.get("data", []) if isinstance(m, dict)}
    except Exception as exc:  # noqa: BLE001
        print(f"Hinweis: Modellliste unter {base}/models nicht abrufbar ({type(exc).__name__}).")
        return None

DEFAULT_MODELS = [
    "deepseek-v4-flash-0731",
    "qwen3-30b-a3b-instruct-2507",
    "qwen3.8-27b",
    "glm-5.3-flash",
    "meta-llama-3.1-8b-instruct",
]
MODEL_LABELS = {
    "deepseek-v4-flash-0731": "DeepSeek V4 Flash",
    "qwen3-30b-a3b-instruct-2507": "Qwen 3 30B A3B",
    "qwen3.8-27b": "Qwen 3.8 27B",
    "qwen3.8:27b": "Qwen 3.8 27B (lokal)",
    "glm-5.3-flash": "GLM 5.3 Flash",
    "meta-llama-3.1-8b-instruct": "Llama 3.1 8B",
}
FORMS = {
    "Synthetisch konform": "data/test_forms/synthetic_compliant_form.xfp",
    "Synthetisch fehlerhaft": "data/test_forms/synthetic_noncompliant_form.xfp",
    "Auslagenerstattung (Prod)": "data/raw_forms/prod/Auslagenerstattung _20260929.xfp",
    "Antrag-Sprints (alt)": "data/raw_forms/test/fachformulare/AFörderung Antrag-Sprints für Drittmittelanträge.xfp",
    "Antrag-Sprints (saniert)": "data/raw_forms/standard/AFörderung Antrag-Sprints für Drittmittelanträge (HWG-Standard).xfp",
    "Master-Formular (Vorlage)": "data/raw_forms/test/adminformulare/Master-Formular.xfp",
}
# Ground Truth der semantischen/hybriden Regeln für die synthetischen Formulare
GROUND_TRUTH = {
    "Synthetisch fehlerhaft": {
        "HWG-STAMM-02": "VIOLATION", "HWG-UI-01": "VIOLATION", "HWG-BTN-01": "VIOLATION",
        "HWG-LANG-01": "VIOLATION", "HWG-FEEDBACK-01": "VIOLATION", "HWG-LANG-02": "PASS", "HWG-UX-02": "NOT_APPLICABLE",
    },
    "Synthetisch konform": {rid: ("NOT_APPLICABLE" if rid == "HWG-UX-02" else "PASS") for rid in SEMANTIC_RULE_IDS},
}


def rule_status(res: dict, rid: str) -> str:
    if rid in res.get("unverified_semantic", []):
        return "UNVERIFIED"
    if any((v.get("rule") or v.get("rule_id")) == rid for v in res.get("violations", [])):
        return "VIOLATION"
    if any((r.get("rule") or r.get("rule_id")) == rid for r in res.get("recommendations", [])):
        return "RECOMMENDATION"
    if rid in res.get("not_applicable", {}):
        return "NOT_APPLICABLE"
    return "PASS"


def tex(s: str) -> str:
    return str(s).replace("&", r"\&").replace("%", r"\%").replace("_", r"\_")


def num(x, d=2):
    return f"{x:.{d}f}".replace(".", "{,}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--api-base", default=None)
    ap.add_argument("--reference-model", default="deepseek-v4-flash-0731")
    ap.add_argument("--no-tex", action="store_true", help="LaTeX-Tabellen nicht überschreiben")
    args = ap.parse_args()

    reg = load_rules()

    # Vorabprüfung: Schlüssel vorhanden und Modelle beim Anbieter verfügbar?
    base, key = resolve_endpoint(args.api_base)
    is_local = base.lower().startswith(("http://localhost", "http://127.0.0.1"))
    if not key and not is_local:
        sys.exit(f"Abbruch: Kein API-Schlüssel für {base} gefunden. Bitte in .env z. B. GWDG_API_KEY=... eintragen.")
    skipped = []
    ids = available_models(base, key)
    if ids is not None:
        skipped = [m for m in args.models if m not in ids]
        for m in skipped:
            print(f"Hinweis: Modell '{m}' ist unter {base} nicht verfügbar und wird übersprungen.")
        args.models = [m for m in args.models if m in ids]
        if not args.models:
            sys.exit("Abbruch: Keines der angefragten Modelle ist verfügbar. Verfügbar sind: " + ", ".join(sorted(i for i in ids if i)))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join("data", "evaluation", "llm_runs", stamp)
    os.makedirs(out_dir, exist_ok=True)
    records = []
    total = len(args.models) * len(FORMS) * args.runs
    done = 0
    t_start = time.perf_counter()
    for model in args.models:
        for fname, path in FORMS.items():
            for run in range(1, args.runs + 1):
                cfg = {"model": model, "disable_fallback": True}
                if args.api_base:
                    cfg["api_base"] = args.api_base
                attempts = 0
                while True:
                    attempts += 1
                    t0 = time.perf_counter()
                    res = evaluate_form(path, reg, llm_config=cfg)
                    wall = (time.perf_counter() - t0) * 1000
                    if is_transport_error(res) and attempts <= len(RETRY_WAIT_S):
                        wait = RETRY_WAIT_S[attempts - 1]
                        print(f"  Übertragungsfehler ({res['llm_evaluation'].get('reason')}); neuer Versuch in {wait} s")
                        time.sleep(wait)
                        continue
                    break
                rec = {
                    "model": model, "form": fname, "run": run, "wall_ms": round(wall, 1),
                    "statuses": {rid: rule_status(res, rid) for rid in SEMANTIC_RULE_IDS},
                    "score": res["score"], "release_status": res["release_status"],
                    "llm_status": res["llm_evaluation"].get("status"),
                    "llm_reason": res["llm_evaluation"].get("reason"),
                    "llm_latency_ms": res.get("provenance", {}).get("llm_latency_ms"),
                    "attempts": attempts,
                    "transport_error": is_transport_error(res),
                }
                records.append(rec)
                safe = f"{model.replace('/', '_').replace(':', '_')}__{fname.replace(' ', '_').replace('(', '').replace(')', '')}__run{run}.json"
                with open(os.path.join(out_dir, safe), "w", encoding="utf-8") as fh:
                    json.dump({"meta": rec, "result": res}, fh, ensure_ascii=False, indent=2)
                done += 1
                elapsed = time.perf_counter() - t_start
                eta_min = (elapsed / done) * (total - done) / 60
                print(f"[{done}/{total}, noch ca. {eta_min:.0f} min] {model:28} {fname:26} Lauf {run}: "
                      f"Score {res['score']:3} {res['release_status']:18} KI {rec['llm_status']}", flush=True)

    summary = {"created": stamp, "registry_version": reg["version"], "runs": args.runs, "api_base": base,
               "skipped_models": skipped, "models": {}}
    for model in args.models:
        recs = [r for r in records if r["model"] == model]
        tp = fp = fn = 0
        for r in recs:
            gt = GROUND_TRUTH.get(r["form"])
            if not gt:
                continue
            for rid, exp in gt.items():
                got = r["statuses"][rid]
                if exp == "VIOLATION" and got == "VIOLATION":
                    tp += 1
                elif exp == "VIOLATION":
                    fn += 1
                elif got == "VIOLATION":
                    fp += 1
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec_ = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec_ / (prec + rec_) if prec + rec_ else 0.0
        # Konsistenz: Anteil (Formular, Regel)-Paare mit identischem Status in allen Läufen
        by_pair = collections.defaultdict(set)
        for r in recs:
            for rid, st in r["statuses"].items():
                by_pair[(r["form"], rid)].add(st)
        consistency = sum(1 for v in by_pair.values() if len(v) == 1) / len(by_pair) if by_pair else 0.0
        complete = sum(1 for r in recs if r["llm_status"] == "ok") / len(recs) if recs else 0.0
        lat = [r["llm_latency_ms"] for r in recs if r["llm_latency_ms"]]
        summary["models"][model] = {
            "label": MODEL_LABELS.get(model, model), "runs_total": len(recs),
            "synthetic_tp": tp, "synthetic_fp": fp, "synthetic_fn": fn,
            "precision": prec, "recall": rec_, "f1": f1,
            "consistency": consistency, "complete_valid_share": complete,
            "latency_median_s": statistics.median(lat) / 1000 if lat else None,
            "transport_errors": sum(1 for r in recs if r["transport_error"]),
            "invalid_responses": sum(1 for r in recs if r["llm_status"] != "ok" and not r["transport_error"]),
        }
    # Referenzmodell: Mehrheitsentscheid je Formular für Tabelle 6.1
    ref = args.reference_model if args.reference_model in args.models else args.models[0]
    hybrid_rows = []
    for fname in FORMS:
        recs = [r for r in records if r["model"] == ref and r["form"] == fname]
        if not recs:
            continue
        scores = sorted(r["score"] for r in recs)
        stat = collections.Counter(r["release_status"] for r in recs).most_common(1)[0][0]
        viol = collections.Counter()
        for r in recs:
            for rid, st in r["statuses"].items():
                if st == "VIOLATION":
                    viol[rid] += 1
        hybrid_rows.append({"form": fname, "score_min": scores[0], "score_max": scores[-1], "status": stat,
                            "semantic_violations": {k: v for k, v in viol.items()}, "runs": len(recs)})
    summary["reference_model"] = ref
    summary["hybrid_rows"] = hybrid_rows
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    if not args.no_tex:
        status_de = {"green_compliant": "Grün", "amber_needs_review": "Orange", "red_blocked": "Rot", "pending_ai_review": "vorläufig"}
        with open("thesis/generated/tab_modellvergleich.tex", "w", encoding="utf-8") as fh:
            fh.write("% Automatisch erzeugt von scripts/run_llm_benchmark.py – nicht von Hand bearbeiten.\n")
            fh.write(f"\\renewcommand{{\\ModellvergleichHinweis}}{{Prompt v{PROMPT_VERSION}, je Modell und Formular {args.runs} Läufe; Rohdaten: \\texttt{{llm\\_runs/{stamp}}}}}\n")
            fh.write("\\renewcommand{\\ModellvergleichZeilen}{%\n")
            for m, s in summary["models"].items():
                lat = num(s["latency_median_s"], 1) if s["latency_median_s"] is not None else "--"
                fh.write(f"\\textit{{{tex(s['label'])}}} & {s['synthetic_tp']}/{s['synthetic_tp'] + s['synthetic_fn']} & {s['synthetic_fp']} & {num(s['f1'])} & {num(100 * s['consistency'], 0)}\\,\\% & {num(100 * s['complete_valid_share'], 0)}\\,\\% & {lat} \\\\\n")
            fh.write("}\n")
        with open("thesis/generated/tab_hybrid.tex", "w", encoding="utf-8") as fh:
            fh.write("% Automatisch erzeugt von scripts/run_llm_benchmark.py – nicht von Hand bearbeiten.\n")
            fh.write(f"\\renewcommand{{\\HybridHinweis}}{{Referenzmodell {tex(MODEL_LABELS.get(ref, ref))}, Prompt v{PROMPT_VERSION}, {args.runs} Läufe je Formular}}\n")
            for row in hybrid_rows:
                key = {"Synthetisch konform": "SynthKonform", "Synthetisch fehlerhaft": "SynthFehler", "Auslagenerstattung (Prod)": "Auslagen",
                       "Antrag-Sprints (alt)": "SprintsAlt", "Antrag-Sprints (saniert)": "SprintsNeu", "Master-Formular (Vorlage)": "Master"}[row["form"]]
                rng = f"{row['score_min']}" if row["score_min"] == row["score_max"] else f"{row['score_min']}--{row['score_max']}"
                sv = ", ".join(f"\\texttt{{{k.replace('HWG-', '')}}} ({v}/{row['runs']})" for k, v in sorted(row["semantic_violations"].items())) or "keine"
                fh.write(f"\\renewcommand{{\\Hybrid{key}Score}}{{{rng}}}\n")
                fh.write(f"\\renewcommand{{\\Hybrid{key}Status}}{{{status_de.get(row['status'], row['status'])}}}\n")
                fh.write(f"\\renewcommand{{\\Hybrid{key}Befunde}}{{{sv}}}\n")
    print(json.dumps(summary["models"], ensure_ascii=False, indent=2))
    print(f"Rohdaten: {out_dir}")


if __name__ == "__main__":
    main()
