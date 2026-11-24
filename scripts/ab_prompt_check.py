"""
A/B-Kontrolle einer Prompt-Änderung durch redaktionelle Korrektur von Registry-Begründungen.

Beide Prompt-Fassungen laufen abwechselnd im selben Zeitraum mit demselben Modell. Eine Veränderung
des Modells beim Anbieter trifft so beide Arme gleich, und ein Unterschied zwischen den Armen lässt
sich auf den Prompt zurückführen. Der Arm "alt" nutzt die Registry aus Git (--old-rev) und die alte
Prompt-Version (--old-prompt-version), der Arm "neu" die aktuelle Registry und die aktuelle
Prompt-Version.

Standardwerte (Änderung 1.6.0 -> 1.6.1, HWG-LANG-01): Commit 86c01ea (Registry v1.6.2) mit Prompt 1.6.0;
dessen Prompt-Hash entspricht dem des Modellvergleichs vom 31.10.2026.
Änderung 1.6.1 -> 1.6.2 (HWG-LANG-02): --old-rev 520619e (Registry v1.6.3) --old-prompt-version 1.6.1.

Ergebnisse: data/evaluation/llm_runs/<Zeitstempel>-ab/ (Einzelläufe und summary.json).
Voraussetzung wie bei run_llm_benchmark.py: API-Schlüssel in der Umgebung oder in .env.

Beispiele:
    python scripts/ab_prompt_check.py --form "Antrag-Sprints (saniert)" --runs 5
    python scripts/ab_prompt_check.py --form "Auslagenerstattung (Prod)" --runs 5 \
        --old-rev 520619e --old-prompt-version 1.6.1
"""
import argparse
import collections
import json
import os
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
os.chdir(REPO)

import generate_chat_ai_prompt as gcp  # noqa: E402
import rule_engine  # noqa: E402
from run_llm_benchmark import FORMS, RETRY_WAIT_S, is_transport_error, rule_status  # noqa: E402

OLD_PROMPT_VERSION = "1.6.0"


def registry_from_git(rev: str) -> dict:
    raw = subprocess.run(["git", "show", f"{rev}:data/rules/rules_registry.json"],
                         check=True, capture_output=True).stdout
    return json.loads(raw.decode("utf-8"))


def run_arm(path: str, registry: dict, prompt_version: str, model: str) -> dict:
    """Ein Lauf der vollständigen Pipeline mit der angegebenen Prompt-Version."""
    saved = (gcp.PROMPT_VERSION, rule_engine.PROMPT_VERSION)
    gcp.PROMPT_VERSION = prompt_version          # steht im Prompttext
    rule_engine.PROMPT_VERSION = prompt_version  # steht in der Provenienz
    try:
        attempts = 0
        while True:
            attempts += 1
            res = rule_engine.evaluate_form(path, registry, llm_config={"model": model, "disable_fallback": True})
            if is_transport_error(res) and attempts <= len(RETRY_WAIT_S):
                time.sleep(RETRY_WAIT_S[attempts - 1])
                continue
            return res
    finally:
        gcp.PROMPT_VERSION, rule_engine.PROMPT_VERSION = saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--form", default="Antrag-Sprints (saniert)", choices=sorted(FORMS))
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--model", default="deepseek-v4-flash-0731")
    ap.add_argument("--old-rev", default="86c01ea", help="Git-Stand der Registry für den Arm 'alt'")
    ap.add_argument("--old-prompt-version", default=OLD_PROMPT_VERSION, help="Prompt-Version für den Arm 'alt'")
    args = ap.parse_args()

    arms = {
        "alt": (registry_from_git(args.old_rev), args.old_prompt_version),
        "neu": (rule_engine.load_rules(), gcp.PROMPT_VERSION),
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join("data", "evaluation", "llm_runs", f"{stamp}-ab")
    os.makedirs(out_dir, exist_ok=True)
    path = FORMS[args.form]
    collected = collections.defaultdict(list)

    for run in range(1, args.runs + 1):
        order = ("alt", "neu") if run % 2 else ("neu", "alt")  # abwechselnde Reihenfolge
        for arm in order:
            registry, pversion = arms[arm]
            res = run_arm(path, registry, pversion, args.model)
            prov = res.get("provenance", {})
            meta = {
                "arm": arm, "run": run, "model": args.model, "form": args.form,
                "registry_version": prov.get("registry_version"), "prompt_version": prov.get("prompt_version"),
                "prompt_sha256": prov.get("prompt_sha256"), "score": res["score"],
                "release_status": res["release_status"], "llm_status": res["llm_evaluation"].get("status"),
                "statuses": {rid: rule_status(res, rid) for rid in rule_engine.SEMANTIC_RULE_IDS},
            }
            collected[arm].append(meta)
            with open(os.path.join(out_dir, f"{arm}__run{run}.json"), "w", encoding="utf-8") as fh:
                json.dump({"meta": meta, "result": res}, fh, ensure_ascii=False, indent=2)
            print(f"Lauf {run} {arm}: Prompt v{meta['prompt_version']} ({str(meta['prompt_sha256'])[:12]}), "
                  f"Score {meta['score']}, {meta['release_status']}, KI {meta['llm_status']}, "
                  + ", ".join(f"{rid.replace('HWG-', '')} {st}" for rid, st in meta["statuses"].items()),
                  flush=True)

    summary = {"created": stamp, "form": args.form, "model": args.model, "runs": args.runs,
               "old_rev": args.old_rev, "old_prompt_version": args.old_prompt_version, "arms": {}}
    for arm, metas in collected.items():
        summary["arms"][arm] = {
            "prompt_version": sorted({m["prompt_version"] for m in metas}),
            "registry_version": sorted({m["registry_version"] for m in metas}),
            "prompt_sha256": sorted({m["prompt_sha256"] for m in metas}),
            "scores": [m["score"] for m in metas],
            "release_status": dict(collections.Counter(m["release_status"] for m in metas)),
            "statuses": {rid: dict(collections.Counter(m["statuses"][rid] for m in metas))
                         for rid in rule_engine.SEMANTIC_RULE_IDS},
        }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
