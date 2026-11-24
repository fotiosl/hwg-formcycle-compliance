"""
Führt die Messläufe vor der Abgabe nacheinander aus und protokolliert alles:

  performance  Laufzeitmessung Stufe 1                (scripts/measure_performance.py --runs 20)
  benchmark    Modellvergleich über die GWDG-API       (scripts/run_llm_benchmark.py --runs 5)
  holdout      Abgleich mit der Annotation, Stufen 1+2 (scripts/holdout_eval.py --with-llm --runs 5);
               Stufe 2 immer mit dem Referenzmodell (LLM_MODEL wird dafür gesetzt)
  lokal        lokales Modell über Ollama              (scripts/run_llm_benchmark.py --models qwen3.8:27b
               --api-base <URL> --runs 5 --no-tex); nur auf dem KI-Server, nicht in der Standardauswahl
  build        Build der Arbeit                        (scripts/build_thesis.py)

Die Ausgaben erscheinen im Fenster und zusätzlich in data/evaluation/messlauf_<n>_<name>.log;
eine Übersicht mit Registry-, Engine- und Prompt-Version und Git-Stand steht in
data/evaluation/messlauf_status.json.

Aufruf (Repository-Wurzel):  python scripts/messlaeufe.py
Optionen:  --schritte performance,benchmark,holdout,build   (Standard; beliebige Auswahl, auch lokal)
           --lokal-url http://localhost:11434/v1            (Endpunkt für den Schritt lokal)
           --nur-build                                      nur der Build
           --ohne-benchmark                                 Standardauswahl ohne benchmark und holdout
"""
import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.chdir(REPO)

DEFAULT_STEPS = ["performance", "benchmark", "holdout", "build"]
# Referenzmodell wie in run_llm_benchmark.py (--reference-model); gilt für den Schritt holdout.
REFERENCE_MODEL = "deepseek-v4-flash-0731"


def step_table(lokal_url: str) -> dict:
    return {
        "performance": ("Laufzeitmessung Stufe 1 (ca. 1-3 Minuten)", ["scripts/measure_performance.py", "--runs", "20"]),
        "benchmark": ("Modellvergleich GWDG, 5 Modelle x 6 Formulare x 5 Läufe (ca. 30-90 Minuten)", ["scripts/run_llm_benchmark.py", "--runs", "5"]),
        "holdout": ("Abgleich mit der Annotation, Stufe 2 mit dem Referenzmodell, 5 Läufe je Formular (ca. 5-15 Minuten)", ["scripts/holdout_eval.py", "--with-llm", "--runs", "5"]),
        "lokal": (f"Lokales Modell Qwen 3.8 27B über {lokal_url}, 6 Formulare x 5 Läufe", ["scripts/run_llm_benchmark.py", "--models", "qwen3.8:27b", "--api-base", lokal_url, "--runs", "5", "--no-tex"]),
        "build": ("Build der Arbeit mit pdflatex und biber (ca. 1-3 Minuten)", ["scripts/build_thesis.py"]),
    }


def versions() -> dict:
    """Stand des eingefrorenen Artefakts, damit die Messwerte eindeutig zuzuordnen sind."""
    info = {}
    try:
        with open(os.path.join("data", "rules", "rules_registry.json"), encoding="utf-8") as fh:
            info["registry"] = json.load(fh).get("version")
    except Exception as exc:  # noqa: BLE001
        info["registry"] = f"unbekannt ({type(exc).__name__})"
    for key, path, name in (("engine", "scripts/rule_engine.py", "ENGINE_VERSION"), ("prompt", "scripts/generate_chat_ai_prompt.py", "PROMPT_VERSION")):
        try:
            with open(path, encoding="utf-8") as fh:
                line = next(l for l in fh if l.startswith(name))
            info[key] = line.split("=", 1)[1].strip().strip('"')
        except Exception as exc:  # noqa: BLE001
            info[key] = f"unbekannt ({type(exc).__name__})"
    try:
        info["git_commit"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        info["git_tag"] = subprocess.run(["git", "describe", "--tags", "--exact-match"], capture_output=True, text=True).stdout.strip() or None
        info["uncommitted_changes"] = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip())
    except Exception:  # noqa: BLE001
        info["git_commit"] = None
    return info


def run_step(idx: int, total: int, key: str, title: str, args: list) -> dict:
    log_path = os.path.join("data", "evaluation", f"messlauf_{idx}_{key}.log")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("LLM_TIMEOUT", "180")
    if key == "holdout":
        # holdout_eval.py nähme sonst LLM_MODEL aus Shell oder .env; am 16.11. lief der Schritt
        # dadurch einmal mit Qwen 3.8 27B statt mit dem Referenzmodell.
        env["LLM_MODEL"] = REFERENCE_MODEL
    print(f"\n=== [{idx}/{total}] {title} ===", flush=True)
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace")
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
        code = proc.wait()
    dur = round(time.time() - t0)
    print(f"--- Schritt {idx} beendet (Code {code}, {dur // 60} min {dur % 60} s), Protokoll: {log_path}", flush=True)
    out = {"step": key, "return_code": code, "duration_s": dur, "log": log_path}
    if key == "holdout":
        out["llm_model"] = env["LLM_MODEL"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schritte", default=",".join(DEFAULT_STEPS))
    ap.add_argument("--lokal-url", default="http://localhost:11434/v1")
    ap.add_argument("--nur-build", action="store_true")
    ap.add_argument("--ohne-benchmark", action="store_true")
    a = ap.parse_args()
    os.makedirs(os.path.join("data", "evaluation"), exist_ok=True)

    table = step_table(a.lokal_url)
    keys = [k.strip() for k in a.schritte.split(",") if k.strip()]
    if a.nur_build:
        keys = ["build"]
    elif a.ohne_benchmark:
        keys = [k for k in keys if k not in ("benchmark", "holdout")]
    unknown = [k for k in keys if k not in table]
    if unknown:
        sys.exit(f"Unbekannte Schritte: {', '.join(unknown)} (möglich: {', '.join(table)})")

    stand = versions()
    print("Messläufe für die Bachelorarbeit. Der Rechner sollte währenddessen nicht in den Ruhezustand gehen.")
    print(f"Stand: Registry {stand.get('registry')}, Engine {stand.get('engine')}, Prompt {stand.get('prompt')}, "
          f"Git {stand.get('git_commit')} {('(Tag ' + stand['git_tag'] + ')') if stand.get('git_tag') else '(kein Tag)'}"
          f"{', nicht committete Änderungen!' if stand.get('uncommitted_changes') else ''}")
    results = [run_step(i, len(keys), k, *table[k]) for i, k in enumerate(keys, start=1)]
    status = {"finished_at": time.strftime("%Y-%m-%d %H:%M"), "python": sys.version.split()[0], "stand": stand, "steps": results}
    with open(os.path.join("data", "evaluation", "messlauf_status.json"), "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=2)

    print("\n=== Übersicht ===")
    for r in results:
        print(f"  {r['step']:12} {'OK' if r['return_code'] == 0 else 'FEHLER (Code ' + str(r['return_code']) + ')'}")
    print("\nFertig. Du kannst das Fenster schließen und Claude Bescheid geben.")


if __name__ == "__main__":
    main()
