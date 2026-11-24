"""
Stufe 1 des Annotationsabgleichs im blinden Stand nachbilden und mit dem Endstand vergleichen.

Blind war nur der erste Abgleich (Engine/Registry v1.6.1, Commit 722e017, vor der Korrektur der
Sichtbarkeitslogik). Das Skript entpackt diesen Stand mit `git archive` in ein temporäres Verzeichnis,
wertet damit die fünf annotierten Formulare ohne Sprachmodell aus und vergleicht die 75 Paare der
Stufe 1 mit data/evaluation/holdout_results.json (Endstand). Ausgegeben werden kappa, das
95-%-Bootstrap-Intervall (Verfahren und Startwert wie holdout_eval.kappa_ci) und das Intervall der
gepaarten Differenz über dieselben Ziehungen.

Aufruf (Repository-Wurzel):  python scripts/holdout_blindstand.py
Ausgabe: data/evaluation/holdout_blindstand.json
"""
import csv
import io
import json
import os
import random
import subprocess
import sys
import tarfile
import tempfile

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
BLIND_COMMIT = "722e017"
SEM = {"HWG-STAMM-02", "HWG-UI-01", "HWG-BTN-01", "HWG-LANG-01", "HWG-LANG-02", "HWG-FEEDBACK-01", "HWG-UX-02"}
OUT = os.path.join(REPO, "data", "evaluation", "holdout_blindstand.json")


def inner(old_dir: str) -> None:
    """Läuft in einem eigenen Prozess mit der alten Engine; gibt die Stufe-1-Urteile als JSON aus."""
    sys.stdout.reconfigure(encoding="utf-8")  # -I ignoriert PYTHONUTF8
    os.environ["LLM_API_KEY"] = ""  # kein Sprachmodell
    os.environ["LLM_API_BASE"] = "http://127.0.0.1:9/v1"
    sys.path.insert(0, os.path.join(old_dir, "scripts"))
    import rule_engine as old  # noqa: E402
    reg = old.load_rules(os.path.join(old_dir, "data", "rules", "rules_registry.json"))
    rows = [r for r in csv.DictReader(open(os.path.join(REPO, "data/evaluation/holdout_annotations.csv"), encoding="utf-8-sig"),
                                      delimiter=";") if (r.get("expert_status") or "").strip()]
    forms = sorted({r["form_file"] for r in rows})
    os.chdir(REPO)
    out = {"engine": old.ENGINE_VERSION, "registry": reg.get("version"), "statuses": {}}
    for form in forms:
        res = old.evaluate_form(form, reg)
        hits = {(v.get("rule") or v.get("rule_id")) for v in res.get("violations", []) + res.get("recommendations", [])}
        out["statuses"][form] = sorted(hits)
    print(json.dumps(out, ensure_ascii=False))


def kappa(pairs):
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return (po - pe) / (1 - pe) if pe < 1 else 0.0


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data = subprocess.run(["git", "archive", BLIND_COMMIT, "scripts", "data/rules"], cwd=REPO,
                              capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(tmp, filter="data")
        env = dict(os.environ, PYTHONUTF8="1", LLM_API_KEY="")
        proc = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), "--inner", tmp], cwd=tmp, env=env,
                              capture_output=True, text=True, encoding="utf-8", check=True)
        old = json.loads(proc.stdout.strip().splitlines()[-1])

    final = json.load(open(os.path.join(REPO, "data/evaluation/holdout_results.json"), encoding="utf-8"))
    keys, bp, fp = [], [], []
    for p in final["pairs"]:  # Reihenfolge wie in holdout_eval, damit die Ziehung kappa_ci entspricht
        if p["rule"] in SEM:
            continue
        exp = p["expert"] == "VIOLATION"
        keys.append((p["form"], p["rule"]))
        bp.append((exp, p["rule"] in old["statuses"][p["form"]]))
        fp.append((exp, p["engine"] in ("VIOLATION", "RECOMMENDATION")))

    rng = random.Random(20261105)
    draws, kb, kf, kd = 4000, [], [], []
    for _ in range(draws):
        idx = rng.choices(range(len(keys)), k=len(keys))
        a, b = kappa([bp[i] for i in idx]), kappa([fp[i] for i in idx])
        kb.append(a), kf.append(b), kd.append(b - a)
    ci = lambda v: [sorted(v)[int(0.025 * draws)], sorted(v)[int(0.975 * draws)]]  # noqa: E731

    def counts(pairs):
        return {"tp": sum(a and b for a, b in pairs), "fp": sum((not a) and b for a, b in pairs),
                "fn": sum(a and not b for a, b in pairs)}

    out = {
        "blind_commit": BLIND_COMMIT, "blind_engine": old["engine"], "blind_registry": old["registry"],
        "final_registry": json.load(open(os.path.join(REPO, "data/rules/rules_registry.json"), encoding="utf-8"))["version"],
        "pairs": len(keys),
        "blind": {**counts(bp), "kappa": kappa(bp), "kappa_ci": ci(kb)},
        "final": {**counts(fp), "kappa": kappa(fp), "kappa_ci": ci(kf)},
        "difference_ci": ci(kd), "share_difference_le_0": sum(d <= 0 for d in kd) / draws,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--inner":
        inner(sys.argv[2])
    else:
        main()
