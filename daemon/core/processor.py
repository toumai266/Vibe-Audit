import collections
from datetime import datetime
from typing import Optional, Tuple
from uuid import uuid4
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.exc import IntegrityError

from db.models import Session, Event, Baseline, Snapshot, Flag, Phase
from services.llm_service import (
    generate_baseline,
    classify_phase,
    summarize_context,
    update_snapshot,
    check_alignment,
    check_for_flag
)
from core.rules import check_rules
from core.turn_aggregator import aggregate_turn_evidence

# In-memory error ring buffer (last 50 errors)
_error_log = collections.deque(maxlen=50)

# Confidence threshold for automatic classification
CLASSIFICATION_CONFIDENCE_THRESHOLD = 0.5
# Pivot should have sufficient confidence; otherwise treat as new_phase unless
# strong off-topic/abandonment signals are present.
PIVOT_CONFIDENCE_THRESHOLD = 0.70
# Strongly off-topic messages should force pivot even when the model hesitates.
OFF_TOPIC_RELATION_THRESHOLD = 0.20
# Follow-up instructions with high topic relation should generally remain
# continuation unless they introduce explicit requirement changes.
FOLLOW_UP_RELATION_THRESHOLD = 0.50

ALIGNMENT_CANONICAL_MAP = {
    "aligned": "aligned",
    "align": "aligned",
    "on_track": "aligned",
    "ontrack": "aligned",
    "drifting": "drifting",
    "drifted": "drifting",  # legacy value
    "drift": "drifting",
    "off_track": "off_track",
    "offtrack": "off_track",
    # Localized values observed in existing sessions
    "일치": "aligned",
    "정렬됨": "aligned",
    "정렬": "aligned",
    "표류": "drifting",
    "이탈": "off_track",
    "벗어남": "off_track",
    "탈선": "off_track",
}


def _log_error(source: str, msg: str):
    entry = {"ts": datetime.utcnow().isoformat(), "source": source, "msg": str(msg)}
    _error_log.append(entry)
    print(f"[PIPELINE ERROR] {source}: {msg}")


def get_pipeline_errors():
    return list(_error_log)


def _normalize_alignment_value(value: Optional[str]) -> str:
    if not value:
        return "unknown"
    raw = str(value).strip().lower()
    key = raw.replace("-", "_").replace(" ", "_")
    mapped = ALIGNMENT_CANONICAL_MAP.get(key)
    if mapped:
        return mapped
    # Fallback heuristics for unexpected localized/variant outputs.
    if any(token in raw for token in ("aligned", "align", "on track", "일치", "정렬")):
        return "aligned"
    if any(token in raw for token in ("drifting", "drift", "표류")):
        return "drifting"
    if any(token in raw for token in ("off track", "off_track", "offtrack", "이탈", "벗어", "탈선")):
        return "off_track"
    return "unknown"


def _normalize_drift_score(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


def _extract_user_content(event: Event) -> Optional[str]:
    """
    Extract user content from an event for baseline generation.
    Handles multiple event formats from different data paths.
    """
    payload = event.payload or {}

    # session_start with user_prompt
    if event.type == "session_start":
        return payload.get("user_prompt") or payload.get("initial_goal") or None

    # user_message event (from hooks or /events API)
    if event.type == "user_message":
        return payload.get("content") or payload.get("user_prompt") or None

    return None


def _get_active_baseline(db: DBSession, session_id: str) -> Optional[Baseline]:
    """Get the active baseline for a session."""
    return db.query(Baseline).filter(
        Baseline.session_id == session_id,
        Baseline.is_active == True
    ).first()


def _get_current_phase(db: DBSession, session_id: str) -> Optional[Phase]:
    """Get the latest phase for a session."""
    return db.query(Phase).filter(
        Phase.session_id == session_id
    ).order_by(Phase.phase_num.desc()).first()


def _sync_user_request(baseline: Baseline):
    """Rebuild user_request from active context_messages (backward compat)."""
    msgs = baseline.context_messages or []
    active_texts = [m["text"] for m in msgs if m.get("active")]
    if active_texts:
        baseline.user_request = "\n---\n".join(active_texts)


def _append_context_message(baseline: Baseline, user_content: str, msg_type: str, event_ts: datetime,
                            parent_message_index: int = None):
    """Add a structured message to context_messages and sync user_request."""
    msgs = list(baseline.context_messages or [])
    # If this is the first structured message on a legacy baseline, seed from existing user_request
    if not msgs and baseline.user_request:
        msgs.append({
            "text": baseline.user_request[:500],
            "type": "task_request",
            "ts": (baseline.created_at or event_ts).isoformat() if hasattr(baseline, 'created_at') and baseline.created_at else event_ts.isoformat(),
            "active": True,
            "supersedes": None,
            "message_id": str(uuid4()),
            "parent_message_id": None
        })

    # Resolve parent_message_id from index
    parent_message_id = None
    if parent_message_index is not None:
        active_msgs = [m for m in msgs if m.get("active")]
        if 0 <= parent_message_index < len(active_msgs):
            parent_message_id = active_msgs[parent_message_index].get("message_id")

    msgs.append({
        "text": user_content[:500],
        "type": msg_type,
        "ts": event_ts.isoformat(),
        "active": True,
        "supersedes": None,
        "message_id": str(uuid4()),
        "parent_message_id": parent_message_id
    })
    baseline.context_messages = msgs
    _sync_user_request(baseline)


def _init_context_messages(baseline: Baseline, user_content: str, event_ts: datetime):
    """Initialize context_messages for a new baseline from its first user content."""
    baseline.context_messages = [{
        "text": user_content[:500],
        "type": "task_request",
        "ts": event_ts.isoformat(),
        "active": True,
        "supersedes": None,
        "message_id": str(uuid4())
    }]


async def _maybe_compact_context(db: DBSession, baseline: Baseline, threshold: int = 10, keep_recent: int = 5):
    """
    Compact old context messages into a summary when count exceeds threshold.
    Keeps the most recent `keep_recent` messages active, summarizes the rest.
    """
    msgs = baseline.context_messages or []
    active = [m for m in msgs if m.get("active")]
    if len(active) <= threshold:
        return

    old_msgs = active[:-keep_recent]
    try:
        summary = await summarize_context(old_msgs)
        if summary:
            # Merge with existing summary if present
            if baseline.context_summary:
                baseline.context_summary = f"{baseline.context_summary}\n{summary}"
            else:
                baseline.context_summary = summary

            # Mark old messages as inactive
            old_ids = {m.get("message_id") for m in old_msgs}
            updated_msgs = []
            for m in msgs:
                if m.get("message_id") in old_ids:
                    m["active"] = False
                updated_msgs.append(m)
            baseline.context_messages = updated_msgs
            _sync_user_request(baseline)
            db.commit()
            print(f"[CONTEXT] Compacted {len(old_msgs)} old messages into summary")
    except Exception as e:
        _log_error("context_compact", f"Context compaction failed: {e}")


async def _handle_phase_transition(
    db: DBSession,
    session_id: str,
    event: Event,
    user_content: str,
    classification: str,
    confidence: float,
    phase_summary: str,
    baseline: Baseline,
    classification_data: dict = None
) -> Tuple[Phase, Baseline]:
    """
    Handle phase transition based on classification.

    Returns: (newly_created_or_updated_phase, current_baseline)
    """
    classification_data = classification_data or {}
    current_phase = _get_current_phase(db, session_id)
    next_phase_num = (current_phase.phase_num + 1) if current_phase else 1

    def _to_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    def _to_score(value):
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, score))

    topic_relation_score = _to_score(classification_data.get("topic_relation_score"))
    explicit_abandonment = _to_bool(classification_data.get("explicit_abandonment"))
    parent_message_index = classification_data.get("parent_message_index")
    goal_changed = _to_bool(classification_data.get("goal_changed"))
    must_have_delta = classification_data.get("must_have_delta")
    message_type = classification_data.get("message_type", "task_request")
    strong_pivot_signal = explicit_abandonment or (
        topic_relation_score is not None and topic_relation_score <= OFF_TOPIC_RELATION_THRESHOLD
    )
    related_topic = topic_relation_score is None or topic_relation_score >= FOLLOW_UP_RELATION_THRESHOLD
    follow_up_without_scope_change = (
        message_type == "task_request"
        and parent_message_index is not None
        and not goal_changed
        and not must_have_delta
        and related_topic
    )

    # Apply confidence policy:
    # - Strong off-topic or explicit abandonment: always treat as pivot
    # - pivot: require enough confidence unless strong pivot signal exists
    # - new_phase: if very low confidence, downgrade to continuation
    effective_classification = classification
    if classification in ("new_phase", "pivot") and strong_pivot_signal:
        effective_classification = "pivot"
        print(
            f"[PHASE] Promoting to pivot due strong signal "
            f"(explicit_abandonment={explicit_abandonment}, topic_relation_score={topic_relation_score})"
        )
    elif classification == "new_phase" and not explicit_abandonment and follow_up_without_scope_change:
        print(
            "[PHASE] Follow-up instruction without scope change; "
            "defaulting new_phase to continuation"
        )
        effective_classification = "continuation"
    elif classification == "pivot" and confidence < PIVOT_CONFIDENCE_THRESHOLD:
        print(f"[PHASE] Pivot confidence ({confidence:.2f}) below threshold, defaulting to new_phase")
        effective_classification = "new_phase"
    elif classification == "new_phase" and confidence < CLASSIFICATION_CONFIDENCE_THRESHOLD:
        print(f"[PHASE] Low confidence ({confidence:.2f}) for new_phase, defaulting to continuation")
        effective_classification = "continuation"

    def _maybe_evolve_goal():
        """Update baseline goal if classify_phase returned an updated_goal."""
        updated_goal = classification_data.get("updated_goal")
        if updated_goal and classification_data.get("goal_changed"):
            baseline.goal = updated_goal
            print(f"[PHASE] Goal evolved: {baseline.goal[:80]}...")

    def _apply_must_have_delta():
        """Apply must_have changes (add/remove/refine) from classification."""
        delta = classification_data.get("must_have_delta")
        if not delta:
            return
        must_have = list(baseline.must_have or [])
        history = list(baseline.must_have_history or [])
        ts_now = datetime.utcnow().isoformat()

        for item in (delta.get("add") or []):
            if item not in must_have:
                must_have.append(item)
                history.append({"action": "add", "item": item, "reason": phase_summary, "ts": ts_now})

        for item in (delta.get("remove") or []):
            if item in must_have:
                must_have.remove(item)
                history.append({"action": "remove", "item": item, "reason": phase_summary, "ts": ts_now})

        for ref in (delta.get("refine") or []):
            old_item = ref.get("from", "")
            new_item = ref.get("to", "")
            if old_item in must_have and new_item:
                idx = must_have.index(old_item)
                must_have[idx] = new_item
                history.append({"action": "refine", "item": f"{old_item} → {new_item}", "reason": phase_summary, "ts": ts_now})

        baseline.must_have = must_have
        baseline.must_have_history = history

    def _apply_supersession():
        """Mark a previous context message as superseded by the current one."""
        supersedes_idx = classification_data.get("supersedes_index")
        if supersedes_idx is None:
            return
        msgs = list(baseline.context_messages or [])
        active_msgs = [m for m in msgs if m.get("active")]
        if 0 <= supersedes_idx < len(active_msgs):
            target = active_msgs[supersedes_idx]
            target["active"] = False
            # Find the last message (just appended) and set supersedes
            for m in reversed(msgs):
                if m.get("active"):
                    m["supersedes"] = target.get("message_id")
                    break
            baseline.context_messages = msgs
            _sync_user_request(baseline)
            print(f"[CONTEXT] Message superseded: {target.get('text', '')[:60]}...")

    if effective_classification == "continuation":
        # Append structured context message + sync user_request
        if user_content not in (baseline.user_request or ""):
            msg_type = classification_data.get("message_type", "task_request")
            _append_context_message(baseline, user_content, msg_type, event.ts or datetime.utcnow(),
                                    parent_message_index=classification_data.get("parent_message_index"))

        # Keep baseline immutable during continuation/new_phase.
        # Baseline version changes only via pivot or explicit user edit.
        _apply_supersession()
        db.commit()

        # Compact old context messages if threshold exceeded
        await _maybe_compact_context(db, baseline)

        # Return existing phase or create first one if missing
        if not current_phase:
            current_phase = Phase(
                session_id=session_id,
                phase_num=1,
                trigger_event_id=event.id,
                baseline_id=baseline.id,
                baseline_version=baseline.version,
                classification="initial",
                user_message=user_content[:500],
                summary=phase_summary,
                confidence=confidence
            )
            db.add(current_phase)
            db.commit()
            db.refresh(current_phase)

        return current_phase, baseline

    elif effective_classification == "new_phase":
        # Create new Phase record, same baseline
        # Append structured context message + sync user_request
        if user_content not in (baseline.user_request or ""):
            msg_type = classification_data.get("message_type", "task_request")
            _append_context_message(baseline, user_content, msg_type, event.ts or datetime.utcnow(),
                                    parent_message_index=classification_data.get("parent_message_index"))

        # Keep baseline immutable during continuation/new_phase.
        # Baseline version changes only via pivot or explicit user edit.
        _apply_supersession()
        db.commit()

        # Compact old context messages if threshold exceeded
        await _maybe_compact_context(db, baseline)

        new_phase = Phase(
            session_id=session_id,
            phase_num=next_phase_num,
            trigger_event_id=event.id,
            baseline_id=baseline.id,
            baseline_version=baseline.version,
            classification="new_phase",
            user_message=user_content[:500],
            summary=phase_summary,
            confidence=confidence
        )
        db.add(new_phase)
        db.commit()
        db.refresh(new_phase)
        
        # SSE push for new phase
        await _push_phase_event(session_id, new_phase)
        
        return new_phase, baseline
    
    elif effective_classification == "pivot":
        # Archive current baseline
        baseline.is_active = False
        baseline.archived_at = datetime.utcnow()
        db.commit()
        
        # Generate new baseline from pivoted direction
        try:
            baseline_data = await generate_baseline(user_content)
            new_baseline = Baseline(
                session_id=session_id,
                version=baseline.version + 1,
                is_active=True,
                user_request=user_content[:500],
                goal=baseline_data.get("goal", "Unknown"),
                must_have=baseline_data.get("must_have", []),
                must_not=baseline_data.get("must_not", []),
                success_checks=baseline_data.get("success_checks", []),
                confidence=baseline_data.get("confidence", 1.0),
                ambiguity_notes=baseline_data.get("ambiguity_notes")
            )
            _init_context_messages(new_baseline, user_content, event.ts or datetime.utcnow())
            db.add(new_baseline)
            db.commit()
            db.refresh(new_baseline)
        except Exception as e:
            _log_error("pivot_baseline", f"Failed to generate new baseline on pivot: {e}")
            # Fallback: reactivate old baseline
            baseline.is_active = True
            baseline.archived_at = None
            db.commit()
            return current_phase, baseline
        
        # Create new phase with pivot classification
        new_phase = Phase(
            session_id=session_id,
            phase_num=next_phase_num,
            trigger_event_id=event.id,
            baseline_id=new_baseline.id,
            baseline_version=new_baseline.version,
            classification="pivot",
            user_message=user_content[:500],
            summary=phase_summary,
            confidence=confidence
        )
        db.add(new_phase)
        db.commit()
        db.refresh(new_phase)
        
        # SSE push for pivot (phase + baseline change)
        await _push_phase_event(session_id, new_phase)
        await _push_baseline_version_event(session_id, new_baseline)
        
        return new_phase, new_baseline
    
    # Fallback
    return current_phase, baseline


async def process_event(db: DBSession, session_id: str, event: Event) -> Optional[Flag]:
    """
    Process incoming event through the analysis pipeline:
    1. Auto-generate baseline from first user content
    2. user_message: baseline + phase classification only (early return)
    3. assistant_response: full LLM analysis (snapshot → alignment → flags)
    4. Push SSE events for real-time UI updates
    """
    session = db.query(Session).filter(Session.id == session_id).first()
    if not session:
        return None

    # --- Step 1: Get or create baseline ---
    baseline = _get_active_baseline(db, session_id)
    current_phase = _get_current_phase(db, session_id)
    user_content = _extract_user_content(event)

    if not baseline and user_content:
        # First user content - create initial baseline and phase
        try:
            baseline_data = await generate_baseline(user_content)
            # Re-check after LLM call to minimize baseline race on concurrent events.
            baseline = _get_active_baseline(db, session_id)
            if not baseline:
                baseline = Baseline(
                    session_id=session_id,
                    version=1,
                    is_active=True,
                    user_request=user_content[:500],
                    goal=baseline_data.get("goal", "Unknown"),
                    must_have=baseline_data.get("must_have", []),
                    must_not=baseline_data.get("must_not", []),
                    success_checks=baseline_data.get("success_checks", []),
                    confidence=baseline_data.get("confidence", 1.0),
                    ambiguity_notes=baseline_data.get("ambiguity_notes")
                )
                _init_context_messages(baseline, user_content, event.ts or datetime.utcnow())
                db.add(baseline)
                db.commit()
                db.refresh(baseline)

            # Create initial phase only once.
            current_phase = _get_current_phase(db, session_id)
            if not current_phase and baseline:
                current_phase = Phase(
                    session_id=session_id,
                    phase_num=1,
                    trigger_event_id=event.id,
                    baseline_id=baseline.id,
                    baseline_version=1,
                    classification="initial",
                    user_message=user_content[:500],
                    summary=baseline_data.get("goal", "Initial goal"),
                    confidence=1.0
                )
                db.add(current_phase)
                db.commit()
                db.refresh(current_phase)
        except IntegrityError:
            # Another background worker/session created baseline first.
            db.rollback()
            baseline = _get_active_baseline(db, session_id)
            current_phase = _get_current_phase(db, session_id)
            if not baseline:
                _log_error("baseline", f"Baseline race detected but not found for session {session_id}")
        except Exception as e:
            db.rollback()
            _log_error("baseline", f"Baseline generation failed: {e}")

    # --- Step 2: Rule-based flag checks (only for agent actions, not user messages) ---
    created_flag = None
    rule_flags = []
    if event.type in ("tool_use", "tool_result"):
        rule_flags = check_rules(event)

    for rf in rule_flags:
        flag = Flag(
            snapshot_id=None,
            session_id=session_id,
            event_id=event.id,
            severity=rf["severity"],
            category=rf["category"],
            title=rf["title"],
            rationale=rf["rationale"],
            evidence={"event_type": event.type, "source": "rules"},
            jump={"event_id": event.id}
        )
        db.add(flag)
        db.commit()
        db.refresh(flag)
        created_flag = flag

        # SSE push for rule-based flags
        await _push_flag_event(session_id, flag)

    # --- tool_use: rule-based checks only, no LLM analysis ---
    # LLM analysis runs on assistant_response (complete agent turn), not per-tool.
    # This prevents snapshot flooding (N snapshots per turn) and false positive flags.
    if event.type in ("tool_use", "tool_result"):
        return created_flag

    # --- Handle session stop (verdict update only, no LLM analysis) ---
    if event.type == "stop":
        high_flags = db.query(Flag).filter(
            Flag.session_id == session_id,
            Flag.severity == "high"
        ).count()

        if high_flags > 0:
            session.verdict = "off_track"
        elif db.query(Flag).filter(Flag.session_id == session_id).count() > 0:
            session.verdict = "needs_fix"
        else:
            session.verdict = "ok"

        db.commit()
        return created_flag

    # --- user_message: lightweight phase classification only, no full LLM analysis ---
    # Full analysis (snapshot + alignment) runs when assistant_response arrives.
    if event.type == "user_message" and baseline and user_content:
        try:
            classification_data = await classify_phase(
                baseline.goal, baseline.must_have or [], user_content,
                context_messages=baseline.context_messages or []
            )
            classification = classification_data.get("phase_classification")
            confidence = classification_data.get("phase_confidence", 0.5)
            phase_summary = classification_data.get("phase_summary", "")

            if classification:
                current_phase, baseline = await _handle_phase_transition(
                    db, session_id, event, user_content,
                    classification, confidence, phase_summary, baseline,
                    classification_data=classification_data
                )
                effective_classification = (
                    current_phase.classification if current_phase and current_phase.classification else classification
                )
                await _push_context_event(session_id, {
                    "classification": effective_classification,
                    "raw_classification": classification,
                    "message_type": classification_data.get("message_type", "task_request"),
                    "confidence": confidence,
                    "baseline_version": baseline.version if baseline else None,
                })
        except Exception as e:
            db.rollback()
            _log_error("phase_classification", f"Phase classification failed: {e}")
        return created_flag

    # --- Step 3: LLM-based analysis (requires baseline) ---
    # Runs for assistant_response and other non-user events.
    if not baseline:
        return created_flag

    if event.type == "session_start":
        return created_flag

    baseline_dict = {
        "goal": baseline.goal,
        "must_have": baseline.must_have,
        "must_not": baseline.must_not,
        "success_checks": baseline.success_checks,
        "user_request": baseline.user_request or "",
        "context_messages": baseline.context_messages or [],
        "context_summary": baseline.context_summary or ""
    }

    # Get previous snapshot
    prev_snapshot = db.query(Snapshot).filter(
        Snapshot.session_id == session_id
    ).order_by(Snapshot.ts.desc()).first()

    prev_snapshot_dict = None
    if prev_snapshot:
        prev_snapshot_dict = {
            "current_goal": prev_snapshot.current_goal,
            "current_plan": prev_snapshot.current_plan,
            "current_tasks": prev_snapshot.current_tasks
        }

    # Build event_dict for LLM analysis
    payload = event.payload or {}
    latest_user_intent = ""
    if event.type in ("assistant_response", "agent_response", "assistant_update"):
        latest_user_event = db.query(Event).filter(
            Event.session_id == session_id,
            Event.type == "user_message",
            Event.ts <= (event.ts or datetime.utcnow())
        ).order_by(Event.ts.desc(), Event.id.desc()).first()
        if latest_user_event and isinstance(latest_user_event.payload, dict):
            latest_user_intent = (latest_user_event.payload.get("content") or "").strip()[:500]

    # Get current phase context to inform the LLM about phase shifts
    current_phase = _get_current_phase(db, session_id)
    phase_context = ""
    if current_phase and current_phase.classification == "new_phase":
        phase_context = (
            f"\n[PHASE SHIFT] Classification: new_phase\n"
            f"Phase summary: {current_phase.summary or 'N/A'}\n"
            f"User message that triggered this: {current_phase.user_message or 'N/A'}\n"
            "For current_goal, keep the existing primary objective and ADD this as a related sub-goal. "
            "Do not replace/drop previous goal unless the user explicitly abandoned it."
        )
    elif current_phase and current_phase.classification == "pivot":
        phase_context = (
            f"\n[PHASE SHIFT] Classification: pivot\n"
            f"Phase summary: {current_phase.summary or 'N/A'}\n"
            f"User message that triggered this: {current_phase.user_message or 'N/A'}\n"
            "For current_goal, replace the previous objective with this new direction."
        )

    if event.type in ("assistant_response", "agent_response", "assistant_update"):
        content = payload.get("content", "")
        event_dict = {
            "tool": f"[{event.type}]",
            "inputs": {
                **({"phase_context": phase_context} if phase_context else {}),
                **({"latest_user_intent": latest_user_intent} if latest_user_intent else {}),
            },
            "outputs": content
        }
    else:
        event_dict = {
            "tool": payload.get("tool", payload.get("tool_name", "Unknown")),
            "inputs": payload.get("inputs", {}),
            "outputs": payload.get("outputs", payload.get("output", {}))
        }
        if phase_context:
            event_dict["inputs"] = {**event_dict["inputs"], "phase_context": phase_context}

    try:
        snapshot_data = await update_snapshot(baseline_dict, prev_snapshot_dict, event_dict)

        # Aggregate structured evidence for this assistant turn only.
        # Window: (previous snapshot ts, current assistant event ts]
        tool_query = db.query(Event).filter(
            Event.session_id == session_id,
            Event.type.in_(("tool_use", "tool_result")),
            Event.ts <= (event.ts or datetime.utcnow())
        )
        prev_boundary_ts = None
        if prev_snapshot:
            prev_event = getattr(prev_snapshot, "event", None)
            prev_boundary_ts = getattr(prev_event, "ts", None) or prev_snapshot.ts
        if prev_boundary_ts:
            tool_query = tool_query.filter(Event.ts > prev_boundary_ts)
        tool_events = [e.payload for e in tool_query.order_by(Event.ts.asc(), Event.id.asc()).all()]
        turn_evidence = aggregate_turn_evidence(tool_events) if tool_events else {}

        # Check alignment with structured evidence (not raw events)
        alignment_data = await check_alignment(baseline_dict, snapshot_data, turn_evidence)
        alignment_data = alignment_data or {}
        alignment_data["alignment"] = _normalize_alignment_value(alignment_data.get("alignment"))
        alignment_data["drift_score"] = _normalize_drift_score(alignment_data.get("drift_score", 0.0))

        def _to_text_list(value):
            if not isinstance(value, list):
                return []
            out = []
            for item in value:
                text = str(item).strip() if item is not None else ""
                if text:
                    out.append(text)
            return out[:5]

        alignment_details = {
            "mismatches": _to_text_list(alignment_data.get("mismatches")),
            "missing_must_have": _to_text_list(alignment_data.get("missing_must_have")),
            "constraint_violations": _to_text_list(alignment_data.get("constraint_violations")),
            "new_scope": _to_text_list(alignment_data.get("new_scope")),
        }
        snapshot_evidence = dict(turn_evidence or {})
        snapshot_evidence["alignment_details"] = alignment_details

        # Ensure baseline_status text is consistent with alignment result
        alignment_label = alignment_data["alignment"]
        if alignment_label == "aligned":
            # Keep LLM-generated status text (language-aware). Force only no correction prompt.
            snapshot_data["suggested_prompt"] = None
        elif alignment_label in ("drifting", "off_track"):
            mismatches = alignment_data.get("mismatches", [])
            if mismatches and not snapshot_data.get("baseline_status"):
                snapshot_data["baseline_status"] = f"Drifting from baseline: {mismatches[0]}"

        # Upsert snapshot: update existing if same event_id, else create new
        # This makes re-processing idempotent (e.g. Reload re-triggers last user_message)
        existing_snapshot = db.query(Snapshot).filter(
            Snapshot.event_id == event.id
        ).first()

        if existing_snapshot:
            existing_snapshot.phase_id = current_phase.id if current_phase else existing_snapshot.phase_id
            existing_snapshot.baseline_id = baseline.id if baseline else existing_snapshot.baseline_id
            existing_snapshot.current_goal = snapshot_data.get("current_goal", existing_snapshot.current_goal)
            existing_snapshot.current_plan = snapshot_data.get("current_plan", [])
            existing_snapshot.baseline_status = snapshot_data.get("baseline_status", "")
            existing_snapshot.suggested_prompt = snapshot_data.get("suggested_prompt")
            existing_snapshot.current_tasks = snapshot_data.get("current_tasks", [])
            existing_snapshot.done_claimed = snapshot_data.get("done_claimed", False)
            existing_snapshot.evidence = snapshot_evidence or existing_snapshot.evidence
            existing_snapshot.alignment = alignment_data.get("alignment", "aligned")
            existing_snapshot.drift_score = alignment_data.get("drift_score", 0.0)
            existing_snapshot.ts = datetime.utcnow()
            db.commit()
            snapshot = existing_snapshot
        else:
            snapshot = Snapshot(
                event_id=event.id,
                session_id=session_id,
                phase_id=current_phase.id if current_phase else None,
                baseline_id=baseline.id if baseline else None,
                current_goal=snapshot_data.get("current_goal", "Unknown"),
                current_plan=snapshot_data.get("current_plan", []),
                baseline_status=snapshot_data.get("baseline_status", ""),
                suggested_prompt=snapshot_data.get("suggested_prompt"),
                current_tasks=snapshot_data.get("current_tasks", []),
                done_claimed=snapshot_data.get("done_claimed", False),
                evidence=snapshot_evidence or {},
                alignment=alignment_data.get("alignment", "aligned"),
                drift_score=alignment_data.get("drift_score", 0.0)
            )
            db.add(snapshot)
            db.commit()
            db.refresh(snapshot)

        # SSE push for alignment changes
        await _push_alignment_event(session_id, alignment_data)

        # LLM flag checks: skip for non-tool events to avoid false positives
        # assistant_response has inputs={} which causes false incomplete_done flags
        flag_data = None
        if event.type not in ("assistant_response", "agent_response", "user_message", "session_start"):
            flag_data = await check_for_flag(baseline_dict, snapshot_data, event_dict, alignment_data)

        if flag_data:
            flag = Flag(
                snapshot_id=snapshot.id,
                session_id=session_id,
                event_id=event.id,
                severity=flag_data.get("severity", "low"),
                category=flag_data.get("category", "intent_drift"),
                title=flag_data.get("title", "Unknown issue"),
                rationale=flag_data.get("rationale", ""),
                evidence={"event_type": event.type, "source": "llm"},
                jump={"event_id": event.id, "snapshot_id": snapshot.id}
            )
            db.add(flag)
            db.commit()
            db.refresh(flag)
            created_flag = flag

            # SSE push for LLM flags
            await _push_flag_event(session_id, flag)

    except Exception as e:
        db.rollback()
        _log_error("llm_analysis", f"LLM analysis failed for event {event.id}: {e}")

    return created_flag


async def _push_flag_event(session_id: str, flag: Flag):
    """Push flag notification via SSE."""
    try:
        from api.sse import push_event
        await push_event(session_id, "flag", {
            "id": flag.id,
            "severity": flag.severity,
            "category": flag.category,
            "title": flag.title,
        })
    except Exception:
        pass


async def _push_alignment_event(session_id: str, alignment_data: dict):
    """Push alignment update via SSE."""
    try:
        from api.sse import push_event
        await push_event(session_id, "alignment", {
            "alignment": alignment_data.get("alignment", "unknown"),
            "drift_score": alignment_data.get("drift_score", 0.0),
        })
    except Exception:
        pass


async def _push_phase_event(session_id: str, phase: Phase):
    """Push phase transition notification via SSE."""
    try:
        from api.sse import push_event
        await push_event(session_id, "phase", {
            "id": phase.id,
            "phase_num": phase.phase_num,
            "classification": phase.classification,
            "summary": phase.summary,
            "baseline_version": phase.baseline_version,
        })
    except Exception:
        pass


async def _push_baseline_version_event(session_id: str, baseline: Baseline):
    """Push baseline version change notification via SSE."""
    try:
        from api.sse import push_event
        await push_event(session_id, "baseline_version", {
            "id": baseline.id,
            "version": baseline.version,
            "goal": baseline.goal,
        })
    except Exception:
        pass


async def _push_context_event(session_id: str, data: dict):
    """Push context evolution update for user-message classification results."""
    try:
        from api.sse import push_event
        await push_event(session_id, "context_updated", data)
    except Exception:
        pass
