"""Shared test fixtures for Vibe Audit backend tests."""
import sys
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add daemon to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.models import Base, Session, Event, Baseline, Snapshot, Flag, Turn, Phase


@pytest.fixture
def db():
    """In-memory SQLite database for tests."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    yield session
    session.close()


@pytest.fixture
def sample_session(db):
    """Create a sample session in DB."""
    s = Session(cli_command="test:hook")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture
def sample_event(db, sample_session):
    """Create a sample tool_use event."""
    e = Event(
        session_id=sample_session.id,
        type="tool_use",
        payload={
            "tool": "Bash",
            "tool_name": "Bash",
            "inputs": {"command": "npm test"},
            "output": "All tests passed",
        },
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


@pytest.fixture
def sample_user_event(db, sample_session):
    """Create a sample user_message event."""
    e = Event(
        session_id=sample_session.id,
        type="user_message",
        payload={"content": "Add a login page with email and password"},
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return e
