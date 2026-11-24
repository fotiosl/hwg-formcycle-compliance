"""
Intranet-Server für das HWG Formcycle Compliance-Dashboard (Registry und Engine v1.7.0).

Sicherheitsrelevante Eigenschaften:
- Uploads nur als .xfp-Archiv (ZIP), Größenlimit über MAX_UPLOAD_BYTES; Archivlimits in der Engine.
- LLM-Endpunkte nur aus der serverseitigen Freigabeliste (LLM_ALLOWED_API_BASES); das Dashboard bietet
  nur diese Endpunkte mit den passenden Modellen zur Auswahl an (/api/config). Der serverseitige
  API-Schlüssel wird ausschließlich an den serverseitig konfigurierten Endpunkt gesendet.
- Verarbeitung im Arbeitsspeicher, keine temporären Dateien; Fehlerdetails nur im Server-Log.
- Mehrere Anfragen parallel (ThreadingHTTPServer).
- Ausgeliefert werden nur das Dashboard und die API-Endpunkte, keine weiteren Dateien.
Der Server ist für den Betrieb hinter einem authentifizierenden Reverse Proxy im Intranet gedacht.
"""
import http.server
import json
import logging
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from rule_engine import (  # noqa: E402
    DEFAULT_API_BASE,
    DEFAULT_LLM_MODEL,
    allowed_api_bases,
    evaluate_form,
    evaluate_form_bytes,
    load_rules,
)

PORT = int(os.environ.get("PORT", 8080))
# Standard: nur lokal erreichbar. Im Container setzt das Dockerfile BIND_HOST=0.0.0.0;
# docker-compose.yml veröffentlicht den Port dann nur auf 127.0.0.1 des Hosts.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(DIRECTORY, ".."))
RULES_PATH = os.path.join(REPO_ROOT, "data", "rules", "rules_registry.json")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 20 * 1024 * 1024))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hwg-compliance")

PRESET_PATHS = {
    "synth_comp": "data/test_forms/synthetic_compliant_form.xfp",
    "synth_noncomp": "data/test_forms/synthetic_noncompliant_form.xfp",
    "auslagen": "data/raw_forms/prod/Auslagenerstattung _20260929.xfp",
    "sprints": "data/raw_forms/test/fachformulare/AFörderung Antrag-Sprints für Drittmittelanträge.xfp",
    "sprints_standard": "data/raw_forms/standard/AFörderung Antrag-Sprints für Drittmittelanträge (HWG-Standard).xfp",
    "master": "data/raw_forms/test/adminformulare/Master-Formular.xfp",
    "fb1_praktikum": "data/raw_forms/prod/FB1-GiP_Praktikum_final_20251115.xfp",
}

ACADEMIC_CLOUD_HOST = "chat-ai.academiccloud.de"
# Modelle der Academic Cloud, die im Modellvergleich (Kap. 6) verwendet wurden
ACADEMIC_CLOUD_MODELS = [
    {"id": "deepseek-v4-flash-0731", "label": "DeepSeek V4 Flash 0731"},
    {"id": "qwen3-30b-a3b-instruct-2507", "label": "Qwen 3 30B A3B Instruct 2507"},
    {"id": "qwen3.8-27b", "label": "Qwen 3.8 27B"},
    {"id": "glm-5.3-flash", "label": "GLM 5.3 Flash"},
    {"id": "meta-llama-3.1-8b-instruct", "label": "Llama 3.1 8B Instruct"},
]
# Modell eines eigenen Inferenzservers (Ollama); für jeden anderen freigegebenen Endpunkt voreingestellt
LOCAL_MODEL = os.environ.get("LLM_LOCAL_MODEL", "qwen3.8:27b").strip() or "qwen3.8:27b"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _normalize_base(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def llm_endpoints() -> list:
    """Auswahl für das Dashboard: nur serverseitig freigegebene Endpunkte, je mit den passenden Modellen."""
    server_base = _normalize_base(os.environ.get("LLM_API_BASE") or os.environ.get("GWDG_API_BASE") or DEFAULT_API_BASE)
    server_key = bool(os.environ.get("LLM_API_KEY") or os.environ.get("GWDG_API_KEY"))
    server_model = (os.environ.get("LLM_MODEL") or "").strip()
    endpoints, seen = [], set()
    for base in allowed_api_bases():
        parts = urllib.parse.urlsplit(base)
        host = (parts.hostname or "").lower()
        ident = ("localhost" if host in LOCAL_HOSTS else host, parts.port, parts.path)
        if ident in seen:  # localhost und 127.0.0.1 sind derselbe Server
            continue
        seen.add(ident)
        if host == ACADEMIC_CLOUD_HOST:
            ids = [m["id"] for m in ACADEMIC_CLOUD_MODELS]
            default = server_model if base == server_base and server_model in ids else DEFAULT_LLM_MODEL
            endpoints.append({
                "api_base": base,
                "label": "GWDG Academic Cloud",
                "models": ACADEMIC_CLOUD_MODELS,
                "default_model": default,
                # Der serverseitige Schlüssel gilt nur für den serverseitig konfigurierten Endpunkt
                "needs_key": not (server_key and base == server_base),
            })
        else:
            endpoints.append({
                "api_base": base,
                "label": f"Eigener Inferenzserver ({parts.netloc})",
                "models": [{"id": LOCAL_MODEL, "label": LOCAL_MODEL}],
                "default_model": LOCAL_MODEL,
                "needs_key": False,
            })
    return endpoints


def validate_upload(filename: str, body: bytes):
    """Gibt eine Fehlermeldung zurück oder None, wenn der Upload zulässig ist."""
    if not filename.lower().endswith(".xfp"):
        return "Es werden nur Formcycle-Exportarchive (.xfp) angenommen."
    if len(body) > MAX_UPLOAD_BYTES:
        return "Die Datei überschreitet die zulässige Größe."
    if not body.startswith(b"PK"):
        return "Die Datei ist kein gültiges .xfp-Archiv."
    return None


class ComplianceHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def _extract_llm_config(self):
        cfg = {}
        api_key = self.headers.get("X-LLM-Api-Key", "").strip()
        api_base = self.headers.get("X-LLM-Api-Base", "").strip()
        model = self.headers.get("X-LLM-Model", "").strip()
        if api_key:
            cfg["api_key"] = api_key
        if api_base:
            cfg["api_base"] = api_base  # wird in der Engine gegen die Freigabeliste geprüft
        if model:
            cfg["model"] = model
        return cfg or None

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length < 0 or length > MAX_UPLOAD_BYTES:
            return None
        return self.rfile.read(length)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ["/", "/index.html", "/dashboard", "/compliance_dashboard.html"]:
            self.path = "/compliance_dashboard.html"
            return super().do_GET()
        if path == "/api/config":
            env_key = os.environ.get("LLM_API_KEY") or os.environ.get("GWDG_API_KEY")
            self._send_json(200, {
                "default_model": os.environ.get("LLM_MODEL") or DEFAULT_LLM_MODEL,
                "default_api_base": os.environ.get("LLM_API_BASE") or DEFAULT_API_BASE,
                "default_endpoint": _normalize_base(os.environ.get("LLM_API_BASE") or os.environ.get("GWDG_API_BASE") or DEFAULT_API_BASE),
                "allowed_api_bases": allowed_api_bases(),
                "has_api_key": bool(env_key),
                "endpoints": llm_endpoints(),
            })
            return
        if path == "/api/rules":
            self._send_json(200, load_rules(RULES_PATH))
            return
        # Keine weiteren Dateien ausliefern (z. B. Quellcode im Verzeichnis web/)
        self._send_json(404, {"error": "Nicht gefunden"})

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path in ["/", "/index.html", "/dashboard", "/compliance_dashboard.html"]:
            self.path = "/compliance_dashboard.html"
            return super().do_HEAD()
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/evaluate":
            body = self._read_body()
            filename = urllib.parse.unquote(self.headers.get("X-File-Name", "uploaded_form.xfp"))
            if body is None:
                self._send_json(413, {"error": "Die Datei überschreitet die zulässige Größe."})
                return
            err = validate_upload(filename, body)
            if err:
                self._send_json(400, {"error": err})
                return
            try:
                result = evaluate_form_bytes(body, os.path.basename(filename), load_rules(RULES_PATH), llm_config=self._extract_llm_config())
                self._send_json(200, result)
            except ValueError as ve:
                # Fachliche Ablehnung (z. B. mehrdeutiges Archiv) darf dem Nutzer angezeigt werden
                self._send_json(422, {"error": str(ve)})
            except Exception:
                log.exception("Auswertung fehlgeschlagen")
                self._send_json(500, {"error": "Die Datei konnte nicht ausgewertet werden."})
            return

        if self.path == "/api/evaluate-preset":
            body = self._read_body()
            try:
                req_obj = json.loads((body or b"{}").decode("utf-8", errors="replace") or "{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Ungültige Anfrage."})
                return
            preset_key = req_obj.get("preset", "")
            if preset_key not in PRESET_PATHS:
                self._send_json(400, {"error": "Unbekanntes Preset."})
                return
            try:
                full_path = os.path.join(REPO_ROOT, PRESET_PATHS[preset_key])
                result = evaluate_form(full_path, load_rules(RULES_PATH), llm_config=self._extract_llm_config())
                self._send_json(200, result)
            except Exception:
                log.exception("Preset-Auswertung fehlgeschlagen")
                self._send_json(500, {"error": "Das Preset konnte nicht ausgewertet werden."})
            return

        self._send_json(404, {"error": "Unbekannter Endpunkt"})


def run_server():
    with http.server.ThreadingHTTPServer((BIND_HOST, PORT), ComplianceHandler) as httpd:
        log.info("HWG Compliance-Dashboard läuft unter http://localhost:%s (gebunden an %s)", PORT, BIND_HOST or "alle Schnittstellen")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("Server gestoppt.")


if __name__ == "__main__":
    run_server()
