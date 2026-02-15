"""Tests for event processing pipeline."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
from db.models import Session, Event, Baseline, Snapshot, Flag, Phase
from core.processor import process_event, _extract_user_content


# --- _extract_user_content ---

class FakeEvent:
    def __init__(self, type, payload):
        self.type = type
        self.payload = payload


def test_extract_user_content_session_start():
    e = FakeEvent("session_start", {"user_prompt": "build a todo app"})
    assert _extract_user_content(e) == "build a todo app"


def test_extract_user_content_user_message():
    e = FakeEvent("user_message", {"content": "add dark mode"})
    assert _extract_user_content(e) == "add dark mode"


def test_extract_user_content_tool_use_returns_none():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {}})
    assert _extract_user_content(e) is None


def test_extract_user_content_empty_payload():
    e = FakeEvent("user_message", {})
    assert _extract_user_content(e) is None


# --- process_event: baseline creation ---

@pytest.mark.asyncio
@patch("core.processor.generate_baseline", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.update_snapshot", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_baseline_created_on_first_user_message(
    mock_flag_check, mock_alignment, mock_snapshot, mock_push_align, mock_push_flag, mock_baseline, db, sample_session
):
    mock_baseline.return_value = {
        "goal": "Build a todo app",
        "must_have": ["task list", "add task"],
        "must_not": [],
        "success_checks": ["app runs"],
    }
    mock_snapshot.return_value = {"current_goal": "Starting", "current_plan": [], "current_tasks": [], "done_claimed": False}
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.0, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(session_id=sample_session.id, type="user_message", payload={"content": "build a todo app"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    baseline = db.query(Baseline).filter(Baseline.session_id == sample_session.id).first()
    assert baseline is not None
    assert baseline.goal == "Build a todo app"
    mock_baseline.assert_called_once()


@pytest.mark.asyncio
@patch("core.processor.generate_baseline", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_follow_up_message_appended_to_baseline(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, mock_baseline, db, sample_session
):
    # Pre-create baseline
    baseline = Baseline(
        session_id=sample_session.id,
        user_request="build a todo app",
        goal="Build a todo app",
        must_have=["task list"],
        must_not=[],
        success_checks=[],
    )
    db.add(baseline)
    db.commit()

    mock_classify.return_value = {"phase_classification": "continuation", "phase_confidence": 0.9, "phase_summary": "Add dark mode", "message_type": "task_request"}
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.1, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(session_id=sample_session.id, type="user_message", payload={"content": "also add dark mode"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(baseline)
    assert "also add dark mode" in baseline.user_request
    assert "---" in baseline.user_request


@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.update_snapshot", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
async def test_assistant_response_normalizes_alignment_before_persist(
    mock_alignment, mock_snapshot, mock_push_align, mock_push_flag, db, sample_session
):
    baseline = Baseline(
        session_id=sample_session.id,
        user_request="test request",
        goal="Test goal",
        must_have=[],
        must_not=[],
        success_checks=[],
    )
    db.add(baseline)
    db.commit()

    mock_snapshot.return_value = {
        "current_goal": "Working",
        "current_plan": [],
        "current_tasks": [],
        "baseline_status": "raw status",
        "suggested_prompt": "raw prompt",
        "done_claimed": False,
    }
    mock_alignment.return_value = {
        "alignment": "정렬됨",
        "drift_score": "0.2",
        "mismatches": [],
        "missing_must_have": [],
        "constraint_violations": [],
        "new_scope": [],
    }

    event = Event(session_id=sample_session.id, type="assistant_response", payload={"content": "answer"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    snapshot = db.query(Snapshot).filter(Snapshot.event_id == event.id).first()
    assert snapshot is not None
    assert snapshot.alignment == "aligned"
    assert snapshot.drift_score == 0.2
    assert snapshot.baseline_status == "raw status"
    assert snapshot.suggested_prompt is None
    assert "alignment_details" in (snapshot.evidence or {})


@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.update_snapshot", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
async def test_assistant_response_includes_latest_user_intent_in_snapshot_prompt(
    mock_alignment, mock_snapshot, mock_push_align, mock_push_flag, db, sample_session
):
    baseline = Baseline(
        session_id=sample_session.id,
        user_request="감바스 레시피",
        goal="감바스 레시피 제공",
        must_have=[],
        must_not=[],
        success_checks=[],
    )
    db.add(baseline)
    db.commit()

    user_event = Event(
        session_id=sample_session.id,
        type="user_message",
        payload={"content": "새우 머리는 잘라야 해? 같이 넣어도 돼?"},
    )
    db.add(user_event)
    db.commit()
    db.refresh(user_event)

    mock_snapshot.return_value = {
        "current_goal": "새우 머리 처리 방법 포함 감바스 안내",
        "current_plan": [],
        "current_tasks": [],
        "baseline_status": "raw status",
        "suggested_prompt": None,
        "done_claimed": False,
    }
    mock_alignment.return_value = {
        "alignment": "aligned",
        "drift_score": 0.0,
        "mismatches": [],
        "missing_must_have": [],
        "constraint_violations": [],
        "new_scope": [],
    }

    event = Event(session_id=sample_session.id, type="assistant_response", payload={"content": "답변"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    call_args = mock_snapshot.call_args[0]
    event_dict = call_args[2]
    assert event_dict.get("inputs", {}).get("latest_user_intent") == "새우 머리는 잘라야 해? 같이 넣어도 돼?"


@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.update_snapshot", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
async def test_new_phase_context_keeps_goal_cumulative(
    mock_alignment, mock_snapshot, mock_push_align, mock_push_flag, db, sample_session
):
    baseline = Baseline(
        session_id=sample_session.id,
        user_request="마라탕 레시피",
        goal="마라탕 레시피 제공",
        must_have=[],
        must_not=[],
        success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase = Phase(
        session_id=sample_session.id,
        phase_num=2,
        baseline_id=baseline.id,
        baseline_version=1,
        classification="new_phase",
        user_message="마라탕 말고 마라샹궈도 알려줘",
        summary="마라샹궈를 추가 요청",
    )
    db.add(phase)
    db.commit()

    mock_snapshot.return_value = {
        "current_goal": "마라탕 + 마라샹궈 레시피 제공",
        "current_plan": [],
        "current_tasks": [],
        "baseline_status": "raw status",
        "suggested_prompt": None,
        "done_claimed": False,
    }
    mock_alignment.return_value = {
        "alignment": "aligned",
        "drift_score": 0.0,
        "mismatches": [],
        "missing_must_have": [],
        "constraint_violations": [],
        "new_scope": [],
    }

    event = Event(session_id=sample_session.id, type="assistant_response", payload={"content": "답변"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    call_args = mock_snapshot.call_args[0]
    event_dict = call_args[2]
    phase_context = event_dict.get("inputs", {}).get("phase_context", "")
    assert "Classification: new_phase" in phase_context
    assert "keep the existing primary objective and ADD" in phase_context
    assert "Do not replace/drop previous goal" in phase_context


@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.update_snapshot", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
async def test_pivot_context_replaces_goal(
    mock_alignment, mock_snapshot, mock_push_align, mock_push_flag, db, sample_session
):
    baseline = Baseline(
        session_id=sample_session.id,
        user_request="마라탕 레시피",
        goal="마라탕 레시피 제공",
        must_have=[],
        must_not=[],
        success_checks=[],
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)

    phase = Phase(
        session_id=sample_session.id,
        phase_num=2,
        baseline_id=baseline.id,
        baseline_version=2,
        classification="pivot",
        user_message="내일 날씨 알려줘",
        summary="요리에서 날씨 조회로 전환",
    )
    db.add(phase)
    db.commit()

    mock_snapshot.return_value = {
        "current_goal": "내일 날씨 안내",
        "current_plan": [],
        "current_tasks": [],
        "baseline_status": "raw status",
        "suggested_prompt": None,
        "done_claimed": False,
    }
    mock_alignment.return_value = {
        "alignment": "aligned",
        "drift_score": 0.0,
        "mismatches": [],
        "missing_must_have": [],
        "constraint_violations": [],
        "new_scope": [],
    }

    event = Event(session_id=sample_session.id, type="assistant_response", payload={"content": "답변"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    call_args = mock_snapshot.call_args[0]
    event_dict = call_args[2]
    phase_context = event_dict.get("inputs", {}).get("phase_context", "")
    assert "Classification: pivot" in phase_context
    assert "replace the previous objective with this new direction" in phase_context


@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.update_snapshot", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_snapshot_evidence_is_scoped_to_current_turn(
    mock_flag_check, mock_alignment, mock_snapshot, mock_push_align, mock_push_flag, db, sample_session
):
    baseline = Baseline(
        session_id=sample_session.id,
        user_request="test request",
        goal="Test goal",
        must_have=[],
        must_not=[],
        success_checks=[],
    )
    db.add(baseline)
    db.commit()

    mock_snapshot.return_value = {
        "current_goal": "Working",
        "current_plan": [],
        "current_tasks": [],
        "baseline_status": "raw status",
        "suggested_prompt": None,
        "done_claimed": False,
    }
    mock_alignment.return_value = {
        "alignment": "aligned",
        "drift_score": 0.1,
        "mismatches": [],
        "missing_must_have": [],
        "constraint_violations": [],
        "new_scope": [],
    }
    mock_flag_check.return_value = None

    now = datetime.utcnow()

    turn1_tool = Event(
        session_id=sample_session.id,
        ts=now + timedelta(seconds=1),
        type="tool_use",
        payload={"tool": "ReadFile", "inputs": {"path": "testhook/main.py"}},
    )
    db.add(turn1_tool)
    db.commit()

    turn1_assistant = Event(
        session_id=sample_session.id,
        ts=now + timedelta(seconds=2),
        type="assistant_response",
        payload={"content": "turn1 done"},
    )
    db.add(turn1_assistant)
    db.commit()
    db.refresh(turn1_assistant)

    await process_event(db, sample_session.id, turn1_assistant)

    snap1 = db.query(Snapshot).filter(Snapshot.event_id == turn1_assistant.id).first()
    assert snap1 is not None
    assert "testhook/main.py" in (snap1.evidence or {}).get("files_read", [])

    turn2_tool = Event(
        session_id=sample_session.id,
        ts=now + timedelta(seconds=3),
        type="tool_use",
        payload={"tool": "WriteFile", "inputs": {"path": "testhook/star_pattern.py"}},
    )
    db.add(turn2_tool)
    db.commit()

    turn2_assistant = Event(
        session_id=sample_session.id,
        ts=now + timedelta(seconds=4),
        type="assistant_response",
        payload={"content": "turn2 done"},
    )
    db.add(turn2_assistant)
    db.commit()
    db.refresh(turn2_assistant)

    await process_event(db, sample_session.id, turn2_assistant)

    snap2 = db.query(Snapshot).filter(Snapshot.event_id == turn2_assistant.id).first()
    assert snap2 is not None
    assert "testhook/star_pattern.py" in (snap2.evidence or {}).get("files_created", [])
    assert "testhook/main.py" not in (snap2.evidence or {}).get("files_read", [])


# --- process_event: discussion messages still evolve context ---

@pytest.mark.asyncio
@patch("core.processor.generate_baseline", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor._push_context_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_discussion_message_appended_to_context(
    mock_flag_check, mock_alignment, mock_classify, mock_push_context, mock_push_align, mock_push_flag, mock_baseline, db, sample_session
):
    """Discussion messages should still be reflected in context evolution."""
    baseline = Baseline(
        session_id=sample_session.id,
        user_request="build a landing page",
        goal="Build a landing page",
        must_have=["hero section"],
        must_not=[],
        success_checks=[],
    )
    db.add(baseline)
    db.commit()

    mock_classify.return_value = {
        "phase_classification": "continuation", "phase_confidence": 0.8,
        "phase_summary": "User asking about competitor", "message_type": "discussion"
    }

    event = Event(
        session_id=sample_session.id, type="user_message",
        payload={"content": "A competitor released something similar. Is it still worth continuing?"},
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(baseline)
    assert "competitor" in baseline.user_request
    assert baseline.user_request.startswith("build a landing page")
    mock_push_context.assert_called_once()


# --- process_event: rule flags only on tool_use ---

@pytest.mark.asyncio
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.update_snapshot", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_rule_flags_on_tool_use(
    mock_flag_check, mock_alignment, mock_snapshot, mock_push_align, mock_push_flag, db, sample_session
):
    # Pre-create baseline so LLM path runs
    baseline = Baseline(
        session_id=sample_session.id, user_request="test", goal="test",
        must_have=[], must_not=[], success_checks=[],
    )
    db.add(baseline)
    db.commit()

    mock_snapshot.return_value = {"current_goal": "test", "current_plan": [], "current_tasks": [], "done_claimed": False}
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.0, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(
        session_id=sample_session.id, type="tool_use",
        payload={"tool": "Bash", "inputs": {"command": "rm -rf /important"}},
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    result = await process_event(db, sample_session.id, event)

    # Rule-based flag should be created
    flags = db.query(Flag).filter(Flag.session_id == sample_session.id).all()
    assert len(flags) >= 1
    assert any("rm -rf" in f.title for f in flags)


@pytest.mark.asyncio
@patch("core.processor.generate_baseline", new_callable=AsyncMock)
@patch("core.processor._push_flag_event", new_callable=AsyncMock)
@patch("core.processor._push_alignment_event", new_callable=AsyncMock)
@patch("core.processor.classify_phase", new_callable=AsyncMock)
@patch("core.processor.check_alignment", new_callable=AsyncMock)
@patch("core.processor.check_for_flag", new_callable=AsyncMock)
async def test_no_rule_flags_on_user_message(
    mock_flag_check, mock_alignment, mock_classify, mock_push_align, mock_push_flag, mock_baseline, db, sample_session
):
    """User messages should NOT trigger rule-based flags (even if content matches patterns)."""
    mock_baseline.return_value = {"goal": "test", "must_have": [], "must_not": [], "success_checks": [], "confidence": 0.9}
    mock_classify.return_value = {"phase_classification": "continuation", "phase_confidence": 0.9, "phase_summary": "test", "message_type": "task_request"}
    mock_alignment.return_value = {"alignment": "aligned", "drift_score": 0.0, "mismatches": [], "missing_must_have": [], "constraint_violations": [], "new_scope": []}
    mock_flag_check.return_value = None

    event = Event(
        session_id=sample_session.id, type="user_message",
        payload={"content": "please run rm -rf /tmp/old"},
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    rule_flags = db.query(Flag).filter(
        Flag.session_id == sample_session.id,
        Flag.evidence.contains("rules"),
    ).all()
    # No rule-based flags should exist for user_message
    assert len(rule_flags) == 0


# --- process_event: verdict on stop ---

@pytest.mark.asyncio
async def test_verdict_ok_when_no_flags(db, sample_session):
    event = Event(session_id=sample_session.id, type="stop", payload={"reason": "completed"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(sample_session)
    assert sample_session.verdict == "ok"


@pytest.mark.asyncio
async def test_verdict_off_track_with_high_flag(db, sample_session):
    # Add a high flag
    flag = Flag(
        session_id=sample_session.id, severity="high", category="risky_action",
        title="test", rationale="test", evidence={}, jump={},
    )
    db.add(flag)
    db.commit()

    event = Event(session_id=sample_session.id, type="stop", payload={"reason": "completed"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(sample_session)
    assert sample_session.verdict == "off_track"


@pytest.mark.asyncio
async def test_verdict_needs_fix_with_low_flag(db, sample_session):
    flag = Flag(
        session_id=sample_session.id, severity="low", category="scope_bloat",
        title="test", rationale="test", evidence={}, jump={},
    )
    db.add(flag)
    db.commit()

    event = Event(session_id=sample_session.id, type="stop", payload={"reason": "completed"})
    db.add(event)
    db.commit()
    db.refresh(event)

    await process_event(db, sample_session.id, event)

    db.refresh(sample_session)
    assert sample_session.verdict == "needs_fix"
