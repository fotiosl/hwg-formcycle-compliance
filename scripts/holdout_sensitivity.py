"""
Zusatzauswertung zum Abgleich mit der Annotation (ohne Sprachmodell, aus den gespeicherten Einzelurteilen).

1. Stufe 2 je Einzelantwort statt als Mehrheitsurteil: Cohens kappa für jeden der fünf Läufe getrennt
   (im Betrieb wertet die Pipeline genau eine Modellantwort aus, Abschnitt 5.2.3 der Arbeit).
2. Cluster-Bootstrap: Die Formular-Regel-Paare sind nicht unabhängig (gleiche Formulare, gleiche Regeln).
   Gezogen werden deshalb ganze Regeln (Cluster = Regel, alle fünf Formulare) bzw. ganze Formulare
   (Cluster = Formular, alle Regeln der jeweiligen Stufe) mit Zurücklegen; angegeben ist das
   95-%-Perzentilintervall von kappa (4.000 Ziehungen, fester Startwert).

Eingabe:  data/evaluation/holdout_results.json (Endstand)
Aufruf (Repository-Wurzel):  python scripts/holdout_sensitivity.py
Ausgabe:  data/evaluation/holdout_sensitivity.json
"""
import json
import os
import random

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SEM = {"HWG-STAMM-02", "HWG-UI-01", "HWG-BTN-01", "HWG-LANG-01", "HWG-LANG-02", "HWG-FEEDBACK-01", "HWG-UX-02"}
DRAWS = 4000
SEED = 20261116


def kappa(pairs):
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def positive(status):
    return status in ("VIOLATION", "RECOMMENDATION")


def cluster_ci(clusters, rng):
    """clusters: Liste von Listen (expert_bool, engine_bool); zieht ganze Cluster."""
    values = []
    for _ in range(DRAWS):
        sample = [p for c in rng.choices(clusters, k=len(clusters)) for p in c]
        k = kappa(sample)
        if k is not None:
            values.append(k)
    values.sort()
    return [values[int(0.025 * len(values))], values[int(0.975 * len(values))], len(values)]


def main():
    data = json.load(open(os.path.join(REPO, "data", "evaluation", "holdout_results.json"), encoding="utf-8"))
    pairs = [p for p in data["pairs"] if p["engine"] != "UNVERIFIED"]
    out = {"draws": DRAWS, "seed": SEED}

    # 1. Einzelantworten der Stufe 2
    sem = [p for p in pairs if p["rule"] in SEM]
    n_runs = min(len(p["runs"]) for p in sem)
    per_run = []
    for i in range(n_runs):
        usable = [(p["expert"] == "VIOLATION", positive(p["runs"][i])) for p in sem if p["runs"][i] != "UNVERIFIED"]
        per_run.append({"run": i + 1, "n": len(usable), "kappa": kappa(usable)})
    out["stage2_per_run"] = per_run
    out["stage2_majority_kappa"] = kappa([(p["expert"] == "VIOLATION", positive(p["engine"])) for p in sem])
    ks = [r["kappa"] for r in per_run if r["kappa"] is not None]
    out["stage2_per_run_range"] = [min(ks), max(ks)]

    # 2. Cluster-Bootstrap
    rng = random.Random(SEED)
    for name, subset in (("stage1", [p for p in pairs if p["rule"] not in SEM]), ("stage2", sem)):
        by_rule, by_form = {}, {}
        for p in subset:
            pair = (p["expert"] == "VIOLATION", positive(p["engine"]))
            by_rule.setdefault(p["rule"], []).append(pair)
            by_form.setdefault(p["form"], []).append(pair)
        out[name] = {
            "kappa": kappa([pr for c in by_rule.values() for pr in c]),
            "cluster_rule": cluster_ci(list(by_rule.values()), rng),
            "cluster_form": cluster_ci(list(by_form.values()), rng),
            "n_rules": len(by_rule),
            "n_forms": len(by_form),
        }

    # Critical-Regeln
    crit = data["by_severity"]["critical"]
    out["critical"] = {"n": crit["n"], "agree": crit["agree"], "kappa": crit["kappa"]}

    path = os.path.join(REPO, "data", "evaluation", "holdout_sensitivity.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
