import pytest

from api.security import verify_password
from common import db
from scripts import create_admin

pytestmark = pytest.mark.api


def test_create_admin_creates_an_admin(migrated_db, monkeypatch):
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda prompt: "admin-password")
    assert create_admin.main(["Root@Example.com"]) == 0
    user = db.get_user_by_email("root@example.com")
    assert user.role == "admin"
    assert verify_password(user.password_hash, "admin-password")


def test_create_admin_promotes_an_existing_user_and_keeps_the_password(migrated_db, monkeypatch):
    user = db.create_user("someone@example.com", "unchanged-hash")
    monkeypatch.setattr(create_admin.getpass, "getpass", pytest.fail)
    assert create_admin.main(["someone@example.com"]) == 0
    promoted = db.get_user(user.id)
    assert (promoted.role, promoted.password_hash) == ("admin", "unchanged-hash")


def test_create_admin_refuses_a_bad_email(migrated_db):
    assert create_admin.main(["nonsense"]) == 1
