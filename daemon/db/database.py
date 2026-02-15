from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager
import os
import uuid

from .models import Base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./vibe_audit.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _migrate_schema():
    """
    Migrate DB schema from old format to new format with phases/baseline versioning.

    Handles:
    - baselines table: old PK(session_id) → new PK(id) + version/is_active/archived_at
    - snapshots table: add phase_id, baseline_id columns
    - phases table: created by create_all() (new table)

    Idempotent: safe to run multiple times.
    """
    if "sqlite" not in DATABASE_URL:
        return

    with engine.connect() as conn:
        # Check what tables exist
        tables = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()}

        if "baselines" not in tables:
            return  # Fresh DB, create_all() will handle everything

        # Check if baselines already has 'version' column (= already migrated)
        columns = {row[1] for row in conn.execute(text(
            "PRAGMA table_info(baselines)"
        )).fetchall()}

        if "version" not in columns:
            print("[MIGRATION] Migrating baselines table to versioned schema...")

            # Read old data
            old_rows = conn.execute(text("SELECT * FROM baselines")).fetchall()
            # PRAGMA table_info returns: (cid, name, type, notnull, default, pk)
            old_col_names = [row[1] for row in conn.execute(text(
                "PRAGMA table_info(baselines)"
            )).fetchall()]

            # Create new table
            conn.execute(text("""
                CREATE TABLE baselines_new (
                    id VARCHAR(36) PRIMARY KEY,
                    session_id VARCHAR(36) NOT NULL REFERENCES sessions(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    archived_at DATETIME,
                    user_request TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    must_have JSON DEFAULT '[]',
                    must_not JSON DEFAULT '[]',
                    success_checks JSON DEFAULT '[]',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(session_id, version)
                )
            """))

            # Copy data: generate UUID for id, set version=1, is_active=1
            for row in old_rows:
                row_dict = dict(zip(old_col_names, row))
                new_id = str(uuid.uuid4())
                conn.execute(text("""
                    INSERT INTO baselines_new
                        (id, session_id, version, is_active, user_request, goal,
                         must_have, must_not, success_checks, created_at)
                    VALUES
                        (:id, :session_id, 1, 1, :user_request, :goal,
                         :must_have, :must_not, :success_checks, :created_at)
                """), {
                    "id": new_id,
                    "session_id": row_dict.get("session_id", ""),
                    "user_request": row_dict.get("user_request", ""),
                    "goal": row_dict.get("goal", ""),
                    "must_have": row_dict.get("must_have", "[]"),
                    "must_not": row_dict.get("must_not", "[]"),
                    "success_checks": row_dict.get("success_checks", "[]"),
                    "created_at": row_dict.get("created_at", ""),
                })

            # Swap tables
            conn.execute(text("DROP TABLE baselines"))
            conn.execute(text("ALTER TABLE baselines_new RENAME TO baselines"))
            print(f"[MIGRATION] Migrated {len(old_rows)} baselines to versioned schema")

        # Check if snapshots needs phase_id/baseline_id columns
        if "snapshots" in tables:
            snap_columns = {row[1] for row in conn.execute(text(
                "PRAGMA table_info(snapshots)"
            )).fetchall()}

            if "phase_id" not in snap_columns:
                conn.execute(text(
                    "ALTER TABLE snapshots ADD COLUMN phase_id VARCHAR(36) REFERENCES phases(id)"
                ))
                print("[MIGRATION] Added phase_id to snapshots")

            if "baseline_id" not in snap_columns:
                conn.execute(text(
                    "ALTER TABLE snapshots ADD COLUMN baseline_id VARCHAR(36) REFERENCES baselines(id)"
                ))
                print("[MIGRATION] Added baseline_id to snapshots")

            if "baseline_status" not in snap_columns:
                conn.execute(text(
                    "ALTER TABLE snapshots ADD COLUMN baseline_status TEXT DEFAULT ''"
                ))
                print("[MIGRATION] Added baseline_status to snapshots")

            if "suggested_prompt" not in snap_columns:
                conn.execute(text(
                    "ALTER TABLE snapshots ADD COLUMN suggested_prompt TEXT"
                ))
                print("[MIGRATION] Added suggested_prompt to snapshots")

        # Check if turns needs turn_key/sealed columns
        if "turns" in tables:
            turn_columns = {row[1] for row in conn.execute(text(
                "PRAGMA table_info(turns)"
            )).fetchall()}

            if "turn_key" not in turn_columns:
                conn.execute(text(
                    "ALTER TABLE turns ADD COLUMN turn_key VARCHAR(100)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_turns_turn_key ON turns(turn_key)"
                ))
                print("[MIGRATION] Added turn_key to turns")

            if "sealed" not in turn_columns:
                conn.execute(text(
                    "ALTER TABLE turns ADD COLUMN sealed BOOLEAN DEFAULT 0"
                ))
                print("[MIGRATION] Added sealed to turns")

        # Check if baselines needs confidence/ambiguity_notes columns
        if "baselines" in tables:
            bl_columns = {row[1] for row in conn.execute(text(
                "PRAGMA table_info(baselines)"
            )).fetchall()}

            if "confidence" not in bl_columns:
                conn.execute(text(
                    "ALTER TABLE baselines ADD COLUMN confidence FLOAT"
                ))
                print("[MIGRATION] Added confidence to baselines")

            if "ambiguity_notes" not in bl_columns:
                conn.execute(text(
                    "ALTER TABLE baselines ADD COLUMN ambiguity_notes TEXT"
                ))
                print("[MIGRATION] Added ambiguity_notes to baselines")

            if "context_messages" not in bl_columns:
                conn.execute(text(
                    "ALTER TABLE baselines ADD COLUMN context_messages JSON DEFAULT '[]'"
                ))
                print("[MIGRATION] Added context_messages to baselines")

            if "context_summary" not in bl_columns:
                conn.execute(text(
                    "ALTER TABLE baselines ADD COLUMN context_summary TEXT"
                ))
                print("[MIGRATION] Added context_summary to baselines")

            if "must_have_history" not in bl_columns:
                conn.execute(text(
                    "ALTER TABLE baselines ADD COLUMN must_have_history JSON DEFAULT '[]'"
                ))
                print("[MIGRATION] Added must_have_history to baselines")

        conn.commit()
        print("[MIGRATION] Schema migration complete")


def init_db():
    """Initialize database tables (with migration for existing DBs)"""
    try:
        _migrate_schema()
    except Exception as e:
        print(f"[MIGRATION WARNING] Migration failed (may be ok for fresh DB): {e}")
    Base.metadata.create_all(bind=engine)


def get_db():
    """Dependency for FastAPI routes"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_context():
    """Context manager for non-FastAPI usage"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
