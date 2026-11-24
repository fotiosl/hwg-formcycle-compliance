"""
Deskriptive Korpusstatistik für Kapitel 3 (Registry v1.7.0, Engine v1.7.0, nur Stufe 1; seit v1.6.12 mit eingegrenzter Regel HWG-A11Y-03, seit v1.7.0 mit zulässiger Kleinschreibung bei HWG-BTN-02).

Ausgewertet werden drei Sichten:
- alle 34 Exportarchive (Transparenz),
- 25 fachlich eigenständige Formulare (Dubletten zwischen Produktiv- und Testsystem entfernt; Primärsicht),
- 13 Formulare des Produktivsystems.
Ergebnis: data/evaluation/corpus_statistics.json

Aufruf im Repository-Wurzelverzeichnis:  python scripts/corpus_statistics.py
"""
import glob
import io
import json
import os
import re
import statistics
import sys
import zipfile

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
os.chdir(REPO)
os.environ["LLM_API_KEY"] = ""

from generate_chat_ai_prompt import decode_text_bytes  # noqa: E402
from rule_engine import evaluate_form, load_rules  # noqa: E402

TEST_DIR = "data/raw_forms/test/fachformulare/"
# Test-Exporte, die ein Produktivformular duplizieren (gleiches Projekt oder >= 94 % identische Feldbeschriftungen)
DUPLICATES_OF_PROD = {
    TEST_DIR + "01_Veranstaltungsmanagement.xfp",
    TEST_DIR + "A Neu Beschaffungsantrag.xfp",
    TEST_DIR + "KI-Beschaffung.xfp",
    TEST_DIR + "Personal_Stellenausschreibung_20260727.xfp",
    TEST_DIR + "Personal_Vertragsaenderung_20251119_final.xfp",
    TEST_DIR + "Auslagenerstattung _20260915.xfp",
    TEST_DIR + "FB1-GiP_Praktikum_final-20251115.xfp",
    TEST_DIR + "HaushaltVergabevermerk3000_20251110.xfp",
    TEST_DIR + "ZFK-Fördermittel.xfp",
}
WIP_OR_TEMPLATE = {
    TEST_DIR + "Wohungsplattform-Formular (WIP).xfp": "Entwurf (WIP)",
    TEST_DIR + "TEST-FB1-BNM_Praktikum_20260309(Test-Version).xfp": "Testversion",
    "data/raw_forms/test/adminformulare/Master-Formular.xfp": "Kopiervorlage",
    "data/raw_forms/test/adminformulare/Authentifizierung Landes-IDP (SAML 2.0).xfp": "Authentifizierungsbeispiel",
}


def workflow_inboxes(path):
    inboxes = set()
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.endswith(".fcexport") and "/projects/" in n and "versions" not in n and "idcheck" not in n:
                d = json.loads(decode_text_bytes(z.read(n)))

                def walk(o):
                    if isinstance(o, dict):
                        if o.get("type") == "FC_MOVE_FORM_RECORD_TO_INBOX":
                            cp = o.get("customParameters")
                            key = cp if isinstance(cp, str) else json.dumps(cp, sort_keys=True)
                            inboxes.add(key)
                        for v in o.values():
                            walk(v)
                    elif isinstance(o, list):
                        for v in o:
                            walk(v)
                walk(d)
    return len(inboxes)


def summarize(rows, label):
    n = len(rows)
    rule_ids = sorted({rid for r in rows for rid in r["violations"]} | {rid for r in rows for rid in r["not_applicable"]})
    per_rule = {}
    for rid in rule_ids:
        v = sum(1 for r in rows if rid in r["violations"])
        na = sum(1 for r in rows if rid in r["not_applicable"])
        per_rule[rid] = {"violations": v, "not_applicable": na, "applicable": n - na,
                         "share_of_all_pct": round(100 * v / n, 1) if n else 0.0,
                         "share_of_applicable_pct": round(100 * v / (n - na), 1) if n - na else None}
    wf02_none = sum(1 for r in rows if r["emails_total"] > 0 and r["emails_with_id"] == 0)
    wf02_partial = sum(1 for r in rows if r["emails_total"] > 0 and 0 < r["emails_with_id"] < r["emails_total"])
    no_emails = sum(1 for r in rows if r["emails_total"] == 0)
    return {
        "label": label,
        "n": n,
        "width_700px": sum(1 for r in rows if "700px" in r["widths"]),
        "width_850px_only": sum(1 for r in rows if r["widths"] == ["850px"]),
        "no_fieldset_at_all": sum(1 for r in rows if r["fieldset_count"] == 0),
        "wf02_no_id": wf02_none,
        "wf02_partial": wf02_partial,
        "no_workflow_emails": no_emails,
        "multi_inbox_workflows": sum(1 for r in rows if r["inboxes"] >= 2),
        "release_status": {s: sum(1 for r in rows if r["release_status"] == s) for s in sorted({r["release_status"] for r in rows})},
        "stage1_score_median": statistics.median(r["score"] for r in rows),
        "stage1_score_max": max(r["score"] for r in rows),
        "per_rule": per_rule,
    }


def main():
    reg = load_rules()
    files = sorted(glob.glob("data/raw_forms/prod/*.xfp")) + sorted(glob.glob("data/raw_forms/test/**/*.xfp", recursive=True))
    rows = []
    for f in files:
        f = f.replace("\\", "/")
        res = evaluate_form(f, reg)
        with zipfile.ZipFile(f) as z:
            fcp = [n for n in z.namelist() if n.endswith(".fcp")][0]
            d = json.loads(decode_text_bytes(z.read(fcp)))
        its = d.get("items") or (d.get("elements") or {}).get("items") or []
        widths = sorted({str(i["properties"].get("maxwidth") or "").lower() for i in its if i.get("className") in ("XHeader", "XPage", "XFooter") and i["properties"].get("maxwidth")})
        # E-Mail-Detail für HWG-WF-02 aus den Befunden ableiten
        msg = next((v["message"] for v in res["violations"] if v["rule"] == "HWG-WF-02"), "")
        m = re.search(r"in (\d+)/(\d+) automatisierten", msg)
        if m:
            missing, total = int(m.group(1)), int(m.group(2))
        elif "HWG-WF-02" in res["not_applicable"]:
            missing, total = 0, 0
        else:
            pm = next((p for p in res["passed"] if p.startswith("HWG-WF-02")), "")
            mt = re.search(r"(\d+)/(\d+)", pm)
            total = int(mt.group(2)) if mt else 0
            missing = 0
        rows.append({
            "file": f,
            "system": "prod" if "/prod/" in f else "test",
            "duplicate_of_prod": f in DUPLICATES_OF_PROD,
            "status_note": WIP_OR_TEMPLATE.get(f, ""),
            "score": res["score"],
            "release_status": res["release_status"],
            "violations": sorted({v["rule"] for v in res["violations"]}),
            "violation_severities": {v["rule"]: v["severity"] for v in res["violations"]},
            "not_applicable": sorted(k for k in res["not_applicable"] if k not in res.get("unverified_semantic", [])),
            "widths": widths,
            "fieldset_count": sum(1 for i in its if i.get("className") == "XFieldSet"),
            "emails_total": total,
            "emails_with_id": total - missing,
            "inboxes": workflow_inboxes(f),
        })
    unique = [r for r in rows if not r["duplicate_of_prod"]]
    prod = [r for r in rows if r["system"] == "prod"]
    out = {
        "registry_version": reg["version"],
        "note": "Nur Stufe 1 (deterministisch); semantische Regeln sind hier nicht enthalten.",
        "views": {
            "unique_25": summarize(unique, "25 fachlich eigenständige Formulare"),
            "prod_13": summarize(prod, "13 Produktivformulare"),
            "all_34": summarize(rows, "34 Exportarchive inkl. Dubletten"),
        },
        "forms": rows,
    }
    os.makedirs("data/evaluation", exist_ok=True)
    with open("data/evaluation/corpus_statistics.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    for key, v in out["views"].items():
        print(f"\n== {v['label']} (n={v['n']})")
        print(f"   700px: {v['width_700px']}, nur 850px: {v['width_850px_only']}, ohne Fieldset: {v['no_fieldset_at_all']}, Mehr-Postfach-Workflows: {v['multi_inbox_workflows']}")
        print(f"   WF-02: ohne ID {v['wf02_no_id']}, teilweise {v['wf02_partial']}, keine E-Mails {v['no_workflow_emails']}")
        print(f"   Status: {v['release_status']}, Median-Score {v['stage1_score_median']}, Max {v['stage1_score_max']}")
        for rid, x in v["per_rule"].items():
            print(f"   {rid:16} {x['violations']:2}/{v['n']} ({x['share_of_all_pct']}%)  N/A {x['not_applicable']}")


if __name__ == "__main__":
    main()
