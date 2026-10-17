import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from common import messages as m

pytestmark = pytest.mark.unit

DOC = Path(__file__).resolve().parents[2] / "docs" / "messages.md"
NOW = datetime(2026, 10, 17, 9, 12, tzinfo=UTC)
TASK_ID = uuid4()

TASK_OUT = m.TaskOut(
    task_id=TASK_ID,
    status="running",
    prompt="a cat",
    size=512,
    created_at=NOW,
    started_at=NOW,
    progress=36,
    stage="step 10 of 25",
    ready_in_s=307.2,
)
SYSTEM = m.SystemStatus(running=1, waiting=2, new_image_ready_in_s=720.0)

EXAMPLES = [
    SYSTEM,
    m.TaskUpdate(id=TASK_ID, status="running", progress=36, stage="step 10 of 25"),
    m.TaskUpdate(id=TASK_ID, status="queued", position=2, queue_length=3, ready_in_s=660.0),
    m.TaskFinal(id=TASK_ID, status="cancelled"),
    m.Snapshot(system=SYSTEM, task=TASK_OUT),
    m.Snapshot(system=SYSTEM, task=None),
    m.AdminOverview(
        machines=[
            m.MachineItem(
                name="nodeA", role="manager", cpu=1.5, ram=40, api=m.ApiStats(sockets=2, requests_per_min=9)
            ),
            m.MachineItem(name="nodeC", role="worker", cpu=3, ram=12),
        ],
        workers=[
            m.WorkerItem(
                name="nodeB-worker-1",
                machine="nodeB",
                state="busy",
                task_id=TASK_ID,
                progress=36,
                rss_mb=5000,
            )
        ],
        tasks=[
            m.TaskItem(
                id=TASK_ID,
                user_email="a@example.com",
                size=512,
                status="running",
                worker_host="nodeB-worker-1",
                progress=36,
                seconds_so_far=12.5,
            )
        ],
        autoscaler=m.AutoscalerItem(
            desired=2, ready=1, starting=1, maximum=4, alerts=[m.Alert(at=NOW, message="no room")]
        ),
    ),
]


@pytest.mark.parametrize("message", EXAMPLES, ids=lambda msg: type(msg).__name__)
def test_message_serialises_with_its_type_and_round_trips(message):
    raw = message.model_dump_json()
    assert json.loads(raw)["type"] == message.type
    parsed = m.parse_message(raw)
    assert type(parsed) is type(message)
    assert parsed == message


def test_task_out_round_trips():
    assert m.TaskOut.model_validate_json(TASK_OUT.model_dump_json()) == TASK_OUT


def test_task_update_omits_fields_not_sent():
    data = json.loads(
        m.TaskUpdate(id=TASK_ID, status="running", progress=4, stage="step 1 of 25").model_dump_json()
    )
    assert data == {
        "type": "task",
        "id": str(TASK_ID),
        "status": "running",
        "progress": 4,
        "stage": "step 1 of 25",
    }


def test_snapshot_without_a_task_sends_null():
    assert json.loads(m.Snapshot(system=SYSTEM).model_dump_json())["task"] is None


def test_unknown_type_is_rejected():
    with pytest.raises(ValueError):
        m.parse_message('{"type": "nope"}')


def doc_examples() -> list[tuple[str, dict]]:
    """(model name from the heading, parsed JSON) for every ```json block in docs/messages.md."""
    examples, model = [], None
    for heading, block in re.findall(r"^## (\w+)[^\n]*|```json\n(.*?)```", DOC.read_text(), re.M | re.S):
        if heading:
            model = heading
        else:
            examples.append((model, json.loads(block)))
    return examples


def test_docs_have_one_example_per_message_type():
    documented = {name for name, _ in doc_examples()}
    assert documented == {"SystemStatus", "TaskUpdate", "TaskFinal", "Snapshot", "AdminOverview", "TaskOut"}


@pytest.mark.parametrize(
    "name, example", doc_examples(), ids=lambda value: value if isinstance(value, str) else ""
)
def test_doc_example_matches_its_model(name, example):
    model = getattr(m, name)
    parsed = model.model_validate(example)
    if name != "TaskOut":
        assert type(m.parse_message(json.dumps(example))) is model
    # The doc shows exactly the fields the server sends.
    assert json.loads(parsed.model_dump_json()).keys() == example.keys()
