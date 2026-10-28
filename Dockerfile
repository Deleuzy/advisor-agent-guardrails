FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ADVISOR_AUDIT_LOG=/app/logs/audit.jsonl
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY advisor ./advisor
COPY data ./data

# Run as a fixed non-root user; the audit log is the only writable path.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/logs && chown app /app/logs
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "advisor.api:app", "--host", "0.0.0.0", "--port", "8000"]
