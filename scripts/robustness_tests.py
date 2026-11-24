"""
Robustheits- und Umgehungstests der Prüf-Engine (Regressionstests, Registry v1.6.12).

Jeder Test verändert eine Kopie eines Referenzformulars im Arbeitsspeicher so, wie es ein
Formularautor tun könnte, um eine Regel zu umgehen, oder simuliert eine fehlerhafte bzw.
manipulierte KI-Antwort. Erwartet wird, dass die Engine den Mangel weiterhin erkennt oder die
Freigabe verweigert. Die Ergebnisse werden in data/evaluation/robustness_results.json abgelegt.

Aufruf (im Repository-Wurzelverzeichnis):  python scripts/robustness_tests.py
"""
import copy
import http.server
import io
import json
import os
import sys
import threading
import zipfile

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
# Optional: ENGINE_DIR=<Pfad zu einem anderen Repository-Stand>, um eine ältere Engine-Version zu vergleichen
ENGINE_DIR = os.path.abspath(os.environ.get("ENGINE_DIR", REPO))
sys.path.insert(0, os.path.join(ENGINE_DIR, "web"))
sys.path.insert(0, os.path.join(ENGINE_DIR, "scripts"))
os.chdir(REPO)
for k in ("LLM_API_KEY", "GWDG_API_KEY", "OPENAI_API_KEY", "LLM_API_BASE", "LLM_ALLOWED_API_BASES"):
    os.environ.pop(k, None)
os.environ["LLM_API_KEY"] = ""  # verhindert, dass eine lokale .env-Datei echte Schlüssel einliest

from generate_chat_ai_prompt import decode_text_bytes  # noqa: E402
from rule_engine import (  # noqa: E402
    _finalize_rule_ids,
    _parse_archive_bytes,
    evaluate_form_bytes,
    evaluate_stage1_ast,
    evaluate_stage2_llm,
    load_rules,
    merge_llm_evaluation_payload,
)

REG = load_rules(os.path.join(ENGINE_DIR, "data", "rules", "rules_registry.json"))
STD = "data/raw_forms/standard/AFörderung Antrag-Sprints für Drittmittelanträge (HWG-Standard).xfp"
OLD = "data/raw_forms/test/fachformulare/AFörderung Antrag-Sprints für Drittmittelanträge.xfp"
NONC = "data/test_forms/synthetic_noncompliant_form.xfp"
COMP = "data/test_forms/synthetic_compliant_form.xfp"
INPUTS = {"XTextField", "XTextArea", "XSelect", "XCheckbox", "XUpload", "XDate", "XNumber", "XRadio"}


def load(path):
    b = open(path, "rb").read()
    z = zipfile.ZipFile(io.BytesIO(b))
    fcp = [n for n in z.namelist() if n.endswith(".fcp")][0]
    return b, fcp, json.loads(decode_text_bytes(z.read(fcp)))


def rezip(orig, fcp_name, data, extra=None, replace=None):
    zin = zipfile.ZipFile(io.BytesIO(orig))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in zin.namelist():
            if n == fcp_name:
                zo.writestr(n, json.dumps(data, ensure_ascii=False))
            elif replace and n in replace:
                zo.writestr(n, replace[n])
            else:
                zo.writestr(n, zin.read(n))
        for n, c in (extra or {}).items():
            zo.writestr(n, c)
    return out.getvalue()


def items(d):
    return d.get("items") or d["elements"]["items"]


def by_name(d, name):
    return next(i for i in items(d) if i["properties"].get("name") == name)


def first_page_id(d):
    pg = next(i for i in items(d) if i["className"] == "XPage")
    return pg["properties"].get("id") or pg["properties"]["name"]


def add_span(d, name, parent, text, viewstatus=None):
    items(d).append({"className": "XSpan", "properties": {"name": name, "id": name, "parentid": parent, "rtevalue": text, "viewstatus": viewstatus or []}})


def ev(b, name="test.xfp"):
    return evaluate_form_bytes(b, name, REG)


def viol(r):
    return {v["rule"] for v in r["violations"]}


def set_buttons(d, element, label):
    for btn in by_name(d, element)["properties"].get("buttons", []):
        if isinstance(btn, dict) and btn.get("value"):
            btn["value"] = label


RESULTS = []


def check(test_id, description, passed, observed):
    RESULTS.append({"id": test_id, "description": description, "passed": bool(passed), "observed": observed})
    print(f"[{'OK ' if passed else 'FAIL'}] {test_id}: {description} -> {observed}")


# --- Positivkontrollen: keine neuen Fehlalarme an den Referenzformularen ---
for label, path in (("P1", STD), ("P2", COMP)):
    r = ev(open(path, "rb").read())
    check(label, f"Referenzformular ohne Verstoß in Stufe 1 ({os.path.basename(path)})", not r["violations"], f"Score {r['score']}, Status {r['release_status']}")

# --- A: Freigabe ohne KI-Prüfung ---
b, fcp, d = load(STD)
d2 = copy.deepcopy(d)
set_buttons(d2, "btnSenden", "senden")
by_name(d2, "tfEmail")["properties"]["label"] = "HWGLU-Email"
add_span(d2, "txtNote", "xi-fs-3", "<p>Hinweis Entwickler: Feld bleibt vorerst sichtbar, Logik später nachziehen (Ticket 4711).</p>")
r = ev(rezip(b, fcp, d2))
check("A", "Ohne KI-Prüfung keine Freigabe trotz 100 Punkten in Stufe 1", r["release_status"] != "green_compliant", f"Score {r['score']}, Status {r['release_status']}")

# --- B: legitimer JavaScript-Hinweis darf kein Critical-Veto auslösen ---
d3 = copy.deepcopy(d)
add_span(d3, "txtJsHint", "xi-fs-3", "<p>Bitte aktivieren Sie JavaScript in Ihrem Browser, damit das Formular funktioniert.</p>")
r = ev(rezip(b, fcp, d3))
check("B", "Nutzerhinweis 'Bitte aktivieren Sie JavaScript' erzeugt keinen Stufe-1-Verstoß HWG-UI-01", "HWG-UI-01" not in viol(r), f"Verstöße: {sorted(viol(r)) or 'keine'}")

# --- C1/C2: Datenschutzhinweis umgehen ---
b, fcp, d = load(OLD)
d4 = copy.deepcopy(d)
add_span(d4, "txtHidden", first_page_id(d4), 'Datenschutz: <a href="https://example.org">Info</a>', viewstatus=["999"])
r = ev(rezip(b, fcp, d4))
check("C1", "Unsichtbarer Datenschutztext mit beliebigem Link erfüllt HWG-DSGVO-01 nicht", "HWG-DSGVO-01" in viol(r), f"HWG-DSGVO-01 {'erkannt' if 'HWG-DSGVO-01' in viol(r) else 'umgangen'}")
d5 = copy.deepcopy(d)
for it in items(d5):
    p = it["properties"]
    if p.get("label"):
        for a, c in (("Vorname", "Rufname"), ("Name Antragstellende", "Antragstellende"), ("E-Mail", "Kontakt"), ("Telefon", "Rückrufnr.")):
            p["label"] = str(p["label"]).replace(a, c)
    nm = p.get("name") or ""
    for a, c in (("Vorname", "Ruf"), ("Email", "Kontakt"), ("Telefon", "Rueckruf"), ("Name", "Ident")):
        nm = nm.replace(a, c)
    p["name"] = nm
r = ev(rezip(b, fcp, d5))
check("C2", "Umbenannte Personenfelder machen HWG-DSGVO-01 nicht unanwendbar", "HWG-DSGVO-01" in viol(r), f"HWG-DSGVO-01 {'erkannt' if 'HWG-DSGVO-01' in viol(r) else 'umgangen'}")

# --- D: Alibi-Legende ---
d6 = copy.deepcopy(d)
items(d6).append({"className": "XFieldSet", "properties": {"name": "fsAlibi", "id": "fsAlibi", "parentid": first_page_id(d6), "legend": ".", "viewstatus": []}})
for it in items(d6):
    if it["className"] in INPUTS:
        it["properties"]["parentid"] = "fsAlibi"
r = ev(rezip(b, fcp, d6))
check("D", "Fieldset mit Legende '.' erfüllt HWG-A11Y-01 nicht", "HWG-A11Y-01" in viol(r), f"HWG-A11Y-01 {'erkannt' if 'HWG-A11Y-01' in viol(r) else 'umgangen'}")

# --- E: Schein-Regex ---
b, fcp, d = load(NONC)
d7 = copy.deepcopy(d)
for it in items(d7):
    p = it["properties"]
    if p.get("name") in ("tfEmail", "tfIBAN"):
        p["datatype"] = "regexp"
        p["vrule"] = "[0-9]*.*"
r = ev(rezip(b, fcp, d7))
check("E", "Regex '[0-9]*.*' (akzeptiert beliebigen Text) gilt nicht als Validierung", "HWG-VAL-01" in viol(r), f"HWG-VAL-01 {'erkannt' if 'HWG-VAL-01' in viol(r) else 'umgangen'}")

# --- F: zusätzliches konformes Maskenmodell im Archiv ---
bc, fcpc, dc = load(COMP)
decoy = rezip(b, fcp, d, extra={"aaa.fcp": json.dumps(dc, ensure_ascii=False)})
try:
    r = ev(decoy)
    check("F", "Archiv mit zweiter (konformer) .fcp-Datei wird abgewiesen", False, f"ausgewertet: Score {r['score']}")
except ValueError as e:
    check("F", "Archiv mit zweiter (konformer) .fcp-Datei wird abgewiesen", True, f"abgewiesen ({e})")

# --- G: Upload nur als .xfp ---
try:
    from server import validate_upload  # noqa: E402
    fake_json = json.dumps({**d, "workflow_states": ["Eingegangen"], "workflow_emails": []}).encode()
    err = validate_upload("formular.json", fake_json)
except ImportError:
    err = None  # ältere Server-Version ohne Upload-Prüfung
check("G", "Server nimmt keine .json-Uploads mit selbst deklarierten Workflow-Daten an", err is not None, err or "angenommen")

# --- H: manipulierte oder fehlerhafte KI-Antworten (über die echte Merge-Funktion) ---
b, fcp, d = load(STD)
d8 = copy.deepcopy(d)
set_buttons(d8, "btnSenden", "senden")
by_name(d8, "tfEmail")["properties"]["label"] = "HWGLU-Email"
fd, em, st = _parse_archive_bytes(rezip(b, fcp, d8), "x.xfp")


def sim(llm_obj):
    r = evaluate_stage1_ast(fd, em, st, "x.xfp", REG)
    r = merge_llm_evaluation_payload(r, llm_obj, REG, "simuliert", "simuliert")
    return _finalize_rule_ids(r, REG)


r = sim({"evaluated_rules": []})
check("H1", "Leere KI-Antwort führt nicht zur Freigabe", r["release_status"] != "green_compliant", f"Status {r['release_status']}, offen {len(r['unverified_semantic'])}")
r = sim({"evaluated_rules": [{"rule_id": "HWG-BTN-01", "status": "RECOMMENDATION", "finding": "x"}]})
check("H2", "Unzulässiger Status RECOMMENDATION für HWG-BTN-01 wird nicht als bestanden gewertet", "HWG-BTN-01" in r["unverified_semantic"] and r["release_status"] != "green_compliant", f"HWG-BTN-01 offen: {'HWG-BTN-01' in r['unverified_semantic']}, Status {r['release_status']}")
r = sim({"evaluated_rules": [{"rule_id": "HWG-BTN-01", "status": "VIOLATION", "finding": "Button 'senden' ist generisch; für 'Weiter' ist keine Korrektur erforderlich."}]})
check("H3", "VIOLATION bleibt VIOLATION, auch wenn die Begründung 'keine Korrektur erforderlich' enthält", "HWG-BTN-01" in viol(r), f"HWG-BTN-01 {'Verstoß' if 'HWG-BTN-01' in viol(r) else 'umgedeutet'}")

# --- I: Vorgangsnummer nur im HTML-Kommentar ---
b, fcp, d = load(STD)
zin = zipfile.ZipFile(io.BytesIO(b))
proj = [n for n in zin.namelist() if "/projects/" in n and n.endswith(".fcexport") and "versions" not in n and "idcheck" not in n][0]
raw = decode_text_bytes(zin.read(proj))
pj = json.loads(raw)


def _mutate_mail(o):
    if isinstance(o, dict):
        if o.get("type") == "FC_EMAIL":
            cp = json.loads(o["customParameters"]) if isinstance(o.get("customParameters"), str) else (o.get("customParameters") or {})
            for k in ("subject", "body"):
                if k in cp:
                    cp[k] = str(cp[k]).replace("[%$RECORD_ID%]", "").replace("[%$ID%]", "")
            cp["body"] = str(cp.get("body", "")) + " <!-- [%$RECORD_ID%] -->"
            o["customParameters"] = json.dumps(cp, ensure_ascii=False)
        for v in o.values():
            _mutate_mail(v)
    elif isinstance(o, list):
        for v in o:
            _mutate_mail(v)


_mutate_mail(pj)
r = ev(rezip(b, fcp, d, replace={proj: json.dumps(pj, ensure_ascii=False)}))
check("I", "Vorgangsnummer nur im HTML-Kommentar erfüllt HWG-WF-02 nicht", "HWG-WF-02" in viol(r), f"HWG-WF-02 {'erkannt' if 'HWG-WF-02' in viol(r) else 'umgangen'}")

# --- J: Eingangsbestätigung nur an eine feste externe Adresse ---
pj2 = json.loads(raw)


def _static_recipient(o):
    if isinstance(o, dict):
        if o.get("type") == "FC_EMAIL":
            cp = json.loads(o["customParameters"]) if isinstance(o.get("customParameters"), str) else (o.get("customParameters") or {})
            for k in ("to", "recipients", "recipient", "address"):
                if k in cp:
                    cp[k] = "extern@example.org"
            o["customParameters"] = json.dumps(cp, ensure_ascii=False)
        for v in o.values():
            _static_recipient(v)
    elif isinstance(o, list):
        for v in o:
            _static_recipient(v)


_static_recipient(pj2)
r = ev(rezip(b, fcp, d, replace={proj: json.dumps(pj2, ensure_ascii=False)}))
check("J", "Feste externe Empfängeradresse gilt nicht als Eingangsbestätigung (HWG-FEEDBACK-01)", "HWG-FEEDBACK-01" in viol(r), f"HWG-FEEDBACK-01 {'erkannt' if 'HWG-FEEDBACK-01' in viol(r) else 'umgangen'}")

# --- K: col-12 ist volle Breite ---
b, fcp, d = load(COMP)
d9 = copy.deepcopy(d)
plz = next(i for i in items(d9) if "plz" in str(i["properties"].get("name", "")).lower())
for k in ("rowid", "maxwidth", "computedwidth", "colwidth", "width"):
    plz["properties"].pop(k, None)
plz["properties"]["cssclasses"] = "col-12"
r = ev(rezip(b, fcp, d9))
check("K", "PLZ-Feld mit Klasse col-12 gilt nicht als kompakt (HWG-UX-01)", "HWG-UX-01" in viol(r), f"HWG-UX-01 {'erkannt' if 'HWG-UX-01' in viol(r) else 'umgangen'}")

# --- L: Endpunkt-Freigabeliste und Schlüsselschutz ---
captured = {}


class _Sink(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        captured["auth"] = self.headers.get("Authorization")
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"choices": [{"message": {"content": "{\"evaluated_rules\": []}"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


sink = http.server.HTTPServer(("127.0.0.1", 0), _Sink)
threading.Thread(target=sink.serve_forever, daemon=True).start()
sink_base = f"http://127.0.0.1:{sink.server_address[1]}/v1"
os.environ["LLM_API_KEY"] = "SERVER-SCHLUESSEL-TEST"
b, fcp, d = load(STD)
fd, em, st = _parse_archive_bytes(b, "x.xfp")
r = evaluate_stage1_ast(fd, em, st, "x.xfp", REG)
r = evaluate_stage2_llm(r, fd, em, "x.xfp", REG, llm_config={"api_base": sink_base})
sent_l1 = "auth" in captured
check("L1", "Nicht freigegebener Endpunkt wird abgelehnt", r["llm_evaluation"]["status"] == "rejected" and not sent_l1, f"Status {r['llm_evaluation']['status']}, Anfrage gesendet: {sent_l1}" + (f", Authorization: {captured.get('auth')}" if sent_l1 else ""))
captured.clear()
os.environ["LLM_ALLOWED_API_BASES"] = sink_base
r = evaluate_stage1_ast(fd, em, st, "x.xfp", REG)
r = evaluate_stage2_llm(r, fd, em, "x.xfp", REG, llm_config={"api_base": sink_base})
check("L2", "Serverschlüssel wird nicht an einen vom Client gewählten Endpunkt gesendet", "auth" in captured and not captured.get("auth"), f"Anfrage gesendet: {'auth' in captured}, Authorization-Header: {captured.get('auth') or 'keiner'}")
sink.shutdown()
os.environ["LLM_API_KEY"] = ""
os.environ.pop("LLM_ALLOWED_API_BASES", None)

# --- M: Zip-Bombe ---
bomb = io.BytesIO()
with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zb:
    zb.writestr("files/x.fcp", b"0" * (60 * 1024 * 1024))
try:
    ev(bomb.getvalue())
    check("M", "Archiv mit übergroßem Eintrag wird abgewiesen", False, "vollständig entpackt und ausgewertet")
except ValueError as e:
    rejected = "Größe" in str(e) or "size" in str(e).lower()
    check("M", "Archiv mit übergroßem Eintrag wird abgewiesen", rejected, f"abgewiesen ({e})" if rejected else f"vollständig entpackt, danach Fehler ({type(e).__name__})")

# ======================= Zweiter Evaluationszyklus (v1.6.1) =======================
import base64  # noqa: E402
import struct  # noqa: E402
import zlib  # noqa: E402


def _tiny_png():
    def chunk(typ, data):
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
    raw = b"\x00\xff\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


# --- N1: fremdes Bild mit korrektem Alternativtext als Logo ---
b, fcp, d = load(STD)
dn = copy.deepcopy(d)
for it in items(dn):
    if it["className"] == "XImage":
        it["properties"]["image"] = "data:image/png;base64," + base64.b64encode(_tiny_png()).decode()
r = ev(rezip(b, fcp, dn))
check("N1", "Fremdes Bild mit HWG-Alternativtext gilt nicht als HWG-Logo (HWG-CD-02)", "HWG-CD-02" in viol(r), f"HWG-CD-02 {'erkannt' if 'HWG-CD-02' in viol(r) else 'umgangen'}")

# --- N2: Platzhalter-Legende ---
dn = copy.deepcopy(d)
for it in items(dn):
    if it["className"] == "XFieldSet":
        it["properties"]["legend"] = "Abc"
r = ev(rezip(b, fcp, dn))
check("N2", "Platzhalter-Legende 'Abc' gilt nicht als aussagekräftige Legende (HWG-A11Y-01)", "HWG-A11Y-01" in viol(r), f"HWG-A11Y-01 {'erkannt' if 'HWG-A11Y-01' in viol(r) else 'umgangen'}")

# --- N3: Vorgangsnummer nur in unsichtbarem HTML ---
pj3 = json.loads(raw)


def _hidden_id(o):
    if isinstance(o, dict):
        if o.get("type") == "FC_EMAIL":
            cp = json.loads(o["customParameters"]) if isinstance(o.get("customParameters"), str) else (o.get("customParameters") or {})
            for k in ("subject", "body"):
                if k in cp:
                    cp[k] = str(cp[k]).replace("[%$RECORD_ID%]", "").replace("[%$ID%]", "")
            cp["body"] = str(cp.get("body", "")) + ' <span style="display:none">[%$RECORD_ID%]</span>'
            o["customParameters"] = json.dumps(cp, ensure_ascii=False)
        for v in o.values():
            _hidden_id(v)
    elif isinstance(o, list):
        for v in o:
            _hidden_id(v)


_hidden_id(pj3)
r = ev(rezip(b, fcp, d, replace={proj: json.dumps(pj3, ensure_ascii=False)}))
check("N3", "Vorgangsnummer nur in unsichtbarem HTML erfüllt HWG-WF-02 nicht", "HWG-WF-02" in viol(r), f"HWG-WF-02 {'erkannt' if 'HWG-WF-02' in viol(r) else 'umgangen'}")

# --- N4: Geldbetrag mit Formcycle-Datentyp posmoney (kein Fehlalarm) ---
dn = copy.deepcopy(d)
fs = next(i for i in items(dn) if i["className"] == "XFieldSet")
fs_id = fs["properties"].get("id") or fs["properties"]["name"]
items(dn).append({"className": "XTextField", "properties": {"name": "tfFoerdersumme", "id": "tfFoerdersumme", "parentid": fs_id, "label": "Fördersumme", "datatype": "posmoney", "viewstatus": []}})
r = ev(rezip(b, fcp, dn))
check("N4", "Geldbetrag mit Datentyp 'posmoney' gilt als validiert (kein Fehlalarm bei HWG-VAL-01)", "HWG-VAL-01" not in viol(r), f"HWG-VAL-01 {'Fehlalarm' if 'HWG-VAL-01' in viol(r) else 'kein Verstoß'}")

# --- N5: Aufzählung mit Zeilenumbrüchen (kein Fehlalarm) ---
dn = copy.deepcopy(d)
add_span(dn, "txListe", first_page_id(dn), "<p>" + "<br>".join(["Bitte legen Sie den Nachweis bei"] * 6) + "</p>")
r = ev(rezip(b, fcp, dn))
check("N5", "Aufzählung mit Zeilenumbrüchen gilt nicht als überlanger Satz (kein Fehlalarm bei HWG-LANG-02)", "HWG-LANG-02" not in viol(r), f"HWG-LANG-02 {'Fehlalarm' if 'HWG-LANG-02' in viol(r) else 'kein Verstoß'}")

# --- N6: Server liefert keine Quelltexte aus ---
try:
    import urllib.request as _ur
    import server as _srv  # aus ENGINE_DIR/web
    _httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _srv.ComplianceHandler)
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()
    _port = _httpd.server_address[1]
    try:
        _ur.urlopen(f"http://127.0.0.1:{_port}/server.py", timeout=10).read()
        leaked = True
    except Exception:
        leaked = False
    _httpd.shutdown()
    check("N6", "Server liefert keine Dateien außer Dashboard und API aus (z. B. server.py)", not leaked, "server.py " + ("ausgeliefert" if leaked else "nicht ausgeliefert"))
except Exception as exc:  # noqa: BLE001
    check("N6", "Server liefert keine Dateien außer Dashboard und API aus (z. B. server.py)", False, f"Test nicht ausführbar ({type(exc).__name__})")

# ======================= Dritter Evaluationszyklus (v1.6.2) =======================
# --- S1: Statusliste bei abgeschalteter statusabhängiger Sichtbarkeit ---
b, fcp, d = load(OLD)
dn = copy.deepcopy(d)
for it in items(dn):
    if it["className"] == "XPage":
        it["properties"].update({"viewstatus": ["[!]-1"], "statusdependent": "0"})
r = ev(rezip(b, fcp, dn))
check("S1", "Statusliste bei abgeschalteter statusabhängiger Sichtbarkeit blendet die Felder nicht aus (HWG-DSGVO-01 bleibt anwendbar)", "HWG-DSGVO-01" in viol(r), f"HWG-DSGVO-01 {'erkannt' if 'HWG-DSGVO-01' in viol(r) else 'übersprungen'}")

# --- S2: Datenschutzhinweis, der erst nach dem Absenden erscheint (Statusabhängigkeit aktiv) ---
dn = copy.deepcopy(d)
add_span(dn, "txtSpaeter", first_page_id(dn), 'Datenschutz: <a href="https://www.hwg-lu.de/datenschutzerklaerung">Datenschutzerklärung</a>', viewstatus=["[!]-1"])
by_name(dn, "txtSpaeter")["properties"]["statusdependent"] = "1"
r = ev(rezip(b, fcp, dn))
check("S2", "Datenschutzhinweis, der erst nach dem Absenden sichtbar ist, erfüllt HWG-DSGVO-01 nicht", "HWG-DSGVO-01" in viol(r), f"HWG-DSGVO-01 {'erkannt' if 'HWG-DSGVO-01' in viol(r) else 'umgangen'}")

# ======================= Vierter Evaluationszyklus (v1.6.12) =======================
# --- T1: Formatierung und geschützte Leerzeichen in einer Beschriftung (kein Fehlalarm) ---
b, fcp, d = load(STD)
dn = copy.deepcopy(d)
by_name(dn, "tfVorname")["properties"]["label"] = '<p><strong><span style="font-size:14px">Vorname</span></strong>&nbsp;<br></p>'
r = ev(rezip(b, fcp, dn))
check("T1", "Formatierungs-Tags und geschütztes Leerzeichen in einer Beschriftung verändern die Ansage nicht (kein Fehlalarm bei HWG-A11Y-03)", "HWG-A11Y-03" not in viol(r), f"HWG-A11Y-03 {'Fehlalarm' if 'HWG-A11Y-03' in viol(r) else 'kein Verstoß'}")

# ======================= Fünfter Evaluationszyklus (v1.7.0) =======================
# --- U1: Regressionstest Zwischenspeichern (HWG-UX-02, Korrekturen v1.6.7 und v1.6.10) ---
# Die Auslagenerstattung (drei Seiten) sendet der antragstellenden Person bereits eine E-Mail mit dem
# Fortsetzungs-Link [%$FORM_PROCESS_LINK%], hat aber keine Schaltfläche zum Zwischenspeichern.
# Erwartet wird: ohne Schaltfläche eine Empfehlung, mit einer Schaltfläche der Aktion 'speichern'
# keine Empfehlung mehr (HWG-UX-02 erfüllt).
AUS = "data/raw_forms/prod/Auslagenerstattung _20260929.xfp"


def _ux02(res):
    if any(str(p).startswith("HWG-UX-02") for p in res.get("passed", [])):
        return "erfüllt"
    if any(x.get("rule") == "HWG-UX-02" for x in res.get("recommendations", [])):
        return "Empfehlung"
    return "nicht bewertet"


b, fcp, d = load(AUS)
before_u1 = _ux02(ev(b))
dn = copy.deepcopy(d)
btn_list = by_name(dn, "btnAntagstellerSenden")
save_btn = copy.deepcopy(btn_list["properties"]["buttons"][0])
save_btn.update({"name": "btnZwischenspeichern", "value": "Zwischenspeichern",
                 "action": {"displayName": "speichern", "optionId": "save", "page": "save", "check": False, "value": "speichern"}})
btn_list["properties"]["buttons"].append(save_btn)
after_u1 = _ux02(ev(rezip(b, fcp, dn)))
check("U1", "Zwischenspeichern mit der Aktion 'speichern' und Fortsetzungs-Link [%$FORM_PROCESS_LINK%] erfüllt HWG-UX-02 (ohne Schaltfläche Empfehlung, mit Schaltfläche keine)",
      before_u1 == "Empfehlung" and after_u1 == "erfüllt",
      f"ohne Schaltfläche: {before_u1}, mit Schaltfläche: {after_u1}")

summary = {
    "engine_registry_version": REG["version"],
    "passed": sum(1 for x in RESULTS if x["passed"]),
    "total": len(RESULTS),
    "tests": RESULTS,
}
out_path = os.environ.get("ROBUSTNESS_OUT", "data/evaluation/robustness_results.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print(f"\n{summary['passed']}/{summary['total']} Tests bestanden.")
sys.exit(0 if summary["passed"] == summary["total"] else 1)
