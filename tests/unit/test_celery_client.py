import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from common import celery_client

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_app(monkeypatch):
    app = MagicMock()
    monkeypatch.setattr(celery_client, "app", lambda: app)
    return app


def test_send_generate_names_the_task_and_uses_the_database_id(fake_app):
    task = SimpleNamespace(id=uuid4(), user_id=uuid4(), prompt="a cat", size=256)
    celery_client.send_generate(task)
    fake_app.send_task.assert_called_once_with(
        "worker.tasks.generate_image",
        args=[str(task.id), str(task.user_id), "a cat", 256],
        task_id=str(task.id),
        queue="image_generation",
    )


def test_revoke_does_not_terminate_by_default(fake_app):
    task_id = uuid4()
    celery_client.revoke(task_id)
    fake_app.control.revoke.assert_called_once_with(str(task_id), terminate=False)


def test_importing_the_client_and_the_api_loads_neither_the_worker_nor_torch():
    code = (
        "import sys, common.celery_client, api.main; "
        "loaded = [m for m in ('worker', 'torch', 'diffusers') if m in sys.modules]; "
        "assert not loaded, loaded"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
