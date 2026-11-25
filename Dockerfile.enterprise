# syntax=docker/dockerfile:1
# BankSealer Enterprise backend image (used by the api, worker and mock-feed services)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data SEED_CONFIG=/app/config/enterprise.yaml
WORKDIR /app

COPY requirements-server.txt .
# Optional build secret "corporate_ca": extra CA for networks with TLS inspection (empty by default).
RUN --mount=type=secret,id=corporate_ca,required=false \
    if [ -s /run/secrets/corporate_ca ]; then \
      cat "$(python -c 'import pip._vendor.certifi as c; print(c.where())')" /run/secrets/corporate_ca > /tmp/ca.pem; \
      export PIP_CERT=/tmp/ca.pem; \
    fi; \
    pip install -r requirements-server.txt && rm -f /tmp/ca.pem

COPY banksealer/__init__.py banksealer/config.py banksealer/models.py banksealer/preparation.py \
     banksealer/reports.py banksealer/storage.py banksealer/cli.py banksealer/controller.py banksealer/
COPY banksealer/engine banksealer/engine
COPY banksealer/channels banksealer/channels
COPY mock_channel mock_channel
COPY server server
COPY config config

RUN useradd --create-home --uid 10001 banksealer \
    && mkdir -p /data/reports /data/models /data/uploads /data/incoming \
    && chown -R banksealer:banksealer /data
USER banksealer
VOLUME ["/data"]
EXPOSE 8000 8088 9009

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)" || exit 1

CMD ["uvicorn", "server.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
