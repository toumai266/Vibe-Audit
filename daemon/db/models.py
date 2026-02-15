import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy import create_engine, Column, String, DateTime, Float, Boolean, ForeignKey, JSON, Enum as SQLEnum, Text, Integer, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.dialects.sqlite import JSON as SQLiteJSON
import enum

Base = declarative_base()


class VerdictEnum(str, enum.Enum):
    OK = "ok"
    NEEDS_FIX = "needs_fix"
    OFF_TRACK = "off_track"
    UNKNOWN = "unknown"


class EventTypeEnum(str, enum.Enum):
    POST_TOOL_USE = "post_tool_use"
    SUBAGENT_STOP = "subagent_stop"
    PRE_COMPACT = "pre_compact"
    STOP = "stop"
    SESSION_START = "session_start"


class SeverityEnum(str, enum.Enum):
    HIGH = "high"
    MED = "med"
    LOW = "low"


class AlignmentEnum(str, enum.Enum):
    ALIGNED = "aligned"
    DRIFTING = "drifting"
    OFF_TRACK = "off_track"


class FlagCategoryEnum(str, enum.Enum):
    INTENT_DRIFT = "intent_drift"
    SCOPE_BLOAT = "scope_bloat"
    INCOMPLETE_DONE = "incomplete_done"
    RISKY_ACTION = "risky_action"
    POLICY_VIOLATION = "policy_violation"


class PhaseClassificationEnum(str, enum.Enum):
    INITIAL = "initial"
    CONTINUATION = "continuation"
    NEW_PHASE = "new_phase"
    PIVOT = "pivot"


def generate_uuid():
    return str(uuid.uuid4())


class Session(Base):
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    start_ts = Column(DateTime, nullable=False, default=datetime.utcnow)
    end_ts = Column(DateTime, nullable=True)
    verdict = Column(String(20), default=VerdictEnum.UNKNOWN.value)
    cli_command = Column(Text, nullable=True)

    # Relationships
    events = relationship("Event", back_populates="session", cascade="all, delete-orphan")
    baselines = relationship("Baseline", back_populates="session", cascade="all, delete-orphan")
    phases = relationship("Phase", back_populates="session", cascade="all, delete-orphan")
    snapshots = relationship("Snapshot", back_populates="session", cascade="all, delete-orphan")
    flags = relationship("Flag", back_populates="session", cascade="all, delete-orphan")

    @property
    def active_baseline(self):
        """Get the currently active baseline (latest active version)."""
        for b in self.baselines:
            if b.is_active:
                return b
        return self.baselines[0] if self.baselines else None

    @property
    def baseline(self):
        """Backward compatibility: alias for active_baseline."""
        return self.active_baseline


class Event(Base):
    __tablename__ = "events"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)
    type = Column(String(30), nullable=False)
    payload = Column(SQLiteJSON, nullable=False, default=dict)

    # Relationships
    session = relationship("Session", back_populates="events")
    snapshots = relationship("Snapshot", back_populates="event")


class Baseline(Base):
    """Request baseline with versioning support."""
    __tablename__ = "baselines"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)
    archived_at = Column(DateTime, nullable=True)
    
    user_request = Column(Text, nullable=False)
    goal = Column(Text, nullable=False)
    must_have = Column(SQLiteJSON, default=list)
    must_not = Column(SQLiteJSON, default=list)
    success_checks = Column(SQLiteJSON, default=list)
    confidence = Column(Float, nullable=True)          # 0.0-1.0 baseline confidence
    ambiguity_notes = Column(Text, nullable=True)      # explanation of ambiguous parts
    context_messages = Column(SQLiteJSON, default=list) # [{text, type, ts, active, supersedes, message_id}, ...]
    context_summary = Column(Text, nullable=True)      # summary of compacted (inactive) older messages
    must_have_history = Column(SQLiteJSON, default=list) # [{action, item, reason, ts}, ...]
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Unique constraint: one version number per session
    __table_args__ = (UniqueConstraint('session_id', 'version', name='uq_session_version'),)

    # Relationships
    session = relationship("Session", back_populates="baselines")
    phases = relationship("Phase", back_populates="baseline")
    snapshots = relationship("Snapshot", back_populates="baseline")


class Phase(Base):
    """Phase represents a distinct sub-goal or direction within a session."""
    __tablename__ = "phases"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False)
    phase_num = Column(Integer, nullable=False)  # 1-based sequential
    trigger_event_id = Column(String(36), ForeignKey("events.id"), nullable=True)
    baseline_id = Column(String(36), ForeignKey("baselines.id"), nullable=False)
    baseline_version = Column(Integer, nullable=False)  # Denormalized for quick lookup
    
    classification = Column(String(20), nullable=False, default=PhaseClassificationEnum.INITIAL.value)
    user_message = Column(Text, nullable=True)  # The triggering user message
    summary = Column(Text, nullable=True)  # LLM-generated one-liner
    confidence = Column(Float, nullable=True)  # Classification confidence (0.0-1.0)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    session = relationship("Session", back_populates="phases")
    trigger_event = relationship("Event", foreign_keys=[trigger_event_id])
    baseline = relationship("Baseline", back_populates="phases")
    snapshots = relationship("Snapshot", back_populates="phase")


class Snapshot(Base):
    __tablename__ = "snapshots"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    event_id = Column(String(36), ForeignKey("events.id"), nullable=False)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False)
    phase_id = Column(String(36), ForeignKey("phases.id"), nullable=True)  # NEW: phase FK
    baseline_id = Column(String(36), ForeignKey("baselines.id"), nullable=True)  # NEW: baseline FK
    
    current_goal = Column(Text, nullable=False)
    current_plan = Column(SQLiteJSON, default=list)
    baseline_status = Column(Text, default="")
    suggested_prompt = Column(Text, nullable=True)
    current_tasks = Column(SQLiteJSON, default=list)
    done_claimed = Column(Boolean, default=False)
    evidence = Column(SQLiteJSON, default=dict)
    alignment = Column(String(20), default=AlignmentEnum.ALIGNED.value)
    drift_score = Column(Float, default=0.0)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    event = relationship("Event", back_populates="snapshots")
    session = relationship("Session", back_populates="snapshots")
    phase = relationship("Phase", back_populates="snapshots")
    baseline = relationship("Baseline", back_populates="snapshots")
    flags = relationship("Flag", back_populates="snapshot")


class Flag(Base):
    __tablename__ = "flags"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    snapshot_id = Column(String(36), ForeignKey("snapshots.id"), nullable=True)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False)
    event_id = Column(String(36), ForeignKey("events.id"), nullable=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)
    severity = Column(String(10), nullable=False)
    category = Column(String(30), nullable=False)
    title = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
    evidence = Column(SQLiteJSON, default=dict)
    jump = Column(SQLiteJSON, default=dict)

    # Relationships
    snapshot = relationship("Snapshot", back_populates="flags")
    session = relationship("Session", back_populates="flags")


class Turn(Base):
    """Conversation turn: 1 user message + all agent responses = 1 turn."""
    __tablename__ = "turns"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False)
    turn_key = Column(String(100), nullable=True, index=True)  # hook message key for upsert
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)
    role = Column(String(20), nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    sealed = Column(Boolean, default=False)  # True when turn is finalized for analysis
    alignment = Column(String(20), default="unknown")  # aligned, drifting, off_track, unknown
    warning = Column(Text, nullable=True)  # Drift warning message if any

    # Relationships
    session = relationship("Session", backref="turns")
