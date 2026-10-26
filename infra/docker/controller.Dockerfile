FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --user-group --no-create-home app
WORKDIR /app

COPY pyproject.toml infra/docker/requirements.py infra/docker/constraints.txt ./
RUN python requirements.py controller > requirements.txt \
    && pip install -r requirements.txt -c constraints.txt \
    && rm requirements.py requirements.txt constraints.txt

COPY common/ common/
COPY controller/ controller/
COPY --chmod=755 infra/docker/controller-entrypoint.sh /usr/local/bin/controller-entrypoint

# The entrypoint drops to the app user before starting Python.
ENTRYPOINT ["controller-entrypoint"]
CMD ["python", "-m", "controller.main"]
