import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOGO_PATH = os.path.join("web", "assets", "logo_hsl_claim.svg")
# Lexend (SIL Open Font License 1.1) wird eingebettet, damit das Dashboard keine externen Server kontaktiert.
FONT_PATH = os.path.join("web", "assets", "fonts", "lexend-latin-wght-normal.woff2")


def _data_uri(path: str, mime: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, "rb") as f:
        return f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"


def _json_for_script(obj) -> str:
    # "</" würde ein <script>-Element vorzeitig schließen
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


PRESETS = [
    ("synth_comp", "Synthetisch, konform", "Testformular", "data/test_forms/synthetic_compliant_form.xfp"),
    ("synth_noncomp", "Synthetisch, fehlerhaft", "Testformular", "data/test_forms/synthetic_noncompliant_form.xfp"),
    ("auslagen", "Auslagenerstattung", "Produktivsystem", "data/raw_forms/prod/Auslagenerstattung _20260929.xfp"),
    ("sprints", "Antrag-Sprints", "Altbestand, Testsystem",
     "data/raw_forms/test/fachformulare/AFörderung Antrag-Sprints für Drittmittelanträge.xfp"),
    ("sprints_standard", "Antrag-Sprints, saniert", "HWG-Standard",
     "data/raw_forms/standard/AFörderung Antrag-Sprints für Drittmittelanträge (HWG-Standard).xfp"),
    ("master", "Master-Formular", "Kopiervorlage", "data/raw_forms/test/adminformulare/Master-Formular.xfp"),
]


def generate_dashboard():
    with open("data/rules/rules_registry.json", "r", encoding="utf-8") as f:
        registry = json.load(f)

    # Für den Initialzustand der Presets wird Stufe 1 (deterministische Strukturprüfung) ohne API-Key
    # ausgewertet, damit das Transparenzbanner den Offline-Zustand ehrlich abbildet.
    os.environ["LLM_API_KEY"] = ""
    os.environ["GWDG_API_KEY"] = ""
    os.environ["OPENAI_API_KEY"] = ""

    from rule_engine import evaluate_form

    offline_cfg = {"api_key": "", "api_base": "https://chat-ai.academiccloud.de/v1"}
    presets = {
        key: {"label": label, "kind": kind, "file": path, "eval": evaluate_form(path, registry, llm_config=offline_cfg)}
        for key, label, kind, path in PRESETS
    }

    html_content = (
        TEMPLATE.replace("__REGISTRY_VERSION__", str(registry.get("version", "")))
        .replace("__LOGO_DATA_URI__", _data_uri(LOGO_PATH, "image/svg+xml"))
        .replace("__FONT_DATA_URI__", _data_uri(FONT_PATH, "font/woff2"))
        .replace("__REGISTRY_JSON__", _json_for_script(registry))
        .replace("__PRESETS_JSON__", _json_for_script(presets))
    )
    with open("web/compliance_dashboard.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("Dashboard erfolgreich erstellt: web/compliance_dashboard.html")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Formcycle-Prüfzentrum – HWG Ludwigshafen</title>
    <!-- Die Prüfung erfolgt ausschließlich serverseitig (scripts/rule_engine.py); keine Regel-Duplikate im Browser.
         Alle Ressourcen sind eingebettet; die Seite lädt nichts von externen Servern. -->
    <style>
        @font-face {
            font-family: 'Lexend';
            font-style: normal;
            font-weight: 100 900;
            font-display: swap;
            src: url(__FONT_DATA_URI__) format('woff2');
        }

        :root {
            --red: #c30d20;
            --red-dark: #a10919;
            --red-soft: #fdf0f1;
            --ink: #18181b;
            --ink-2: #3f3f46;
            --muted: #57534e;
            --bg: #f6f5f2;
            --surface: #ffffff;
            --line: #e4e2dd;
            --line-strong: #cfcbc3;
            --major: #c2410c;
            --minor: #854d0e;
            --minor-line: #ca8a04;
            --rec: #1d4ed8;
            --green: #15803d;
            --green-soft: #effaf2;
            --amber: #b45309;
            --amber-soft: #fff7e8;
            --focus: #1d4ed8;
            --radius: 12px;
            --shadow: 0 1px 2px rgba(24, 24, 27, 0.05), 0 6px 16px -8px rgba(24, 24, 27, 0.08);
            --font: 'Lexend', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
            --mono: ui-monospace, 'Cascadia Mono', Consolas, 'Liberation Mono', monospace;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            font-family: var(--font);
            font-size: 15px;
            line-height: 1.55;
            color: var(--ink);
            background: var(--bg);
            -webkit-font-smoothing: antialiased;
        }

        :focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }

        .skip-link {
            position: absolute; left: -9999px; top: 8px; z-index: 100;
            background: var(--ink); color: #fff; padding: 8px 14px; border-radius: 8px; text-decoration: none;
        }
        .skip-link:focus { left: 8px; }

        .visually-hidden {
            position: absolute !important; width: 1px; height: 1px; padding: 0; margin: -1px;
            overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
        }

        .wrap { max-width: 1120px; margin: 0 auto; padding: 0 24px; }

        /* Kopfbereich */
        header.site { background: var(--surface); border-top: 4px solid var(--red); border-bottom: 1px solid var(--line); }
        header.site .wrap {
            display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap;
            padding-top: 16px; padding-bottom: 16px;
        }
        .brand { display: flex; align-items: center; gap: 20px; }
        .brand img { height: 46px; width: auto; display: block; }
        .brand-text { border-left: 1px solid var(--line); padding-left: 20px; }
        .brand h1 { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; }
        .brand p { margin: 2px 0 0; font-size: 13px; color: var(--muted); }
        .actions { display: flex; gap: 8px; flex-wrap: wrap; }

        .btn {
            font: inherit; font-size: 14px; font-weight: 500; line-height: 1.2;
            display: inline-flex; align-items: center; gap: 6px;
            padding: 9px 16px; border-radius: 8px; cursor: pointer;
            border: 1px solid var(--line-strong); background: var(--surface); color: var(--ink);
        }
        .btn:hover { border-color: var(--ink-2); }
        .btn-primary { background: var(--red); border-color: var(--red); color: #fff; }
        .btn-primary:hover { background: var(--red-dark); border-color: var(--red-dark); }
        .btn:disabled { opacity: 0.6; cursor: progress; }

        main { padding: 28px 0 40px; }

        .panel {
            background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
            box-shadow: var(--shadow); padding: 24px; margin-bottom: 20px;
        }
        .panel h2 { margin: 0; font-size: 17px; font-weight: 600; }
        .section-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
        .hint { margin: 0; font-size: 13px; color: var(--muted); }

        /* Auswahl */
        .presets { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
        .preset {
            font: inherit; text-align: left; cursor: pointer;
            display: flex; flex-direction: column; align-items: flex-start; gap: 8px;
            padding: 12px 14px; border-radius: 10px; border: 1px solid var(--line); background: var(--surface); color: var(--ink);
        }
        .preset:hover { border-color: var(--line-strong); background: #fcfbfa; }
        .preset[aria-pressed="true"] { border-color: var(--red); box-shadow: inset 0 0 0 1px var(--red); background: var(--red-soft); }
        .preset-name { display: block; font-size: 14px; font-weight: 500; }
        .preset-kind { display: block; font-size: 12px; color: var(--muted); }

        .pill { font-size: 12px; font-weight: 600; padding: 2px 9px; border-radius: 999px; white-space: nowrap; }
        .tone-red { background: var(--red-soft); color: var(--red); }
        .tone-amber { background: var(--amber-soft); color: var(--amber); }
        .tone-green { background: var(--green-soft); color: var(--green); }

        .dropzone {
            margin-top: 16px; padding: 16px; border: 1.5px dashed var(--line-strong); border-radius: 10px;
            display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;
        }
        .dropzone.is-drag { border-color: var(--red); background: var(--red-soft); }
        .dropzone strong { font-weight: 500; }

        details.config { margin-top: 14px; }
        details.config summary { cursor: pointer; font-size: 14px; color: var(--ink-2); }
        .config-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; align-items: end; margin-top: 12px; }
        .field label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 4px; }
        .field input, .field select {
            width: 100%; font: inherit; font-size: 14px; padding: 8px 10px;
            border: 1px solid var(--line-strong); border-radius: 8px; background: var(--surface); color: var(--ink);
        }
        details.config .hint { margin-top: 8px; }

        .message { margin: 12px 0 0; font-size: 14px; }
        .message:empty { display: none; }
        .message.is-error { color: var(--red); }

        /* Ergebnis */
        #result:focus { outline: none; }
        .result-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
        .form-file { font-family: var(--mono); font-size: 12.5px; color: var(--muted); word-break: break-all; }

        .summary { display: grid; grid-template-columns: auto 1fr; gap: 28px; align-items: center; }
        .gauge { position: relative; width: 136px; height: 136px; }
        .gauge svg { display: block; transform: rotate(-90deg); }
        .gauge-track { fill: none; stroke: var(--line); stroke-width: 12; }
        .gauge-value { fill: none; stroke-width: 12; stroke-linecap: round; transition: stroke-dashoffset 0.6s ease; }
        .gauge-label { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        .gauge-label strong { font-size: 36px; font-weight: 600; line-height: 1; }
        .gauge-label span { font-size: 12px; color: var(--muted); margin-top: 4px; }

        .verdict { display: inline-block; font-size: 15px; font-weight: 600; padding: 5px 14px; border-radius: 999px; }
        .verdict-reason { margin: 8px 0 16px; font-size: 14px; color: var(--ink-2); }

        .tiles { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
        .tile { border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; }
        .tile-value { display: block; font-size: 26px; font-weight: 600; line-height: 1.15; }
        .tile-label { font-size: 13px; color: var(--muted); }
        .tile-critical .tile-value { color: var(--red); }
        .tile-major .tile-value { color: var(--major); }
        .tile-minor .tile-value { color: var(--minor); }
        .tile-passed .tile-value { color: var(--green); }

        .ai-banner { margin-top: 20px; padding: 12px 14px; border-radius: 10px; font-size: 14px; }
        .ai-banner p { margin: 0; }
        .ai-banner details { margin-top: 6px; font-size: 13px; }
        .ai-banner summary { cursor: pointer; }
        .ai-ok { background: var(--green-soft); color: #14532d; }
        .ai-warn { background: var(--amber-soft); color: #78350f; }

        details.legend { margin-top: 14px; font-size: 13px; color: var(--ink-2); }
        details.legend summary { cursor: pointer; }
        details.legend ul { margin: 8px 0 0; padding-left: 18px; }

        /* Regeln */
        .toolbar { display: flex; gap: 6px; flex-wrap: wrap; }
        .chip {
            font: inherit; font-size: 13px; cursor: pointer; padding: 5px 12px; border-radius: 999px;
            border: 1px solid var(--line-strong); background: var(--surface); color: var(--ink-2);
        }
        .chip[aria-pressed="true"] { background: var(--ink); border-color: var(--ink); color: #fff; }
        .chip .count { font-weight: 600; margin-left: 4px; }

        .rules { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
        .rule {
            border: 1px solid var(--line); border-left: 4px solid var(--line-strong);
            border-radius: 10px; padding: 14px 16px; background: var(--surface);
        }
        .rule-critical { border-left-color: var(--red); }
        .rule-major { border-left-color: var(--major); }
        .rule-minor { border-left-color: var(--minor-line); }
        .rule-rec { border-left-color: var(--rec); }
        .rule-passed { border-left-color: var(--green); }
        .rule-unverified { border-left-color: var(--amber); }
        .rule-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
        .rule h3 { margin: 0; font-size: 15px; font-weight: 500; }
        .rule-id { font-family: var(--mono); font-size: 12.5px; font-weight: 400; color: var(--muted); margin-right: 8px; }
        .rule-tags { display: flex; align-items: center; gap: 8px; }
        .rule-stage { font-size: 12px; color: var(--muted); }
        .rule p { margin: 8px 0 0; font-size: 14px; }
        .rule p.rule-note { margin-top: 4px; font-size: 13px; color: var(--muted); }
        .rule details { margin-top: 8px; font-size: 14px; }
        .rule summary { cursor: pointer; font-size: 13.5px; font-weight: 500; color: var(--red); }
        .rule details p { color: var(--ink-2); }
        .rule .basis { font-size: 13px; color: var(--muted); }
        .badge-critical { background: var(--red-soft); color: var(--red); }
        .badge-major { background: #fff1e8; color: var(--major); }
        .badge-minor { background: #fdf7e3; color: var(--minor); }
        .badge-rec { background: #eef3ff; color: var(--rec); }
        .badge-passed { background: var(--green-soft); color: var(--green); }
        .badge-unverified { background: var(--amber-soft); color: var(--amber); }
        .badge-na { background: #f1f0ec; color: var(--muted); }
        .empty { margin: 0; font-size: 14px; color: var(--muted); }

        footer { padding: 0 0 32px; font-size: 12.5px; color: var(--muted); }

        @media (max-width: 760px) {
            .summary { grid-template-columns: 1fr; justify-items: start; }
            .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .brand-text { border-left: 0; padding-left: 0; }
        }
        @media (prefers-reduced-motion: reduce) {
            * { transition: none !important; }
        }
        @media print {
            body { background: #fff; }
            .no-print, header.site .actions, .toolbar, details.legend { display: none !important; }
            .panel { box-shadow: none; border-color: #ccc; }
            .rule { break-inside: avoid; }
        }
    </style>
</head>
<body>
    <a class="skip-link" href="#result">Zum Prüfergebnis springen</a>

    <header class="site">
        <div class="wrap">
            <div class="brand">
                <img src="__LOGO_DATA_URI__" alt="Hochschule für Wirtschaft und Gesellschaft Ludwigshafen">
                <div class="brand-text">
                    <h1>Formcycle-Prüfzentrum</h1>
                    <p>Prüfung gegen den HWG-Formular-Standard · Registry v__REGISTRY_VERSION__</p>
                </div>
            </div>
            <div class="actions">
                <button type="button" class="btn" id="btnPrint">Prüfbericht als PDF</button>
                <button type="button" class="btn btn-primary" id="btnJson">JSON exportieren</button>
            </div>
        </div>
    </header>

    <main>
        <div class="wrap">
            <section class="panel no-print" aria-labelledby="select-heading">
                <div class="section-head">
                    <h2 id="select-heading">Formular wählen</h2>
                    <p class="hint">22 Regeln: 15 per Strukturprüfung, 7 KI-gestützt</p>
                </div>
                <div class="presets" id="presets" role="group" aria-label="Referenzformulare"></div>

                <div class="dropzone" id="dropzone">
                    <div>
                        <strong>Eigenes Formular prüfen</strong>
                        <p class="hint">Formcycle-Export (.xfp) hierher ziehen oder auswählen. Die Datei wird nur im Arbeitsspeicher des Prüf-Servers verarbeitet.</p>
                    </div>
                    <label class="btn" for="fileInput">Datei auswählen</label>
                    <input type="file" id="fileInput" accept=".xfp" class="visually-hidden">
                </div>

                <details class="config">
                    <summary>KI-Anbindung für Stufe 2 (optional)</summary>
                    <div class="config-grid">
                        <div class="field">
                            <label for="llmApiBase">Endpunkt</label>
                            <select id="llmApiBase">
                                <option value="https://chat-ai.academiccloud.de/v1">GWDG Academic Cloud</option>
                            </select>
                        </div>
                        <div class="field">
                            <label for="llmModel">Modell</label>
                            <select id="llmModel">
                                <option value="deepseek-v4-flash-0731">DeepSeek V4 Flash 0731</option>
                            </select>
                        </div>
                        <div class="field" id="llmKeyField">
                            <label for="llmApiKey">API-Schlüssel</label>
                            <input type="password" id="llmApiKey" autocomplete="off">
                        </div>
                        <button type="button" class="btn btn-primary" id="btnLlm">Mit KI prüfen</button>
                    </div>
                    <p class="hint">Zur Auswahl stehen nur die auf dem Server freigegebenen Endpunkte. Einen Schlüssel fragt das Dashboard nur ab, wenn der Server für den Endpunkt keinen eigenen hat; er bleibt in diesem Browser-Tab.</p>
                </details>

                <p class="message" id="message" role="status" aria-live="polite"></p>
            </section>

            <section class="panel" id="result" tabindex="-1" aria-labelledby="result-heading">
                <div class="result-head">
                    <h2 id="result-heading">Prüfergebnis</h2>
                    <span class="form-file" id="formFile"></span>
                </div>
                <div class="summary">
                    <div class="gauge">
                        <svg width="136" height="136" viewBox="0 0 136 136" aria-hidden="true" focusable="false">
                            <circle class="gauge-track" cx="68" cy="68" r="58"></circle>
                            <circle class="gauge-value" id="gaugeValue" cx="68" cy="68" r="58"></circle>
                        </svg>
                        <div class="gauge-label"><strong id="score">–</strong><span>von 100</span></div>
                    </div>
                    <div>
                        <span class="verdict" id="verdict"></span>
                        <p class="verdict-reason" id="verdictReason"></p>
                        <ul class="tiles" aria-label="Ergebnis nach Schweregrad">
                            <li class="tile tile-critical"><span class="tile-value" id="nCritical">0</span><span class="tile-label">Critical</span></li>
                            <li class="tile tile-major"><span class="tile-value" id="nMajor">0</span><span class="tile-label">Major</span></li>
                            <li class="tile tile-minor"><span class="tile-value" id="nMinor">0</span><span class="tile-label">Minor</span></li>
                            <li class="tile tile-passed"><span class="tile-value" id="nPassed">0</span><span class="tile-label" id="passedLabel">bestanden</span></li>
                        </ul>
                    </div>
                </div>
                <div class="ai-banner" id="aiBanner"></div>
                <details class="legend">
                    <summary>So wird bewertet</summary>
                    <ul>
                        <li>Abzug je verletzter Regel: Critical 15, Major 8, Minor 3 Punkte; Empfehlungen 0 Punkte.</li>
                        <li><strong>Freigabefähig:</strong> ab 80 Punkten, ohne Critical-Verstoß und mit vollständiger Prüfung.</li>
                        <li><strong>Überarbeitung nötig:</strong> 60 bis 79 Punkte ohne Critical-Verstoß.</li>
                        <li><strong>Gesperrt:</strong> unter 60 Punkten oder mindestens ein Critical-Verstoß.</li>
                        <li><strong>Vorläufig:</strong> Die KI-Prüfung ist unvollständig; keine Freigabe.</li>
                    </ul>
                </details>
            </section>

            <section class="panel" aria-labelledby="rules-heading">
                <div class="section-head">
                    <h2 id="rules-heading">Regeln</h2>
                    <div class="toolbar" id="toolbar" role="group" aria-label="Regeln filtern"></div>
                </div>
                <ul class="rules" id="rules"></ul>
            </section>
        </div>
    </main>

    <footer>
        <div class="wrap">Prototyp aus einer Bachelorarbeit an der HWG Ludwigshafen. Ein grünes Ergebnis ist eine Empfehlung für die Freigabe, die ein Mensch erteilt.</div>
    </footer>

    <script>
        const registry = __REGISTRY_JSON__;
        const presets = __PRESETS_JSON__;
        const POINTS = { critical: 15, major: 8, minor: 3 };
        const SEVERITY_LABEL = { critical: 'Critical', major: 'Major', minor: 'Minor' };
        const STAGE_LABEL = { deterministic: 'Strukturprüfung', hybrid: 'Struktur + KI', semantic: 'KI-Prüfung' };
        const STATUS = {
            red_blocked: { label: 'Gesperrt', tone: 'tone-red', color: 'var(--red)' },
            pending_ai_review: { label: 'Vorläufig', tone: 'tone-amber', color: 'var(--amber)' },
            amber_needs_review: { label: 'Überarbeitung nötig', tone: 'tone-amber', color: 'var(--amber)' },
            green_compliant: { label: 'Freigabefähig', tone: 'tone-green', color: 'var(--green)' },
        };
        const FILTERS = [
            { key: 'issues', label: 'Handlungsbedarf' },
            { key: 'unverified', label: 'KI-ungeprüft' },
            { key: 'passed', label: 'Bestanden' },
            { key: 'na', label: 'Nicht zutreffend' },
            { key: 'all', label: 'Alle' },
        ];
        const GAUGE_LENGTH = 2 * Math.PI * 58;

        let activePresetKey = new URLSearchParams(location.search).get('preset');
        if (!presets[activePresetKey]) activePresetKey = 'auslagen';
        let currentEval = presets[activePresetKey].eval;
        let currentFilter = 'issues';

        const $ = (id) => document.getElementById(id);

        function escapeHtml(str) {
            return String(str ?? '')
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
        }

        function setMessage(text, isError = false) {
            const el = $('message');
            el.textContent = text || '';
            el.classList.toggle('is-error', !!isError);
        }

        function llmHeaders() {
            const headers = {};
            const key = $('llmApiKey').value.trim();
            const base = $('llmApiBase').value.trim().replace(/\/+$/, '');
            const model = $('llmModel').value.trim();
            if (key) headers['X-LLM-Api-Key'] = key;
            if (base) headers['X-LLM-Api-Base'] = base;
            if (model) headers['X-LLM-Model'] = model;
            return headers;
        }

        // Endpunkte und Modelle liefert der Prüf-Server; ohne Server bleiben die Vorgaben stehen
        let llmEndpoints = [];

        function fillModels() {
            const ep = llmEndpoints.find(e => e.api_base === $('llmApiBase').value);
            if (!ep) return;
            $('llmModel').innerHTML = ep.models
                .map(m => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`).join('');
            $('llmModel').value = ep.default_model;
            $('llmKeyField').hidden = !ep.needs_key;
            if (!ep.needs_key) $('llmApiKey').value = '';
        }

        async function loadLlmConfig() {
            try {
                const resp = await fetch('/api/config');
                if (!resp.ok) return;
                const cfg = await resp.json();
                if (!Array.isArray(cfg.endpoints) || !cfg.endpoints.length) return;
                llmEndpoints = cfg.endpoints;
                $('llmApiBase').innerHTML = llmEndpoints
                    .map(e => `<option value="${escapeHtml(e.api_base)}">${escapeHtml(e.label)}</option>`).join('');
                const preferred = llmEndpoints.find(e => e.api_base === cfg.default_endpoint) || llmEndpoints[0];
                $('llmApiBase').value = preferred.api_base;
                fillModels();
            } catch (err) {
                // Dashboard ohne Prüf-Server geöffnet: Auswahl bleibt bei den Vorgaben
            }
        }

        function statusOf(e) {
            return STATUS[e.release_status] || STATUS.pending_ai_review;
        }

        // Ordnet jede Regel genau einer Kategorie zu (Darstellung, keine Bewertungslogik)
        function classify(e) {
            const violations = Object.fromEntries((e.violations || []).map(v => [v.rule, v]));
            const recs = Object.fromEntries((e.recommendations || []).map(r => [r.rule, r]));
            const passed = new Set((e.passed || []).map(p => p.split(' ')[0]));
            const unverified = new Set(e.unverified_semantic || []);
            return registry.rules.map(rule => {
                if (violations[rule.id]) return { rule, kind: 'violation', severity: violations[rule.id].severity, message: violations[rule.id].message };
                if (recs[rule.id]) return { rule, kind: 'rec', message: recs[rule.id].message };
                if (passed.has(rule.id)) return { rule, kind: 'passed' };
                const reason = (e.not_applicable || {})[rule.id];
                if (unverified.has(rule.id)) return { rule, kind: 'unverified', message: reason || 'Nicht von einem Sprachmodell bewertet.' };
                return { rule, kind: 'na', message: reason || 'Auf dieses Formular nicht anwendbar.' };
            });
        }

        function renderPresets() {
            const grid = $('presets');
            grid.innerHTML = '';
            for (const [key, p] of Object.entries(presets)) {
                const st = statusOf(p.eval);
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'preset';
                btn.setAttribute('aria-pressed', String(key === activePresetKey));
                btn.innerHTML = `
                    <span><span class="preset-name">${escapeHtml(p.label)}</span><span class="preset-kind">${escapeHtml(p.kind)}</span></span>
                    <span class="pill ${st.tone}">${p.eval.score} · ${escapeHtml(st.label)}</span>`;
                btn.addEventListener('click', () => selectPreset(key));
                grid.appendChild(btn);
            }
        }

        function selectPreset(key) {
            activePresetKey = key;
            currentEval = presets[key].eval;
            currentFilter = 'issues';
            setMessage('');
            renderPresets();
            renderEvaluation();
        }

        function renderEvaluation() {
            const e = currentEval;
            const st = statusOf(e);
            const items = classify(e);
            const count = (pred) => items.filter(pred).length;
            const nCritical = count(i => i.kind === 'violation' && i.severity === 'critical');
            const nUnverified = count(i => i.kind === 'unverified');

            $('formFile').textContent = e.form || '';
            $('score').textContent = e.score;
            const gauge = $('gaugeValue');
            gauge.style.strokeDasharray = GAUGE_LENGTH;
            gauge.style.strokeDashoffset = GAUGE_LENGTH * (1 - Math.max(0, Math.min(100, e.score)) / 100);
            gauge.style.stroke = st.color;

            const verdict = $('verdict');
            verdict.textContent = st.label;
            verdict.className = `verdict ${st.tone}`;
            let reason;
            if (e.release_status === 'red_blocked') {
                const parts = [];
                if (nCritical) parts.push(`${nCritical} Critical-Verstoß${nCritical > 1 ? 'e' : ''}`);
                if (e.score < 60) parts.push('weniger als 60 Punkte');
                reason = `Freigabe gesperrt: ${parts.join(' und ')}.`;
            } else if (e.release_status === 'pending_ai_review') {
                reason = `${nUnverified} Regel${nUnverified === 1 ? '' : 'n'} noch ohne KI-Prüfung; das Ergebnis ist nicht freigabefähig.`;
            } else if (e.release_status === 'amber_needs_review') {
                reason = 'Kein Critical-Verstoß, aber weniger als 80 Punkte: vor der Veröffentlichung überarbeiten.';
            } else {
                reason = 'Alle Regeln geprüft, kein Critical-Verstoß, mindestens 80 Punkte.';
            }
            $('verdictReason').textContent = reason;

            $('nCritical').textContent = nCritical;
            $('nMajor').textContent = count(i => i.kind === 'violation' && i.severity === 'major');
            $('nMinor').textContent = count(i => i.kind === 'violation' && i.severity === 'minor');
            $('nPassed').textContent = count(i => i.kind === 'passed');
            $('passedLabel').textContent = `von ${registry.rules.length - count(i => i.kind === 'na' || i.kind === 'unverified')} geprüften bestanden`;

            const llm = e.llm_evaluation || {};
            const banner = $('aiBanner');
            if (llm.evaluated && nUnverified === 0) {
                banner.className = 'ai-banner ai-ok';
                banner.innerHTML = `<p><strong>Vollständig geprüft:</strong> Strukturprüfung und KI-Prüfung (Modell <code>${escapeHtml(llm.model || 'unbekannt')}</code>).</p>`;
            } else {
                const unverifiedIds = items.filter(i => i.kind === 'unverified').map(i => i.rule.id);
                banner.className = 'ai-banner ai-warn';
                banner.innerHTML = `
                    <p><strong>KI-Prüfung unvollständig:</strong> ${unverifiedIds.length} Regel${unverifiedIds.length === 1 ? '' : 'n'} wurden nicht von einem Sprachmodell bewertet. Solange Regeln offen sind, ist das Formular nicht freigabefähig.</p>
                    <details><summary>Details</summary><p>${escapeHtml(unverifiedIds.join(', '))}${llm.reason ? ' · ' + escapeHtml(llm.reason) : ''}</p></details>`;
            }

            // Ohne Handlungsbedarf zuerst die offenen KI-Prüfungen zeigen, sonst alle Regeln
            if (currentFilter === 'issues' && !items.some(i => matchesFilter(i, 'issues'))) {
                currentFilter = items.some(i => i.kind === 'unverified') ? 'unverified' : 'all';
            }
            renderToolbar(items);
            renderRules(items);
        }

        function matchesFilter(item, filter) {
            if (filter === 'all') return true;
            if (filter === 'issues') return item.kind === 'violation' || item.kind === 'rec';
            return item.kind === filter;
        }

        function renderToolbar(items) {
            const bar = $('toolbar');
            bar.innerHTML = '';
            for (const f of FILTERS) {
                const n = items.filter(i => matchesFilter(i, f.key)).length;
                if (!n && !['issues', 'all'].includes(f.key)) continue;
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'chip';
                chip.setAttribute('aria-pressed', String(f.key === currentFilter));
                chip.innerHTML = `${escapeHtml(f.label)}<span class="count">${n}</span>`;
                chip.addEventListener('click', () => { currentFilter = f.key; renderToolbar(items); renderRules(items); });
                bar.appendChild(chip);
            }
        }

        const ORDER = { critical: 0, major: 1, minor: 2 };
        function rank(item) {
            if (item.kind === 'violation') return ORDER[item.severity] ?? 2;
            return { rec: 3, unverified: 4, passed: 5, na: 6 }[item.kind];
        }

        function ruleCard(item) {
            const r = item.rule;
            const stage = STAGE_LABEL[r.evaluation_type] || '';
            let cls = 'rule', badge = '', body = '';
            if (item.kind === 'violation') {
                cls += ` rule-${item.severity}`;
                badge = `<span class="pill badge-${item.severity}">${SEVERITY_LABEL[item.severity]} · −${POINTS[item.severity]}</span>`;
                body = `<p>${escapeHtml(item.message)}</p>
                    <details><summary>Korrektur in Formcycle</summary>
                        <p>${escapeHtml(r.remediation)}</p>
                        <p class="basis">Grundlage: ${escapeHtml(r.rationale)}</p>
                    </details>`;
            } else if (item.kind === 'rec') {
                cls += ' rule-rec';
                badge = '<span class="pill badge-rec">Empfehlung</span>';
                body = `<p>${escapeHtml(item.message)}</p>
                    <details><summary>Umsetzung in Formcycle</summary><p>${escapeHtml(r.remediation)}</p></details>`;
            } else if (item.kind === 'passed') {
                cls += ' rule-passed';
                badge = '<span class="pill badge-passed">Bestanden</span>';
            } else if (item.kind === 'unverified') {
                cls += ' rule-unverified';
                badge = '<span class="pill badge-unverified">KI-ungeprüft</span>';
                body = `<p class="rule-note">${escapeHtml(item.message)}</p>`;
            } else {
                badge = '<span class="pill badge-na">Nicht zutreffend</span>';
                body = `<p class="rule-note">${escapeHtml(item.message)}</p>`;
            }
            return `<li class="${cls}">
                <div class="rule-head">
                    <h3><span class="rule-id">${escapeHtml(r.id)}</span>${escapeHtml(r.name)}</h3>
                    <span class="rule-tags"><span class="rule-stage">${escapeHtml(stage)}</span>${badge}</span>
                </div>${body}</li>`;
        }

        function renderRules(items) {
            const shown = items.filter(i => matchesFilter(i, currentFilter)).sort((a, b) => rank(a) - rank(b));
            $('rules').innerHTML = shown.length
                ? shown.map(ruleCard).join('')
                : '<li><p class="empty">Keine Regeln in dieser Ansicht.</p></li>';
        }

        async function evaluateFile(file) {
            if (!file) return;
            if (!file.name.toLowerCase().endsWith('.xfp')) {
                setMessage('Bitte einen Formcycle-Export im Format .xfp wählen.', true);
                return;
            }
            setMessage(`Prüfe ${file.name} …`);
            try {
                const response = await fetch('/api/evaluate', {
                    method: 'POST',
                    headers: { 'X-File-Name': encodeURIComponent(file.name), ...llmHeaders() },
                    body: file,
                });
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    setMessage(payload.error || `Prüfung fehlgeschlagen (HTTP ${response.status}).`, true);
                    return;
                }
                activePresetKey = null;
                currentEval = payload;
                currentFilter = 'issues';
                renderPresets();
                renderEvaluation();
                setMessage(`Geprüft: ${file.name} – ${statusOf(payload).label}, ${payload.score} Punkte.`);
                $('result').focus();
            } catch (err) {
                setMessage('Der Prüf-Server ist nicht erreichbar. Die Prüfung erfolgt ausschließlich serverseitig.', true);
            }
        }

        async function evaluatePresetWithLlm() {
            if (!activePresetKey) {
                setMessage('Bitte zuerst ein Referenzformular wählen oder die eigene Datei erneut hochladen.', true);
                return;
            }
            const btn = $('btnLlm');
            btn.disabled = true;
            setMessage('KI-Prüfung läuft …');
            try {
                const resp = await fetch('/api/evaluate-preset', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...llmHeaders() },
                    body: JSON.stringify({ preset: activePresetKey }),
                });
                const res = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(res.error || `HTTP ${resp.status}`);
                presets[activePresetKey].eval = res;
                currentEval = res;
                renderPresets();
                renderEvaluation();
                setMessage(`KI-Prüfung abgeschlossen: ${statusOf(res).label}, ${res.score} Punkte.`);
            } catch (err) {
                setMessage(`Die KI-Prüfung braucht den Prüf-Server (python web/server.py). ${err.message}`, true);
            } finally {
                btn.disabled = false;
            }
        }

        function exportJson() {
            const blob = new Blob([JSON.stringify(currentEval, null, 2)], { type: 'application/json' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `pruefbericht_${String(currentEval.form || 'formular').replace(/[^a-zA-Z0-9_-]/g, '_')}.json`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(a.href);
        }

        // Für den Prüfbericht alle Regeln und Korrekturhinweise ausklappen
        let filterBeforePrint = null;
        window.addEventListener('beforeprint', () => {
            filterBeforePrint = currentFilter;
            currentFilter = 'all';
            const items = classify(currentEval);
            renderRules(items);
            document.querySelectorAll('#rules details').forEach(d => { d.open = true; });
        });
        window.addEventListener('afterprint', () => {
            if (filterBeforePrint === null) return;
            currentFilter = filterBeforePrint;
            filterBeforePrint = null;
            renderEvaluation();
        });

        const dropzone = $('dropzone');
        ['dragenter', 'dragover'].forEach(t => dropzone.addEventListener(t, ev => { ev.preventDefault(); dropzone.classList.add('is-drag'); }));
        ['dragleave', 'drop'].forEach(t => dropzone.addEventListener(t, ev => { ev.preventDefault(); dropzone.classList.remove('is-drag'); }));
        dropzone.addEventListener('drop', ev => evaluateFile(ev.dataTransfer.files[0]));
        $('fileInput').addEventListener('change', ev => { evaluateFile(ev.target.files[0]); ev.target.value = ''; });
        $('btnLlm').addEventListener('click', evaluatePresetWithLlm);
        $('llmApiBase').addEventListener('change', fillModels);
        $('btnPrint').addEventListener('click', () => window.print());
        $('btnJson').addEventListener('click', exportJson);

        renderPresets();
        renderEvaluation();
        loadLlmConfig();
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    generate_dashboard()
