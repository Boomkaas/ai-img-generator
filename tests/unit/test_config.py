import pytest
from pydantic import ValidationError

from common.config import Settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ignore settings from the surrounding environment (CI sets PIPELINE=fake, for instance)."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)


def make(**overrides) -> Settings:
    return Settings(_env_file=None, JWT_SECRET="x", **overrides)


def test_derived_timeouts_follow_the_formulas():
    s = make(MEASURED_MAX_RUNTIME_S=1080)
    assert s.TASK_SOFT_TIME_LIMIT == int(1080 * 1.5) == 1620
    assert s.TASK_TIME_LIMIT == 1620 + 60
    assert s.BROKER_VISIBILITY_TIMEOUT == 1680 * 2
    assert s.WORKER_STOP_GRACE_S == 1680 + 120
    assert s.CANCEL_FLAG_TTL_S == 1680


def test_derived_timeouts_follow_measured_runtime_from_env(monkeypatch):
    monkeypatch.setenv("MEASURED_MAX_RUNTIME_S", "600")
    s = make()
    assert (s.TASK_SOFT_TIME_LIMIT, s.TASK_TIME_LIMIT, s.BROKER_VISIBILITY_TIMEOUT) == (900, 960, 1920)


def test_one_derived_timeout_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("TASK_TIME_LIMIT", "1700")
    s = make()
    assert s.TASK_SOFT_TIME_LIMIT == 1620  # still derived
    assert s.TASK_TIME_LIMIT == 1700
    assert s.BROKER_VISIBILITY_TIMEOUT == 3400  # derived from the override
    assert s.WORKER_STOP_GRACE_S == 1820
    assert s.CANCEL_FLAG_TTL_S == 1700


def test_short_test_timeouts_are_accepted(monkeypatch):
    # The shape the integration tests use (step 13): everything shortened, order kept.
    short = {"MEASURED_MAX_RUNTIME_S": "2", "TASK_SOFT_TIME_LIMIT": "3", "TASK_TIME_LIMIT": "10"}
    for name, value in short.items():
        monkeypatch.setenv(name, value)
    s = make()
    assert (s.BROKER_VISIBILITY_TIMEOUT, s.WORKER_STOP_GRACE_S) == (20, 130)


@pytest.mark.parametrize(
    "overrides, lower, higher",
    [
        ({"TASK_SOFT_TIME_LIMIT": 1000}, "MEASURED_MAX_RUNTIME_S", "TASK_SOFT_TIME_LIMIT"),
        ({"TASK_TIME_LIMIT": 1620}, "TASK_SOFT_TIME_LIMIT", "TASK_TIME_LIMIT"),
        ({"BROKER_VISIBILITY_TIMEOUT": 1000}, "TASK_TIME_LIMIT", "BROKER_VISIBILITY_TIMEOUT"),
        ({"WORKER_STOP_GRACE_S": 1680}, "TASK_TIME_LIMIT", "WORKER_STOP_GRACE_S"),
    ],
)
def test_broken_order_is_refused_naming_both_settings(overrides, lower, higher):
    with pytest.raises(ValidationError) as exc:
        make(**overrides)
    message = str(exc.value)
    assert f"{lower} (" in message and f"{higher} (" in message


def test_min_workers_above_max_is_refused():
    with pytest.raises(ValidationError, match="MIN_WORKERS"):
        make(MIN_WORKERS=5, MAX_WORKERS=4)


@pytest.mark.parametrize("sizes", [(256, 300), (0, 512), ()])
def test_sizes_must_be_positive_multiples_of_64(sizes):
    with pytest.raises(ValidationError, match="ALLOWED_SIZES"):
        make(ALLOWED_SIZES=sizes)


def test_jwt_secret_is_required(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None)


def test_list_settings_parse_from_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("ALLOWED_SIZES", "[256, 512]")
    s = make()
    assert s.ALLOWED_ORIGINS == ("https://a.example", "https://b.example")
    assert s.ALLOWED_SIZES == (256, 512)


def test_defaults_match_the_architecture():
    s = make()
    assert s.ALLOWED_SIZES == (256, 384, 512, 640, 768)
    assert (s.NUM_STEPS, s.MAX_PROMPT_LEN, s.MAX_ACTIVE_TASKS_PER_USER) == (25, 500, 1)
    assert (s.MAX_QUEUED_TOTAL, s.MAX_HISTORY_PER_USER) == (30, 50)
    assert (s.PIPELINE, s.SCALER, s.COOKIE_SECURE) == ("real", "none", True)
    assert s.MACHINE_NAME
