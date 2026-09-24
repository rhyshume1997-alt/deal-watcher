import os

os.environ["DRY_RUN"] = "1"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["GMAIL_ADDRESS"] = "me@gmail.com"
os.environ.pop("GMAIL_APP_PASSWORD", None)
os.environ["TRACK_BASE_URL"] = "https://example.supabase.co/functions/v1/track"
os.environ["LINK_SECRET"] = "test-secret"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from watcher import db


@pytest.fixture
def engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.metadata.create_all(e)
    return e


@pytest.fixture
def conn(engine):
    with engine.begin() as c:
        yield c


@pytest.fixture
def sent(monkeypatch):
    """Capture emails instead of sending."""
    from watcher import emailer

    box = []
    monkeypatch.setattr(emailer, "send", lambda subject, html, text: box.append((subject, html, text)) or True)
    return box
