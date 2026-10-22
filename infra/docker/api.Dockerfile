# API server; also runs the migrations (the stack's migrate service) and the admin scripts.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --user-group --no-create-home app
WORKDIR /app

COPY pyproject.toml infra/docker/requirements.py ./
RUN python requirements.py api > requirements.txt \
    && pip install -r requirements.txt \
    && rm requirements.py requirements.txt

COPY alembic.ini ./
COPY common/ common/
COPY api/ api/
COPY migrations/ migrations/
COPY scripts/ scripts/

USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", \
     "--ws-ping-interval", "25", "--ws-ping-timeout", "20", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
