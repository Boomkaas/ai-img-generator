"""Enqueue and revoke generation jobs by task name.

The API and the controller never import the worker package, so this app knows only the broker: no task
modules, no torch.
"""

from functools import lru_cache

from celery import Celery

from common import live
from common.config import get_settings

GENERATE_TASK = "worker.tasks.generate_image"


@lru_cache
def app() -> Celery:
    settings = get_settings()
    celery = Celery("imagegen", broker=settings.BROKER_URL)
    # kombu's Redis transport has no socket timeouts by default, so a broker host that stops answering
    # would hold a submit or cancel request for the OS TCP timeout on every retry.
    celery.conf.update(
        broker_transport_options={
            "socket_connect_timeout": settings.REDIS_CONNECT_TIMEOUT_S,
            "socket_timeout": settings.REDIS_SOCKET_TIMEOUT_S,
        },
        task_publish_retry_policy={"max_retries": settings.BROKER_PUBLISH_MAX_RETRIES},
    )
    return celery


def reset_app() -> None:
    """Close the broker connections and forget the app (tests, after the settings change)."""
    if app.cache_info().currsize:
        app().close()
    app.cache_clear()


def send_generate(task) -> None:
    # Celery's task id is the database id, so revoke() and the worker's claim refer to the same task.
    app().send_task(
        GENERATE_TASK,
        args=[str(task.id), str(task.user_id), task.prompt, task.size],
        task_id=str(task.id),
        queue=live.GENERATION_QUEUE,
    )


def revoke(task_id, terminate: bool = False) -> None:
    app().control.revoke(str(task_id), terminate=terminate)
