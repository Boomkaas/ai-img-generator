"""The Celery app the worker containers run."""

from celery import Celery

from common import live
from common.config import get_settings

settings = get_settings()

celery_app = Celery("imagegen", broker=settings.BROKER_URL, include=["worker.tasks"])
celery_app.conf.update(
    task_default_queue=live.GENERATION_QUEUE,
    task_acks_late=True,  # with prefetch 1, a busy worker never holds a second job
    worker_prefetch_multiplier=1,
    task_ignore_result=True,  # progress and results go to Redis keys and Postgres instead
    broker_transport_options={"visibility_timeout": settings.BROKER_VISIBILITY_TIMEOUT},
    worker_proc_alive_timeout=settings.WORKER_PROC_ALIVE_TIMEOUT,  # the child loads the model first
)
