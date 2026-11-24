"""
Vergleicht zwei Läufe von run_llm_benchmark.py für dasselbe Modell (z. B. Prompt 1.6.0 gegen 1.6.1).

Je Formular und semantischer Regel werden die Urteile der Wiederholungen gezählt und das
Mehrheitsurteil verglichen. Ausgegeben werden die Prompt-/Registry-Versionen beider Läufe, alle
abweichenden Mehrheitsurteile und die Übereinstimmung je Regel.

Beispiel:
    python scripts/compare_llm_runs.py data/evaluation/llm_runs/20261031-173843 data/evaluation/llm_runs/<neu> \
        --model deepseek-v4-flash-0731
"""
import argparse
import collections
import glob
import json
import os


def load_runs(run_dir, model):
    """Liest alle Einzelläufe eines Modells: {Formular: [statuses, ...]} sowie die Versionsangaben."""
    forms = collections.defaultdict(list)
    versions = set()
    for path in sorted(glob.glob(os.path.join(run_dir, "*.json"))):
        if os.path.basename(path) == "summary.json":
            continue
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data["meta"]["model"] != model:
            continue
        prov = data["result"].get("provenance", {})
        versions.add((prov.get("registry_version"), prov.get("prompt_version"), prov.get("prompt_sha256")))
        forms[data["meta"]["form"]].append(data["meta"]["statuses"])
    return forms, versions


def majority(values):
    counts = collections.Counter(values)
    top = counts.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return "UNENTSCHIEDEN"
    return top[0][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("alt")
    ap.add_argument("neu")
    ap.add_argument("--model", default="deepseek-v4-flash-0731")
    args = ap.parse_args()

    alt, v_alt = load_runs(args.alt, args.model)
    neu, v_neu = load_runs(args.neu, args.model)
    print(f"Modell: {args.model}")
    print(f"Alt: {args.alt}\n  (Registry, Prompt, Prompt-SHA256): {sorted(v_alt)}")
    print(f"Neu: {args.neu}\n  (Registry, Prompt, Prompt-SHA256): {sorted(v_neu)}")

    rules = sorted({r for runs in list(alt.values()) + list(neu.values()) for st in runs for r in st})
    agree = collections.Counter()
    total = collections.Counter()
    run_agree = collections.Counter()
    run_total = collections.Counter()
    diffs = []
    for form in sorted(set(alt) & set(neu)):
        for rule in rules:
            va = [st.get(rule) for st in alt[form]]
            vn = [st.get(rule) for st in neu[form]]
            ma, mn = majority(va), majority(vn)
            total[rule] += 1
            if ma == mn:
                agree[rule] += 1
            else:
                diffs.append((form, rule, dict(collections.Counter(va)), dict(collections.Counter(vn))))
            # Übereinstimmung der Einzelläufe mit dem alten Mehrheitsurteil
            for v in vn:
                run_total[rule] += 1
                run_agree[rule] += int(v == ma)

    print("\nÜbereinstimmung der Mehrheitsurteile je Regel (Formulare):")
    for rule in rules:
        print(f"  {rule:16} {agree[rule]}/{total[rule]}   Einzelläufe neu = altes Mehrheitsurteil: {run_agree[rule]}/{run_total[rule]}")
    print("\nAbweichende Mehrheitsurteile:" if diffs else "\nKeine abweichenden Mehrheitsurteile.")
    for form, rule, a, n in diffs:
        print(f"  {form} / {rule}: alt {a} -> neu {n}")


if __name__ == "__main__":
    main()
