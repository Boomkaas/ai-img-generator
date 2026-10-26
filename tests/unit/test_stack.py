from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

STACK = yaml.safe_load((Path(__file__).parents[2] / "infra" / "stack.yml").read_text())
APP_SERVICES = ("migrate", "api", "worker", "controller")
SETTINGS_FROM_SECRETS = {"DATABASE_URL", "BROKER_URL", "REDIS_URL", "S3_SECRET_KEY", "JWT_SECRET"}


def secret_refs(service: str) -> list[dict]:
    refs = STACK["services"][service].get("secrets", [])
    return [ref if isinstance(ref, dict) else {"source": ref, "target": ref} for ref in refs]


@pytest.mark.parametrize("service", sorted(STACK["services"]))
def test_no_secret_is_mounted_twice_in_one_service(service):
    # Swarm keeps only one mount per source and drops the rest without a word, so the setting behind the
    # dropped one would fall back to its localhost default.
    sources = [ref["source"] for ref in secret_refs(service)]
    assert len(sources) == len(set(sources))


@pytest.mark.parametrize("service", APP_SERVICES)
def test_app_services_get_every_setting_kept_in_a_secret(service):
    assert {ref["target"] for ref in secret_refs(service)} >= SETTINGS_FROM_SECRETS
    assert not SETTINGS_FROM_SECRETS & set(STACK["services"][service].get("environment", {}))


def test_every_secret_a_service_uses_is_declared():
    used = {ref["source"] for service in STACK["services"] for ref in secret_refs(service)}
    assert used <= set(STACK["secrets"])


def test_backups_run_next_to_postgres_and_keep_a_week():
    backup = STACK["services"]["backup"]
    # pg_dump refuses a server newer than itself, so the backup uses the database's own image.
    assert backup["image"] == STACK["services"]["postgres"]["image"]
    assert backup["deploy"]["placement"] == STACK["services"]["postgres"]["deploy"]["placement"]
    assert backup["environment"]["BACKUP_KEEP"] == "7"
    assert "pg_dump -Fc" in backup["command"][-1]
    assert "backups:/backups" in backup["volumes"]
    assert "backups" in STACK["volumes"]
