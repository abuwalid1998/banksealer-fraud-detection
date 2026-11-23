# VeriShield - single container (web UI + API + scoring engine).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    VERISHIELD_HOST=0.0.0.0 VERISHIELD_PORT=8000 VERISHIELD_DATA_DIR=/data

WORKDIR /app
COPY requirements-verishield.txt requirements-postgres.txt ./
RUN pip install -r requirements-verishield.txt -r requirements-postgres.txt

COPY banksealer ./banksealer
COPY mock_channel ./mock_channel
COPY config ./config
COPY verishield ./verishield
COPY run_verishield.py ./

RUN useradd --create-home --uid 10001 verishield && mkdir -p /data && chown -R verishield /data
USER verishield
VOLUME ["/data"]
EXPOSE 8000 9009 9010
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",os.environ[\"VERISHIELD_PORT\"])}/ready',timeout=4)"
CMD ["python", "run_verishield.py"]
