# Bereitstellungsanleitung: HWG Formcycle Compliance-Dashboard (v1.7.0)

Diese Anleitung richtet sich an die IT-Administration der Hochschule für Wirtschaft und Gesellschaft Ludwigshafen. Der Prototyp besteht aus einem Python-Prüf-Server (`web/server.py` mit `scripts/rule_engine.py`) und dem davon ausgelieferten Dashboard (`web/compliance_dashboard.html`). Alle Prüfungen laufen auf dem Server; das Dashboard enthält keine eigene Regel-Logik.

---

## Bereitstellung mit Docker Compose (empfohlen)

```bash
export LLM_API_BASE="https://chat-ai.academiccloud.de/v1"
export LLM_MODEL="deepseek-v4-flash-0731"
export LLM_API_KEY="<GWDG-API-KEY>"
# optional: weitere erlaubte Endpunkte, z. B. ein interner Ollama-Server
export LLM_ALLOWED_API_BASES="http://ollama.intern.example:11434/v1"
# optional: Ersatzmodell, falls das Hauptmodell nicht antwortet (Zeitüberschreitung, HTTP 429/5xx)
export LLM_FALLBACK_MODEL="qwen3-30b-a3b-instruct-2507"
# optional: Modell eigener Inferenzserver in der Auswahl des Dashboards (Standard: qwen3.8:27b)
export LLM_LOCAL_MODEL="qwen3.8:27b"
# optional: Zeitlimit je Modellanfrage in Sekunden (Standard: 45, bei Qwen 3.8 120)
export LLM_TIMEOUT="120"
docker compose -f deploy/docker-compose.yml up -d
```

Der Container bindet nur an `127.0.0.1:8080`. Erreichbar wird das Dashboard über den Reverse Proxy aus `deploy/nginx.conf`.

Antwortet das Hauptmodell nicht, versucht der Server einmalig das Ersatzmodell `LLM_FALLBACK_MODEL` am selben Endpunkt; das Ergebnis vermerkt dann `fallback_used` und das verwendete Modell. Ohne `LLM_API_KEY` (oder wenn auch das Ersatzmodell nicht antwortet) prüft der Server die 15 deterministischen Regeln. Die semantischen Regeln bleiben als „KI-ungeprüft“ markiert, und das Ergebnis ist **vorläufig und nicht freigabefähig** (`pending_ai_review`).

---

## Reverse Proxy (Pflicht)

Der Prüf-Server hat keine eigene Benutzerverwaltung. Er darf daher nur hinter einem Reverse Proxy betrieben werden, der den Zugriff einschränkt (Hochschul-SSO/Shibboleth, LDAP-Anmeldung oder IP-Freigabe). Eine Vorlage mit TLS, Upload-Limit und Basic-Auth enthält `deploy/nginx.conf`.

---

## Sicherheits- und Betriebsmerkmale

| Kriterium | Umsetzung |
| :--- | :--- |
| **Uploads** | Nur `.xfp`-Archive, Größenlimit `MAX_UPLOAD_BYTES` (Standard 20 MB). Archive mit mehr als einem Maskenmodell (`.fcp`), übergroßen Einträgen oder zu vielen Einträgen werden abgewiesen. |
| **Ausgelieferte Dateien** | Nur Dashboard (`/`, `/compliance_dashboard.html`) und API (`/api/...`); alle anderen Pfade liefern 404. Ohne `BIND_HOST` lauscht der Server nur auf `127.0.0.1` (im Container setzt das Dockerfile `0.0.0.0`). |
| **Dateispeicherung** | Keine. Archive werden im Arbeitsspeicher (`io.BytesIO`) entpackt und nach der Antwort verworfen. |
| **LLM-Endpunkte** | Nur der konfigurierte Endpunkt sowie Einträge aus `LLM_ALLOWED_API_BASES` (und lokales Ollama). Das Dashboard bietet genau diese Endpunkte zur Auswahl an: die Academic Cloud mit den Modellen des Modellvergleichs, jeden anderen Endpunkt als eigenen Inferenzserver mit dem Modell aus `LLM_LOCAL_MODEL`. Der serverseitige API-Schlüssel wird ausschließlich an `LLM_API_BASE` gesendet; hat der Server keinen Schlüssel für die Academic Cloud, fragt das Dashboard einen ab. |
| **Fehlermeldungen** | Technische Details nur im Server-Log, nicht in der Antwort an den Browser. |
| **Parallelität** | `ThreadingHTTPServer`; lange LLM-Anfragen blockieren andere Nutzer nicht. |
| **Datenbank** | Nicht erforderlich (zustandslos). |
| **Ressourcen** | 1 vCPU, 512 MB RAM genügen für Stufe 1; Stufe 2 läuft auf dem LLM-Dienst. |

---

## Datenschutz beim Betrieb von Stufe 2

1. **Stufe 1** läuft vollständig lokal.
2. **Stufe 2** übermittelt einen kompakten Formularextrakt (Beschriftungen, Hilfetexte, Schaltflächen, Texte und Empfänger der Workflow-E-Mails) an den LLM-Dienst. Workflow-Konfigurationen können Namen oder E-Mail-Adressen von Beschäftigten enthalten.
   * Für die *Academic Cloud* der GWDG ist ein Auftragsverarbeitungsvertrag nach Art. 28 DSGVO erforderlich.
   * Alternativ läuft Stufe 2 auf einem internen Inferenz-Server (Ollama/vLLM) im Hochschulnetz; dann verlassen keine Daten die Hochschule.
3. Für Analysen mit externen KI-Diensten sollten Exporte vorher pseudonymisiert werden (Empfängeradressen in Workflows ersetzen).

---

## Prüfung nach der Installation

```bash
python scripts/robustness_tests.py   # Regressionstests der Umgehungsversuche (erwartet: alle bestanden)
python scripts/rule_engine.py   # Prüfung der sechs Referenzformulare
```
