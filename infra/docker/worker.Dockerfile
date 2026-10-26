# Generation worker. Two targets:
#   fake  the sleeping pipeline, no torch, no weights (tests and the one-machine smoke run)
#   real  CPU torch, diffusers and the Stable Diffusion 1.5 weights baked in
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --user-group --create-home app
WORKDIR /app

COPY pyproject.toml infra/docker/requirements.py infra/docker/constraints.txt ./
RUN python requirements.py worker > requirements.txt \
    && pip install -r requirements.txt -c constraints.txt \
    && rm requirements.py requirements.txt constraints.txt

CMD ["celery", "-A", "worker.app:celery_app", "worker", "--pool=prefork", "--concurrency=1", \
     "-Q", "image_generation", "--loglevel=info"]


FROM base AS real

ARG TORCH_VERSION=2.8.0
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_VERSION}"
RUN pip install \
        diffusers==0.35.1 \
        transformers==4.56.1 \
        accelerate==1.10.1 \
        safetensors==0.6.2 \
        huggingface-hub==0.34.4

# Pinned to a commit so a rebuild never picks up different weights.
ARG MODEL_ID=stable-diffusion-v1-5/stable-diffusion-v1-5
ARG MODEL_REVISION=451f4fe16113bff5a5d2269ed5ad43b0592e9a14
COPY scripts/download_model.py /tmp/
RUN python /tmp/download_model.py "$MODEL_ID" "$MODEL_REVISION" /models/sd15 && rm /tmp/download_model.py

COPY common/ common/
COPY worker/ worker/
COPY scripts/ scripts/

ENV PIPELINE=real \
    MODEL_DIR=/models/sd15 \
    HF_HUB_OFFLINE=1
USER app


FROM base AS fake

COPY common/ common/
COPY worker/ worker/
COPY scripts/ scripts/

ENV PIPELINE=fake
USER app
