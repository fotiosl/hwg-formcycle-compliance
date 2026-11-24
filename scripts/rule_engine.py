"""
Zweistufige hybride Compliance-Prüf-Engine für XIMA Formcycle (v8), Version 1.6.5:
- Stufe 1 (deterministische Strukturprüfung): Layout-, Barrierefreiheits-, Validierungs-,
  Navigations- und Workflow-Prüfung für die 15 deterministischen Regeln sowie deterministische
  Vorprüfungen für die 4 hybriden Regeln. Prüfparameter stammen aus der Registry.
- Stufe 2 (semantisch/hybrid über eine OpenAI-kompatible LLM-REST-API, z. B. GWDG Academic Cloud
  oder lokal über Ollama): Bewertung der 7 semantischen/hybriden Regeln anhand des kompakten
  JSON-Extrakts aus generate_chat_ai_prompt.py.
- Fail-closed: Ist keine LLM-API angebunden, fällt sie aus oder liefert sie unvollständige bzw.
  unzulässige Bewertungen, bleiben die betroffenen Regeln als ungeprüft markiert
  ('unverified_semantic'). Ein Formular mit ungeprüften Regeln ist nie freigabefähig
  (Status 'pending_ai_review').
"""
import hashlib
import html
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_chat_ai_prompt import (
    DEFAULT_RESUME_LINK_PLACEHOLDERS,
    INPUT_CLASSES,
    PROMPT_VERSION,
    SYSTEM_MESSAGE,
    build_prompt_from_payload,
    classify_button_role,
    clean_visible_text,
    decode_text_bytes,
    extract_compact_payload_from_data,
    extract_long_sentences,
    find_dev_markers,
    has_resume_link_email,
    hidden_by_status,
    is_dynamic_recipient,
    rule_spec,
)

ENGINE_VERSION = "1.7.0"

# Schutz vor manipulierten oder übergroßen Archiven (Zip-Bomb)
MAX_ARCHIVE_ENTRIES = 5000
MAX_ENTRY_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

SEMANTIC_RULE_IDS = [
    "HWG-STAMM-02",
    "HWG-UI-01",
    "HWG-BTN-01",
    "HWG-UX-02",
    "HWG-LANG-01",
    "HWG-LANG-02",
    "HWG-FEEDBACK-01",
]

DEFAULT_API_BASE = "https://chat-ai.academiccloud.de/v1"
DEFAULT_LLM_MODEL = "deepseek-v4-flash-0731"


def _load_dotenv_if_present():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env_path = os.path.join(repo_root, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


def load_rules(registry_path="data/rules/rules_registry.json"):
    with open(registry_path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_workflow_emails_and_states_from_fcexport(proj_data):
    emails = []
    states = []

    def walk_nodes(obj):
        if isinstance(obj, dict):
            node_type = obj.get("type")
            if node_type == "FC_EMAIL":
                cp_str = obj.get("customParameters", "{}")
                try:
                    cp = json.loads(cp_str) if isinstance(cp_str, str) else (cp_str or {})
                except Exception:
                    cp = {}
                recipient = (
                    cp.get("to")
                    or cp.get("recipients")
                    or cp.get("recipient")
                    or cp.get("address")
                    or ""
                )
                emails.append({
                    "node_name": str(obj.get("name", "")),
                    "recipient": str(recipient),
                    "subject": str(cp.get("subject", "")),
                    "body": str(cp.get("body", "")),
                })
            elif node_type in ("FC_CHANGE_STATE", "CUSTOM", "RECEIVED"):
                st_name = str(obj.get("name", "")).strip()
                if st_name and st_name != "FC_CHANGE_STATE":
                    states.append(st_name)
            for v in obj.values():
                walk_nodes(v)
        elif isinstance(obj, list):
            for item in obj:
                walk_nodes(item)

    walk_nodes(proj_data)

    dedup_states = []
    for s in states:
        if s not in dedup_states:
            dedup_states.append(s)
    return emails, dedup_states


def _parse_archive_bytes(file_bytes: bytes, filename: str):
    workflow_emails = []
    workflow_states = []
    if filename.lower().endswith(".json"):
        form_data = json.loads(decode_text_bytes(file_bytes))
        workflow_emails = form_data.get("workflow_emails", []) or []
        workflow_states = form_data.get("workflow_states", []) or []
    else:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            infos = z.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(f"Archiv '{filename}' enthält zu viele Einträge ({len(infos)}).")
            total = 0
            for info in infos:
                if info.file_size > MAX_ENTRY_UNCOMPRESSED_BYTES:
                    raise ValueError(f"Archiveintrag '{info.filename}' überschreitet die zulässige Größe.")
                total += info.file_size
            if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ValueError(f"Archiv '{filename}' überschreitet die zulässige entpackte Gesamtgröße.")
            fcp_candidates = [n for n in z.namelist() if n.lower().endswith(".fcp")]
            if not fcp_candidates:
                raise ValueError(f"Keine .fcp-Modelldatei im Formulararchiv '{filename}' gefunden.")
            if len(fcp_candidates) != 1:
                # Mehrdeutige Archive werden abgewiesen, damit nicht ein anderes als das importierte
                # Maskenmodell geprüft wird (z. B. eine zusätzlich eingelegte konforme .fcp-Datei).
                raise ValueError(
                    f"Archiv '{filename}' enthält {len(fcp_candidates)} Maskenmodelle (.fcp); erwartet wird genau eines."
                )
            fcp_name = fcp_candidates[0]
            form_data = json.loads(decode_text_bytes(z.read(fcp_name)))
            fcexports = [
                n for n in z.namelist()
                if n.endswith(".fcexport")
                and ("/projects/" in n or "\\projects\\" in n)
                and "idcheck" not in n
                and "versions" not in n
            ]
            for fce in fcexports:
                proj_data = json.loads(decode_text_bytes(z.read(fce)))
                ems, sts = extract_workflow_emails_and_states_from_fcexport(proj_data)
                workflow_emails.extend(ems)
                for st in sts:
                    if st not in workflow_states:
                        workflow_states.append(st)
    return form_data, workflow_emails, workflow_states


_HTML_TAG_RE = re.compile(r"<!--.*?-->|</?[A-Za-z][^<>]*>", re.DOTALL)
_LABEL_ARROW_RE = re.compile(r"=>|->|<-")
_LABEL_TAG_REMNANT_RE = re.compile(r"</?[A-Za-z][^<>]*>")
_LABEL_ENTITY_CODE_RE = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#[xX][0-9A-Fa-f]+);")


def rendered_label_text(raw_label: str) -> str:
    """Text einer Beschriftung, wie der Browser ihn anzeigt: Tags entfernt, Entities einmal dekodiert."""
    s = _HTML_TAG_RE.sub(" ", str(raw_label or ""))
    s = html.unescape(s)
    return " ".join(s.split())


def _label_speech_issues(raw_label: str) -> list:
    """
    HWG-A11Y-03: Zeichen im angezeigten Beschriftungstext, deren Ansage durch NVDA 2026.3.1
    (Symbolwörterbuch, Standard-Zeichenausführlichkeit 'Einige') die Bedeutung verliert oder verfälscht.
    Formatierungs-Tags (<br>, <span>, <strong>, style=) und geschützte Leerzeichen verändern die Ansage
    nicht und werden daher nicht beanstandet.
    """
    if not raw_label:
        return []
    shown = rendered_label_text(raw_label)
    issues = []
    if "|" in shown:  # wird in der Standardeinstellung übergangen, ab 'Meiste' als 'Senkrechter Strich' angesagt
        issues.append("Pipe-Zeichen")
    if _LABEL_ARROW_RE.search(shown):  # z. B. '=>' wird als 'gleich größer als' angesagt
        issues.append("Pfeil aus Satzzeichen")
    if _LABEL_TAG_REMNANT_RE.search(shown):  # als Text sichtbarer Tag, z. B. aus '&lt;br&gt;'
        issues.append("sichtbarer Tag-Rest")
    if _LABEL_ENTITY_CODE_RE.search(shown):  # doppelt maskierte Entity, z. B. '&szlig;' als Text ('und szlig')
        issues.append("sichtbarer Zeichencode")
    return issues


DEFAULT_FREE_TEXT_PROBES = ("Lorem ipsum dolor", "abc", "x y z", "!!?", "Bitte ausfüllen")


def _is_nontrivial_regex(pattern: str, probes=DEFAULT_FREE_TEXT_PROBES) -> bool:
    """Ein Muster gilt nur als Validierung, wenn es kompilierbar ist und keinen beliebigen Freitext akzeptiert."""
    if not pattern:
        return False
    p = str(pattern).strip()
    if not p:
        return False
    try:
        rx = re.compile(p)
    except re.error:
        return False
    return not any(rx.fullmatch(probe) for probe in probes)


def _css_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value or "")


def _is_width_compact(props: dict, row_counts: dict) -> bool:
    if props.get("maxwidth"):
        mw = str(props.get("maxwidth")).strip().lower()
        if mw not in ("", "100%"):
            return True
    col_w = props.get("colwidth") or props.get("width")
    if col_w is not None and str(col_w).strip() not in ("", "100%", "12"):
        return True
    css_all = f"{_css_text(props.get('cssclasses'))} {_css_text(props.get('cssclasseswrapper'))}".lower()
    # Bootstrap-Spaltenklassen gelten nur bei weniger als 12 Spalten als kompakt (col-12 = volle Breite)
    for m in re.finditer(r"\bcol-(?:[a-z]{2}-)?(\d{1,2})\b", css_all):
        if int(m.group(1)) < 12:
            return True
    if any(cls in css_all for cls in ("w-25", "w-33", "w-50", "compact")):
        return True
    cw = str(props.get("computedwidth") or "").strip()
    if cw.endswith("%"):
        try:
            val = float(cw[:-1])
            if val < 100.0:
                return True
            if val >= 100.0:
                return False
        except ValueError:
            pass
    row_id = str(props.get("rowid") or "").strip()
    parent_id = str(props.get("parentid") or "root").strip()
    if row_id and row_counts.get((parent_id, row_id), 0) >= 2:
        return True
    return False


_INVISIBLE_TAG = re.compile(
    r"<(?P<tag>[a-z0-9]+)\b[^>]*?(?:style\s*=\s*[\"'][^\"']*(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0)[^\"']*[\"']|\shidden(?:\s|=|>))[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _strip_invisible_markup(text: str) -> str:
    """Entfernt HTML-Elemente, die im E-Mail-Client nicht angezeigt werden (display:none, visibility:hidden, hidden)."""
    prev = None
    s = str(text or "")
    while prev != s:
        prev = s
        s = _INVISIBLE_TAG.sub(" ", s)
    return s


def _image_sha256(img_props: dict, images_dict: dict):
    """SHA-256 der eingebetteten Bilddatei (Data-URI oder Verweis auf das images-Verzeichnis des Exports)."""
    import base64
    src = img_props.get("image") or img_props.get("src") or img_props.get("base64") or img_props.get("url") or ""
    if isinstance(src, str) and not src.startswith("data:") and src in (images_dict or {}):
        src = images_dict[src]
    if isinstance(src, dict):
        src = src.get("data") or src.get("image") or ""
    if not isinstance(src, str) or not src.startswith("data:") or "," not in src:
        return None
    try:
        return hashlib.sha256(base64.b64decode(src.split(",", 1)[1])).hexdigest()
    except Exception:
        return None


def evaluate_stage1_ast(form_data: dict, workflow_emails: list, workflow_states: list, filename: str, registry: dict) -> dict:
    """
    Prüft alle 15 deterministischen Governance-Regeln (Stufe 1) sowie die deterministischen
    Vorabbedingungen der hybriden Regeln.
    """
    results = {
        "form": filename,
        "violations": [],
        "recommendations": [],
        "passed": [],
        "not_applicable": {},
        "unverified_semantic": [],
        "llm_evaluation": {
            "evaluated": False,
            "status": "pending",
            "model": None,
            "api_base": None,
            "reason": "",
            "unverified_rules": [],
        },
        "score": 100,
    }

    items = (
        form_data.get("items")
        or (form_data.get("elements") or {}).get("items")
        or []
    )
    form_props = form_data.get("properties") or {}
    form_images_dict = form_data.get("images") if isinstance(form_data.get("images"), dict) else {}
    penalties = registry["scoring"]["severity_penalties"]

    # Prüfparameter aus der Registry (Single Source of Truth)
    cd01 = rule_spec(registry, "HWG-CD-01")
    expected_width = str(cd01.get("expected_maxwidth", "850px")).strip().lower()
    hf_minwidth = str(cd01.get("allowed_minwidth_header_footer", "300px")).strip().lower()
    cd02 = rule_spec(registry, "HWG-CD-02")
    logo_height = str(cd02.get("required_height", "60px")).strip().lower().replace("px", "")
    logo_align = str(cd02.get("required_alignment", "right")).strip().lower()
    alt_patterns = [str(a).lower() for a in cd02.get("alt_patterns", [])]
    alt_logo_term = str(cd02.get("alt_logo_term", "logo")).lower()
    alt_logo_context = [str(a).lower() for a in cd02.get("alt_logo_context_terms", [])]
    approved_logo_hashes = {str(h).lower() for h in cd02.get("approved_logo_sha256", [])}
    a11y01 = rule_spec(registry, "HWG-A11Y-01")
    min_inputs_for_fieldset = int(a11y01.get("min_input_fields_for_fieldset", 4))
    min_legend_letters = int(a11y01.get("min_legend_letters", 3))
    max_ungrouped = int(a11y01.get("max_ungrouped_inputs", 2))
    placeholder_legends = {str(x).strip().lower() for x in a11y01.get("placeholder_legends", [])}
    dsgvo = rule_spec(registry, "HWG-DSGVO-01")
    privacy_markers = [str(m).lower() for m in dsgvo.get("privacy_link_markers", ["datenschutz"])]
    val01 = rule_spec(registry, "HWG-VAL-01")
    free_text_probes = tuple(val01.get("free_text_probes", DEFAULT_FREE_TEXT_PROBES))
    currency_datatypes = {str(x) for x in val01.get("accepted_currency_datatypes", ["formattedNumber", "number", "money", "posmoney", "posmoneyOptionalComma", "integer", "posinteger"])}
    ux02 = rule_spec(registry, "HWG-UX-02")
    ux02_pages = int(ux02.get("min_pages", 3))
    ux02_fields = int(ux02.get("min_input_fields", 25))
    lang02_max = int(rule_spec(registry, "HWG-LANG-02").get("max_sentence_length_words", 25))
    wf01_prefixes = [str(p).lower() for p in rule_spec(registry, "HWG-WF-01").get("canonical_prefixes", [])]
    wf02 = rule_spec(registry, "HWG-WF-02")
    wf02_tokens = [str(t).lower() for t in wf02.get("accepted_placeholders", ["[%$RECORD_ID%]"])]
    wf02_ignore_comments = bool(wf02.get("ignore_html_comments", True))
    wf02_ignore_invisible = bool(wf02.get("ignore_invisible_markup", True))

    def nontrivial(pattern) -> bool:
        return _is_nontrivial_regex(pattern, free_text_probes)

    def legend_ok(text: str) -> bool:
        if len(re.findall(r"[A-Za-zÄÖÜäöüß]", text or "")) < min_legend_letters:
            return False
        core = re.sub(r"[^a-zäöüß ]", "", (text or "").lower())
        core = " ".join(core.split())
        return core not in placeholder_legends

    id_to_item = {}
    for it in items:
        p = it.get("properties", {}) or {}
        iid = str(p.get("id") or p.get("name") or "")
        if iid:
            id_to_item[iid] = it

    def get_ancestors(props: dict):
        chain = []
        pid = str(props.get("parentid") or "")
        visited = set()
        for _ in range(8):
            if not pid or pid in visited or pid not in id_to_item:
                break
            visited.add(pid)
            parent_it = id_to_item[pid]
            chain.append(parent_it)
            pid = str((parent_it.get("properties") or {}).get("parentid") or "")
        return chain

    def is_in_header(it: dict) -> bool:
        c = it.get("className", "")
        props = it.get("properties", {}) or {}
        if c == "XHeader":
            return True
        pid = str(props.get("parentid") or "").strip().lower()
        if pid == "xi-header-1":
            return True
        return any(anc.get("className") == "XHeader" for anc in get_ancestors(props))

    def is_applicant_visible(props: dict) -> bool:
        if hidden_by_status(props):
            return False
        return not any(hidden_by_status(anc.get("properties") or {}) for anc in get_ancestors(props))

    def get_section_key(props: dict) -> str:
        # Gruppiere nach semantischer Sektion (XFieldSet oder XPage), NICHT nach Layout-Spaltencontainer (XContainer)
        for anc in get_ancestors(props):
            if anc.get("className") in ("XFieldSet", "XPage"):
                ap = anc.get("properties") or {}
                return str(ap.get("id") or ap.get("name") or "root")
        return "root"

    row_counts = {}
    in_header_ids = set()
    for it in items:
        props = it.get("properties", {}) or {}
        iid = str(props.get("id") or props.get("name") or "")
        if is_in_header(it) and iid:
            in_header_ids.add(iid)
        if not is_applicant_visible(props):
            continue
        rid = str(props.get("rowid") or "").strip()
        pid = str(props.get("parentid") or "root").strip()
        if rid:
            row_counts[(pid, rid)] = row_counts.get((pid, rid), 0) + 1

    # Container-Breiten (HWG-CD-01)
    container_widths = []
    container_minwidths_invalid = []
    if isinstance(form_props, dict) and form_props.get("maxwidth"):
        container_widths.append(("form_properties", str(form_props.get("maxwidth")).strip().lower()))

    header_images = []
    header_h1_count = 0
    header_has_non_h1_title = False
    has_footer = False
    footer_shows_logo = False
    header_or_footer_shows_req_hint = False
    footer_shows_privacy = False

    fieldsets_all = []
    fieldsets_missing_legend = []
    fieldset_input_counts = {}
    unsemantic_section_containers = []
    container_input_children = {}
    loose_page_inputs = {}

    required_fields_total = 0
    manual_asterisks_in_labels = []
    dirty_labels = []
    dev_notes = []

    section_personal_sequences = {}
    has_personal_data = False
    has_text_dsgvo_with_link = False

    has_upload_elements = False
    deficient_uploads = []
    structured_fields_unvalidated = []
    has_compact_fields = False
    compact_fields_unconstrained = []

    pagination_buttons = []
    has_submit_no_check = False
    raw_xpage_count = 0
    applicant_xpage_count = 0
    total_input_count = 0
    applicant_input_count = 0

    non_person_keywords = [
        "vorname", "antrag", "bank", "kredit", "projekt", "programm",
        "foerder", "förder", "titel", "datei", "forschung", "vorhaben", "institut",
        "organisation", "unternehmen", "thema", "kurs", "modul", "fach",
        "abteilung", "studiengang", "dokument", "bezeichnung", "stelle",
        "einrichtung", "arbeit", "abschlussarbeit", "hochschule", "firma",
        "suche", "praxispartner", "betreuer", "veranstaltung", "weiterbildung",
        "seminar", "tagung", "workshop", "konferenz", "dienstleister",
        "anbieter", "auftragnehmer"
    ]

    for it in items:
        c = it.get("className", "")
        props = it.get("properties", {}) or {}
        elem_name = str(props.get("name") or "unnamed")
        name_low = elem_name.lower()
        app_vis = is_applicant_visible(props)

        if c == "XPage":
            raw_xpage_count += 1
            if app_vis:
                applicant_xpage_count += 1
        if c in INPUT_CLASSES:
            total_input_count += 1

        raw_lbl = str(props.get("label") or props.get("text") or props.get("heading") or "")
        lbl_clean = clean_visible_text(raw_lbl)
        lbl_lower = lbl_clean.lower()
        help_raw = str(props.get("helptext") or props.get("help") or props.get("tooltip") or props.get("description") or "")
        help_clean = clean_visible_text(help_raw).lower()

        # 1. HWG-CD-01: Breiten auf XHeader, XPage, XFooter (inkl. Erkennung fehlender Breiten)
        if c in ("XHeader", "XPage", "XFooter"):
            mw = str(props.get("maxwidth") or "").strip().lower()
            container_widths.append((elem_name, mw or "undefiniert"))
            minw = str(props.get("minwidth") or "").strip().lower()
            if c in ("XHeader", "XFooter") and minw and minw != hf_minwidth:
                container_minwidths_invalid.append(f"{elem_name} (minwidth={minw})")
        elif c == "XContainer" and not props.get("parentid") and props.get("maxwidth"):
            mw = str(props.get("maxwidth") or "").strip().lower()
            container_widths.append((elem_name, mw))

        # 2. HWG-CD-02 & HWG-CD-04: Header-Prüfung
        if is_in_header(it):
            if c == "XImage":
                header_images.append(props)
            rte_h = str(props.get("rtevalue") or "")
            hdg_h = str(props.get("heading") or "")
            lbl_h = str(props.get("label") or "")
            txt_h = str(props.get("text") or "")
            lvl_h = str(props.get("headinglevel") or "").strip()
            combined_h = f"{rte_h} {hdg_h} {lbl_h} {txt_h}"
            h1_blocks = re.findall(r"<\s*h1\b[^>]*>(.*?)<\s*/\s*h1\s*>", combined_h, re.IGNORECASE | re.DOTALL)
            non_empty_h1 = [b for b in h1_blocks if clean_visible_text(b)]
            if non_empty_h1:
                header_h1_count += len(non_empty_h1)
            elif lvl_h == "1" and clean_visible_text(combined_h):
                header_h1_count += 1
            elif re.search(r"<\s*h[2-6]\b", combined_h, re.IGNORECASE) or (
                c in ("XSpan", "XHeading", "XText") and len(clean_visible_text(combined_h)) >= 5
            ):
                header_has_non_h1_title = True

        # 3. HWG-CD-03, HWG-A11Y-02 (Container-Flag), HWG-DSGVO-01 (Footer-Flag)
        if c in ("XHeader", "XFooter"):
            if str(props.get("showrequiredhint", "0")).strip().lower() in ("1", "true"):
                header_or_footer_shows_req_hint = True
        if c == "XFooter":
            has_footer = True
            if str(props.get("showlogo", "0")).strip().lower() in ("1", "true"):
                footer_shows_logo = True
            if str(props.get("privacyPolicyShow", "0")).strip().lower() in ("1", "true"):
                footer_shows_privacy = True

        # 4. HWG-A11Y-01: Fieldsets & Legenden vs. unsemantische Container / lose Seitenfelder
        if c == "XFieldSet" and app_vis:
            fs_key = str(props.get("id") or elem_name)
            fieldsets_all.append(elem_name)
            fieldset_input_counts.setdefault(fs_key, 0)
            leg_txt = clean_visible_text(props.get("legend") or props.get("label") or props.get("heading") or "")
            if not legend_ok(leg_txt):
                fieldsets_missing_legend.append(elem_name)

        if c in INPUT_CLASSES and app_vis:
            applicant_input_count += 1
            ancs = get_ancestors(props)
            fs_anc = next((a for a in ancs if a.get("className") == "XFieldSet"), None)
            if fs_anc:
                fs_p = fs_anc.get("properties") or {}
                fs_key = str(fs_p.get("id") or fs_p.get("name") or "")
                fieldset_input_counts[fs_key] = fieldset_input_counts.get(fs_key, 0) + 1
            else:
                cont_anc = next(
                    (a for a in ancs if a.get("className") in ("XContainer", "XContainerInvisible") and not is_in_header(a)),
                    None,
                )
                if cont_anc:
                    cname = str((cont_anc.get("properties") or {}).get("name") or "container")
                    container_input_children[cname] = container_input_children.get(cname, 0) + 1
                else:
                    page_anc = next((a for a in ancs if a.get("className") == "XPage"), None)
                    pname = str(((page_anc or {}).get("properties") or {}).get("name") or "page")
                    loose_page_inputs[pname] = loose_page_inputs.get(pname, 0) + 1

        # 5. HWG-A11Y-02: Pflichtfelder & manuelle Sternchen
        if c in INPUT_CLASSES and app_vis:
            is_req = str(props.get("required", "0")).strip().lower() in ("1", "true") or bool(str(props.get("requiredif") or "").strip())
            if is_req:
                required_fields_total += 1
            elif re.search(r"(?:\*\s*$|\(\*\)|(?:^|\s|\w)\*(?:\s|[:;,.!?\)]|$))", lbl_clean):
                manual_asterisks_in_labels.append(elem_name)

        # 6. HWG-A11Y-03: Zeichen in Feld- und Fieldset-Beschriftungen, die die Screenreader-Ansage verändern
        if (c in INPUT_CLASSES or c == "XFieldSet") and app_vis:
            for attr_k in ("label", "legend"):
                raw_field_lbl = str(props.get(attr_k) or "")
                lbl_issues = _label_speech_issues(raw_field_lbl) if raw_field_lbl else []
                if lbl_issues:
                    dirty_labels.append((elem_name, rendered_label_text(raw_field_lbl)[:45], lbl_issues))
                    break

        # 7. HWG-UI-01 (Vorab-Detektion bekannter technischer Entwickler-Tokens in Stufe 1)
        if app_vis:
            for tk in ("label", "text", "heading", "rtevalue", "value"):
                tval = str(props.get(tk) or "")
                if not tval:
                    continue
                tval_clean = clean_visible_text(tval)
                tval_low = tval_clean.lower()
                # Stufe 1 zieht nur bei generischen Code-/Arbeitsmarkern ab; fachliche Begriffe
                # (z. B. JavaScript, Shibboleth) gehen lediglich als Kandidaten an Stufe 2.
                if find_dev_markers(tval_clean, registry) == "deterministisch":
                    dev_notes.append((elem_name, tval_clean[:65]))
                    break

        # 8. HWG-BTN-02 & HWG-UX-02: Mehrseiten-Navigationsschaltflächen & Zwischenspeichern extrahieren
        if "Button" in c or c == "XButtonList":
            b_list = props.get("buttons", [])
            if isinstance(b_list, list) and b_list:
                for b in b_list:
                    if not isinstance(b, dict):
                        continue
                    val = clean_visible_text(b.get("value") or b.get("text") or b.get("title") or "")
                    act = b.get("action") if isinstance(b.get("action"), dict) else {}
                    act_page = act.get("page", "")
                    act_name = act.get("displayName") or act.get("value") or ""
                    role = classify_button_role(act_page, "", act_name, ux02.get("save_action_names"))
                    if app_vis and role in ("submit_no_check", "save"):
                        has_submit_no_check = True
                    if val and role == "pagination":
                        pagination_buttons.append({
                            "element": elem_name,
                            "label": val,
                            "action_page": str(act_page),
                            "applicant_visible": app_vis,
                        })
            btn_lbl = clean_visible_text(props.get("text") or props.get("label") or props.get("value") or "")
            btn_role = classify_button_role("", str(props.get("type", "")))
            if app_vis and btn_role == "submit_no_check":
                has_submit_no_check = True
            if btn_lbl and btn_role == "pagination":
                pagination_buttons.append({
                    "element": elem_name,
                    "label": btn_lbl,
                    "action_page": str(props.get("type", "")),
                    "applicant_visible": app_vis,
                })

        # 9. HWG-STAMM-01: Stammdaten-Sequenz pro semantischer Sektion
        if c in INPUT_CLASSES and c != "XTextArea" and app_vis:
            sec_key = get_section_key(props)
            seq = section_personal_sequences.setdefault(sec_key, [])
            has_nachname_in_sec = any(f[0] == "nachname" for f in seq)

            is_explicit_person_lastname = (
                "nachname" in name_low
                or "nachname" in lbl_lower
                or lbl_lower in ("name", "familienname")
                or any(
                    p in lbl_lower
                    for p in ("name antragstellende", "name antragsteller", "name der antragstellenden")
                )
            ) and "vorname" not in name_low and "vorname" not in lbl_lower and "suche" not in name_low and "suche" not in lbl_lower

            is_non_person = any(k in name_low or k in lbl_lower for k in non_person_keywords)

            if any(t in name_low or t in lbl_lower for t in ("anrede", "salutation")) and len(lbl_clean) <= 30:
                seq.append(("anrede", elem_name, lbl_clean or "Anrede", 1))
            elif ("vorname" in name_low or "vorname" in lbl_lower) and "suche" not in name_low:
                seq.append(("vorname", elem_name, lbl_clean or "Vorname", 2))
            elif is_explicit_person_lastname or (
                not is_non_person and not has_nachname_in_sec and "name" in name_low
            ):
                seq.append(("nachname", elem_name, lbl_clean or "Nachname", 3))
            elif any(t in name_low or t in lbl_lower for t in ("email", "e-mail", "mail")) and c == "XTextField":
                seq.append(("email", elem_name, lbl_clean or "E-Mail-Adresse", 4))
            elif any(t in name_low or t in lbl_lower for t in ("telefon", "telefonnummer", "phone", "mobilfunk")) and c == "XTextField":
                seq.append(("telefon", elem_name, lbl_clean or "Telefonnummer", 5))

        # 10. HWG-DSGVO-01: Personendaten & Datenschutzhinweis mit aktivem Link
        if c in INPUT_CLASSES and any(
            t in name_low or t in lbl_lower
            for t in (
                "vorname", "nachname", "email", "e-mail", "mail", "matrikel",
                "geburtsdatum", "iban", "anschrift", "adresse", "telefon",
                "antragsteller", "studierend", "beschäftigt", "mitarbeiter", "person"
            )
        ):
            has_personal_data = True

        raw_html_ctx = f"{raw_lbl} {help_raw} {props.get('rtevalue', '')} {props.get('html', '')}".lower()
        has_priv_term = any(
            t in raw_html_ctx
            for t in ("datenschutz", "dsgvo", "art. 13", "artikel 13", "datenverarbeitung")
        )
        # Linkziele (href oder ausgeschriebene URL) müssen auf eine Datenschutzerklärung zeigen
        link_targets = re.findall(r"href\s*=\s*['\"]([^'\"]+)['\"]", raw_html_ctx) + re.findall(r"https?://[^\s<>'\"]+", raw_html_ctx)
        has_priv_link = any(any(m in tgt for m in privacy_markers) for tgt in link_targets)
        if app_vis and has_priv_term and has_priv_link:
            has_text_dsgvo_with_link = True

        # 11. HWG-UPLOAD-01: Technische & sichtbare Upload-Spezifikation inkl. Kongruenzprüfung
        if (c in ("XUpload", "XFile", "XUploadButton") or "upload" in name_low) and app_vis:
            has_upload_elements = True
            vis_ctx = f"{lbl_lower} {help_clean}"
            has_vis_formats = any(
                fmt in vis_ctx for fmt in ("pdf", "jpg", "jpeg", "png", "docx", "xlsx", "zip", "dateiformat")
            )
            has_vis_size = any(
                sz in vis_ctx for sz in ("mb", "kb", "maximal", "max.", "dateigröße", "dateigroesse")
            )
            fext = str(props.get("fileextension") or "").strip().lower()
            try:
                msize = int(str(props.get("maxfilesize") or "0").strip() or "0")
            except ValueError:
                msize = 0
            has_tech_limits = bool(fext) and msize > 0

            contradiction = False
            if fext:
                for promised_ext in ("pdf", "jpg", "png", "docx", "xlsx", "zip"):
                    if re.search(rf"\b{promised_ext}\b", vis_ctx) and promised_ext[:3] not in fext:
                        contradiction = True
            if msize > 0:
                mb_match = re.search(r"(\d+)\s*mb\b", vis_ctx)
                if mb_match:
                    promised_mb = int(mb_match.group(1))
                    tech_mb_1024 = round(msize / 1024)
                    tech_mb_1000 = round(msize / 1000)
                    if promised_mb not in (tech_mb_1024, tech_mb_1000):
                        contradiction = True

            if not (has_vis_formats and has_vis_size and has_tech_limits) or contradiction:
                reasons = []
                if not has_tech_limits:
                    reasons.append("technische Dateiendung/Maximalgröße fehlt")
                if not has_vis_formats:
                    reasons.append("Formate im Hilfetext/Label fehlen")
                if not has_vis_size:
                    reasons.append("Größenlimit im Hilfetext/Label fehlt")
                if contradiction:
                    reasons.append("Widerspruch zwischen Textangabe und technischen Upload-Limits")
                deficient_uploads.append(f"{elem_name} ({', '.join(reasons)})")

        # 12. HWG-VAL-01: Strukturierte Eingabefelder (E-Mail, PLZ, IBAN, Telefon, Datum, Matrikelnummer, Geldbeträge)
        is_perm_hidden = (
            str(props.get("ishidden", "0")).strip().lower() in ("1", "true")
            and not str(props.get("hiddenif") or "").strip()
        )
        if c in ("XTextField", "XDate", "XNumber") and app_vis and not is_perm_hidden:
            dt = str(props.get("datatype") or "").strip()
            vrule = str(props.get("vrule") or props.get("pattern") or "").strip()
            val_dict = props.get("validation") if isinstance(props.get("validation"), dict) else {}
            has_custom_regex = (
                (dt == "regexp" and nontrivial(vrule))
                or nontrivial(props.get("pattern") or "")
                or nontrivial(val_dict.get("regex") or "")
            )

            is_email = (
                any(t in name_low or t in lbl_lower for t in ("email", "e-mail"))
                or bool(re.search(r"\bmail\b", lbl_lower))
            ) and "infotext" not in lbl_lower
            is_plz = (
                bool(re.search(r"\b(?:plz|postleitzahl)\b", lbl_lower))
                or "plz" in name_low
                or "postleitzahl" in name_low
            ) and "stadt" not in lbl_lower and "ort" not in lbl_lower
            is_iban = bool(re.search(r"\biban\b", lbl_lower)) or "iban" in name_low
            is_phone = any(t in name_low or t in lbl_lower for t in ("telefon", "mobilfunk", "handynummer"))
            is_date = (
                c == "XDate"
                or any(t in name_low or t in lbl_lower for t in ("geburtsdatum", "einreichungsfrist"))
                or (bool(re.search(r"\bdatum\b", lbl_lower)) and "ort" not in lbl_lower)
            )
            is_matrikel = "matrikel" in name_low or "matrikel" in lbl_lower
            is_currency = (
                c == "XNumber"
                or bool(re.search(r"\b(?:rechnungsbetrag|auszahlungsbetrag|geldbetrag|gesamtsumme|antragssumme|fördersumme|foerdersumme|teilnahmegebühr|teilnahmegebuehr)\b", lbl_lower))
            )

            if is_email and is_phone:
                structured_fields_unvalidated.append(
                    f"{elem_name} (Kombiniertes 2-in-1-Kontaktfeld '{lbl_clean}': E-Mail und Telefon müssen für eine eindeutige Formatvalidierung in getrennte Felder aufgeteilt werden)"
                )
            elif is_email:
                if not (dt in ("email", "traditionalEmail") or val_dict.get("type") == "email" or has_custom_regex):
                    structured_fields_unvalidated.append(f"{elem_name} (E-Mail: Typ '{dt or 'text'}')")
            elif is_plz:
                if not (dt == "plzDE" or has_custom_regex):
                    structured_fields_unvalidated.append(f"{elem_name} (PLZ: Typ '{dt or 'text'}')")
            elif is_iban:
                if not (has_custom_regex or val_dict.get("type") == "iban"):
                    structured_fields_unvalidated.append(f"{elem_name} (IBAN: leeres/fehlendes Regex-Muster)")
            elif is_phone:
                if not (dt == "phone" or has_custom_regex):
                    structured_fields_unvalidated.append(f"{elem_name} (Telefon: unvalidierter Freitext)")
            elif is_date:
                if not (c == "XDate" or dt in ("dateDE", "dateEN") or has_custom_regex):
                    structured_fields_unvalidated.append(f"{elem_name} (Datum: Typ '{dt or 'text'}')")
            elif is_matrikel:
                if not has_custom_regex:
                    structured_fields_unvalidated.append(f"{elem_name} (Matrikelnummer: Typ '{dt or 'text'}' ohne 6–7-stelligen Regex)")
            elif is_currency:
                if not (c == "XNumber" or dt in currency_datatypes or has_custom_regex):
                    structured_fields_unvalidated.append(f"{elem_name} (Geldbetrag: Typ '{dt or 'text'}')")

        # 13. HWG-UX-01: Kompakte Feldbreiten (PLZ, Hausnummer)
        if c == "XTextField" and app_vis:
            is_compact = (
                (bool(re.search(r"\b(?:plz|postleitzahl|hausnummer|hausnr|hnr)\b", lbl_lower)) and "stadt" not in lbl_lower and "ort" not in lbl_lower)
                or any(k in name_low for k in ("plz", "postleitzahl", "hausnummer", "hnr"))
            )
            if is_compact:
                has_compact_fields = True
                if not _is_width_compact(props, row_counts):
                    compact_fields_unconstrained.append(elem_name)

    for cname, cnt in container_input_children.items():
        if cnt > max_ungrouped:
            unsemantic_section_containers.append(f"{cname} ({cnt} Felder)")
    for pname, cnt in loose_page_inputs.items():
        if cnt > max_ungrouped:
            unsemantic_section_containers.append(f"Direkt auf Seite {pname} ({cnt} ungruppierte Felder)")
    total_ungrouped_inputs = sum(container_input_children.values()) + sum(loose_page_inputs.values())
    if total_ungrouped_inputs > max_ungrouped and not unsemantic_section_containers:
        unsemantic_section_containers.append(f"Insgesamt {total_ungrouped_inputs} ungruppierte Eingabefelder außerhalb von XFieldSet")

    # --- Regelauswertung Stufe 1 (15 deterministische Regeln + hybride Vorabfilter) ---

    # 1. HWG-CD-01
    distinct_widths = {w for _, w in container_widths if w}
    if not distinct_widths or distinct_widths != {expected_width} or container_minwidths_invalid:
        bad_w = sorted(w for w in distinct_widths if w != expected_width)
        if container_minwidths_invalid:
            bad_w.extend(container_minwidths_invalid)
        results["violations"].append({
            "rule": "HWG-CD-01",
            "severity": "major",
            "message": f"Layout-Container weichen vom {expected_width}-Standard ab (gefunden: {', '.join(bad_w or ['undefiniert'])})."
        })
        results["score"] -= penalties["major"]
    else:
        results["passed"].append(f"HWG-CD-01 (Layout-Breite durchgängig {expected_width})")

    # 2. HWG-CD-02
    valid_source_images = [
        ip for ip in header_images
        if ip.get("image") or ip.get("url") or ip.get("src") or ip.get("base64")
    ]
    if approved_logo_hashes:
        approved_images = [ip for ip in valid_source_images if _image_sha256(ip, form_images_dict) in approved_logo_hashes]
    else:
        approved_images = valid_source_images
    if not valid_source_images:
        results["violations"].append({
            "rule": "HWG-CD-02",
            "severity": "critical",
            "message": "Kein offizielles HWG-Header-Banner (XImage mit gültiger Bilddatei) innerhalb des Header-Containers (XHeader) vorhanden."
        })
        results["score"] -= penalties["critical"]
    elif not approved_images:
        results["violations"].append({
            "rule": "HWG-CD-02",
            "severity": "critical",
            "message": "Das Bild im Header-Container ist keine freigegebene Datei des HWG-Logos (Prüfsumme stimmt mit keiner Logodatei aus der Registry überein)."
        })
        results["score"] -= penalties["critical"]
    else:
        valid_source_images = approved_images
        def _eval_header_logo(img_p: dict):
            img_h = str(img_p.get("imageheight") or img_p.get("height") or "").strip().lower()
            img_align = str(img_p.get("textalign") or img_p.get("align") or "").strip().lower()
            img_alt = clean_visible_text(
                img_p.get("alt")
                or img_p.get("alttext")
                or img_p.get("title")
                or ""
            )
            img_alt_low = img_alt.lower()

            is_h60 = img_h.replace("px", "") == logo_height
            css_classes = img_p.get("cssclasses") or []
            if isinstance(css_classes, str):
                css_classes = [css_classes]
            is_right = img_align in (logo_align, "2") or f"xm-t-{logo_align}" in css_classes
            has_valid_alt = bool(img_alt) and (
                any(pat in img_alt_low for pat in alt_patterns)
                or (alt_logo_term in img_alt_low and any(t in img_alt_low for t in alt_logo_context))
            )
            issues = []
            if not is_h60:
                issues.append(f"Höhe ist '{img_h or 'undefiniert'}' statt {logo_height}px")
            if not is_right:
                issues.append(f"Ausrichtung ist '{img_align or 'links'}' statt rechtsbündig")
            if not has_valid_alt:
                issues.append("barrierefreier Alternativtext (alt) fehlt oder unvollständig")
            return (is_h60 and is_right and has_valid_alt), issues

        eval_candidates = [_eval_header_logo(ip) for ip in valid_source_images]
        if any(ok for ok, _ in eval_candidates):
            results["passed"].append("HWG-CD-02 (Header-Logo 60px, rechtsbündig, mit korrektem Alt-Text)")
        else:
            issues = eval_candidates[0][1]
            results["violations"].append({
                "rule": "HWG-CD-02",
                "severity": "major",
                "message": f"HWG-Header-Logo weicht von den Vorgaben ab: {', '.join(issues)}."
            })
            results["score"] -= penalties["major"]

    # 3. HWG-CD-03
    if not has_footer:
        results["violations"].append({
            "rule": "HWG-CD-03",
            "severity": "major",
            "message": "Kein normierter Fußzeilen-Container (XFooter) mit deaktiviertem Herstellerlogo (showlogo='0') vorhanden."
        })
        results["score"] -= penalties["major"]
    elif footer_shows_logo:
        results["violations"].append({
            "rule": "HWG-CD-03",
            "severity": "major",
            "message": "Im Footer (XFooter) ist 'Logo anzeigen' (showlogo='1') aktiviert ('Powered by XIMA Formcycle')."
        })
        results["score"] -= penalties["major"]
    else:
        results["passed"].append("HWG-CD-03 (XIMA-Herstellerlogo im Footer deaktiviert)")

    # 4. HWG-CD-04
    if header_h1_count == 1:
        results["passed"].append("HWG-CD-04 (Formulartitel im Header eindeutig als H1 ausgezeichnet)")
    else:
        if header_h1_count > 1:
            msg = f"Mehrere H1-Überschriften ({header_h1_count}) im Header gefunden; zulässig ist genau eine H1-Hauptüberschrift."
        elif header_has_non_h1_title:
            msg = "Formulartitel im Header (XHeader) ist nicht als Überschrift erster Ordnung (<h1>) ausgezeichnet (nur H2/Fließtext gefunden)."
        else:
            msg = "Kein Formulartitel als <h1> innerhalb des Kopfbereichs (XHeader) vorhanden."
        results["violations"].append({
            "rule": "HWG-CD-04",
            "severity": "major",
            "message": msg
        })
        results["score"] -= penalties["major"]

    # 5. HWG-A11Y-01
    non_empty_fieldsets = [k for k, cnt in fieldset_input_counts.items() if cnt > 0]
    if applicant_input_count < min_inputs_for_fieldset and not fieldsets_all:
        results["not_applicable"]["HWG-A11Y-01"] = f"Kleinstformular (< {min_inputs_for_fieldset} Eingabefelder): Keine Abschnittsgruppierung erforderlich."
    elif not non_empty_fieldsets:
        results["violations"].append({
            "rule": "HWG-A11Y-01",
            "severity": "critical",
            "message": f"Keine semantischen XFieldSet-Gruppierungen für {applicant_input_count} Eingabefelder vorhanden."
        })
        results["score"] -= penalties["critical"]
    elif fieldsets_missing_legend or unsemantic_section_containers:
        issues = []
        if fieldsets_missing_legend:
            issues.append(f"XFieldSet ohne aussagekräftige Legende (mind. {min_legend_letters} Buchstaben): {', '.join(fieldsets_missing_legend[:4])}")
        if unsemantic_section_containers:
            issues.append(f"Ungruppierte Eingabefelder außerhalb von XFieldSet: {', '.join(unsemantic_section_containers[:3])}")
        results["violations"].append({
            "rule": "HWG-A11Y-01",
            "severity": "critical",
            "message": f"Semantische Abschnittsgruppierung unvollständig: {'; '.join(issues)}."
        })
        results["score"] -= penalties["critical"]
    else:
        results["passed"].append(f"HWG-A11Y-01 ({len(non_empty_fieldsets)} XFieldSet-Gruppen mit Legenden vorhanden)")

    # 6. HWG-A11Y-02
    a11y02_issues = []
    if required_fields_total > 0 and not header_or_footer_shows_req_hint:
        a11y02_issues.append("Pflichtfeldlegende (showrequiredhint='1') weder in Kopfzeile (XHeader) noch Fußzeile (XFooter) aktiviert")
    if manual_asterisks_in_labels:
        a11y02_issues.append(f"Manuelles Sternchen (*) im Label ohne native Pflichtfeldfunktion (required='1') in {manual_asterisks_in_labels[:4]}")

    if a11y02_issues:
        results["violations"].append({
            "rule": "HWG-A11Y-02",
            "severity": "major",
            "message": f"Pflichtfeld-Deklaration fehlerhaft: {'; '.join(a11y02_issues)}."
        })
        results["score"] -= penalties["major"]
    elif required_fields_total > 0:
        results["passed"].append(f"HWG-A11Y-02 ({required_fields_total} Pflichtfelder programmatisch inkl. globaler Legende deklariert)")
    else:
        results["not_applicable"]["HWG-A11Y-02"] = "Formular ohne Pflichtfelder (zulässig)."

    # 7. HWG-A11Y-03
    if dirty_labels:
        affected = [d[0] for d in dirty_labels]
        preview = ", ".join(affected[:6]) + (f" und {len(affected) - 6} weitere" if len(affected) > 6 else "")
        kinds = []
        for d in dirty_labels:
            for k in d[2]:
                if k not in kinds:
                    kinds.append(k)
        results["violations"].append({
            "rule": "HWG-A11Y-03",
            "severity": "minor",
            "message": f"Zeichen, die die Screenreader-Ansage verändern ({', '.join(kinds)}), in {len(dirty_labels)} Beschriftung(en): [{preview}]."
        })
        results["score"] -= penalties["minor"]
    else:
        results["passed"].append("HWG-A11Y-03 (Beschriftungen ohne Pipe-Zeichen, Pfeile und sichtbare Code-Reste)")

    # 8. HWG-STAMM-01
    total_personal_fields = sum(len(v) for v in section_personal_sequences.values())
    out_of_order = []
    for sec_key, seq in section_personal_sequences.items():
        if len(seq) < 2:
            continue
        seen_ranks_in_current_person = set()
        for i in range(len(seq) - 1):
            curr, nxt = seq[i], seq[i + 1]
            seen_ranks_in_current_person.add(curr[3])
            # Falls in derselben Sektion ein zweiter Personenblock beginnt (nach Vorname+Nachname oder nach E-Mail/Telefon):
            if (
                (nxt[3] in (1, 2) and 2 in seen_ranks_in_current_person and 3 in seen_ranks_in_current_person)
                or (nxt[3] in (1, 2, 3) and any(r in seen_ranks_in_current_person for r in (4, 5)))
            ):
                seen_ranks_in_current_person = set()
                continue
            if curr[3] > nxt[3]:
                out_of_order.append(f"'{curr[2]}' ({curr[1]}) vor '{nxt[2]}' ({nxt[1]})")

    if any(len(v) >= 2 for v in section_personal_sequences.values()):
        if out_of_order:
            results["violations"].append({
                "rule": "HWG-STAMM-01",
                "severity": "major",
                "message": f"Inkonsistente Stammdaten-Abfolge: {', '.join(out_of_order[:3])}. Erwartet: Anrede -> Vorname -> Nachname -> E-Mail-Adresse -> Telefonnummer."
            })
            results["score"] -= penalties["major"]
        else:
            results["passed"].append("HWG-STAMM-01 (Kanonische Stammdaten-Abfolge in allen Sektionen eingehalten)")
    else:
        results["not_applicable"]["HWG-STAMM-01"] = (
            f"Keine zusammenhängende Stammdaten-Sektion mit >= 2 Personenfeldern vorhanden (gefunden: {total_personal_fields})."
        )

    # 9. HWG-BTN-02 (Deterministische Prüfung der Mehrseiten-Navigation)
    if not pagination_buttons:
        results["not_applicable"]["HWG-BTN-02"] = (
            "Einseitiges Formular ohne Seitennavigation."
            if raw_xpage_count <= 1
            else "Keine expliziten Seitenwechsel-Schaltflächen im Formular definiert."
        )
    else:
        app_pag = [b for b in pagination_buttons if b["applicant_visible"]] or pagination_buttons
        bad_pag = []
        for b in app_pag:
            # Seit Registry 1.7.0 ist auch die Kleinschreibung zulässig (Wunsch der Hochschul-IT).
            if b["label"] not in ("Weiter", "Zurück", "weiter", "zurück"):
                bad_pag.append(f"'{b['label']}' ({b['element']})")
        if bad_pag:
            dedup_bad = list(dict.fromkeys(bad_pag))
            results["violations"].append({
                "rule": "HWG-BTN-02",
                "severity": "minor",
                "message": f"Uneinheitliche Seitennavigation (erlaubt: 'Weiter' / 'Zurück' oder 'weiter' / 'zurück'): {', '.join(dedup_bad[:4])}."
            })
            results["score"] -= penalties["minor"]
        else:
            results["passed"].append("HWG-BTN-02 (Einheitliche Mehrseiten-Navigation 'Weiter' / 'Zurück' bzw. 'weiter' / 'zurück' eingehalten)")

    # 10. HWG-DSGVO-01
    has_dsgvo_notice = footer_shows_privacy or has_text_dsgvo_with_link
    if applicant_input_count == 0:
        results["not_applicable"]["HWG-DSGVO-01"] = "Formular ohne für Antragstellende sichtbare Eingabefelder."
    elif not has_dsgvo_notice:
        results["violations"].append({
            "rule": "HWG-DSGVO-01",
            "severity": "critical",
            "message": "Das Formular erhebt Eingaben, aber weder die Footer-Datenschutzerklärung (privacyPolicyShow='1') noch ein sichtbarer, auf die Datenschutzerklärung verlinkter Art.-13-Hinweis ist vorhanden."
        })
        results["score"] -= penalties["critical"]
    else:
        results["passed"].append("HWG-DSGVO-01 (DSGVO-Datenschutzhinweis mit Verlinkung / Footer-Option vorhanden)")

    # 11. HWG-UPLOAD-01
    if not has_upload_elements:
        hidden_uploads = any(it.get("className") in ("XUpload", "XFile", "XUploadButton") for it in items)
        results["not_applicable"]["HWG-UPLOAD-01"] = (
            "Dateiupload-Elemente erscheinen erst in späteren Bearbeitungsstatus; für die Sicht der Antragstellenden im Ausgangsstatus nicht anwendbar."
            if hidden_uploads
            else "Keine Dateiupload-Elemente im Formular vorhanden."
        )
    elif deficient_uploads:
        results["violations"].append({
            "rule": "HWG-UPLOAD-01",
            "severity": "major",
            "message": f"{len(deficient_uploads)} Upload-Element(e) unvollständig oder widersprüchlich spezifiziert: {', '.join(deficient_uploads[:3])}."
        })
        results["score"] -= penalties["major"]
    else:
        results["passed"].append("HWG-UPLOAD-01 (Alle Dateiuploads technisch und im Hilfetext konsistent spezifiziert)")

    # 12. HWG-VAL-01
    if structured_fields_unvalidated:
        results["violations"].append({
            "rule": "HWG-VAL-01",
            "severity": "major",
            "message": f"Strukturierte Eingabefelder ohne gültigen Validator / Regex gefunden: {', '.join(structured_fields_unvalidated[:4])}."
        })
        results["score"] -= penalties["major"]
    else:
        results["passed"].append("HWG-VAL-01 (Alle strukturierten Eingabefelder formal validiert)")

    # 13. HWG-UX-01
    if not has_compact_fields:
        results["not_applicable"]["HWG-UX-01"] = "Keine typischen Kompaktfelder (PLZ, Hausnummer) im Formular vorhanden."
    elif compact_fields_unconstrained:
        results["violations"].append({
            "rule": "HWG-UX-01",
            "severity": "minor",
            "message": f"Kompakte Felder über volle 100%-Zeilenbreite gestreckt: {', '.join(compact_fields_unconstrained[:3])}."
        })
        results["score"] -= penalties["minor"]
    else:
        results["passed"].append("HWG-UX-01 (Affordanzgerechte kompakte Feldbreiten eingehalten)")

    # 14. HWG-WF-01
    if not workflow_states:
        results["violations"].append({
            "rule": "HWG-WF-01",
            "severity": "minor",
            "message": "Keine expliziten Status-Knoten (FC_CHANGE_STATE) im Workflow hinterlegt."
        })
        results["score"] -= penalties["minor"]
    else:
        canonical_roots = tuple(wf01_prefixes)
        non_canonical_states = [
            s for s in workflow_states
            if not any(s.lower().strip().startswith(root) for root in canonical_roots)
        ]
        if non_canonical_states:
            results["violations"].append({
                "rule": "HWG-WF-01",
                "severity": "minor",
                "message": f"Nicht-standardisierte Ad-hoc-Workflow-Status gefunden ({len(non_canonical_states)}): {non_canonical_states}."
            })
            results["score"] -= penalties["minor"]
        else:
            results["passed"].append(f"HWG-WF-01 (Kanonischer Workflow-Lebenszyklus eingehalten: {', '.join(workflow_states)})")

    # 15. HWG-WF-02 (Prüfung über ALLE Workflow-E-Mails, kein 1-aus-N-Loophole)
    if not workflow_emails:
        results["not_applicable"]["HWG-WF-02"] = "Keine automatisierten Workflow-E-Mails im Prozessmodell vorhanden."
    else:
        def _mail_text(em):
            txt = f"{em.get('subject', '')} {em.get('body', '')}"
            if wf02_ignore_comments:
                txt = re.sub(r"<!--.*?-->", " ", txt, flags=re.DOTALL)
            if wf02_ignore_invisible:
                txt = _strip_invisible_markup(txt)
            return txt.lower()
        emails_with_record_id = [
            em for em in workflow_emails
            if any(tok in _mail_text(em) for tok in wf02_tokens)
        ]
        missing_count = len(workflow_emails) - len(emails_with_record_id)
        if missing_count > 0:
            results["violations"].append({
                "rule": "HWG-WF-02",
                "severity": "minor",
                "message": f"Fehlender dynamischer Vorgangsplatzhalter ([%$RECORD_ID%]) in {missing_count}/{len(workflow_emails)} automatisierten Workflow-E-Mails (FC_EMAIL)."
            })
            results["score"] -= penalties["minor"]
        else:
            results["passed"].append(f"HWG-WF-02 (Dynamische Vorgangsreferenz ([%$RECORD_ID%]) in allen {len(workflow_emails)}/{len(workflow_emails)} Workflow-E-Mails vorhanden)")

    # --- Hybride Vorab-Regeln (HWG-UI-01, HWG-UX-02, HWG-LANG-02, HWG-FEEDBACK-01) ---
    # HWG-UI-01 (Stufe 1: bekannte technische Leck-Tokens)
    if dev_notes:
        preview_dn = "; ".join(f"{n}: '{txt}'" for n, txt in dev_notes[:3])
        results["violations"].append({
            "rule": "HWG-UI-01",
            "severity": "critical",
            "message": f"Sichtbare Entwickler-Notizen / technische Hinweise im Frontend: {preview_dn}."
        })
        results["score"] -= penalties["critical"]

    # HWG-UX-02 (Stufe 1: Komplexitätsschwellenwert & Zwischenspeichern-Mechanismus)
    effective_pages = max(1, raw_xpage_count)
    resume_placeholders = ux02.get("resume_link_placeholders") or list(DEFAULT_RESUME_LINK_PLACEHOLDERS)
    resume_label = " oder ".join(str(p) for p in resume_placeholders)
    has_resume_link = has_resume_link_email(workflow_emails, resume_placeholders)
    if effective_pages < ux02_pages and total_input_count < ux02_fields:
        results["not_applicable"]["HWG-UX-02"] = (
            f"Formular unterhalb des Komplexitätsschwellenwerts ({effective_pages} Seite(n), {total_input_count} Eingabefelder; Schwellenwert: >= {ux02_pages} Seiten oder >= {ux02_fields} Felder)."
        )
    elif has_submit_no_check and has_resume_link:
        results["passed"].append(
            f"HWG-UX-02 (Zwischenspeichern-Schaltfläche [Aktion 'speichern' oder 'absenden' ohne Prüfung] und Fortsetzungs-Link {resume_label} in einer E-Mail an die antragstellende Person vorhanden)"
        )
    else:
        results["recommendations"].append({
            "rule": "HWG-UX-02",
            "rule_id": "HWG-UX-02",
            "severity": "recommendation",
            "message": (
                f"Umfangreiches Formular ({effective_pages} Seite(n), {total_input_count} Eingabefelder): "
                f"Einrichtung einer 'Zwischenspeichern'-Schaltfläche (Aktion 'speichern') mit Fortsetzungs-Link ({resume_label}) in einer E-Mail an die antragstellende Person empfohlen."
            ),
        })

    # HWG-LANG-02 (Stufe 1: deterministische Satzlängenprüfung > 25 Wörter)
    applicant_items = [it for it in items if is_applicant_visible(it.get("properties", {}) or {})]
    long_sents = extract_long_sentences(applicant_items, max_words=lang02_max, in_header_ids=in_header_ids)
    if long_sents:
        s0 = long_sents[0]
        results["violations"].append({
            "rule": "HWG-LANG-02",
            "severity": "minor",
            "message": (
                f"{len(long_sents)} überlange(r) Satz/Sätze (> {lang02_max} Wörter) detektiert, z. B. in '{s0['element']}' "
                f"({s0['word_count']} Wörter: '{s0['preview']}')."
            ),
        })
        results["score"] -= penalties["minor"]

    # HWG-FEEDBACK-01 (Stufe 1: Existenzprüfung eines an Antragstellende adressierten FC_EMAIL-Knotens)
    applicant_addressed_emails = [
        em for em in workflow_emails if is_dynamic_recipient(em.get("recipient", ""))
    ]
    if workflow_emails and not applicant_addressed_emails:
        results["violations"].append({
            "rule": "HWG-FEEDBACK-01",
            "severity": "minor",
            "message": (
                "Keine automatisierte Eingangsbestätigung (FC_EMAIL) an einen dynamischen Empfänger-Platzhalter "
                "([%...%], z. B. das E-Mail-Feld der antragstellenden Person) hinterlegt."
            ),
        })
        results["score"] -= penalties["minor"]

    results["score"] = max(0, results["score"])
    return results


def _mark_semantic_unverified(results: dict, registry: dict, status: str, reason: str, api_base: str, model: str, rule_ids=None):
    target_ids = rule_ids if rule_ids is not None else SEMANTIC_RULE_IDS
    rule_lookup = {r["id"]: r for r in registry["rules"]}
    already_violated = {v.get("rule") or v.get("rule_id") for v in results.get("violations", [])}
    already_recommended = {r.get("rule") or r.get("rule_id") for r in results.get("recommendations", [])}
    already_passed = {p.split()[0] for p in results.get("passed", []) if p}

    for rid in target_ids:
        # Falls eine hybride Regel (z. B. HWG-UI-01, HWG-UX-02, HWG-LANG-02, HWG-FEEDBACK-01) bereits deterministisch in Stufe 1 entschieden wurde:
        if rid in already_violated or rid in already_recommended or rid in already_passed:
            continue
        if rid == "HWG-UX-02" and rid in results.get("not_applicable", {}):
            continue
        rtype = (rule_lookup.get(rid) or {}).get("evaluation_type", "semantic")
        label = "Semantische KI-Regel" if rtype == "semantic" else "Hybride AST/KI-Regel"
        if rid not in results["unverified_semantic"]:
            results["unverified_semantic"].append(rid)
        results["not_applicable"][rid] = (
            f"KI-Prüfung ausstehend ({label}): Diese Regel wurde NICHT durch ein Sprachmodell (LLM) begutachtet. "
            f"Grund: {reason}"
        )
    results["llm_evaluation"] = {
        "evaluated": False,
        "status": status,
        "api_base": api_base,
        "model": model,
        "reason": reason,
        "unverified_rules": list(results["unverified_semantic"]),
    }


def _extract_json_from_llm_text(raw_text: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL | re.IGNORECASE).strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    else:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


ALLOWED_STATUSES = {
    "HWG-UX-02": {"PASS", "RECOMMENDATION", "NOT_APPLICABLE"},
}
DEFAULT_ALLOWED_STATUSES = {"PASS", "VIOLATION", "NOT_APPLICABLE"}


def merge_llm_evaluation_payload(results: dict, llm_obj: dict, registry: dict, model_name: str, api_base: str) -> dict:
    """
    Fusioniert die JSON-Antwort eines LLMs für die 7 semantischen/hybriden Regeln mit den
    Stufe-1-Ergebnissen. Die Punktberechnung folgt Gleichung (4.1). Unzulässige, widersprüchliche
    oder fehlende Bewertungen werden nicht umgedeutet, sondern als ungeprüft markiert.
    """
    penalties = registry["scoring"]["severity_penalties"]
    rule_lookup = {r["id"]: r for r in registry["rules"]}
    evaluated_entries = llm_obj.get("evaluated_rules") if isinstance(llm_obj, dict) else None
    if not isinstance(evaluated_entries, list):
        evaluated_entries = []
    seen_semantic = set()
    existing_violation_map = {(v.get("rule") or v.get("rule_id")): v for v in results["violations"]}
    existing_rec_map = {(r.get("rule") or r.get("rule_id")): r for r in results["recommendations"]}
    existing_passed_ids = {p.split()[0] for p in results["passed"] if p}
    invalid_entries = []

    for entry in evaluated_entries:
        if not isinstance(entry, dict):
            continue
        rid = str(entry.get("rule_id") or entry.get("rule") or "").strip()
        if rid not in SEMANTIC_RULE_IDS or rid in seen_semantic:
            continue
        seen_semantic.add(rid)

        reg_rule = rule_lookup[rid]
        canonical_sev = reg_rule["severity"]
        status = str(entry.get("status") or "").strip().upper().replace("N/A", "NOT_APPLICABLE")
        finding = str(entry.get("finding") or entry.get("message") or "").strip()
        rem_sugg = str(entry.get("remediation_suggestion") or "").strip()
        allowed = ALLOWED_STATUSES.get(rid, DEFAULT_ALLOWED_STATUSES)

        if status not in allowed:
            # z. B. "RECOMMENDATION" für eine Pflichtregel: keine stille Herabstufung, sondern ungeprüft
            invalid_entries.append(rid)
            if rid not in existing_violation_map and rid not in existing_rec_map and rid not in existing_passed_ids:
                if rid not in results["unverified_semantic"]:
                    results["unverified_semantic"].append(rid)
                results["not_applicable"][rid] = (
                    f"KI-Prüfung unvollständig: unzulässiger Status '{status or 'leer'}' vom Modell '{model_name}'."
                )
            continue

        results["not_applicable"].pop(rid, None)
        if rid in results["unverified_semantic"]:
            results["unverified_semantic"].remove(rid)

        affected = entry.get("affected_elements") or []
        if not isinstance(affected, list):
            affected = [affected]
        aff_str = f" (Betroffen: {', '.join(str(a) for a in affected[:4])})" if affected else ""
        full_msg = f"[KI-Befund · {model_name}] {finding}{aff_str}" + (f" — Vorschlag: {rem_sugg}" if rem_sugg else "")

        if status == "VIOLATION":
            if rid in existing_violation_map:
                # Hybride Regel bereits in Stufe 1 abgezogen -> Befund anreichern, kein Doppelabzug
                existing_violation_map[rid]["message"] = full_msg
            else:
                results["violations"].append({
                    "rule": rid,
                    "rule_id": rid,
                    "severity": canonical_sev,
                    "message": full_msg,
                })
                results["score"] -= penalties.get(canonical_sev, 0)
        elif status == "RECOMMENDATION":
            if rid in existing_rec_map:
                existing_rec_map[rid]["message"] = full_msg
            elif rid not in existing_passed_ids:
                results["recommendations"].append({
                    "rule": rid,
                    "rule_id": rid,
                    "severity": "recommendation",
                    "message": full_msg,
                })
        elif status == "NOT_APPLICABLE":
            if rid not in existing_violation_map and rid not in existing_rec_map and rid not in existing_passed_ids:
                results["not_applicable"][rid] = f"[KI-Prüfung · {model_name}] {finding or 'Auf diesen Formularkontext nicht anwendbar.'}"
        elif status == "PASS":
            # Ein PASS des Modells hebt einen deterministischen Stufe-1-Befund nie auf
            if rid not in existing_violation_map and rid not in existing_rec_map and rid not in existing_passed_ids:
                results["passed"].append(f"{rid} ({finding or 'Semantische KI-Prüfung bestanden'})")

    missing = [
        rid for rid in SEMANTIC_RULE_IDS
        if rid not in seen_semantic
        and rid not in existing_violation_map
        and rid not in existing_rec_map
        and rid not in existing_passed_ids
    ]
    for rid in missing:
        if rid == "HWG-UX-02" and rid in results.get("not_applicable", {}) and rid not in results["unverified_semantic"]:
            continue
        if rid not in results["unverified_semantic"]:
            results["unverified_semantic"].append(rid)
        results["not_applicable"][rid] = (
            f"KI-Prüfung unvollständig: Modell '{model_name}' lieferte keine Bewertung für {rid}."
        )

    results["score"] = max(0, results["score"])
    complete = len(results["unverified_semantic"]) == 0
    results["llm_evaluation"] = {
        "evaluated": complete,
        "status": "ok" if complete else "partial",
        "api_base": api_base,
        "model": model_name,
        "invalid_entries": invalid_entries,
        "reason": f"Semantische Prüfung über Modell '{model_name}' abgeschlossen."
        if complete
        else f"Modell '{model_name}' hat nicht alle semantischen Regeln zulässig bewertet (offen: {', '.join(results['unverified_semantic'])}).",
        "unverified_rules": list(results["unverified_semantic"]),
    }
    return results


def _normalize_base(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def allowed_api_bases() -> list:
    """Erlaubte LLM-Endpunkte (serverseitig konfiguriert); schützt vor SSRF und Schlüsselabfluss."""
    raw = os.environ.get("LLM_ALLOWED_API_BASES", "")
    bases = [b for b in (x.strip() for x in raw.split(",")) if b]
    default_base = os.environ.get("LLM_API_BASE") or os.environ.get("GWDG_API_BASE") or DEFAULT_API_BASE
    bases += [default_base, "http://localhost:11434/v1", "http://127.0.0.1:11434/v1"]
    return list(dict.fromkeys(_normalize_base(b) for b in bases))


def evaluate_stage2_llm(results: dict, form_data: dict, workflow_emails: list, filename: str, registry: dict, llm_config: dict = None) -> dict:
    _load_dotenv_if_present()
    cfg = llm_config or {}
    env_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("GWDG_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    server_base = (
        os.environ.get("LLM_API_BASE")
        or os.environ.get("LLM_BASE_URL")
        or os.environ.get("GWDG_API_BASE")
        or DEFAULT_API_BASE
    ).strip().rstrip("/")
    api_base = (cfg.get("api_base") or server_base).strip().rstrip("/")
    model = (
        cfg.get("model")
        or os.environ.get("LLM_MODEL")
        or os.environ.get("GWDG_MODEL")
        or DEFAULT_LLM_MODEL
    ).strip()

    if _normalize_base(api_base) not in allowed_api_bases():
        _mark_semantic_unverified(
            results, registry, status="rejected",
            reason=f"LLM-Endpunkt '{api_base}' ist nicht freigegeben (LLM_ALLOWED_API_BASES).",
            api_base=api_base, model=model,
        )
        return results

    # Der serverseitige Schlüssel wird ausschließlich an den serverseitig konfigurierten Endpunkt gesendet.
    client_key = str(cfg.get("api_key") or "").strip()
    if client_key:
        api_key = client_key
    elif _normalize_base(api_base) == _normalize_base(server_base):
        api_key = env_key
    else:
        api_key = ""

    MODEL_ALIASES = {
        "qwen-3.8-27b": "qwen3.8-27b",
        "qwen-3-8-27b": "qwen3.8-27b",
        "qwen 3.8 27b": "qwen3.8-27b",
        "qwen3.8": "qwen3.8-27b",
        "qwen-3-30b-a3b-instruct": "qwen3-30b-a3b-instruct-2507",
        "qwen-3-30b": "qwen3-30b-a3b-instruct-2507",
        "qwen3-30b": "qwen3-30b-a3b-instruct-2507",
        "deepseek-v4-flash": "deepseek-v4-flash-0731",
        "deepseek-v4": "deepseek-v4-flash-0731",
        "glm5.3": "glm-5.3-flash",
    }
    model = MODEL_ALIASES.get(model.lower(), model)

    timeout_sec = int(cfg.get("timeout") or os.environ.get("LLM_TIMEOUT") or (120 if "qwen3.8" in model else 45))
    is_local = _normalize_base(api_base).startswith(("http://localhost", "http://127.0.0.1"))
    allow_no_key = str(os.environ.get("LLM_ALLOW_NO_KEY", "0")).strip() == "1" or is_local

    if not api_key and not allow_no_key:
        _mark_semantic_unverified(
            results,
            registry,
            status="unconfigured",
            reason="Keine LLM-API angebunden (kein API-Key in LLM_API_KEY / GWDG_API_KEY oder Dashboard-Konfiguration hinterlegt).",
            api_base=api_base,
            model=model,
        )
        return results

    payload = extract_compact_payload_from_data(form_data, workflow_emails, filename, registry)
    prompt = build_prompt_from_payload(payload, registry)
    prov = results.setdefault("provenance", {})
    prov["prompt_sha256"] = hashlib.sha256((SYSTEM_MESSAGE + "\n" + prompt).encode("utf-8")).hexdigest()
    prov["model"] = model
    prov["api_base"] = api_base

    endpoint = api_base if api_base.endswith("/chat/completions") else f"{api_base}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"HWG-Formcycle-Compliance-Checker/{ENGINE_VERSION}",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Ersatzmodell bei Ausfall (Zeitüberschreitung, Rate-Limit, Serverfehler); im Benchmark abgeschaltet
    fallback_model = None
    if not cfg.get("disable_fallback"):
        fb = str(cfg.get("fallback_model") or os.environ.get("LLM_FALLBACK_MODEL") or "").strip()
        if fb:
            fallback_model = MODEL_ALIASES.get(fb.lower(), fb)
    candidates = [model] + ([fallback_model] if fallback_model and fallback_model != model else [])

    last_reason = ""
    tried = []
    for idx, current_model in enumerate(candidates):
        tried.append(current_model)
        req_body = json.dumps({
            "model": current_model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(endpoint, data=req_body, headers=headers, method="POST")
        transport_error = False
        try:
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                resp_raw = resp.read().decode("utf-8", errors="replace")
            prov["llm_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            resp_json = json.loads(resp_raw)
            content = (
                ((resp_json.get("choices") or [{}])[0].get("message") or {}).get("content")
                or ""
            )
            prov["llm_raw_response"] = content
            if not content:
                raise ValueError("Leere Modellantwort im Feld choices[0].message.content.")
            llm_obj = _extract_json_from_llm_text(content)
            if idx > 0:
                prov["model"] = current_model
                prov["fallback_used"] = True
                prov["primary_model_error"] = last_reason
            return merge_llm_evaluation_payload(results, llm_obj, registry, model_name=current_model, api_base=api_base)
        except urllib.error.HTTPError as he:
            last_reason = f"LLM-API HTTP-Fehler {he.code} bei '{api_base}'."
            transport_error = he.code in (429, 500, 502, 503, 504)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            last_reason = f"LLM-API nicht erreichbar oder Antwort ungültig ({type(exc).__name__})."
            transport_error = True
        except Exception as exc:
            last_reason = f"LLM-API nicht erreichbar oder Antwort ungültig ({type(exc).__name__})."
        if not transport_error:
            break

    reason = last_reason
    if len(tried) > 1:
        reason += f" Ersatzmodell '{tried[-1]}' ebenfalls ohne gültige Antwort."
    _mark_semantic_unverified(
        results,
        registry,
        status="error",
        reason=reason,
        api_base=api_base,
        model=model,
    )
    return results


def _finalize_rule_ids(results: dict, registry: dict = None) -> dict:
    rule_lookup = {r["id"]: r for r in (registry or {}).get("rules", [])}
    for v in results["violations"]:
        r_id = v.get("rule") or v.get("rule_id")
        v["rule"] = r_id
        v["rule_id"] = r_id
        if r_id in rule_lookup and "remediation" not in v:
            v["remediation"] = rule_lookup[r_id].get("remediation", "")
    for r in results["recommendations"]:
        r_id = r.get("rule") or r.get("rule_id")
        r["rule"] = r_id
        r["rule_id"] = r_id
        if r_id in rule_lookup and "remediation" not in r:
            r["remediation"] = rule_lookup[r_id].get("remediation", "")
    results["score"] = max(0, results["score"])
    crit_count = sum(1 for v in results["violations"] if v.get("severity") == "critical")
    results["critical_count"] = crit_count
    unverified = list(results.get("unverified_semantic") or [])
    if crit_count >= 1 or results["score"] < 60:
        results["release_status"] = "red_blocked"
        results["release_label"] = (
            "Kritisch unzureichend (Gesperrt — Critical-Veto)"
            if (crit_count >= 1 and results["score"] >= 60)
            else "Kritisch unzureichend (Gesperrt)"
        )
        results["is_releasable"] = False
    elif unverified:
        # Fail-closed: ohne vollständige semantische Prüfung keine Freigabe
        results["release_status"] = "pending_ai_review"
        results["release_label"] = f"Vorläufig – {len(unverified)} Regel(n) ohne KI-Prüfung (nicht freigabefähig)"
        results["is_releasable"] = False
    elif results["score"] < 80:
        results["release_status"] = "amber_needs_review"
        results["release_label"] = "Überarbeitungsbedürftig"
        results["is_releasable"] = False
    else:
        results["release_status"] = "green_compliant"
        results["release_label"] = "Konform (Freigabefähig)"
        results["is_releasable"] = True
    return results


def evaluate_form_bytes(file_bytes: bytes, filename: str, registry: dict, llm_config: dict = None) -> dict:
    """
    Reine In-Memory-Auswertung eines .xfp-Archivs oder Formular-JSONs (keine temporären Dateien).
    """
    form_data, workflow_emails, workflow_states = _parse_archive_bytes(file_bytes, filename)
    t0 = time.perf_counter()
    results = evaluate_stage1_ast(form_data, workflow_emails, workflow_states, filename, registry)
    stage1_ms = round((time.perf_counter() - t0) * 1000, 3)
    results["provenance"] = {
        **results.get("provenance", {}),
        "engine_version": ENGINE_VERSION,
        "registry_version": registry.get("version"),
        "prompt_version": PROMPT_VERSION,
        "input_sha256": hashlib.sha256(file_bytes).hexdigest(),
        "stage1_ms": stage1_ms,
    }
    results = evaluate_stage2_llm(results, form_data, workflow_emails, filename, registry, llm_config=llm_config)
    return _finalize_rule_ids(results, registry)


def evaluate_form(xfp_path: str, registry: dict, llm_config: dict = None) -> dict:
    with open(xfp_path, "rb") as f:
        raw_bytes = f.read()
    return evaluate_form_bytes(raw_bytes, os.path.basename(xfp_path), registry, llm_config=llm_config)


if __name__ == "__main__":
    reg = load_rules()
    test_forms = [
        "data/test_forms/synthetic_compliant_form.xfp",
        "data/test_forms/synthetic_noncompliant_form.xfp",
        "data/raw_forms/prod/Auslagenerstattung _20260929.xfp",
        "data/raw_forms/test/fachformulare/AFörderung Antrag-Sprints für Drittmittelanträge.xfp",
        "data/raw_forms/standard/AFörderung Antrag-Sprints für Drittmittelanträge (HWG-Standard).xfp",
        "data/raw_forms/test/adminformulare/Master-Formular.xfp",
    ]

    print(f"=== HWG COMPLIANCE PIPELINE EVALUATION (Registry v{reg['version']}) ===")
    print(f"Gesamtanzahl Governance-Regeln: {len(reg['rules'])} (15 deterministisch [Stufe 1] + 7 semantisch/hybrid [Stufe 2])")

    for tf in test_forms:
        if not os.path.exists(tf) and tf.endswith(".xfp"):
            tf = tf[:-4] + ".json"
        res = evaluate_form(tf, reg)
        llm_info = res.get("llm_evaluation", {})
        print(f"\n{'=' * 22} {res['form']} {'=' * 22}")
        if llm_info.get("evaluated"):
            print(f"[KI-Stufe 2 AKTIV] Modell: {llm_info.get('model')} ({llm_info.get('api_base')})")
        else:
            print(f"[!] WARNUNG – KI-STUFENPRÜFUNG INAKTIV ({llm_info.get('status', 'unavailable').upper()}):")
            print(f"    {llm_info.get('reason')}")
            print(f"    -> {len(res.get('unverified_semantic', []))} semantische Regeln wurden NICHT durch eine KI geprüft!")
        print(f"Compliance-Score:     {res['score']} / 100 Punkte")
        print(f"Freigabestatus:       {res['release_label']} ({res['release_status']})")
        print(f"Bestanden:            {len(res['passed'])}")
        print(f"Verstöße:             {len(res['violations'])}")
        print(f"Empfehlungen (0 P.):  {len(res['recommendations'])}")
        print(f"Nicht zutreffend:     {len(res['not_applicable'])} (davon {len(res.get('unverified_semantic', []))} KI-ungeprüft)")
        for v in res["violations"]:
            print(f"  [X] [{v['severity'].upper():8}] {v['rule']}: {v['message']}")
        for r in res["recommendations"]:
            print(f"  [i] [RECOMM. ] {r['rule']}: {r['message']}")
        for na_k, na_v in res["not_applicable"].items():
            tag = "KEINE KI" if na_k in res.get("unverified_semantic", []) else "N/A     "
            print(f"  [-] [{tag}] {na_k}: {na_v}")
