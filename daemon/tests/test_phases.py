"""Tests for Phase/Stage system: classification, baseline versioning, API endpoints."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from db.models import Base, Session, Event, Baseline, Snapshot, Flag, Phase
from core.processor import process_event, _handle_phase_transition, _get_active_baseline, _get_current_phase


# ============ Processor: Phase Creation on First Baseline ============

@pytest.mark.asyncio
@patch("core.processor.generate_baseline", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.update_snapshot", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_initial_phase_created_with_baseline(
    mock_flag_check, mock_alignment, mock_snapshot, mock_push_align, mock_push_flag, mock_baseline, db, sample_session
):
    """First user message creates both baseline v1 and phase #1 (initial)."""
    mock_baseline.return_value = {
        "goal": "Port Python to Kotlin",
        "must_have": ["Kotlin code"],
        "must_not": [],
        "success_checks": ["compiles"],
    }
    mock_snapshot.return_value = {
        "current_goal": "Porting to Kotlin",
        "current_plan": [], "current_tasks": [], "done_claimed": False,
    }
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.0, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(session_id=sample_session.id, type="user_message", payload={"content": "Port this Python to Kotlin"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    baseline = db.query(Baseline).filter(Baseline.session_id == sample_session.id).first()
    assert baseline is not None
    assert baseline.version == 1
    assert baseline.is_active == True

    phases = db.query(Phase).filter(Phase.session_id == sample_session.id).all()
    assert len(phases) == 1
    assert phases[0].phase_num == 1
    assert phases[0].classification == "initial"
    assert phases[0].baseline_version == 1


# ============ Processor: new_phase Classification ============

@pytest.mark.asyncio
@patch("core.processor._push_phase_event", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_new_phase_creates_phase_record(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, mock_push_phase, db, sample_session
):
    """A 'new_phase' classification creates a new Phase record, same baseline."""
    # Pre-create baseline + initial phase
    baseline = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Port Python to Kotlin", goal="Port to Kotlin",
        must_have=["Kotlin code"], must_not=[], success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline.id,
        baseline_version=1, classification="initial",
        user_message="Port Python to Kotlin", summary="Port to Kotlin",
    )
    db.add(phase1)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "new_phase", "phase_confidence": 0.95,
        "phase_summary": "Port to Ruby as well", "message_type": "task_request",
    }
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.1, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(session_id=sample_session.id, type="user_message", payload={"content": "Now do Ruby too"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    # Should have 2 phases now
    phases = db.query(Phase).filter(Phase.session_id == sample_session.id).order_by(Phase.phase_num).all()
    assert len(phases) == 2
    assert phases[1].classification == "new_phase"
    assert phases[1].phase_num == 2
    assert phases[1].baseline_version == 1  # Same baseline

    # Baseline should still be active, user_request appended
    db.refresh(baseline)
    assert baseline.is_active == True
    assert "Now do Ruby too" in baseline.user_request

    # SSE push should have been called
    mock_push_phase.assert_called_once()


# ============ Processor: pivot Classification ============

@pytest.mark.asyncio
@patch("core.processor.generate_baseline", new_callable=AsyncMock)
@patch("core.processor._push_phase_event", new_callable=AsyncMock)
@patch("core.processor._push_baseline_version_event", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_pivot_archives_baseline_creates_new(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag,
    mock_push_baseline, mock_push_phase, mock_gen_baseline, db, sample_session
):
    """A 'pivot' classification archives the old baseline and creates a new v2."""
    # Pre-create baseline v1 + initial phase
    baseline_v1 = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Fix the login bug", goal="Fix login bug",
        must_have=["bug fixed"], must_not=[], success_checks=[],
    )
    db.add(baseline_v1)
    db.commit()
    db.refresh(baseline_v1)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline_v1.id,
        baseline_version=1, classification="initial",
        user_message="Fix the login bug", summary="Fix login bug",
    )
    db.add(phase1)
    db.commit()

    # LLM returns pivot classification
    mock_classify.return_value = {
        "phase_classification": "pivot", "phase_confidence": 0.92,
        "phase_summary": "Redesign the entire auth system", "message_type": "task_request",
    }
    mock_gen_baseline.return_value = {
        "goal": "Redesign auth system",
        "must_have": ["OAuth support", "JWT tokens"],
        "must_not": ["plaintext passwords"],
        "success_checks": ["auth tests pass"],
    }
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.0, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(session_id=sample_session.id, type="user_message",
                  payload={"content": "Forget the bug, let's redesign the whole auth system"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    # Old baseline should be archived
    db.refresh(baseline_v1)
    assert baseline_v1.is_active == False
    assert baseline_v1.archived_at is not None

    # New baseline v2 should exist and be active
    baseline_v2 = db.query(Baseline).filter(
        Baseline.session_id == sample_session.id,
        Baseline.version == 2,
    ).first()
    assert baseline_v2 is not None
    assert baseline_v2.is_active == True
    assert baseline_v2.goal == "Redesign auth system"

    # Phase should be pivot with new baseline version
    phases = db.query(Phase).filter(Phase.session_id == sample_session.id).order_by(Phase.phase_num).all()
    assert len(phases) == 2
    assert phases[1].classification == "pivot"
    assert phases[1].baseline_version == 2

    # generate_baseline called for pivot
    mock_gen_baseline.assert_called_once()
    # SSE pushes for both phase and baseline version
    mock_push_phase.assert_called_once()
    mock_push_baseline.assert_called_once()


# ============ Processor: continuation (no new phase) ============

@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_continuation_no_new_phase(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, db, sample_session
):
    """A 'continuation' classification does not create a new Phase, just appends to baseline."""
    baseline = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Build a REST API", goal="Build REST API",
        must_have=["endpoints"], must_not=[], success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline.id,
        baseline_version=1, classification="initial",
        user_message="Build a REST API", summary="Build REST API",
    )
    db.add(phase1)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "continuation", "phase_confidence": 0.88,
        "phase_summary": "Add input validation", "message_type": "task_request",
    }
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.0, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(session_id=sample_session.id, type="user_message", payload={"content": "Add input validation"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    # Still only 1 phase
    phases = db.query(Phase).filter(Phase.session_id == sample_session.id).all()
    assert len(phases) == 1

    # Baseline user_request should be appended
    db.refresh(baseline)
    assert "Add input validation" in baseline.user_request


@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_follow_up_new_phase_without_scope_change_becomes_continuation(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, db, sample_session
):
    """Follow-up instructions without scope change should stay as continuation."""
    baseline = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Build a mini task API", goal="Build a mini task API",
        must_have=["/health", "/tasks"], must_not=[], success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline.id,
        baseline_version=1, classification="initial",
        user_message="Build a mini task API", summary="Build API",
    )
    db.add(phase1)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "new_phase",
        "phase_confidence": 0.88,
        "phase_summary": "Proceed implementation",
        "message_type": "task_request",
        "topic_relation_score": 0.95,
        "explicit_abandonment": False,
        "parent_message_index": 0,
        "goal_changed": False,
        "must_have_delta": None,
    }
    mock_alignment.return_value = {
        "alignment": "aligned", "drift_score": 0.0, "mismatches": [],
        "missing_must_have": [], "constraint_violations": [], "new_scope": []
    }
    mock_flag_check.return_value = None

    event = Event(
        session_id=sample_session.id,
        type="user_message",
        payload={"content": "Great, implement it according to the plan."},
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    phases = db.query(Phase).filter(Phase.session_id == sample_session.id).order_by(Phase.phase_num).all()
    assert len(phases) == 1
    assert phases[0].classification == "initial"


@pytest.mark.asyncio
@patch("core.processor._push_phase_event", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_follow_up_new_phase_with_scope_change_keeps_new_phase(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, mock_push_phase, db, sample_session
):
    """When scope changes are present, follow-up requests should keep new_phase."""
    baseline = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Build a mini task API", goal="Build a mini task API",
        must_have=["/health", "/tasks"], must_not=[], success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline.id,
        baseline_version=1, classification="initial",
        user_message="Build a mini task API", summary="Build API",
    )
    db.add(phase1)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "new_phase",
        "phase_confidence": 0.9,
        "phase_summary": "Add pytest coverage",
        "message_type": "task_request",
        "topic_relation_score": 0.9,
        "explicit_abandonment": False,
        "parent_message_index": 0,
        "goal_changed": True,
        "updated_goal": "Build a mini task API and include pytest coverage",
        "must_have_delta": {"add": ["At least 2 pytest tests"], "remove": [], "refine": []},
    }
    mock_alignment.return_value = {
        "alignment": "aligned", "drift_score": 0.0, "mismatches": [],
        "missing_must_have": [], "constraint_violations": [], "new_scope": []
    }
    mock_flag_check.return_value = None

    event = Event(
        session_id=sample_session.id,
        type="user_message",
        payload={"content": "Also add 2 pytest tests."},
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    phases = db.query(Phase).filter(Phase.session_id == sample_session.id).order_by(Phase.phase_num).all()
    assert len(phases) == 2
    assert phases[1].classification == "new_phase"
    mock_push_phase.assert_called_once()


# ============ Processor: Evolving Goal (#2) ============

@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_continuation_updates_goal(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, db, sample_session
):
    """Continuation should NOT auto-mutate baseline goal."""
    baseline = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Build a todo app", goal="Build a todo app",
        must_have=["task list"], must_not=[], success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline.id,
        baseline_version=1, classification="initial",
        user_message="Build a todo app", summary="Build todo app",
    )
    db.add(phase1)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "continuation", "phase_confidence": 0.9,
        "phase_summary": "Add drag-and-drop", "message_type": "task_request",
        "goal_changed": True,
        "updated_goal": "Build a todo app with drag-and-drop reordering",
    }

    event = Event(session_id=sample_session.id, type="user_message",
                  payload={"content": "Add drag-and-drop reordering"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(baseline)
    assert baseline.goal == "Build a todo app"


@pytest.mark.asyncio
@patch("core.processor._push_phase_event", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_new_phase_updates_goal(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, mock_push_phase, db, sample_session
):
    """new_phase should NOT auto-mutate baseline goal."""
    baseline = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Build a REST API", goal="Build a REST API",
        must_have=["endpoints"], must_not=[], success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline.id,
        baseline_version=1, classification="initial",
        user_message="Build a REST API", summary="Build REST API",
    )
    db.add(phase1)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "new_phase", "phase_confidence": 0.85,
        "phase_summary": "Add authentication", "message_type": "task_request",
        "goal_changed": True,
        "updated_goal": "Build a REST API with authentication",
    }

    event = Event(session_id=sample_session.id, type="user_message",
                  payload={"content": "Now add authentication"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(baseline)
    assert baseline.goal == "Build a REST API"


@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_goal_unchanged_when_no_expansion(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, db, sample_session
):
    """When goal_changed=false, baseline goal should remain unchanged."""
    baseline = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Build a todo app", goal="Build a todo app",
        must_have=["task list"], must_not=[], success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline.id,
        baseline_version=1, classification="initial",
        user_message="Build a todo app", summary="Build todo app",
    )
    db.add(phase1)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "continuation", "phase_confidence": 0.9,
        "phase_summary": "Confirm to proceed", "message_type": "task_request",
        "goal_changed": False,
        "updated_goal": None,
    }

    event = Event(session_id=sample_session.id, type="user_message",
                  payload={"content": "Ok go ahead"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(baseline)
    assert baseline.goal == "Build a todo app"  # Unchanged


# ============ Processor: low confidence defaults to continuation ============

@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_low_confidence_pivot_becomes_new_phase(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, db, sample_session
):
    """Low confidence pivot should downgrade to new_phase (no baseline version bump)."""
    baseline = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Build todo app", goal="Build todo",
        must_have=[], must_not=[], success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline.id,
        baseline_version=1, classification="initial",
        user_message="Build todo app", summary="Build todo",
    )
    db.add(phase1)
    db.commit()

    # LLM says pivot but with low confidence
    mock_classify.return_value = {
        "phase_classification": "pivot", "phase_confidence": 0.4,
        "phase_summary": "Ambiguous request", "message_type": "task_request",
    }
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.0, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(session_id=sample_session.id, type="user_message", payload={"content": "hmm maybe change something"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    # Should create new_phase but keep same baseline version (no pivot archival)
    phases = db.query(Phase).filter(Phase.session_id == sample_session.id).order_by(Phase.phase_num).all()
    assert len(phases) == 2
    assert phases[1].classification == "new_phase"
    assert phases[1].baseline_version == 1
    assert baseline.is_active == True


# ============ Processor: off-topic / abandonment pivot promotion ============

@pytest.mark.asyncio
@patch("core.processor.generate_baseline", new_callable=AsyncMock)
@patch("core.processor._push_phase_event", new_callable=AsyncMock)
@patch("core.processor._push_baseline_version_event", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_new_phase_with_off_topic_signal_promotes_to_pivot(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag,
    mock_push_baseline, mock_push_phase, mock_gen_baseline, db, sample_session
):
    """Off-topic signal should upgrade new_phase to pivot and create a new baseline."""
    baseline_v1 = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Provide a gambas recipe", goal="Provide a gambas recipe",
        must_have=["ingredients", "steps"], must_not=[], success_checks=[],
    )
    db.add(baseline_v1)
    db.commit()
    db.refresh(baseline_v1)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline_v1.id,
        baseline_version=1, classification="initial",
        user_message="Provide a gambas recipe", summary="Provide recipe",
    )
    db.add(phase1)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "new_phase",
        "phase_confidence": 0.78,
        "phase_summary": "Switch to weather forecast",
        "message_type": "task_request",
        "topic_relation_score": 0.05,
        "explicit_abandonment": False,
    }
    mock_gen_baseline.return_value = {
        "goal": "Provide weather forecast information",
        "must_have": ["location prompt", "forecast summary"],
        "must_not": [],
        "success_checks": ["mentions weather timeline"],
    }
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.0, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(
        session_id=sample_session.id,
        type="user_message",
        payload={"content": "Tell me tomorrow's weather instead"},
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(baseline_v1)
    assert baseline_v1.is_active is False

    baseline_v2 = db.query(Baseline).filter(
        Baseline.session_id == sample_session.id,
        Baseline.version == 2,
    ).first()
    assert baseline_v2 is not None
    assert baseline_v2.is_active is True
    assert baseline_v2.goal == "Provide weather forecast information"

    phases = db.query(Phase).filter(Phase.session_id == sample_session.id).order_by(Phase.phase_num).all()
    assert len(phases) == 2
    assert phases[1].classification == "pivot"
    assert phases[1].baseline_version == 2
    mock_gen_baseline.assert_called_once()
    mock_push_phase.assert_called_once()
    mock_push_baseline.assert_called_once()


@pytest.mark.asyncio
@patch("core.processor.generate_baseline", new_callable=AsyncMock)
@patch("core.processor._push_phase_event", new_callable=AsyncMock)
@patch("core.processor._push_baseline_version_event", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_low_confidence_pivot_kept_when_explicit_abandonment(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag,
    mock_push_baseline, mock_push_phase, mock_gen_baseline, db, sample_session
):
    """Explicit abandonment keeps pivot even when phase confidence is low."""
    baseline_v1 = Baseline(
        session_id=sample_session.id, version=1, is_active=True,
        user_request="Build a todo app", goal="Build a todo app",
        must_have=["task list"], must_not=[], success_checks=[],
    )
    db.add(baseline_v1)
    db.commit()
    db.refresh(baseline_v1)

    phase1 = Phase(
        session_id=sample_session.id, phase_num=1, baseline_id=baseline_v1.id,
        baseline_version=1, classification="initial",
        user_message="Build a todo app", summary="Build todo app",
    )
    db.add(phase1)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "pivot",
        "phase_confidence": 0.35,
        "phase_summary": "Switch from coding task to trip planning",
        "message_type": "task_request",
        "topic_relation_score": 0.1,
        "explicit_abandonment": True,
    }
    mock_gen_baseline.return_value = {
        "goal": "Plan a 2-day Tokyo trip itinerary",
        "must_have": ["day plan", "food spots"],
        "must_not": [],
        "success_checks": ["clear itinerary"],
    }
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.0, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(
        session_id=sample_session.id,
        type="user_message",
        payload={"content": "Forget this app. Let's plan my Tokyo trip."},
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(baseline_v1)
    assert baseline_v1.is_active is False

    baseline_v2 = db.query(Baseline).filter(
        Baseline.session_id == sample_session.id,
        Baseline.version == 2,
    ).first()
    assert baseline_v2 is not None
    assert baseline_v2.is_active is True
    assert baseline_v2.goal == "Plan a 2-day Tokyo trip itinerary"

    phases = db.query(Phase).filter(Phase.session_id == sample_session.id).order_by(Phase.phase_num).all()
    assert len(phases) == 2
    assert phases[1].classification == "pivot"
    assert phases[1].baseline_version == 2
    mock_gen_baseline.assert_called_once()
    mock_push_phase.assert_called_once()
    mock_push_baseline.assert_called_once()


# ============ Helper functions ============

def test_get_active_baseline(db, sample_session):
    """_get_active_baseline returns the active baseline."""
    b1 = Baseline(session_id=sample_session.id, version=1, is_active=False,
                  user_request="old", goal="old", must_have=[], must_not=[], success_checks=[])
    b2 = Baseline(session_id=sample_session.id, version=2, is_active=True,
                  user_request="new", goal="new", must_have=[], must_not=[], success_checks=[])
    db.add_all([b1, b2])
    db.commit()

    active = _get_active_baseline(db, sample_session.id)
    assert active is not None
    assert active.version == 2
    assert active.is_active == True


def test_get_current_phase(db, sample_session):
    """_get_current_phase returns the latest phase by phase_num."""
    b = Baseline(session_id=sample_session.id, version=1, is_active=True,
                 user_request="test", goal="test", must_have=[], must_not=[], success_checks=[])
    db.add(b)
    db.commit()
    db.refresh(b)

    p1 = Phase(session_id=sample_session.id, phase_num=1, baseline_id=b.id,
               baseline_version=1, classification="initial", user_message="x", summary="x")
    p2 = Phase(session_id=sample_session.id, phase_num=2, baseline_id=b.id,
               baseline_version=1, classification="new_phase", user_message="y", summary="y")
    db.add_all([p1, p2])
    db.commit()

    current = _get_current_phase(db, sample_session.id)
    assert current is not None
    assert current.phase_num == 2


# ============ API: New Phase/Baseline Endpoints ============

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

    from db.database import get_db
    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    from main import app
    app.dependency_overrides[get_db] = override_get_db
    from fastapi.testclient import TestClient
    client = TestClient(app)
    yield client, TestSessionLocal
    app.dependency_overrides.clear()


def test_get_baselines_empty(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = Session(cli_command="test")
    db.add(session)
    db.commit()
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/baselines")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_get_baselines_with_versions(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = Session(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)

    b1 = Baseline(session_id=session.id, version=1, is_active=False,
                  user_request="v1", goal="Goal v1", must_have=[], must_not=[], success_checks=[])
    b2 = Baseline(session_id=session.id, version=2, is_active=True,
                  user_request="v2", goal="Goal v2", must_have=[], must_not=[], success_checks=[])
    db.add_all([b1, b2])
    db.commit()
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/baselines")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["active_version"] == 2
    assert len(data["baselines"]) == 2
    assert data["baselines"][0]["version"] == 1
    assert data["baselines"][1]["version"] == 2


def test_get_baseline_specific_version(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = Session(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)

    b1 = Baseline(session_id=session.id, version=1, is_active=False,
                  user_request="v1", goal="Fix bug", must_have=["fix"], must_not=[], success_checks=[])
    b2 = Baseline(session_id=session.id, version=2, is_active=True,
                  user_request="v2", goal="Redesign auth", must_have=["OAuth"], must_not=[], success_checks=[])
    db.add_all([b1, b2])
    db.commit()
    sid = session.id
    db.close()

    # Get v1
    resp = client.get(f"/sessions/{sid}/baseline/1")
    assert resp.status_code == 200
    assert resp.json()["goal"] == "Fix bug"
    assert resp.json()["version"] == 1

    # Get v2
    resp = client.get(f"/sessions/{sid}/baseline/2")
    assert resp.status_code == 200
    assert resp.json()["goal"] == "Redesign auth"

    # Get nonexistent version
    resp = client.get(f"/sessions/{sid}/baseline/99")
    assert resp.status_code == 404


def test_get_phases_empty(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = Session(cli_command="test")
    db.add(session)
    db.commit()
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/phases")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_get_phases_with_data(test_app):
    client, SessionLocal = test_app
    db = SessionLocal()
    session = Session(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)

    b = Baseline(session_id=session.id, version=1, is_active=True,
                 user_request="test", goal="test", must_have=[], must_not=[], success_checks=[])
    db.add(b)
    db.commit()
    db.refresh(b)

    p1 = Phase(session_id=session.id, phase_num=1, baseline_id=b.id,
               baseline_version=1, classification="initial",
               user_message="Build REST API", summary="Build API", confidence=1.0)
    p2 = Phase(session_id=session.id, phase_num=2, baseline_id=b.id,
               baseline_version=1, classification="new_phase",
               user_message="Add auth", summary="Add authentication", confidence=0.9)
    db.add_all([p1, p2])
    db.commit()
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/phases")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["phases"][0]["classification"] == "initial"
    assert data["phases"][1]["classification"] == "new_phase"
    assert data["phases"][1]["summary"] == "Add authentication"


def test_active_baseline_returned_by_default(test_app):
    """GET /sessions/{id}/baseline returns the active baseline."""
    client, SessionLocal = test_app
    db = SessionLocal()
    session = Session(cli_command="test")
    db.add(session)
    db.commit()
    db.refresh(session)

    b1 = Baseline(session_id=session.id, version=1, is_active=False,
                  user_request="v1", goal="Old goal", must_have=[], must_not=[], success_checks=[])
    b2 = Baseline(session_id=session.id, version=2, is_active=True,
                  user_request="v2", goal="New goal", must_have=[], must_not=[], success_checks=[])
    db.add_all([b1, b2])
    db.commit()
    sid = session.id
    db.close()

    resp = client.get(f"/sessions/{sid}/baseline")
    assert resp.status_code == 200
    data = resp.json()
    assert data["goal"] == "New goal"
    assert data["version"] == 2
    assert data["is_active"] == True
    assert data["total_versions"] == 2
