FROM python:3.11-slim

RUN useradd --create-home --uid 10001 agent
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY samples ./samples
RUN pip install --no-cache-dir ".[azure]" && mkdir -p /data && chown agent:agent /data

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/disputes.db \
    RULES_PATH=/app/config/rules.yaml \
    TEMPLATES_DIR=/app/config/templates \
    LOCAL_INBOX_DIR=/app/samples/emails \
    LOCAL_OUTBOX_DIR=/data/outbox
USER agent
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

# API (review screen + Graph webhook). Run a second replica with `dispute-agent loop` as the scheduler,
# or call POST /run-cycle from an Azure Container Apps job every few minutes.
CMD ["dispute-agent", "serve", "--port", "8080"]
