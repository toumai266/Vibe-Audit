"""Tests for REST API endpoints."""
import sys, os
import sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, Session as DBSession, Event, Baseline, Snapshot, Flag, Turn, Phase
from db.database import get_db


# --- Test App Setup ---

@pytest.fixture
def test_app():
    """Create a test FastAPI app with in-memory DB."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    # Import app after path setup
    from main import app
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)
    yield client, TestSessionLocal

    app.dependency_overrides.clear()


# --- Health ---

def test_health(test_app):
    client, _ = test_app
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --- Sessions ---

def test_list_sessions_empty(test_app):
    client, _ = test_app
    resp = client.get("/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["sessions"] == []


def test_create_event_creates_session(test_app):
    client, _ = test_app
    resp = client.post("/events", json={
        "type": "session_start",
        "payload": {"cli_command": "test", "user_prompt": "hello"},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "session_id" in data
    assert "event_id" in data

    # Session should now exist
    resp2 = client.get("/sessions")
    assert resp2.json()["total"] == 1


def test_create_event_with_existing_session(test_app):
    client, SessionLocal = test_app
    # Create session first
    db = SessionLocal()
    session = DBSession(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)
    sid = session.id
    db.close()

    resp = client.post("/events", json={
        "session_id": sid,
        "type": "tool_use",
        "payload": {"tool": "Bash", "inputs": {"command": "ls"}},
    })
    assert resp.status_code == 201
    assert resp.json()["session_id"] == sid


def test_get_session_detail(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test:detail")
    db.add(session)
    db.commit()
    db.refresh(session)
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == sid


def test_get_session_not_found(test_app):
    client, _ = test_app
    resp = client.get("/sessions/nonexistent-id")
    assert resp.status_code == 404


def test_delete_session(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test:delete")
    db.add(session)
    db.commit()
    db.refresh(session)
    sid = session.id
    db.close()

    resp = client.delete(f"/sessions/{sid}")
    assert resp.status_code == 200

    resp2 = client.get(f"/sessions/{sid}")
    assert resp2.status_code == 404


# --- Baseline ---

def test_get_baseline_none(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/baseline")
    assert resp.status_code == 200
    # No baseline yet


def test_get_baseline_exists(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)
    baseline = Baseline(
        session_id=session.id, user_request="build todo", goal="Build a todo app",
        must_have=["task list"], must_not=[], success_checks=["app runs"],
    )
    db.add(baseline)
    db.commit()
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/baseline")
    assert resp.status_code == 200
    data = resp.json()
    assert data["goal"] == "Build a todo app"


# --- Baseline Update ---

def test_update_baseline(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)
    baseline = Baseline(
        session_id=session.id, user_request="original", goal="Original goal",
        must_have=["item1"], must_not=["bad1"], success_checks=["check1"],
    )
    db.add(baseline)
    db.commit()
    sid = session.id
    db.close()

    resp = client.put(f"/sessions/{sid}/baseline", json={
        "goal": "Updated goal",
        "must_have": ["item1", "item2"],
        "must_not": [],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["goal"] == "Updated goal"
    assert len(data["must_have"]) == 2
    assert data["must_not"] == []


def test_update_baseline_not_found(test_app):
    client, _ = test_app
    resp = client.put("/sessions/nonexistent/baseline", json={"goal": "x"})
    assert resp.status_code == 404


def test_rebuild_baseline_creates_new_version(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test:rebuild")
    db.add(session)
    db.commit()
    db.refresh(session)
    baseline = Baseline(
        session_id=session.id,
        version=1,
        is_active=True,
        user_request="build todo app",
        goal="Original goal",
        must_have=["item1"],
        must_not=[],
        success_checks=["check1"],
    )
    db.add(baseline)
    db.commit()
    sid = session.id
    db.close()

    with patch("api.routes.rebuild_baseline_from_feedback", new_callable=AsyncMock) as mock_rebuild:
        mock_rebuild.return_value = {
            "goal": "Corrected goal",
            "must_have": ["item1", "item2"],
            "must_not": ["no rewrite"],
            "success_checks": ["tests pass"],
            "confidence": 0.88,
            "ambiguity_notes": None,
        }
        resp = client.post(f"/sessions/{sid}/baseline/rebuild", json={
            "reason": "Current baseline misses the real intent",
            "hint": "Keep it outcome-oriented",
            "expected_active_version": 1,
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["goal"] == "Corrected goal"
    assert data["version"] == 2
    assert data["is_active"] is True

    db = SessionLocal()
    all_baselines = db.query(Baseline).filter(Baseline.session_id == sid).order_by(Baseline.version.asc()).all()
    assert len(all_baselines) == 2
    assert all_baselines[0].version == 1
    assert all_baselines[0].is_active is False
    assert all_baselines[1].version == 2
    assert all_baselines[1].is_active is True
    db.close()


def test_rebuild_baseline_conflict_on_expected_version(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test:rebuild-conflict")
    db.add(session)
    db.commit()
    db.refresh(session)
    baseline = Baseline(
        session_id=session.id,
        version=3,
        is_active=True,
        user_request="request",
        goal="Goal v3",
        must_have=["x"],
        must_not=[],
        success_checks=["ok"],
    )
    db.add(baseline)
    db.commit()
    sid = session.id
    db.close()

    resp = client.post(f"/sessions/{sid}/baseline/rebuild", json={
        "reason": "wrong baseline",
        "expected_active_version": 2,
    })
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["current_active_version"] == 3

# --- Flags ---

def test_get_flags_empty(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/flags")
    assert resp.status_code == 200
    assert resp.json()["flags"] == []


# --- Snapshots ---

def test_get_snapshots_empty(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/snapshots")
    assert resp.status_code == 200
    assert resp.json()["snapshots"] == []


def test_get_snapshots_normalizes_alignment_aliases(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()

    session = DBSession(cli_command="test:snapshots:normalize")
    db.add(session)
    db.commit()
    db.refresh(session)

    event = Event(session_id=session.id, type="assistant_response", payload={"content": "ok"})
    db.add(event)
    db.commit()
    db.refresh(event)

    snapshot = Snapshot(
        event_id=event.id,
        session_id=session.id,
        current_goal="goal",
        current_plan=[],
        current_tasks=[],
        baseline_status="status",
        alignment="정렬됨",
        drift_score=0.0,
    )
    db.add(snapshot)
    db.commit()
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["snapshots"]) == 1
    assert data["snapshots"][0]["alignment"] == "aligned"


def test_get_snapshots_includes_evidence(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()

    session = DBSession(cli_command="test:snapshots:evidence")
    db.add(session)
    db.commit()
    db.refresh(session)

    event = Event(session_id=session.id, type="assistant_response", payload={"content": "ok"})
    db.add(event)
    db.commit()
    db.refresh(event)

    snapshot = Snapshot(
        event_id=event.id,
        session_id=session.id,
        current_goal="goal",
        current_plan=[],
        current_tasks=[],
        baseline_status="status",
        alignment="aligned",
        drift_score=0.0,
        evidence={"commands_run": ["npm test"], "tool_count": 1},
    )
    db.add(snapshot)
    db.commit()
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["snapshots"]) == 1
    assert data["snapshots"][0]["evidence"]["tool_count"] == 1
    assert data["snapshots"][0]["evidence"]["commands_run"] == ["npm test"]


def test_api_turn_update_reuses_assistant_response_event(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test:turn:update")
    db.add(session)
    db.commit()
    db.refresh(session)
    sid = session.id
    db.close()

    # First assistant response: creates user/assistant turns + assistant_response event
    resp1 = client.post("/api/turn", json={
        "session_id": sid,
        "turn_key": "turn-1",
        "user_message": "check files",
        "assistant_response": "I will inspect the folder."
    })
    assert resp1.status_code == 200

    # Second response on same turn_key: updates assistant turn + reuses assistant_response event
    resp2 = client.post("/api/turn", json={
        "session_id": sid,
        "turn_key": "turn-1",
        "assistant_response": "Folder inspected. Here is the full result."
    })
    assert resp2.status_code == 200
    assert resp2.json()["action"] == "updated"

    db = SessionLocal()
    assistant_turns = db.query(Turn).filter(
        Turn.session_id == sid,
        Turn.turn_key == "turn-1",
        Turn.role == "assistant"
    ).all()
    assert len(assistant_turns) == 1
    assert assistant_turns[0].content == "Folder inspected. Here is the full result."

    asst_events = db.query(Event).filter(
        Event.session_id == sid,
        Event.type == "assistant_response",
    ).all()
    assert len(asst_events) == 1
    payload = asst_events[0].payload or {}
    assert payload.get("content") == "Folder inspected. Here is the full result."
    assert payload.get("turn_key") == "turn-1"
    assert payload.get("update") is True

    ev_types = db.query(Event.type).filter(Event.session_id == sid).all()
    ev_type_list = [t[0] for t in ev_types]
    assert "assistant_response" in ev_type_list
    assert "assistant_update" not in ev_type_list
    db.close()


# --- Export ---

def test_export_json(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test:export")
    db.add(session)
    db.commit()
    db.refresh(session)
    baseline = Baseline(
        session_id=session.id,
        version=1,
        is_active=True,
        user_request="Build backup",
        goal="Backup all fields",
        must_have=["json"],
        must_not=[],
        success_checks=["all exported"],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)
    phase = Phase(
        session_id=session.id,
        phase_num=1,
        baseline_id=baseline.id,
        baseline_version=baseline.version,
        classification="initial",
        user_message="start",
        summary="initial phase",
    )
    db.add(phase)
    db.commit()
    db.refresh(phase)
    event = Event(session_id=session.id, type="user_message", payload={"content": "hello"})
    db.add(event)
    db.commit()
    db.refresh(event)
    snapshot = Snapshot(
        event_id=event.id,
        session_id=session.id,
        phase_id=phase.id,
        baseline_id=baseline.id,
        current_goal="backup",
        current_plan=["collect"],
        current_tasks=[{"text": "collect", "status": "done"}],
        evidence={"k": "v"},
        alignment="aligned",
        drift_score=0.0,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    flag = Flag(
        snapshot_id=snapshot.id,
        session_id=session.id,
        event_id=event.id,
        severity="low",
        category="scope_bloat",
        title="sample",
        rationale="sample",
    )
    db.add(flag)
    db.commit()
    db.refresh(flag)
    turn = Turn(
        session_id=session.id,
        role="user",
        content="hello",
        sealed=True,
        alignment="unknown",
    )
    db.add(turn)
    db.commit()
    sid = session.id
    phase_id = phase.id
    db.close()

    resp = client.get(f"/sessions/{sid}/export?format=json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == sid
    assert data["schema_version"] == "session_backup.v2"
    assert "summary" in data
    assert "raw" in data
    assert "phases" in data
    assert len(data["phases"]) == 1
    phase_block = data["phases"][0]
    assert phase_block["phase"]["id"] == phase_id
    assert len(phase_block["snapshots"]) == 1
    assert len(phase_block["flags"]) == 1
    assert len(phase_block["events"]) == 1
    assert len(phase_block["turns"]) == 1


def test_export_markdown(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test:export")
    db.add(session)
    db.commit()
    db.refresh(session)
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/export?format=markdown")
    assert resp.status_code == 200
    data = resp.json()
    assert data["format"] == "markdown"
    assert "Session Report" in data["content"]


def test_db_backup_download_unsupported_for_memory_db(test_app, monkeypatch):
    client, _ = test_app
    from api import routes
    monkeypatch.setattr(routes, "_get_sqlite_db_file_path", lambda: None)
    resp = client.get("/backup/db")
    assert resp.status_code == 400
    assert "file-based SQLite" in resp.json()["detail"]


def test_db_backup_upload_unsupported_for_memory_db(test_app, monkeypatch):
    client, _ = test_app
    from api import routes
    monkeypatch.setattr(routes, "_get_sqlite_db_file_path", lambda: None)
    resp = client.post(
        "/backup/db",
        content=b"dummy",
        headers={"Content-Type": "application/octet-stream", "X-Backup-Filename": "backup.db"},
    )
    assert resp.status_code == 400
    assert "file-based SQLite" in resp.json()["detail"]


def test_db_backup_state_unsupported_for_memory_db(test_app, monkeypatch):
    client, _ = test_app
    from api import routes
    monkeypatch.setattr(routes, "_get_sqlite_db_file_path", lambda: None)
    resp = client.get("/backup/db/state")
    assert resp.status_code == 400
    assert "file-based SQLite" in resp.json()["detail"]


def test_db_backup_archives_delete_unsupported_for_memory_db(test_app, monkeypatch):
    client, _ = test_app
    from api import routes
    monkeypatch.setattr(routes, "_get_sqlite_db_file_path", lambda: None)
    resp = client.delete("/backup/db/archives")
    assert resp.status_code == 400
    assert "file-based SQLite" in resp.json()["detail"]


def test_db_backup_state_returns_counts(test_app, monkeypatch, tmp_path):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = DBSession(cli_command="test:db-state")
    db.add(session)
    db.commit()
    db.refresh(session)
    db.close()

    db_file = tmp_path / "state.db"
    db_file.write_bytes(b"SQLite format 3\x00dummy")
    from api import routes
    monkeypatch.setattr(routes, "_get_sqlite_db_file_path", lambda: str(db_file))

    resp = client.get("/backup/db/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"]["sessions"] == 1
    assert data["file"]["name"] == "state.db"
    assert data["file"]["size_bytes"] > 0


def test_db_backup_state_reports_backup_archives(test_app, monkeypatch, tmp_path):
    client, _ = test_app
    db_file = tmp_path / "state.db"
    db_file.write_bytes(b"SQLite format 3\x00dummy")
    backup_1 = tmp_path / "state.db.bak-20260215-010101"
    backup_2 = tmp_path / "state.db.bak-20260215-010102"
    backup_1.write_bytes(b"b1")
    backup_2.write_bytes(b"b22")
    from api import routes
    monkeypatch.setattr(routes, "_get_sqlite_db_file_path", lambda: str(db_file))

    resp = client.get("/backup/db/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["backup_archives"]["count"] == 2
    assert data["backup_archives"]["total_size_bytes"] == 5
    assert len(data["backup_archives"]["items"]) == 2


def test_db_backup_archives_delete_removes_files(test_app, monkeypatch, tmp_path):
    client, _ = test_app
    db_file = tmp_path / "state.db"
    db_file.write_bytes(b"SQLite format 3\x00dummy")
    backup_1 = tmp_path / "state.db.bak-20260215-010101"
    backup_2 = tmp_path / "state.db.bak-20260215-010102"
    backup_1.write_bytes(b"a")
    backup_2.write_bytes(b"bb")
    from api import routes
    monkeypatch.setattr(routes, "_get_sqlite_db_file_path", lambda: str(db_file))

    resp = client.delete("/backup/db/archives")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["deleted_count"] == 2
    assert data["deleted_bytes"] == 3
    assert data["failed"] == []
    assert not backup_1.exists()
    assert not backup_2.exists()


def test_cleanup_sqlite_orphans_removes_stale_rows(tmp_path):
    from api.routes import _cleanup_sqlite_orphans

    db_path = tmp_path / "orphan.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE events (id TEXT PRIMARY KEY, session_id TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE snapshots ("
        "id TEXT PRIMARY KEY, "
        "event_id TEXT NOT NULL, "
        "session_id TEXT NOT NULL, "
        "current_goal TEXT NOT NULL, "
        "ts TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE flags ("
        "id TEXT PRIMARY KEY, "
        "snapshot_id TEXT, "
        "session_id TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO snapshots (id, event_id, session_id, current_goal, ts) "
        "VALUES ('snap-1', 'evt-missing', 'sess-missing', 'orphan', '2026-02-13T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO flags (id, snapshot_id, session_id) "
        "VALUES ('flag-1', 'snap-1', 'sess-missing')"
    )
    conn.commit()
    conn.close()

    removed = _cleanup_sqlite_orphans(str(db_path))
    assert removed >= 1

    check = sqlite3.connect(db_path)
    snapshot_count = check.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    flag_count = check.execute("SELECT COUNT(*) FROM flags").fetchone()[0]
    check.close()
    assert snapshot_count == 0
    assert flag_count == 0


def test_db_backup_download_includes_wal_commits(test_app, monkeypatch, tmp_path):
    client, _ = test_app
    from api import routes

    source_db = tmp_path / "source.db"
    conn = sqlite3.connect(source_db)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT);")
    conn.execute("INSERT INTO t(value) VALUES ('keep'), ('remove');")
    conn.commit()
    conn.execute("DELETE FROM t WHERE value='remove';")
    conn.commit()

    monkeypatch.setattr(routes, "_get_sqlite_db_file_path", lambda: str(source_db))
    resp = client.get("/backup/db")
    assert resp.status_code == 200

    downloaded = tmp_path / "downloaded.db"
    downloaded.write_bytes(resp.content)

    with sqlite3.connect(downloaded) as restored:
        count = restored.execute("SELECT COUNT(*) FROM t;").fetchone()[0]
        value = restored.execute("SELECT value FROM t LIMIT 1;").fetchone()[0]
    assert count == 1
    assert value == "keep"

    conn.close()


# --- Settings ---

def test_get_settings(test_app):
    client, _ = test_app
    resp = client.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "response_language" in data
    assert "interface_language" in data
    assert "time_zone" in data
    assert "has_anthropic_key" in data


def test_get_settings_models(test_app):
    client, _ = test_app
    resp = client.get("/settings/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["default_model"] == "openai/gpt-5-nano"
    assert isinstance(data.get("models"), list)
    assert any(m.get("value") == "openai/gpt-5.2" for m in data["models"])
    assert any(m.get("value") == "gemini/gemini-3-pro-preview" for m in data["models"])
    assert any(m.get("value") == "gemini/gemini-3-flash-preview" for m in data["models"])
    assert any(m.get("value") == "openai/gpt-5" for m in data["models"])
    assert any(m.get("value") == "anthropic/claude-sonnet-4-5" for m in data["models"])


# --- Delete all ---

def test_delete_all_sessions(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    for i in range(3):
        db.add(DBSession(cli_command=f"test:{i}"))
    db.commit()
    db.close()

    resp = client.delete("/sessions/all")
    assert resp.status_code == 200

    resp2 = client.get("/sessions")
    assert resp2.json()["total"] == 0
