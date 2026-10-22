import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from common.config import Settings

pytestmark = pytest.mark.unit

STACK_FILE = Path(__file__).parents[2] / "infra" / "stack.yml"
DURATION_UNITS = {"h": 3600, "m": 60, "s": 1}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ignore settings from the surrounding environment (CI sets PIPELINE=fake, for instance)."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)


def make(**overrides) -> Settings:
    return Settings(_env_file=None, JWT_SECRET="j" * 32, **overrides)


def parse_duration(value: str) -> int:
    """Seconds in a Compose duration such as "30m", "1h30m" or "1800s"."""
    parts = re.findall(r"(\d+)([hms])", value)
    assert parts and "".join(n + u for n, u in parts) == value, f"unsupported duration {value!r}"
    return sum(int(n) * DURATION_UNITS[u] for n, u in parts)


@pytest.mark.parametrize(
    ("value", "seconds"), [("30m", 1800), ("1h30m", 5400), ("1800s", 1800), ("1h", 3600)]
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


def test_timeouts_are_in_order():
    s = make()
    assert s.MEASURED_MAX_RUNTIME_S < s.TASK_SOFT_TIME_LIMIT < s.TASK_TIME_LIMIT
    assert s.TASK_TIME_LIMIT < s.BROKER_VISIBILITY_TIMEOUT
    assert s.TASK_TIME_LIMIT < s.WORKER_STOP_GRACE_S
    # Swarm kills a worker this long after SIGTERM; any shorter and scaling down could cut a job off.
    grace = yaml.safe_load(STACK_FILE.read_text())["services"]["worker"]["stop_grace_period"]
    assert parse_duration(grace) >= s.WORKER_STOP_GRACE_S


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
    # The shape the integration tests use: everything shortened, order kept.
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


def test_short_jwt_secret_is_refused():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None, JWT_SECRET="j" * 31)


def test_secrets_are_read_from_files_named_after_the_setting(tmp_path):
    (tmp_path / "DATABASE_URL").write_text("postgresql://imagegen:from-file@postgres/imagegen\n")
    (tmp_path / "JWT_SECRET").write_text("s" * 32 + "\n")
    s = Settings(_env_file=None, _secrets_dir=tmp_path)
    assert s.DATABASE_URL == "postgresql://imagegen:from-file@postgres/imagegen"
    assert s.JWT_SECRET == "s" * 32


def test_environment_wins_over_a_secret_file(tmp_path, monkeypatch):
    (tmp_path / "JWT_SECRET").write_text("f" * 32)
    monkeypatch.setenv("JWT_SECRET", "e" * 32)
    assert Settings(_env_file=None, _secrets_dir=tmp_path).JWT_SECRET == "e" * 32


def test_list_settings_parse_from_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("ALLOWED_SIZES", "[256, 512]")
    s = make()
    assert s.ALLOWED_ORIGINS == ("https://a.example", "https://b.example")
    assert s.ALLOWED_SIZES == (256, 512)


def test_default_values():
    s = make()
    assert s.ALLOWED_SIZES == (256, 384, 512, 640, 768)
    assert (s.NUM_STEPS, s.MAX_PROMPT_LEN, s.MAX_ACTIVE_TASKS_PER_USER) == (25, 500, 1)
    assert (s.MAX_QUEUED_TOTAL, s.MAX_HISTORY_PER_USER) == (30, 50)
    assert (s.PIPELINE, s.SCALER, s.COOKIE_SECURE) == ("real", "none", True)
    assert s.MACHINE_NAME
    assert (s.DB_POOL_SIZE, s.DB_POOL_TIMEOUT_S) == (5, 5)
