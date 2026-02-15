from datetime import datetime
from copy import deepcopy
from typing import List, Optional
import asyncio
import os
import shutil
import sqlite3
import tempfile
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession, sessionmaker
from sqlalchemy.exc import IntegrityError

from db import get_db, Session, Event, Baseline, Snapshot, Flag, Turn, Phase
from core.processor import process_event
from services.llm_service import generate_report, rebuild_baseline_from_feedback

router = APIRouter()
_session_processing_locks: dict[str, asyncio.Lock] = {}
_session_processing_locks_guard = asyncio.Lock()

ALIGNMENT_CANONICAL_MAP = {
    "aligned": "aligned",
    "align": "aligned",
    "on_track": "aligned",
    "ontrack": "aligned",
    "drifting": "drifting",
    "drifted": "drifting",
    "drift": "drifting",
    "off_track": "off_track",
    "offtrack": "off_track",
    "일치": "aligned",
    "정렬됨": "aligned",
    "정렬": "aligned",
    "표류": "drifting",
    "이탈": "off_track",
    "벗어남": "off_track",
    "탈선": "off_track",
}


def _normalize_alignment(alignment: Optional[str]) -> str:
    if not alignment:
        return "unknown"
    raw = str(alignment).strip().lower()
    key = raw.replace("-", "_").replace(" ", "_")
    mapped = ALIGNMENT_CANONICAL_MAP.get(key)
    if mapped:
        return mapped
    # Fallback for variant strings from legacy snapshots.
    if any(token in raw for token in ("aligned", "align", "on track", "일치", "정렬")):
        return "aligned"
    if any(token in raw for token in ("drifting", "drift", "표류")):
        return "drifting"
    if any(token in raw for token in ("off track", "off_track", "offtrack", "이탈", "벗어", "탈선")):
        return "off_track"
    return "unknown"


# ============ Pydantic Models ============

class EventCreate(BaseModel):
    session_id: Optional[str] = None
    type: str
    ts: Optional[datetime] = None
    payload: dict = {}


class EventResponse(BaseModel):
    event_id: str
    session_id: str


class SessionSummary(BaseModel):
    id: str
    start_ts: datetime
    end_ts: Optional[datetime]
    verdict: str
    flag_count: int
    cli_command: Optional[str] = None
    baseline_goal: Optional[str] = None
    latest_phase: Optional[str] = None


class BaselineResponse(BaseModel):
    session_id: str
    user_request: str
    goal: str
    must_have: List[str]
    must_not: List[str]
    success_checks: List[str]
    # Versioning fields
    id: Optional[str] = None
    version: Optional[int] = None
    is_active: Optional[bool] = None
    total_versions: Optional[int] = None
    # Confidence fields
    confidence: Optional[float] = None
    ambiguity_notes: Optional[str] = None
    # Structured context
    context_messages: Optional[list] = None
    context_summary: Optional[str] = None


class PhaseResponse(BaseModel):
    id: str
    phase_num: int
    classification: str
    baseline_version: int
    user_message: Optional[str]
    summary: Optional[str]
    confidence: Optional[float]
    started_at: datetime
    snapshot_count: Optional[int] = 0


class SnapshotResponse(BaseModel):
    id: str
    ts: datetime
    current_goal: str
    current_plan: List[str]
    current_tasks: List[dict]
    evidence: dict = Field(default_factory=dict)
    baseline_status: Optional[str] = ""
    suggested_prompt: Optional[str] = None
    alignment: str
    drift_score: float
    phase_id: Optional[str] = None
    baseline_id: Optional[str] = None


class FlagResponse(BaseModel):
    id: str
    ts: datetime
    severity: str
    category: str
    title: str
    rationale: str
    evidence: dict
    jump: dict


class TurnCreate(BaseModel):
    session_id: Optional[str] = None
    turn_key: Optional[str] = None
    timestamp: Optional[str] = None
    user_message: Optional[str] = None
    assistant_response: Optional[str] = None
    cli_command: Optional[str] = None


class TurnResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    ts: datetime
    alignment: str


# ============ Background Task ============

def _make_background_session_factory(db: DBSession):
    """
    Build a SessionLocal-compatible factory bound to the same engine used by
    the current request session.
    """
    bind = db.get_bind()
    engine = bind.engine if hasattr(bind, "engine") else bind
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


async def _acquire_session_processing_lock(session_id: str) -> asyncio.Lock:
    """Serialize background pipeline processing per session to avoid DB races."""
    async with _session_processing_locks_guard:
        lock = _session_processing_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_processing_locks[session_id] = lock
    await lock.acquire()
    return lock


async def process_event_background(session_id: str, event_id: str, db_session_factory=None):
    """Background task to process event with LLM calls (uses own DB session)"""
    from db.database import SessionLocal
    from core.processor import _log_error
    from api.sse import push_event

    session_factory = db_session_factory or SessionLocal
    session_lock = await _acquire_session_processing_lock(session_id)
    evt = None

    try:
        # Only primary assistant_response events should drive UI processing overlays.
        # Non-assistant events and assistant_response update replays still run analysis
        # but skip start/complete SSE to avoid overlay flicker.
        db_check = session_factory()
        try:
            evt = db_check.query(Event).filter(Event.id == event_id).first()
            evt_payload = evt.payload if evt and isinstance(evt.payload, dict) else {}
            is_primary_assistant_event = bool(evt) and evt.type == "assistant_response" and not evt_payload.get("update")
            skip_sse = not is_primary_assistant_event
        finally:
            db_check.close()

        # Notify frontend: processing started
        evt_type = evt.type if evt else "unknown"
        if not skip_sse:
            print(f"[SSE] processing_start for {evt_type} event {event_id[:8]}")
            try:
                await push_event(session_id, "processing_start", {"event_id": event_id})
            except Exception:
                pass
        else:
            print(f"[SSE] skipped for {evt_type} event {event_id[:8]}")

        db = session_factory()
        try:
            event = db.query(Event).filter(Event.id == event_id).first()
            if event:
                await process_event(db, session_id, event)
            else:
                _log_error("background", f"Event {event_id} not found in DB")
        except Exception as e:
            _log_error("background", f"Error processing event {event_id}: {e}")
            import traceback
            traceback.print_exc()
            # Notify frontend: processing failed
            if not skip_sse:
                try:
                    await push_event(session_id, "processing_error", {"event_id": event_id, "error": str(e)})
                except Exception:
                    pass
        finally:
            db.close()

        # Notify frontend: processing complete
        if not skip_sse:
            print(f"[SSE] processing_complete for {evt_type} event {event_id[:8]}")
            try:
                await push_event(session_id, "processing_complete", {"event_id": event_id})
            except Exception:
                pass
    finally:
        session_lock.release()


# ============ Routes ============

@router.post("/events", response_model=EventResponse, status_code=201)
async def create_event(
    event_data: EventCreate,
    background_tasks: BackgroundTasks,
    db: DBSession = Depends(get_db)
):
    """Receive event from hooks"""
    # Create or get session
    session_id = event_data.session_id
    
    if event_data.type == "session_start" and not session_id:
        # Create new session only when no session_id provided
        session = Session(
            cli_command=event_data.payload.get("cli_command", "")
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id
    elif session_id:
        # Use existing session
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            # Session ID provided but not found - create with that ID
            session = Session(id=session_id, cli_command=event_data.payload.get("cli_command", ""))
            db.add(session)
            db.commit()
    else:
        # No session_id, not session_start - create new session
        session = Session(cli_command=event_data.payload.get("cli_command", ""))
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    # Create event
    event = Event(
        session_id=session_id,
        ts=event_data.ts or datetime.utcnow(),
        type=event_data.type,
        payload=event_data.payload
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    # Process event in background (LLM calls)
    background_tasks.add_task(
        process_event_background,
        session_id,
        event.id,
        _make_background_session_factory(db)
    )

    return EventResponse(event_id=event.id, session_id=session_id)


@router.get("/sessions", response_model=dict)
def list_sessions(
    limit: int = 20,
    offset: int = 0,
    db: DBSession = Depends(get_db)
):
    """List all sessions"""
    total = db.query(Session).count()
    sessions = db.query(Session).order_by(Session.start_ts.desc()).offset(offset).limit(limit).all()
    
    result = []
    for s in sessions:
        flag_count = db.query(Flag).filter(Flag.session_id == s.id).count()
        active_baseline = db.query(Baseline).filter(
            Baseline.session_id == s.id, Baseline.is_active == True
        ).first()
        if not active_baseline:
            active_baseline = db.query(Baseline).filter(
                Baseline.session_id == s.id
            ).order_by(Baseline.version.desc()).first()
        latest_phase = db.query(Phase).filter(
            Phase.session_id == s.id
        ).order_by(Phase.phase_num.desc()).first()
        result.append(SessionSummary(
            id=s.id,
            start_ts=s.start_ts,
            end_ts=s.end_ts,
            verdict=s.verdict,
            flag_count=flag_count,
            cli_command=s.cli_command,
            baseline_goal=active_baseline.goal if active_baseline else None,
            latest_phase=latest_phase.classification if latest_phase else None
        ))
    
    return {"sessions": result, "total": total}


@router.delete("/sessions/all")
async def delete_all_sessions(db: DBSession = Depends(get_db)):
    """Delete all sessions (for testing)"""
    session_ids = [row[0] for row in db.query(Session.id).all()]
    held_locks = []
    try:
        for sid in sorted(set(session_ids)):
            held_locks.append(await _acquire_session_processing_lock(sid))

        db.query(Flag).delete()
        db.query(Snapshot).delete()
        db.query(Phase).delete()
        db.query(Baseline).delete()
        db.query(Event).delete()
        db.query(Turn).delete()
        db.query(Session).delete()
        db.commit()

        db_path = _get_sqlite_db_file_path()
        if db_path and os.path.exists(db_path):
            _cleanup_sqlite_orphans(db_path)
        return {"status": "deleted", "message": "All sessions cleared"}
    finally:
        for lock in reversed(held_locks):
            lock.release()


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, db: DBSession = Depends(get_db)):
    """Delete a single session"""
    session_lock = await _acquire_session_processing_lock(session_id)
    try:
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        db.query(Flag).filter(Flag.session_id == session_id).delete()
        db.query(Snapshot).filter(Snapshot.session_id == session_id).delete()
        db.query(Phase).filter(Phase.session_id == session_id).delete()
        db.query(Turn).filter(Turn.session_id == session_id).delete()
        db.query(Baseline).filter(Baseline.session_id == session_id).delete()
        db.query(Event).filter(Event.session_id == session_id).delete()
        db.delete(session)
        db.commit()

        db_path = _get_sqlite_db_file_path()
        if db_path and os.path.exists(db_path):
            _cleanup_sqlite_orphans(db_path)

        return {"status": "deleted", "session_id": session_id}
    finally:
        session_lock.release()


@router.get("/projects/recent")
def get_recent_projects():
    """Get recent projects from Claude/Gemini config directories"""
    import os
    from pathlib import Path
    
    projects = []
    seen = set()
    
    # Claude projects
    claude_projects_dir = Path.home() / ".claude" / "projects"
    if claude_projects_dir.exists():
        for d in claude_projects_dir.iterdir():
            if d.is_dir():
                # Parse directory name to get actual path
                # Format: -Users-name-path-to-project
                name = d.name
                if name.startswith("-"):
                    path = name.replace("-", "/")
                    if os.path.exists(path):
                        if path not in seen:
                            projects.append({"path": path, "source": "claude"})
                            seen.add(path)
    
    # Gemini projects (check recent dirs)
    gemini_dir = Path.home() / ".gemini"
    if gemini_dir.exists():
        for item in gemini_dir.iterdir():
            if item.is_dir() and not item.name.startswith("."):
                path = str(item)
                if path not in seen:
                    projects.append({"path": path, "source": "gemini"})
                    seen.add(path)
    
    # Also add some known project directories
    known_dirs = [
        Path.home() / "Desktop" / "Study" / "00_PROJ",
        Path.home() / "projects",
        Path.home() / "code"
    ]
    
    for parent in known_dirs:
        if parent.exists():
            for d in parent.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    path = str(d)
                    if path not in seen:
                        projects.append({"path": path, "source": "local"})
                        seen.add(path)
    
    return {"projects": projects[:20]}  # Limit to 20


@router.get("/sessions/{session_id}")
def get_session(session_id: str, db: DBSession = Depends(get_db)):
    """Get session detail"""
    session = db.query(Session).filter(Session.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    event_count = db.query(Event).filter(Event.session_id == session_id).count()
    flag_count = db.query(Flag).filter(Flag.session_id == session_id).count()
    baseline = db.query(Baseline).filter(Baseline.session_id == session_id).first()
    latest_snapshot = db.query(Snapshot).filter(
        Snapshot.session_id == session_id
    ).order_by(Snapshot.ts.desc()).first()

    latest_snapshot_payload = None
    if latest_snapshot:
        latest_snapshot_payload = {k: v for k, v in latest_snapshot.__dict__.items() if k != "_sa_instance_state"}
        latest_snapshot_payload["alignment"] = _normalize_alignment(latest_snapshot_payload.get("alignment"))

    return {
        "id": session.id,
        "start_ts": session.start_ts,
        "end_ts": session.end_ts,
        "verdict": session.verdict,
        "event_count": event_count,
        "flag_count": flag_count,
        "baseline": baseline.__dict__ if baseline else None,
        "latest_snapshot": latest_snapshot_payload
    }


def _sanitize(obj):
    """Remove surrogate characters that break JSON serialization."""
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


@router.get("/sessions/{session_id}/events")
def get_session_events(session_id: str, db: DBSession = Depends(get_db)):
    """Get all events for a session"""
    events = db.query(Event).filter(
        Event.session_id == session_id
    ).order_by(Event.ts.asc()).all()

    return {"events": [{"id": e.id, "ts": e.ts, "type": e.type, "payload": _sanitize(e.payload)} for e in events]}


@router.get("/sessions/{session_id}/baseline", response_model=Optional[BaselineResponse])
def get_session_baseline(session_id: str, db: DBSession = Depends(get_db)):
    """Get active baseline for a session"""
    baseline = db.query(Baseline).filter(
        Baseline.session_id == session_id,
        Baseline.is_active == True
    ).first()
    
    # Fallback for old data without is_active
    if not baseline:
        baseline = db.query(Baseline).filter(Baseline.session_id == session_id).first()
    
    if not baseline:
        return None
    
    total_versions = db.query(Baseline).filter(Baseline.session_id == session_id).count()
    
    return BaselineResponse(
        id=baseline.id,
        session_id=baseline.session_id,
        user_request=baseline.user_request,
        goal=baseline.goal,
        must_have=baseline.must_have,
        must_not=baseline.must_not,
        success_checks=baseline.success_checks,
        version=baseline.version,
        is_active=baseline.is_active,
        total_versions=total_versions,
        confidence=baseline.confidence,
        ambiguity_notes=baseline.ambiguity_notes,
        context_messages=baseline.context_messages,
        context_summary=baseline.context_summary
    )


@router.get("/sessions/{session_id}/baselines")
def get_all_baselines(session_id: str, db: DBSession = Depends(get_db)):
    """Get all baseline versions for a session"""
    baselines = db.query(Baseline).filter(
        Baseline.session_id == session_id
    ).order_by(Baseline.version.asc()).all()
    
    active_version = None
    result = []
    for b in baselines:
        if b.is_active:
            active_version = b.version
        result.append({
            "id": b.id,
            "version": b.version,
            "is_active": b.is_active,
            "goal": b.goal,
            "created_at": b.created_at,
            "archived_at": b.archived_at
        })
    
    return {"baselines": result, "active_version": active_version, "total": len(baselines)}


@router.get("/sessions/{session_id}/baseline/{version}")
def get_baseline_version(session_id: str, version: int, db: DBSession = Depends(get_db)):
    """Get specific baseline version"""
    baseline = db.query(Baseline).filter(
        Baseline.session_id == session_id,
        Baseline.version == version
    ).first()
    
    if not baseline:
        raise HTTPException(status_code=404, detail=f"Baseline version {version} not found")
    
    total_versions = db.query(Baseline).filter(Baseline.session_id == session_id).count()
    
    return BaselineResponse(
        id=baseline.id,
        session_id=baseline.session_id,
        user_request=baseline.user_request,
        goal=baseline.goal,
        must_have=baseline.must_have,
        must_not=baseline.must_not,
        success_checks=baseline.success_checks,
        version=baseline.version,
        is_active=baseline.is_active,
        total_versions=total_versions,
        confidence=baseline.confidence,
        ambiguity_notes=baseline.ambiguity_notes,
        context_messages=baseline.context_messages,
        context_summary=baseline.context_summary
    )


@router.get("/sessions/{session_id}/phases")
def get_session_phases(session_id: str, db: DBSession = Depends(get_db)):
    """Get all phases for a session with snapshot counts"""
    phases = db.query(Phase).filter(
        Phase.session_id == session_id
    ).order_by(Phase.phase_num.asc()).all()
    
    result = []
    for p in phases:
        snapshot_count = db.query(Snapshot).filter(Snapshot.phase_id == p.id).count()
        result.append(PhaseResponse(
            id=p.id,
            phase_num=p.phase_num,
            classification=p.classification,
            baseline_version=p.baseline_version,
            user_message=p.user_message,
            summary=p.summary,
            confidence=p.confidence,
            started_at=p.started_at,
            snapshot_count=snapshot_count
        ))
    
    return {"phases": result, "total": len(phases)}


@router.get("/sessions/{session_id}/snapshots")
def get_session_snapshots(session_id: str, db: DBSession = Depends(get_db)):
    """Get all snapshots for a session"""
    snapshots = db.query(Snapshot).filter(
        Snapshot.session_id == session_id
    ).order_by(Snapshot.ts.asc()).all()
    
    return {"snapshots": [SnapshotResponse(
        id=s.id,
        ts=s.ts,
        current_goal=s.current_goal,
        current_plan=s.current_plan,
        current_tasks=s.current_tasks,
        evidence=s.evidence or {},
        baseline_status=getattr(s, 'baseline_status', '') or '',
        suggested_prompt=getattr(s, 'suggested_prompt', None),
        alignment=_normalize_alignment(s.alignment),
        drift_score=s.drift_score,
        phase_id=s.phase_id,
        baseline_id=s.baseline_id
    ) for s in snapshots]}


@router.get("/sessions/{session_id}/flags")
def get_session_flags(session_id: str, db: DBSession = Depends(get_db)):
    """Get all flags for a session"""
    flags = db.query(Flag).filter(
        Flag.session_id == session_id
    ).order_by(Flag.ts.asc()).all()
    
    return {"flags": [FlagResponse(
        id=f.id,
        ts=f.ts,
        severity=f.severity,
        category=f.category,
        title=f.title,
        rationale=f.rationale,
        evidence=f.evidence,
        jump=f.jump
    ) for f in flags]}


@router.get("/sessions/{session_id}/report")
async def get_session_report(session_id: str, db: DBSession = Depends(get_db)):
    """Generate final report for a session"""
    baseline = db.query(Baseline).filter(Baseline.session_id == session_id).first()
    if not baseline:
        raise HTTPException(status_code=404, detail="Baseline not found")
    
    snapshots = db.query(Snapshot).filter(Snapshot.session_id == session_id).all()
    flags = db.query(Flag).filter(Flag.session_id == session_id).all()
    
    baseline_dict = {
        "goal": baseline.goal,
        "must_have": baseline.must_have,
        "must_not": baseline.must_not,
        "success_checks": baseline.success_checks
    }
    
    snapshots_list = [{"current_tasks": s.current_tasks} for s in snapshots]
    flags_list = [{"severity": f.severity, "title": f.title} for f in flags]
    
    report = await generate_report(baseline_dict, snapshots_list, flags_list)
    report["session_id"] = session_id
    
    return report


class BaselineUpdate(BaseModel):
    goal: Optional[str] = None
    must_have: Optional[List[str]] = None
    must_not: Optional[List[str]] = None
    success_checks: Optional[List[str]] = None


class BaselineRebuildRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=2000)
    hint: Optional[str] = Field(default=None, max_length=2000)
    expected_active_version: Optional[int] = None


@router.put("/sessions/{session_id}/baseline")
def update_baseline(session_id: str, data: BaselineUpdate, db: DBSession = Depends(get_db)):
    """Update active baseline (manual edit by user)"""
    baseline = db.query(Baseline).filter(
        Baseline.session_id == session_id,
        Baseline.is_active == True
    ).first()
    
    # Fallback for old data
    if not baseline:
        baseline = db.query(Baseline).filter(Baseline.session_id == session_id).first()
    
    if not baseline:
        raise HTTPException(status_code=404, detail="Baseline not found")

    if data.goal is not None:
        baseline.goal = data.goal
    if data.must_have is not None:
        baseline.must_have = data.must_have
    if data.must_not is not None:
        baseline.must_not = data.must_not
    if data.success_checks is not None:
        baseline.success_checks = data.success_checks

    db.commit()
    
    total_versions = db.query(Baseline).filter(Baseline.session_id == session_id).count()
    
    return BaselineResponse(
        id=baseline.id,
        session_id=baseline.session_id,
        user_request=baseline.user_request,
        goal=baseline.goal,
        must_have=baseline.must_have,
        must_not=baseline.must_not,
        success_checks=baseline.success_checks,
        version=baseline.version,
        is_active=baseline.is_active,
        total_versions=total_versions,
        confidence=baseline.confidence,
        ambiguity_notes=baseline.ambiguity_notes,
        context_messages=baseline.context_messages,
        context_summary=baseline.context_summary
    )


@router.post("/sessions/{session_id}/baseline/rebuild", response_model=BaselineResponse)
async def rebuild_baseline(session_id: str, data: BaselineRebuildRequest, db: DBSession = Depends(get_db)):
    """
    Rebuild baseline via LLM and create a new immutable baseline version.
    Existing versions are preserved; only active pointer changes.
    """
    session_lock = await _acquire_session_processing_lock(session_id)
    response_payload = None
    new_baseline = None

    try:
        active_baseline = db.query(Baseline).filter(
            Baseline.session_id == session_id,
            Baseline.is_active == True
        ).order_by(Baseline.version.desc()).first()

        if not active_baseline:
            active_baseline = db.query(Baseline).filter(
                Baseline.session_id == session_id
            ).order_by(Baseline.version.desc()).first()

        if not active_baseline:
            raise HTTPException(status_code=404, detail="Baseline not found")

        if (
            data.expected_active_version is not None
            and active_baseline.version != data.expected_active_version
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Active baseline version changed. Reload and retry.",
                    "current_active_version": active_baseline.version,
                },
            )

        rebuilt = await rebuild_baseline_from_feedback(
            user_request=active_baseline.user_request or "",
            current_goal=active_baseline.goal or "",
            must_have=list(active_baseline.must_have or []),
            must_not=list(active_baseline.must_not or []),
            success_checks=list(active_baseline.success_checks or []),
            reason=data.reason.strip(),
            hint=(data.hint or "").strip() or None,
        )

        rebuilt_goal = (rebuilt.get("goal") or "").strip()
        if not rebuilt_goal:
            raise HTTPException(status_code=502, detail="LLM failed to return a valid baseline goal")

        now = datetime.utcnow()
        currently_active = db.query(Baseline).filter(
            Baseline.session_id == session_id,
            Baseline.is_active == True
        ).all()
        for b in currently_active:
            b.is_active = False
            b.archived_at = now

        latest_version = db.query(Baseline).filter(
            Baseline.session_id == session_id
        ).order_by(Baseline.version.desc()).first()
        next_version = (latest_version.version if latest_version else 0) + 1

        new_baseline = Baseline(
            session_id=session_id,
            version=next_version,
            is_active=True,
            archived_at=None,
            user_request=(active_baseline.user_request or "")[:500],
            goal=rebuilt_goal,
            must_have=list(rebuilt.get("must_have") or []),
            must_not=list(rebuilt.get("must_not") or []),
            success_checks=list(rebuilt.get("success_checks") or []),
            confidence=rebuilt.get("confidence"),
            ambiguity_notes=rebuilt.get("ambiguity_notes"),
            # Carry structured context forward so phase classifier context remains stable.
            context_messages=deepcopy(active_baseline.context_messages or []),
            context_summary=active_baseline.context_summary,
            must_have_history=deepcopy(active_baseline.must_have_history or []),
        )
        db.add(new_baseline)
        db.commit()
        db.refresh(new_baseline)

        total_versions = db.query(Baseline).filter(Baseline.session_id == session_id).count()
        response_payload = BaselineResponse(
            id=new_baseline.id,
            session_id=new_baseline.session_id,
            user_request=new_baseline.user_request,
            goal=new_baseline.goal,
            must_have=new_baseline.must_have,
            must_not=new_baseline.must_not,
            success_checks=new_baseline.success_checks,
            version=new_baseline.version,
            is_active=new_baseline.is_active,
            total_versions=total_versions,
            confidence=new_baseline.confidence,
            ambiguity_notes=new_baseline.ambiguity_notes,
            context_messages=new_baseline.context_messages,
            context_summary=new_baseline.context_summary,
        )
    except IntegrityError:
        db.rollback()
        latest_active = db.query(Baseline).filter(
            Baseline.session_id == session_id,
            Baseline.is_active == True
        ).order_by(Baseline.version.desc()).first()
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Baseline version conflict. Please retry.",
                "current_active_version": latest_active.version if latest_active else None,
            },
        )
    finally:
        session_lock.release()

    # Emit SSE after DB commit; UI subscribers can reload active baseline/versions.
    try:
        from api.sse import push_event
        await push_event(session_id, "baseline_version", {
            "id": new_baseline.id,
            "version": new_baseline.version,
            "goal": new_baseline.goal,
            "reason": data.reason[:200],
        })
    except Exception:
        pass

    return response_payload


def _serialize_export_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize_export_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_export_value(v) for k, v in value.items()}
    return value


def _model_to_export_dict(row):
    data = {}
    for col in row.__table__.columns:
        data[col.name] = _serialize_export_value(getattr(row, col.name))
    return _sanitize(data)


def _resolve_phase_id_for_ts(ts: Optional[datetime], phase_windows: list[dict]) -> Optional[str]:
    if not ts:
        return None
    for window in phase_windows:
        start = window["start"]
        end = window["end"]
        if start and ts < start:
            continue
        if end and ts >= end:
            continue
        return window["phase_id"]
    return None


def _get_sqlite_db_file_path() -> Optional[str]:
    from db.database import DATABASE_URL

    if not DATABASE_URL.startswith("sqlite:///"):
        return None
    raw_path = DATABASE_URL[len("sqlite:///"):]
    if not raw_path or raw_path == ":memory:":
        return None
    if raw_path.startswith("./") or raw_path.startswith(".\\") or not os.path.isabs(raw_path):
        return os.path.abspath(raw_path)
    return raw_path


def _validate_sqlite_file(path: str) -> None:
    conn = None
    try:
        conn = sqlite3.connect(path)
        row = conn.execute("PRAGMA integrity_check;").fetchone()
        status = row[0] if row else "failed"
        if str(status).lower() != "ok":
            raise ValueError(f"SQLite integrity_check failed: {status}")
    finally:
        if conn is not None:
            conn.close()


def _cleanup_sqlite_orphans(db_path: str) -> int:
    """
    Purge rows whose parent session/event/snapshot no longer exists.

    SQLite foreign keys are often disabled in local setups; this keeps backups
    from containing stale orphan records after session deletes.
    """
    conn = None
    removed_total = 0
    statements = [
        "DELETE FROM turns WHERE session_id NOT IN (SELECT id FROM sessions)",
        "DELETE FROM baselines WHERE session_id NOT IN (SELECT id FROM sessions)",
        "DELETE FROM phases WHERE session_id NOT IN (SELECT id FROM sessions)",
        "DELETE FROM events WHERE session_id NOT IN (SELECT id FROM sessions)",
        (
            "DELETE FROM snapshots "
            "WHERE session_id NOT IN (SELECT id FROM sessions) "
            "OR event_id NOT IN (SELECT id FROM events)"
        ),
        "DELETE FROM flags WHERE session_id NOT IN (SELECT id FROM sessions)",
        (
            "DELETE FROM flags "
            "WHERE snapshot_id IS NOT NULL AND snapshot_id NOT IN (SELECT id FROM snapshots)"
        ),
    ]
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        for sql in statements:
            try:
                cur.execute(sql)
                rowcount = cur.rowcount or 0
                if rowcount > 0:
                    removed_total += rowcount
            except sqlite3.Error:
                # Ignore missing tables/columns during partial/legacy schemas.
                continue
        conn.commit()
    finally:
        if conn is not None:
            conn.close()
    return removed_total


def _safe_remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _list_db_backup_archives(db_path: str) -> list[dict]:
    """
    List backup archives created by DB restore flow.

    Files follow: <db_path>.bak-YYYYMMDD-HHMMSS
    """
    base_dir = os.path.dirname(db_path) or "."
    base_name = os.path.basename(db_path)
    prefix = f"{base_name}.bak-"
    items: list[dict] = []
    try:
        for name in os.listdir(base_dir):
            if not name.startswith(prefix):
                continue
            full_path = os.path.join(base_dir, name)
            if not os.path.isfile(full_path):
                continue
            try:
                stat = os.stat(full_path)
            except OSError:
                continue
            items.append(
                {
                    "name": name,
                    "path": full_path,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
                    "modified_ts": stat.st_mtime,
                }
            )
    except OSError:
        return []

    items.sort(key=lambda x: x.get("modified_ts", 0), reverse=True)
    return items


def _create_sqlite_backup_copy(source_path: str) -> str:
    """
    Create a consistent SQLite backup snapshot as a temp file.

    Using sqlite3 backup API ensures WAL changes are included; copying only the
    main .db file can miss recent commits.
    """
    fd, temp_path = tempfile.mkstemp(prefix="vibe-audit-db-backup-", suffix=".db")
    os.close(fd)
    src_conn = None
    dst_conn = None
    try:
        # Read-only URI keeps source immutable while still reading WAL state.
        src_conn = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(temp_path)
        src_conn.backup(dst_conn)
        dst_conn.commit()
        _validate_sqlite_file(temp_path)
        return temp_path
    except Exception:
        _safe_remove_file(temp_path)
        raise
    finally:
        if dst_conn is not None:
            dst_conn.close()
        if src_conn is not None:
            src_conn.close()


@router.get("/sessions/{session_id}/export")
def export_session(session_id: str, format: str = "json", db: DBSession = Depends(get_db)):
    """Export session data as full JSON backup (phase-centric) or Markdown summary."""
    session = db.query(Session).filter(Session.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    baselines = db.query(Baseline).filter(Baseline.session_id == session_id).order_by(Baseline.version.asc(), Baseline.created_at.asc()).all()
    phases = db.query(Phase).filter(Phase.session_id == session_id).order_by(Phase.phase_num.asc(), Phase.started_at.asc()).all()
    events = db.query(Event).filter(Event.session_id == session_id).order_by(Event.ts.asc(), Event.id.asc()).all()
    turns = db.query(Turn).filter(Turn.session_id == session_id).order_by(Turn.ts.asc(), Turn.id.asc()).all()
    snapshots = db.query(Snapshot).filter(Snapshot.session_id == session_id).order_by(Snapshot.ts.asc(), Snapshot.id.asc()).all()
    flags = db.query(Flag).filter(Flag.session_id == session_id).order_by(Flag.ts.asc(), Flag.id.asc()).all()
    active_baseline = next((b for b in baselines if b.is_active), baselines[-1] if baselines else None)

    if format == "markdown":
        md_lines = [
            f"# Session Report: {session_id[:8]}...",
            f"\n**Started:** {session.start_ts}",
            f"**Status:** {session.verdict}",
        ]

        if active_baseline:
            md_lines.extend([
                f"\n## Baseline",
                f"**Original Request:** {active_baseline.user_request}",
            ])

        md_lines.append("\n## Conversation Timeline\n")

        for e in events:
            payload = e.payload or {}
            if e.type == "user_message":
                content = payload.get("content", "")[:200]
                md_lines.append(f"### 👤 USER ({e.ts.strftime('%H:%M')})\n{content}\n")
            elif e.type in ["assistant_response", "agent_response", "assistant_update"]:
                content = payload.get("content", "")[:500]
                thought = payload.get("thought", "")
                if thought:
                    md_lines.append(f"### 🤖 AGENT ({e.ts.strftime('%H:%M')})\n> **Thought:** {thought[:200]}...\n\n{content}\n")
                else:
                    md_lines.append(f"### 🤖 AGENT ({e.ts.strftime('%H:%M')})\n{content}\n")
            elif e.type in ["tool_use", "tool_result"]:
                tool = payload.get("tool", "unknown")
                md_lines.append(f"### 🔧 {tool} ({e.ts.strftime('%H:%M')})\n")

        if snapshots:
            md_lines.append("\n## Alignment History\n")
            for s in snapshots:
                normalized_alignment = _normalize_alignment(s.alignment)
                emoji = "✅" if normalized_alignment == "aligned" else "⚠️" if normalized_alignment == "drifting" else "🚨"
                md_lines.append(f"- {emoji} **{normalized_alignment}** at {s.ts.strftime('%H:%M')} - {s.current_goal[:50] if s.current_goal else 'N/A'}...")

        return {"format": "markdown", "content": "\n".join(md_lines)}

    baselines_data = [_model_to_export_dict(b) for b in baselines]
    phases_data = [_model_to_export_dict(p) for p in phases]
    events_data = [_model_to_export_dict(e) for e in events]
    turns_data = [_model_to_export_dict(t) for t in turns]
    snapshots_data = [_model_to_export_dict(s) for s in snapshots]
    flags_data = [_model_to_export_dict(f) for f in flags]
    session_data = _model_to_export_dict(session)

    event_by_id = {e["id"]: e for e in events_data}
    snapshot_by_id = {s["id"]: s for s in snapshots_data}
    baseline_by_id = {b["id"]: b for b in baselines_data}

    phase_windows = []
    for idx, phase_row in enumerate(phases):
        start = phase_row.started_at
        if not start and phase_row.trigger_event_id:
            trig = event_by_id.get(phase_row.trigger_event_id)
            if trig:
                try:
                    start = datetime.fromisoformat(trig.get("ts"))
                except Exception:
                    start = None
        end = phases[idx + 1].started_at if idx + 1 < len(phases) else None
        phase_windows.append({
            "phase_id": phase_row.id,
            "start": start,
            "end": end,
        })

    phase_blocks = {
        p["id"]: {
            "phase": p,
            "baseline": baseline_by_id.get(p.get("baseline_id")),
            "events": [],
            "turns": [],
            "snapshots": [],
            "flags": [],
        }
        for p in phases_data
    }

    unphased = {
        "events": [],
        "turns": [],
        "snapshots": [],
        "flags": [],
    }

    for event in events_data:
        event_phase_id = _resolve_phase_id_for_ts(
            datetime.fromisoformat(event["ts"]) if event.get("ts") else None,
            phase_windows,
        )
        if event_phase_id and event_phase_id in phase_blocks:
            phase_blocks[event_phase_id]["events"].append(event)
        else:
            unphased["events"].append(event)

    for turn in turns_data:
        turn_phase_id = _resolve_phase_id_for_ts(
            datetime.fromisoformat(turn["ts"]) if turn.get("ts") else None,
            phase_windows,
        )
        if turn_phase_id and turn_phase_id in phase_blocks:
            phase_blocks[turn_phase_id]["turns"].append(turn)
        else:
            unphased["turns"].append(turn)

    for snapshot in snapshots_data:
        snapshot_phase_id = snapshot.get("phase_id")
        if not snapshot_phase_id:
            snapshot_phase_id = _resolve_phase_id_for_ts(
                datetime.fromisoformat(snapshot["ts"]) if snapshot.get("ts") else None,
                phase_windows,
            )
        if snapshot_phase_id and snapshot_phase_id in phase_blocks:
            phase_blocks[snapshot_phase_id]["snapshots"].append(snapshot)
        else:
            unphased["snapshots"].append(snapshot)

    for flag in flags_data:
        flag_phase_id = None
        snap = snapshot_by_id.get(flag.get("snapshot_id"))
        if snap:
            flag_phase_id = snap.get("phase_id")
        if not flag_phase_id:
            event = event_by_id.get(flag.get("event_id"))
            if event and event.get("ts"):
                try:
                    flag_phase_id = _resolve_phase_id_for_ts(datetime.fromisoformat(event["ts"]), phase_windows)
                except Exception:
                    flag_phase_id = None
        if not flag_phase_id and flag.get("ts"):
            try:
                flag_phase_id = _resolve_phase_id_for_ts(datetime.fromisoformat(flag["ts"]), phase_windows)
            except Exception:
                flag_phase_id = None

        if flag_phase_id and flag_phase_id in phase_blocks:
            phase_blocks[flag_phase_id]["flags"].append(flag)
        else:
            unphased["flags"].append(flag)

    phase_export_list = []
    for phase in phases_data:
        block = phase_blocks.get(phase["id"])
        if not block:
            continue
        block["counts"] = {
            "events": len(block["events"]),
            "turns": len(block["turns"]),
            "snapshots": len(block["snapshots"]),
            "flags": len(block["flags"]),
        }
        phase_export_list.append(block)

    export_data = {
        "schema_version": "session_backup.v2",
        "exported_at": datetime.utcnow().isoformat(),
        "session_id": session.id,
        "session": session_data,
        "summary": {
            "baselines": len(baselines_data),
            "phases": len(phases_data),
            "events": len(events_data),
            "turns": len(turns_data),
            "snapshots": len(snapshots_data),
            "flags": len(flags_data),
        },
        "baselines": baselines_data,
        "phases": phase_export_list,
        "unphased": unphased,
        "raw": {
            "events": events_data,
            "turns": turns_data,
            "snapshots": snapshots_data,
            "flags": flags_data,
            "baselines": baselines_data,
            "phases": phases_data,
        },
    }

    return export_data


@router.get("/backup/db")
def download_db_backup():
    """Download current SQLite DB file for full backup."""
    db_path = _get_sqlite_db_file_path()
    if not db_path:
        raise HTTPException(status_code=400, detail="DB backup supports only file-based SQLite.")
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="DB file not found.")

    try:
        _cleanup_sqlite_orphans(db_path)
    except Exception:
        # Best-effort cleanup only; continue backup even if cleanup fails.
        pass

    try:
        temp_backup_path = _create_sqlite_backup_copy(db_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create DB backup snapshot: {e}") from e

    filename = f"vibe-audit-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.db"
    return FileResponse(
        path=temp_backup_path,
        media_type="application/x-sqlite3",
        filename=filename,
        background=BackgroundTask(_safe_remove_file, temp_backup_path),
    )


@router.get("/backup/db/state")
def get_db_backup_state(db: DBSession = Depends(get_db)):
    """Capture current DB state summary for backup/restore modal."""
    db_path = _get_sqlite_db_file_path()
    if not db_path:
        raise HTTPException(status_code=400, detail="DB state supports only file-based SQLite.")
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="DB file not found.")

    stat = os.stat(db_path)
    existing_sessions = db.query(Session.id)
    existing_events = db.query(Event.id)
    existing_snapshots = db.query(Snapshot.id)

    snapshots_without_session = db.query(Snapshot).filter(
        ~Snapshot.session_id.in_(existing_sessions)
    ).count()
    snapshots_without_event = db.query(Snapshot).filter(
        ~Snapshot.event_id.in_(existing_events)
    ).count()
    flags_without_session = db.query(Flag).filter(
        ~Flag.session_id.in_(existing_sessions)
    ).count()
    flags_without_snapshot = db.query(Flag).filter(
        Flag.snapshot_id.isnot(None),
        ~Flag.snapshot_id.in_(existing_snapshots),
    ).count()

    backup_archives = _list_db_backup_archives(db_path)
    return {
        "captured_at": datetime.utcnow().isoformat(),
        "file": {
            "name": os.path.basename(db_path),
            "size_bytes": stat.st_size,
            "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
        },
        "counts": {
            "sessions": db.query(Session).count(),
            "events": db.query(Event).count(),
            "turns": db.query(Turn).count(),
            "baselines": db.query(Baseline).count(),
            "phases": db.query(Phase).count(),
            "snapshots": db.query(Snapshot).count(),
            "flags": db.query(Flag).count(),
        },
        "orphans": {
            "snapshots_without_session": snapshots_without_session,
            "snapshots_without_event": snapshots_without_event,
            "flags_without_session": flags_without_session,
            "flags_without_snapshot": flags_without_snapshot,
            "total": snapshots_without_session + snapshots_without_event + flags_without_session + flags_without_snapshot,
        },
        "backup_archives": {
            "count": len(backup_archives),
            "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in backup_archives),
            "latest_modified_at": backup_archives[0]["modified_at"] if backup_archives else None,
            "items": [
                {
                    "name": item["name"],
                    "size_bytes": item["size_bytes"],
                    "modified_at": item["modified_at"],
                }
                for item in backup_archives[:20]
            ],
        },
    }


@router.delete("/backup/db/archives")
def delete_db_backup_archives():
    """Delete local DB backup archive files created during restore."""
    db_path = _get_sqlite_db_file_path()
    if not db_path:
        raise HTTPException(status_code=400, detail="DB archive cleanup supports only file-based SQLite.")
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="DB file not found.")

    archives = _list_db_backup_archives(db_path)
    deleted_count = 0
    deleted_bytes = 0
    failed: list[str] = []

    for item in archives:
        file_path = item.get("path")
        if not file_path:
            continue
        try:
            os.remove(file_path)
            deleted_count += 1
            deleted_bytes += int(item.get("size_bytes") or 0)
        except OSError:
            failed.append(item.get("name") or os.path.basename(file_path))

    return {
        "status": "ok" if not failed else "partial",
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "failed": failed,
        "remaining_count": max(len(archives) - deleted_count, 0),
    }


@router.post("/backup/db")
async def upload_db_backup(request: Request):
    """Upload SQLite DB backup file (raw binary body) and replace current DB."""
    db_path = _get_sqlite_db_file_path()
    if not db_path:
        raise HTTPException(status_code=400, detail="DB restore supports only file-based SQLite.")

    payload = await request.body()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded DB backup is empty.")

    filename_hint = request.headers.get("x-backup-filename", "")
    suffix = os.path.splitext(filename_hint)[1] or ".db"
    fd, temp_path = tempfile.mkstemp(prefix="vibe-audit-db-upload-", suffix=suffix)
    os.close(fd)
    backup_path = None

    try:
        with open(temp_path, "wb") as out:
            out.write(payload)

        _validate_sqlite_file(temp_path)

        if os.path.exists(db_path):
            backup_path = f"{db_path}.bak-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(db_path, backup_path)

        from db.database import engine, init_db
        engine.dispose()
        shutil.copy2(temp_path, db_path)
        init_db()

        return {
            "status": "restored",
            "db_path": db_path,
            "backup_path": backup_path,
            "restart_recommended": True,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to restore DB backup: {e}") from e
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


# Apply saved settings on import
try:
    from core.config import apply_settings
    apply_settings()
except Exception:
    pass


@router.get("/health")
def health_check():
    """Health check endpoint"""
    return {"status": "ok", "version": "0.2.0"}


@router.get("/debug/pipeline")
async def debug_pipeline(db: DBSession = Depends(get_db)):
    """Diagnostic endpoint: test LLM connectivity + show pipeline status and recent errors."""
    import os
    from services.llm_service import call_llm, _get_model
    from core.processor import get_pipeline_errors

    model = _get_model()
    openai_set = bool(os.environ.get("OPENAI_API_KEY"))
    gemini_set = bool(os.environ.get("GEMINI_API_KEY"))

    baseline_count = db.query(Baseline).count()
    snapshot_count = db.query(Snapshot).count()
    event_count = db.query(Event).count()
    session_count = db.query(Session).count()

    # Test LLM call
    llm_status = "unknown"
    llm_error = None
    try:
        result = await call_llm('Return exactly: {"test": "ok"}', model=model)
        llm_status = "ok" if result else "empty_response"
    except Exception as e:
        llm_status = "error"
        llm_error = str(e)

    return {
        "model": model,
        "keys": {"openai": openai_set, "gemini": gemini_set},
        "llm_test": {"status": llm_status, "error": llm_error},
        "counts": {
            "sessions": session_count,
            "events": event_count,
            "baselines": baseline_count,
            "snapshots": snapshot_count,
        },
        "recent_errors": get_pipeline_errors(),
    }


# ============ Settings API ============

class SettingsUpdate(BaseModel):
    llm_model: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    response_language: Optional[str] = None
    interface_language: Optional[str] = None
    time_zone: Optional[str] = None


@router.get("/settings")
def get_settings():
    """Get current settings (API keys masked)"""
    from core.config import get_settings_for_display
    return get_settings_for_display()


@router.get("/settings/models")
def get_settings_models():
    """Get curated model options used by Settings UI."""
    from core.config import get_model_catalog
    return get_model_catalog()


@router.post("/settings")
def update_settings(data: SettingsUpdate):
    """Update settings (API keys, model)"""
    from core.config import load_settings, save_settings
    current = load_settings()

    if data.llm_model is not None:
        current["llm_model"] = data.llm_model
    # Only update keys if non-empty (don't overwrite with empty)
    if data.gemini_api_key:
        current["gemini_api_key"] = data.gemini_api_key
    if data.openai_api_key:
        current["openai_api_key"] = data.openai_api_key
    if data.anthropic_api_key:
        current["anthropic_api_key"] = data.anthropic_api_key
    if data.response_language is not None:
        current["response_language"] = data.response_language
    if data.interface_language is not None:
        current["interface_language"] = data.interface_language
    if data.time_zone is not None:
        current["time_zone"] = data.time_zone

    save_settings(current)

    from core.config import get_settings_for_display
    return {"status": "saved", "settings": get_settings_for_display()}


# ============ Seal (Legacy, kept for backward compat) ============

@router.post("/api/sessions/{session_id}/seal")
async def seal_pending_turns(
    session_id: str,
    db: DBSession = Depends(get_db)
):
    """Legacy seal endpoint. Analysis now triggers on assistant_response arrival."""
    return {"success": True, "sealed_count": 0, "refreshed": False}


# ============ Conversation Turns API ============

def _find_assistant_response_event(
    db: DBSession,
    session_id: str,
    turn_key: Optional[str],
    scan_limit: int = 50,
):
    """
    Find the assistant_response event associated with a turn.

    Payload-based lookup keeps behavior compatible across SQLite/Postgres
    without depending on JSON SQL operators.
    """
    recent_events = db.query(Event).filter(
        Event.session_id == session_id,
        Event.type == "assistant_response",
    ).order_by(Event.ts.desc()).limit(scan_limit).all()

    if turn_key:
        for evt in recent_events:
            payload = evt.payload if isinstance(evt.payload, dict) else {}
            if payload.get("turn_key") == turn_key:
                return evt

    return recent_events[0] if recent_events else None


@router.post("/api/turn")
async def log_turn(
    turn_data: TurnCreate,
    background_tasks: BackgroundTasks,
    db: DBSession = Depends(get_db)
):
    """
    Log a conversation turn from CLI hooks (Gemini / Claude).

    - user_message: creates user Turn + user_message event -> baseline/phase processing
    - assistant_response: upserts assistant Turn + assistant_response event -> full LLM analysis
    - Dedup: turn_key prevents duplicate Turn records for the same conversation turn
    """
    session_id = turn_data.session_id

    # Find or create session
    if not session_id or session_id == "default":
        session = db.query(Session).filter(Session.end_ts == None).order_by(Session.start_ts.desc()).first()
        if not session:
            session = Session(cli_command=(turn_data.cli_command or "cli:hook"))
            db.add(session)
            db.commit()
            db.refresh(session)
        session_id = session.id
    else:
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            session = Session(id=session_id, cli_command=(turn_data.cli_command or "cli:hook"))
            db.add(session)
            db.commit()

    result = {"success": True, "session_id": session_id, "action": "none"}
    turns_created = []

    # --- Handle user message ---
    if turn_data.user_message:
        existing_user = None
        if turn_data.turn_key:
            existing_user = db.query(Turn).filter(
                Turn.session_id == session_id,
                Turn.turn_key == turn_data.turn_key,
                Turn.role == "user"
            ).first()

        if not existing_user:
            user_turn = Turn(
                session_id=session_id,
                turn_key=turn_data.turn_key,
                role="user",
                content=turn_data.user_message,
                sealed=True,
                alignment="unknown"
            )
            db.add(user_turn)
            db.commit()
            db.refresh(user_turn)
            turns_created.append({"id": user_turn.id, "role": "user"})

            user_event = Event(
                session_id=session_id,
                ts=datetime.utcnow(),
                type="user_message",
                payload={"content": turn_data.user_message}
            )
            db.add(user_event)
            db.commit()
            db.refresh(user_event)
            background_tasks.add_task(
                process_event_background,
                session_id,
                user_event.id,
                _make_background_session_factory(db)
            )

    # --- Handle assistant response (upsert + analysis trigger) ---
    if turn_data.assistant_response:
        existing_assistant = None
        if turn_data.turn_key:
            existing_assistant = db.query(Turn).filter(
                Turn.session_id == session_id,
                Turn.turn_key == turn_data.turn_key,
                Turn.role == "assistant"
            ).first()

        if existing_assistant:
            previous_content = existing_assistant.content or ""
            updated_content = turn_data.assistant_response or ""
            if updated_content != previous_content:
                existing_assistant.content = updated_content
                existing_assistant.ts = datetime.utcnow()
                # Reuse/update assistant_response event for this turn so
                # analysis re-runs with final content and snapshot upsert
                # updates the same event_id.
                analysis_event = _find_assistant_response_event(db, session_id, turn_data.turn_key)
                if analysis_event:
                    payload = deepcopy(analysis_event.payload) if isinstance(analysis_event.payload, dict) else {}
                    payload["content"] = updated_content
                    if turn_data.turn_key:
                        payload["turn_key"] = turn_data.turn_key
                    payload["update"] = True
                    analysis_event.payload = payload
                    analysis_event.ts = datetime.utcnow()
                else:
                    analysis_event = Event(
                        session_id=session_id,
                        ts=datetime.utcnow(),
                        type="assistant_response",
                        payload={
                            "content": updated_content,
                            "turn_key": turn_data.turn_key,
                            "update": True,
                        }
                    )
                    db.add(analysis_event)

                db.commit()
                db.refresh(existing_assistant)
                db.refresh(analysis_event)
                result["analysis_event_id"] = analysis_event.id

                background_tasks.add_task(
                    process_event_background,
                    session_id,
                    analysis_event.id,
                    _make_background_session_factory(db)
                )

            result["action"] = "updated"
            result["turn_id"] = existing_assistant.id
        else:
            assistant_turn = Turn(
                session_id=session_id,
                turn_key=turn_data.turn_key,
                role="assistant",
                content=turn_data.assistant_response,
                sealed=True,
                alignment="unknown"
            )
            db.add(assistant_turn)
            db.commit()
            db.refresh(assistant_turn)
            turns_created.append({"id": assistant_turn.id, "role": "assistant"})
            result["action"] = "created"

            # Only create Event + trigger analysis for NEW turns
            asst_event = Event(
                session_id=session_id,
                ts=datetime.utcnow(),
                type="assistant_response",
                payload={
                    "content": turn_data.assistant_response,
                    "turn_key": turn_data.turn_key,
                }
            )
            db.add(asst_event)
            db.commit()
            db.refresh(asst_event)
            background_tasks.add_task(
                process_event_background,
                session_id,
                asst_event.id,
                _make_background_session_factory(db)
            )

    if turns_created:
        result["turns"] = turns_created
    if result["action"] == "none" and turns_created:
        result["action"] = "created"

    return result


@router.get("/sessions/{session_id}/turns")
def get_turns(
    session_id: str,
    db: DBSession = Depends(get_db)
):
    """Get all conversation turns for a session"""
    turns = db.query(Turn).filter(Turn.session_id == session_id).order_by(Turn.ts.asc()).all()
    return {
        "turns": [
            {
                "id": t.id,
                "role": t.role,
                "content": t.content,
                "ts": t.ts,
                "alignment": t.alignment,
                "warning": t.warning
            }
            for t in turns
        ]
    }

