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
    return Celery("imagegen", broker=get_settings().BROKER_URL)


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
