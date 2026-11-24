# ==============================================================================
# HWG Ludwigshafen - Formcycle Compliance Checker
# Minimales Dockerfile für den Prototyp (Python 3.11 Alpine)
# Keine externen Pip-Abhängigkeiten erforderlich (100% Standardbibliothek)
# Verarbeitung im Arbeitsspeicher (io.BytesIO), keine temporären Dateien
# ==============================================================================

FROM python:3.11-alpine

# Metadaten
LABEL maintainer="HWG Ludwigshafen"
LABEL description="HWG Ludwigshafen Formcycle Compliance Dashboard & Hybrid Engine"

# Python-Optimierungen für Read-Only-Container (keine .pyc Schreibversuche)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BIND_HOST=0.0.0.0

WORKDIR /app

# Nur die zur Laufzeit benötigten Dateien kopieren: Registry und die synthetischen Testformulare.
# Die vier Referenzformulare aus Hochschulbeständen sind nicht Teil des veröffentlichten Artefakts;
# ihre Ergebnisse aus dem Messlauf zeigt das Dashboard weiterhin.
COPY data/rules/ /app/data/rules/
COPY ["data/test_forms/synthetic_compliant_form.xfp", "data/test_forms/synthetic_noncompliant_form.xfp", "/app/data/test_forms/"]
COPY scripts/rule_engine.py /app/scripts/rule_engine.py
COPY scripts/generate_chat_ai_prompt.py /app/scripts/generate_chat_ai_prompt.py
COPY web/server.py web/compliance_dashboard.html /app/web/

# Sicherheitsbest-Practice: Als nicht-privilegierter Benutzer ausführen
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/').read()" || exit 1

CMD ["python", "web/server.py"]
