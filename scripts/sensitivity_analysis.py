"""
Sensitivitätsanalyse des Bewertungsmodells (Stufe 1, 25 fachlich eigenständige Formulare).

Prüft, ob die Rangfolge der Formulare und die Freigabeampel stabil bleiben, wenn die gesetzten
Gewichte (Critical 15 / Major 8 / Minor 3) durch alternative Gewichtungen ersetzt werden.
Voraussetzung: data/evaluation/corpus_statistics.json (scripts/corpus_statistics.py).
Ergebnis: data/evaluation/sensitivity_analysis.json
"""
import json
import os

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.chdir(REPO)

SCHEMES = {
    "Basis 15/8/3": {"critical": 15, "major": 8, "minor": 3},
    "flach 10/5/2": {"critical": 10, "major": 5, "minor": 2},
    "steil 20/10/5": {"critical": 20, "major": 10, "minor": 5},
    "gleichgewichtet 6/6/6": {"critical": 6, "major": 6, "minor": 6},
    "Critical betont 25/8/3": {"critical": 25, "major": 8, "minor": 3},
}


def ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    r = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(a, b):
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return cov / (va * vb) if va and vb else 1.0


def status(score, crit, veto=True):
    if (veto and crit >= 1) or score < 60:
        return "rot"
    if score < 80:
        return "orange"
    return "grün"


def main():
    reg = json.load(open("data/rules/rules_registry.json", encoding="utf-8"))
    sev = {r["id"]: r["severity"] for r in reg["rules"]}
    stats = json.load(open("data/evaluation/corpus_statistics.json", encoding="utf-8"))
    forms = [f for f in stats["forms"] if not f["duplicate_of_prod"]]

    def fsev(f, rid):
        # gestufte Regeln (z. B. HWG-CD-02) tragen den tatsächlich vergebenen Schweregrad
        return (f.get("violation_severities") or {}).get(rid, sev[rid])

    def score(f, w):
        s = 100
        for rid in f["violations"]:
            s -= w.get(fsev(f, rid), 0)
        return max(0, s)

    def crit(f):
        return sum(1 for rid in f["violations"] if fsev(f, rid) == "critical")

    base = [score(f, SCHEMES["Basis 15/8/3"]) for f in forms]
    out = {"n": len(forms), "forms_with_critical": sum(1 for f in forms if crit(f) >= 1), "schemes": {}}
    for name, w in SCHEMES.items():
        sc = [score(f, w) for f in forms]
        out["schemes"][name] = {
            "spearman_vs_basis": round(spearman(base, sc), 3),
            "median_score": sorted(sc)[len(sc) // 2],
            "status_with_veto": {s: sum(1 for f, x in zip(forms, sc) if status(x, crit(f)) == s) for s in ("grün", "orange", "rot")},
            "status_without_veto": {s: sum(1 for f, x in zip(forms, sc) if status(x, crit(f), veto=False) == s) for s in ("grün", "orange", "rot")},
        }
    with open("data/evaluation/sensitivity_analysis.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
