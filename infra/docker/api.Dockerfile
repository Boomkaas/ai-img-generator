# API server; also runs the migrations (the stack's migrate service) and the admin scripts.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --user-group --no-create-home app
WORKDIR /app

COPY pyproject.toml infra/docker/requirements.py infra/docker/constraints.txt ./
RUN python requirements.py api > requirements.txt \
    && pip install -r requirements.txt -c constraints.txt \
    && rm requirements.py requirements.txt constraints.txt

COPY alembic.ini ./
COPY common/ common/
COPY api/ api/
COPY migrations/ migrations/
COPY scripts/ scripts/

USER app
EXPOSE 8000
# Any answer counts, a 503 too: that only means Postgres or Redis is down, which a restart wouldn't fix.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import http.client as h; c = h.HTTPConnection('127.0.0.1', 8000, timeout=4); c.request('GET', '/api/health'); c.getresponse()"]
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", \
     "--ws-ping-interval", "25", "--ws-ping-timeout", "20", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
