"""Test setup: a throwaway SQLite database, media folder and test credentials, set before the app is imported."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="tikiriiai-test-")
# The suite runs on a throwaway SQLite file. To run it on MySQL/MariaDB too, give TEST_DB_URL (the database name must
# end in "test": every table is wiped before each test, so it must never be the real database).
_db_url = os.environ.get("TEST_DB_URL") or f"sqlite:///{_tmp}/test.db"
if not _db_url.startswith("sqlite") and not _db_url.split("?")[0].rsplit("/", 1)[-1].endswith("test"):
    raise RuntimeError("TEST_DB_URL must name a database ending in 'test'; the tests delete everything in it")
os.environ.update({
    "DB_URL": _db_url,
    "MEDIA_DIR": f"{_tmp}/media",
    "RECORDINGS_DIR": f"{_tmp}/recordings",  # test calls are recorded too; never into the real recordings/ folder
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "test-password-123",
    "SESSION_SECRET": "test-secret",
    "GEMINI_API_KEY": "test-key",
    "GEMINI_TRANSCRIBE_MODEL": "",  # no transcript checks against the network after test calls
    "PBX_ENABLED": "false",
    "SMS_PROVIDER": "",  # a developer's real .env must never send an SMS from the tests
    "SMS_API_KEY": "",
    "WHATSAPP_TOKEN": "",
    "ASTERISK_SOUNDS_DIR": "",
    "LEAD_NOTIFY_PHONE": "",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from app import auth, models  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


@pytest.fixture(autouse=True)
def _messages_run_inline(monkeypatch):
    """The tools queue messages on a background thread; the tests want them done (and recorded) before they look."""
    from app import notify
    monkeypatch.setattr(notify, "queue_details", notify.send_details)
    monkeypatch.setattr(notify, "queue_lead_alert", notify.send_lead_alert)
    monkeypatch.setattr(notify, "queue_demo_confirm", notify.send_demo_confirm)


@pytest.fixture(scope="session")
def _app_started():
    with TestClient(app) as c:  # runs the lifespan: migrations + seed
        yield c


@pytest.fixture
def db(_app_started):
    """Fresh seed data (TIKIRII's product knowledge) for every test."""
    with SessionLocal() as s:
        for m in (models.Lead, models.SmsMessage, models.Call, models.FaqEntry, models.Integration, models.Industry,
                  models.Capability, models.AgentProfile):
            s.execute(delete(m))
        s.commit()
        seed_if_empty(s)
        yield s
    auth._failures.clear()


@pytest.fixture
def client(db, _app_started):
    _app_started.cookies.clear()
    return _app_started


@pytest.fixture
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    assert r.status_code == 200
    return client
