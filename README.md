# Formular-Standard und KI-gestützte Compliance-Prüfung für Formcycle

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.24513486.svg)](https://doi.org/10.5281/zenodo.24513486)

Artefakte zur Bachelorarbeit **„Konzeption eines einheitlichen Formular-Standards für die HWG Ludwigshafen und prototypische Umsetzung einer KI-gestützten Compliance-Prüfung in Formcycle“** von Fotios Logaras (Hochschule für Wirtschaft und Gesellschaft Ludwigshafen, 2026; Erstgutachter: Prof. Dr. Simon Kloker).

Das Repository enthält:

- den Formular-Standard als maschinenlesbares Regelwerk;
- den Prototyp der zweistufigen Prüfung (Engine, Prompt-Erzeugung, Prüf-Server, Dashboard, Bereitstellung);
- zwei synthetische Testformulare;
- die Mess- und Auswertungsergebnisse aus Kapitel 6.

Der Stand ist der des abschließenden Messlaufs vom 15./16.11.2026 (Registry v1.7.0, Engine v1.7.0, Prompt v1.6.6).

## Inhalt

| Pfad | Inhalt | Arbeit |
| :--- | :--- | :--- |
| `data/rules/rules_registry.json` | Formular-Standard: 22 Regeln mit Schweregrad, Herleitung und Prüfstufe | Kap. 4, Anhang A |
| `scripts/rule_engine.py` | Prüf-Engine: Stufe 1 prüft 15 Regeln deterministisch am Formcycle-Export, Stufe 2 bewertet 7 Regeln mit einem Sprachmodell | Kap. 5 |
| `scripts/generate_chat_ai_prompt.py` | Formularextrakt und Prompt für Stufe 2 | Kap. 5, Anhang B |
| `web/server.py`, `web/compliance_dashboard.html` | Prüf-Server mit Upload und Dashboard; das Dashboard lässt sich auch ohne Server öffnen | Kap. 5 |
| `deploy/` | Dockerfile, docker-compose, nginx, Bereitstellungsanleitung | Kap. 5 |
| `data/test_forms/` | synthetisches konformes und synthetisches fehlerhaftes Testformular (Formcycle-Exporte) | Kap. 6 |
| `scripts/` (übrige) | Korpusstatistik, Laufzeitmessung, Robustheitstests, Sensitivität, Modellvergleich, A/B-Prüfung von Prompt-Versionen, Abgleich mit der Annotation, Steuerung der Messläufe | Kap. 3, 6, Anhang C, D |
| `data/evaluation/` | Messergebnisse (siehe unten) | Kap. 6 |
| `thesis/generated/` | von den Skripten erzeugte Tabellen der Arbeit | Kap. 6 |

### Messergebnisse in `data/evaluation/`

- **`llm_runs/`:** Rohantworten aller Modellläufe, je Modell, Formular und Lauf eine Datei, dazu `summary.json`.
  - `20261115-204259`: abschließender Modellvergleich über die Academic Cloud der GWDG
  - `20261115-201756`: lokaler Lauf mit Ollama
  - Ordner mit `-ab`: A/B-Prüfungen von Prompt-Versionen
  - übrige Ordner: frühere Messläufe mit älteren Registry-Versionen (Anhang C)
- **`lokaltest.zip`:** der erste lokale Lauf (`20261102-102011`).
- **`messlauf_status*.json`, `messlauf_umgebung_server.txt`:** Versionen, Git-Stand und Umgebung der Messläufe.
- **`corpus_statistics.json`, `performance_stage1.json`, `robustness_results*.json`, `robustness_u1_altversionen.json`, `sensitivity_analysis.json`:**
  - Korpusstatistik
  - Laufzeit der Stufe 1
  - Robustheitstests (Endstand 29 von 29 bestanden)
  - Sensitivität des Scores

## Nicht enthalten

Die echten Formcycle-Exporte der Hochschule (34 Archive aus Produktiv- und Testsystem) enthalten Namen und dienstliche E-Mail-Adressen von Beschäftigten. Sie werden deshalb nicht veröffentlicht, auch nicht in pseudonymisierter Form. Ebenfalls nicht enthalten sind:

- die aus einem dieser Exporte erstellte überarbeitete Fassung des Formulars „Antrag-Sprints“;
- gespeicherte Formularansichten;
- Unterlagen und Annotationen der Expertensitzung;
- das Skript zur Pseudonymisierung, dessen Wortlisten auf die Originaldaten abgestimmt sind.

Die veröffentlichten Messergebnisse enthalten keine Personenangaben.

Skripte, die diese Formulare einlesen, laufen hier deshalb nur teilweise:

- `scripts/rule_engine.py` ohne Argumente: bricht nach den beiden synthetischen Formularen ab;
- Korpusstatistik und Laufzeitmessung;
- Robustheitstests;
- Modellvergleich;
- Abgleich mit der Annotation.

Ihre Ergebnisse liegen unter `data/evaluation/` und `thesis/generated/`. Das Dashboard zeigt für alle sechs Referenzformulare die Ergebnisse aus dem Messlauf. Erneut prüfen lassen sich nur die beiden synthetischen Formulare und eigene Uploads.

## Abweichungen vom Messstand

- **Testformulare:** Benutzer-, Gruppen- und Mandantenangaben sind neutralisiert („Autor“, „mandant“). Die Prüfsummen weichen deshalb von den Werten in `llm_runs/` ab; das Ergebnis der Stufe 1 bleibt gleich.
- **`deploy/`:** Interne Beispiel-Hostnamen sind durch `*.intern.example` ersetzt. Das Dockerfile kopiert nur die hier enthaltenen Formulare.

## Ausführen

Voraussetzung ist Python 3.11 oder neuer; es wird nur die Standardbibliothek gebraucht. Alle Befehle laufen im Wurzelverzeichnis.

Prüf-Server mit Dashboard und Upload starten (danach erreichbar unter http://127.0.0.1:8080):

```bash
python web/server.py
```

Ein Formular auf der Kommandozeile prüfen:

```bash
python -c "import sys; sys.path.insert(0, 'scripts'); import rule_engine as r; e = r.evaluate_form('data/test_forms/synthetic_noncompliant_form.xfp', r.load_rules()); print(e['score'], e['release_label']); print(*sorted({v['rule'] for v in e['violations']}))"
```

Ohne konfigurierten Endpunkt läuft nur Stufe 1. Die anwendbaren Regeln der Stufe 2 gelten dann als ungeprüft, und das Formular wird nicht freigegeben. Für Stufe 2 die folgenden Umgebungsvariablen setzen oder eine `.env` im Wurzelverzeichnis anlegen:

```bash
LLM_API_BASE=https://chat-ai.academiccloud.de/v1   # lokal mit Ollama: http://localhost:11434/v1
LLM_API_KEY=<Schlüssel der Academic Cloud>          # lokal nicht nötig
LLM_MODEL=deepseek-v4-flash-0731                   # Referenzmodell der Arbeit; lokal z. B. qwen3.8:27b
```

Im Dashboard stehen nur die serverseitig freigegebenen Endpunkte zur Auswahl. Weitere Variablen (`LLM_ALLOWED_API_BASES`, `LLM_FALLBACK_MODEL`, `LLM_LOCAL_MODEL`, `LLM_TIMEOUT`) beschreibt `deploy/DEPLOYMENT_ANLEITUNG.md`.

Als Container starten:

```bash
docker compose -f deploy/docker-compose.yml up --build
```

## Zitieren

Logaras, F. (2026). *Formular-Standard und KI-gestützte Compliance-Prüfung für Formcycle – Artefakte zur Bachelorarbeit* (Version 1.7.0) [Computer software]. Zenodo. https://doi.org/10.5281/zenodo.24513486

Die DOI verweist auf alle Versionen und führt immer zur neuesten; die DOI der einzelnen Version steht auf der Zenodo-Seite. Siehe auch `CITATION.cff`.

## Lizenz

Rechteinhaber ist Fotios Logaras. Die Leitung des IT-Service Centers der HWG Ludwigshafen hat die Veröffentlichung freigegeben.

- **Quellcode und Bereitstellung** (`scripts/`, `web/`, `deploy/`): MIT-Lizenz, siehe [`LICENSE`](LICENSE).
- **Regelwerk, Testformulare und Messergebnisse** (`data/`, `thesis/generated/`) sowie diese README: [Creative Commons Namensnennung 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/deed.de).
- **Ausgenommen** sind zwei fremde Bestandteile, für die diese Lizenzen nicht gelten:
  - das Logo der HWG Ludwigshafen, im Dashboard (`web/assets/logo_hsl_claim.svg`) wie in den Testformularen. Es bleibt Eigentum der Hochschule und ist hier mit deren Zustimmung enthalten;
  - die Schrift Lexend (`web/assets/fonts/`), die unter der SIL Open Font License 1.1 steht (siehe `web/assets/fonts/README.md`).
