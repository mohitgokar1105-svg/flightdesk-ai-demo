# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    TRUST_PROXY_HEADERS=1

WORKDIR /app

# Runtime dependencies only: the dev/test tools listed in requirements.txt are filtered out.
COPY requirements.txt .
RUN grep -v -E "^(pytest|hypothesis|ruff|mypy)==" requirements.txt > requirements.runtime.txt \
    && pip install -r requirements.runtime.txt

COPY app.py pyproject.toml ./
COPY .streamlit ./.streamlit
COPY src ./src
COPY scripts ./scripts
COPY evals ./evals
COPY data/samples ./data/samples

# Non-root user; /app/data is the only writable path (mount a volume there).
RUN useradd --system --uid 10001 --home-dir /app flightdesk \
    && mkdir -p /app/data/exports /app/data/logs /app/data/backups \
    && chown -R flightdesk:flightdesk /app/data
USER flightdesk
VOLUME ["/app/data"]

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4).status == 200 else 1)"

CMD ["streamlit", "run", "app.py"]
