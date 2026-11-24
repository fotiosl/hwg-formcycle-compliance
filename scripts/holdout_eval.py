"""
Abgleich der Engine mit einer unabhängigen Expertenannotation realer Formulare.

Ablauf:
1. Die Vorlage data/evaluation/holdout_annotation_template.csv nach
   data/evaluation/holdout_annotations.csv kopieren.
2. Eine zweite Person (z. B. der Formcycle-Administrator) trägt für jedes Formular und alle 22 Regeln
   in 'expert_status' PASS, VIOLATION oder NOT_APPLICABLE ein, ohne die Ausgabe der Engine zu kennen.
   Bei HWG-UX-02 bedeutet VIOLATION: Zwischenspeichern wäre nach der Regel angebracht, fehlt aber.
   Optional annotiert eine weitere Person dieselben Paare in 'second_status'; daraus wird die
   Übereinstimmung zwischen den Menschen berechnet (Obergrenze für die Engine).
3. python scripts/holdout_eval.py                     (nur Stufe 1, ohne LLM)
   python scripts/holdout_eval.py --with-llm --runs 5 (zusätzlich Stufe 2 über die konfigurierte API;
                                                       je Regel zählt das Mehrheitsurteil der Läufe)
   python scripts/holdout_eval.py --from-results      (Kennwerte und Makros aus holdout_results.json
                                                       neu berechnen, ohne neuen Lauf)

Berichtet werden Precision, Recall, F1 und Cohens Kappa für die Erkennung von Verstößen sowie die
Übereinstimmung je Regel. Ergebnis: data/evaluation/holdout_results.json und
thesis/generated/holdout.tex.
"""
import argparse
import collections
import csv
import json
import os
import random
import sys

REPO =os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
os.chdir(REPO)

from rule_engine import SEMANTIC_RULE_IDS, evaluate_form, load_rules  # noqa: E402
from run_llm_benchmark import rule_status  # noqa: E402


def kappa(pairs):
    n = len(pairs)
    if not n:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    pa = sum(1 for a, _ in pairs if a) / n
    pb = sum(1 for _, b in pairs if b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def num(x, d=2):
    return "--" if x is None else f"{x:.{d}f}".replace(".", "{,}")


def majority_status(statuses):
    """Mehrheitsurteil über mehrere LLM-Läufe; ungültige Läufe zählen nicht, Gleichstand gilt als Verstoß."""
    valid = [s for s in statuses if s != "UNVERIFIED"]
    if not valid:
        return "UNVERIFIED"
    hits = sum(1 for s in valid if s in ("VIOLATION", "RECOMMENDATION"))
    if hits * 2 >= len(valid):
        return "RECOMMENDATION" if "RECOMMENDATION" in valid and "VIOLATION" not in valid else "VIOLATION"
    return collections.Counter(s for s in valid if s not in ("VIOLATION", "RECOMMENDATION")).most_common(1)[0][0]


RESULTS = "data/evaluation/holdout_results.json"
SEMANTIC_ONLY = ("HWG-STAMM-02", "HWG-BTN-01", "HWG-LANG-01")  # Regeln ohne deterministischen Anteil
SEVERITY_LABELS = {"critical": "Critical", "major": "Major", "minor": "Minor", "recommendation": "Empfehlung"}


def kappa_ci(pairs, draws=4000, seed=20261105):
    """95-%-Bootstrap-Intervall von Kappa über die Formular-Regel-Paare."""
    rng = random.Random(seed)
    values = sorted(kappa(rng.choices(pairs, k=len(pairs))) for _ in range(draws))
    return [values[int(0.025 * draws)], values[int(0.975 * draws)]]


def derived(details, reg):
    """Zusatzkennwerte aus den gespeicherten Einzelurteilen: Schweregrad, semantische Regeln, Unsicherheit."""
    severity = {r.get("id") or r.get("rule_id"): str(r.get("severity")).lower() for r in reg["rules"]}
    usable = [p for p in details if p["engine"] != "UNVERIFIED"]

    def pair(p):
        return p["expert"] == "VIOLATION", p["engine"] in ("VIOLATION", "RECOMMENDATION")

    by_severity = {}
    for key in SEVERITY_LABELS:
        pairs = [pair(p) for p in usable if severity.get(p["rule"]) == key]
        by_severity[key] = {"n": len(pairs), "agree": sum(a == b for a, b in pairs), "kappa": kappa(pairs)}
    stage1 = [pair(p) for p in usable if p["rule"] not in SEMANTIC_RULE_IDS]
    stage2 = [pair(p) for p in usable if p["rule"] in SEMANTIC_RULE_IDS]
    return {
        "by_severity": by_severity,
        "kappa_semantic_only": kappa([pair(p) for p in usable if p["rule"] in SEMANTIC_ONLY]),
        "kappa_ci": {"stage1": kappa_ci(stage1), "stage2": kappa_ci(stage2)},
        "stable_stage2_pairs": sum(1 for p in usable if p["rule"] in SEMANTIC_RULE_IDS and len(set(p["runs"])) == 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotations", default="data/evaluation/holdout_annotations.csv")
    ap.add_argument("--with-llm", action="store_true")
    ap.add_argument("--runs", type=int, default=5, help="LLM-Läufe je Formular (nur mit --with-llm)")
    ap.add_argument("--from-results", action="store_true", help="Kennwerte und Makros aus den gespeicherten Ergebnissen neu berechnen")
    args = ap.parse_args()
    if args.from_results:
        os.environ["LLM_API_KEY"] = ""
        with open(RESULTS, encoding="utf-8") as fh:
            out = json.load(fh)
        out.update(derived(out["pairs"], load_rules()))
        with open(RESULTS, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        write_tex(out)
        return
    if not os.path.exists(args.annotations):
        sys.exit(f"Annotationsdatei fehlt: {args.annotations} (Vorlage: data/evaluation/holdout_annotation_template.csv)")
    if not args.with_llm:
        os.environ["LLM_API_KEY"] = ""
    runs = max(1, args.runs) if args.with_llm else 1
    reg = load_rules()
    all_rows = list(csv.DictReader(open(args.annotations, encoding="utf-8-sig"), delimiter=";"))
    rows = [r for r in all_rows if (r.get("expert_status") or "").strip()]
    by_form = collections.defaultdict(dict)
    for r in rows:
        by_form[r["form_file"]][r["rule_id"]] = r["expert_status"].strip().upper()
    second = [
        (r["rule_id"], r["expert_status"].strip().upper() == "VIOLATION", r["second_status"].strip().upper() == "VIOLATION")
        for r in rows if (r.get("second_status") or "").strip()
    ]
    pairs_det, pairs_sem, per_rule = [], [], collections.defaultdict(lambda: [0, 0])
    unverified = 0
    details = []
    for form, gold in by_form.items():
        results = [evaluate_form(form, reg) for _ in range(runs)]
        for rid, exp in gold.items():
            run_statuses = [rule_status(res, rid) for res in results]
            got = majority_status(run_statuses)
            details.append({"form": form, "rule": rid, "expert": exp, "engine": got, "runs": run_statuses})
            if got == "UNVERIFIED":
                unverified += 1
                continue
            # UX-02 ist eine Empfehlung: Die Engine meldet RECOMMENDATION, die Annotation VIOLATION.
            pair = (exp == "VIOLATION", got in ("VIOLATION", "RECOMMENDATION"))
            (pairs_sem if rid in SEMANTIC_RULE_IDS else pairs_det).append(pair)
            per_rule[rid][0] += int(pair[0] == pair[1])
            per_rule[rid][1] += 1

    def metrics(pairs):
        tp = sum(1 for e, g in pairs if e and g)
        fp = sum(1 for e, g in pairs if not e and g)
        fn = sum(1 for e, g in pairs if e and not g)
        p = tp / (tp + fp) if tp + fp else None
        r = tp / (tp + fn) if tp + fn else None
        f1 = 2 * p * r / (p + r) if p and r else None
        return {"n": len(pairs), "tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1, "kappa": kappa(pairs)}

    out = {
        "forms": len(by_form), "annotations": len(rows), "with_llm": args.with_llm, "llm_runs": runs if args.with_llm else 0,
        "unverified_pairs": unverified,
        "stage1": metrics(pairs_det), "stage2": metrics(pairs_sem) if pairs_sem else None,
        "per_rule_agreement": {k: v[0] / v[1] for k, v in sorted(per_rule.items()) if v[1]},
        "inter_rater": {
            "n": len(second),
            "kappa_all": kappa([(a, b) for _, a, b in second]),
            "kappa_stage1": kappa([(a, b) for rid, a, b in second if rid not in SEMANTIC_RULE_IDS]),
            "kappa_stage2": kappa([(a, b) for rid, a, b in second if rid in SEMANTIC_RULE_IDS]),
        } if second else None,
        "pairs": details,
    }
    out.update(derived(details, reg))
    with open(RESULTS, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    write_tex(out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def write_tex(out):
    """Schreibt die Makros für die Arbeit aus dem Ergebnisobjekt von main()."""
    s1, s2 = out["stage1"], out["stage2"]
    with open("thesis/generated/holdout.tex", "w", encoding="utf-8") as fh:
        fh.write("% Automatisch erzeugt von scripts/holdout_eval.py – nicht von Hand bearbeiten.\n")
        fh.write(f"\\renewcommand{{\\HoldoutFormulare}}{{{out['forms']}}}\n")
        fh.write(f"\\renewcommand{{\\HoldoutPaare}}{{{s1['n']}}}\n")
        fh.write(f"\\renewcommand{{\\HoldoutTreffer}}{{{s1['tp']}/{s1['fp']}/{s1['fn']}}}\n")
        fh.write(f"\\renewcommand{{\\HoldoutPrecision}}{{{num(s1['precision'])}}}\n")
        fh.write(f"\\renewcommand{{\\HoldoutRecall}}{{{num(s1['recall'])}}}\n")
        fh.write(f"\\renewcommand{{\\HoldoutFeins}}{{{num(s1['f1'])}}}\n")
        fh.write(f"\\renewcommand{{\\HoldoutKappa}}{{{num(s1['kappa'])}}}\n")
        if s2:
            fh.write(f"\\renewcommand{{\\HoldoutSemLaeufe}}{{{out['llm_runs']}}}\n")
            fh.write(f"\\renewcommand{{\\HoldoutSemPaare}}{{{s2['n']}}}\n")
            fh.write(f"\\renewcommand{{\\HoldoutSemTreffer}}{{{s2['tp']}/{s2['fp']}/{s2['fn']}}}\n")
            fh.write(f"\\renewcommand{{\\HoldoutSemPrecision}}{{{num(s2['precision'])}}}\n")
            fh.write(f"\\renewcommand{{\\HoldoutSemRecall}}{{{num(s2['recall'])}}}\n")
            fh.write(f"\\renewcommand{{\\HoldoutSemFeins}}{{{num(s2['f1'])}}}\n")
            fh.write(f"\\renewcommand{{\\HoldoutSemKappa}}{{{num(s2['kappa'])}}}\n")
        if out["inter_rater"]:
            fh.write(f"\\renewcommand{{\\HoldoutKappaMenschen}}{{{num(out['inter_rater']['kappa_all'])}}}\n")
        low, high = out["kappa_ci"]["stage1"]
        fh.write(f"\\renewcommand{{\\HoldoutKappaKI}}{{{num(low)} bis {num(high)}}}\n")
        if s2:
            low, high = out["kappa_ci"]["stage2"]
            fh.write(f"\\renewcommand{{\\HoldoutSemKappaKI}}{{{num(low)} bis {num(high)}}}\n")
            fh.write(f"\\renewcommand{{\\HoldoutSemantischKappa}}{{{num(out['kappa_semantic_only'])}}}\n")
            fh.write(f"\\renewcommand{{\\HoldoutSemStabil}}{{{out['stable_stage2_pairs']}}}\n")
        rows = " ".join(f"{SEVERITY_LABELS[k]} & {v['agree']}/{v['n']} & {num(v['kappa'])} \\\\" for k, v in out["by_severity"].items())
        fh.write("\\renewcommand{\\HoldoutSchweregradZeilen}{" + rows + "}\n")
        critical = out["by_severity"]["critical"]
        fh.write(f"\\renewcommand{{\\HoldoutCriticalKappa}}{{{num(critical['kappa'])}}}\n")
        fh.write(f"\\renewcommand{{\\HoldoutCriticalTreffer}}{{{critical['agree']} von {critical['n']}}}\n")


if __name__ == "__main__":
    main()
