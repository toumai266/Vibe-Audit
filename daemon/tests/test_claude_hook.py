"""Tests for Claude hook filtering and turn forwarding behavior."""
import importlib.util
from pathlib import Path


def _load_claude_hook_module():
    root = Path(__file__).resolve().parents[2]
    hook_path = root / "hooks" / "claude_hook.py"
    spec = importlib.util.spec_from_file_location("claude_hook_test_module", hook_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


claude_hook = _load_claude_hook_module()


def test_is_ignored_user_prompt_task_notification():
    prompt = "<task-notification>\n<status>completed</status>\n</task-notification>"
    assert claude_hook.is_ignored_user_prompt(prompt) is True
    assert claude_hook.is_ignored_user_prompt("  <TASK-NOTIFICATION>...") is True
    assert claude_hook.is_ignored_user_prompt("real user message") is False


def test_handle_user_prompt_submit_skips_task_notification(monkeypatch):
    calls = {"send_turn": 0, "ensure_session": 0}

    def fake_send_turn(_payload):
        calls["send_turn"] += 1
        return {"action": "created"}

    def fake_ensure_session(*_args, **_kwargs):
        calls["ensure_session"] += 1
        return "vibe-session-1"

    monkeypatch.setattr(claude_hook, "send_turn", fake_send_turn)
    monkeypatch.setattr(claude_hook, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(claude_hook, "get_vibe_session_id", lambda _sid: "vibe-session-1")
    monkeypatch.setattr(claude_hook, "load_turn_state", lambda: {})
    monkeypatch.setattr(claude_hook, "save_turn_state", lambda _state: None)
    monkeypatch.setattr(claude_hook, "load_tool_buffer", lambda: {})
    monkeypatch.setattr(claude_hook, "save_tool_buffer", lambda _buf: None)
    monkeypatch.setattr(claude_hook, "log_debug", lambda *_args, **_kwargs: None)

    claude_hook.handle_user_prompt_submit(
        "claude-session-1",
        {"prompt": "<task-notification>\n<status>completed</status>\n</task-notification>"},
    )

    assert calls["send_turn"] == 0
    assert calls["ensure_session"] == 0


def test_post_tool_use_aliases_unknown_session_to_active_turn_owner(monkeypatch):
    posted = []
    aliases = []

    monkeypatch.setattr(claude_hook, "get_vibe_session_id", lambda _sid: None)
    monkeypatch.setattr(
        claude_hook,
        "load_sessions_map",
        lambda: {
            "claude-main": "vibe-main",
            "claude-old": "vibe-old",
        },
    )
    monkeypatch.setattr(
        claude_hook,
        "load_turn_state",
        lambda: {
            "claude-main": {"prompt_hash": "abc", "started_at": 999_999_999_999},
            "claude-old": {"prompt_hash": "def", "started_at": 1},
        },
    )
    monkeypatch.setattr(claude_hook, "time", type("T", (), {"time": staticmethod(lambda: 1_000_000_000_000)}))
    monkeypatch.setattr(claude_hook, "load_tool_buffer", lambda: {"claude-main": [{"tool": "Read"}]})
    monkeypatch.setattr(claude_hook, "set_vibe_session_id", lambda sid, vibe: aliases.append((sid, vibe)))
    monkeypatch.setattr(claude_hook, "post_event", lambda payload: posted.append(payload) or {"ok": True})
    monkeypatch.setattr(claude_hook, "save_tool_buffer", lambda _buf: None)
    monkeypatch.setattr(claude_hook, "log_debug", lambda *_args, **_kwargs: None)

    claude_hook.handle_post_tool_use(
        "claude-internal-new",
        {"tool_name": "Read", "tool_input": {"file_path": "README.md"}, "tool_output": "ok"},
    )

    assert aliases == [("claude-internal-new", "vibe-main")]
    assert len(posted) == 1
    assert posted[0]["session_id"] == "vibe-main"
    assert posted[0]["type"] == "tool_use"


def test_post_tool_use_drops_unresolved_unknown_session(monkeypatch):
    posted = []

    monkeypatch.setattr(claude_hook, "get_vibe_session_id", lambda _sid: None)
    monkeypatch.setattr(claude_hook, "load_sessions_map", lambda: {})
    monkeypatch.setattr(claude_hook, "load_turn_state", lambda: {})
    monkeypatch.setattr(claude_hook, "load_tool_buffer", lambda: {})
    monkeypatch.setattr(claude_hook, "set_vibe_session_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claude_hook, "post_event", lambda payload: posted.append(payload) or {"ok": True})
    monkeypatch.setattr(claude_hook, "save_tool_buffer", lambda _buf: None)
    monkeypatch.setattr(claude_hook, "log_debug", lambda *_args, **_kwargs: None)

    claude_hook.handle_post_tool_use(
        "claude-unknown",
        {"tool_name": "Read", "tool_input": {"file_path": "README.md"}, "tool_output": "ok"},
    )

    assert posted == []


def test_resolve_non_user_event_prefers_transcript_binding(monkeypatch):
    aliases = []

    monkeypatch.setattr(claude_hook, "get_vibe_session_id", lambda _sid: None)
    monkeypatch.setattr(
        claude_hook,
        "get_transcript_binding",
        lambda _path: {"vibe_session_id": "vibe-main", "owner_session_id": "claude-main"},
    )
    monkeypatch.setattr(
        claude_hook,
        "_alias_session",
        lambda sid, vibe, owner_sid="", transcript_path="": aliases.append((sid, vibe, owner_sid, transcript_path)),
    )
    monkeypatch.setattr(claude_hook, "log_debug", lambda *_args, **_kwargs: None)

    vibe = claude_hook.resolve_vibe_session_for_non_user_event(
        "claude-internal-new",
        {"transcript_path": "C:/tmp/session.jsonl"},
    )

    assert vibe == "vibe-main"
    assert aliases == [("claude-internal-new", "vibe-main", "claude-main", "C:/tmp/session.jsonl")]


def test_resolve_non_user_event_uses_transcript_key_turn_state(monkeypatch):
    aliases = []
    transcript_path = "C:/tmp/session-2.jsonl"
    transcript_key = claude_hook.get_transcript_key(transcript_path)

    monkeypatch.setattr(claude_hook, "get_vibe_session_id", lambda _sid: None)
    monkeypatch.setattr(claude_hook, "get_transcript_binding", lambda _path: None)
    monkeypatch.setattr(
        claude_hook,
        "load_sessions_map",
        lambda: {"claude-a": "vibe-a", "claude-b": "vibe-b"},
    )
    monkeypatch.setattr(
        claude_hook,
        "load_turn_state",
        lambda: {
            "claude-a": {"prompt_hash": "x", "started_at": 1_000.0, "transcript_key": "other"},
            "claude-b": {"prompt_hash": "y", "started_at": 1_200.0, "transcript_key": transcript_key},
        },
    )
    monkeypatch.setattr(claude_hook, "load_tool_buffer", lambda: {})
    monkeypatch.setattr(claude_hook, "time", type("T", (), {"time": staticmethod(lambda: 1_500.0)}))
    monkeypatch.setattr(
        claude_hook,
        "_alias_session",
        lambda sid, vibe, owner_sid="", transcript_path="": aliases.append((sid, vibe, owner_sid, transcript_path)),
    )
    monkeypatch.setattr(claude_hook, "log_debug", lambda *_args, **_kwargs: None)

    vibe = claude_hook.resolve_vibe_session_for_non_user_event(
        "claude-internal-next",
        {"transcript_path": transcript_path},
    )

    assert vibe == "vibe-b"
    assert aliases == [("claude-internal-next", "vibe-b", "claude-b", transcript_path)]


def test_handle_user_prompt_submit_rebinds_from_transcript_before_create(monkeypatch):
    calls = {"ensure_session": 0}
    sent = []
    aliased = []

    def fake_ensure(*_args, **_kwargs):
        calls["ensure_session"] += 1
        return "should-not-be-used"

    monkeypatch.setattr(claude_hook, "get_vibe_session_id", lambda _sid: None)
    monkeypatch.setattr(
        claude_hook,
        "get_transcript_binding",
        lambda _path: {"vibe_session_id": "vibe-existing", "owner_session_id": "claude-main"},
    )
    monkeypatch.setattr(claude_hook, "ensure_session", fake_ensure)
    monkeypatch.setattr(
        claude_hook,
        "_alias_session",
        lambda sid, vibe, owner_sid="", transcript_path="": aliased.append((sid, vibe, owner_sid, transcript_path)),
    )
    monkeypatch.setattr(claude_hook, "load_turn_state", lambda: {})
    monkeypatch.setattr(claude_hook, "save_turn_state", lambda _state: None)
    monkeypatch.setattr(claude_hook, "load_tool_buffer", lambda: {})
    monkeypatch.setattr(claude_hook, "save_tool_buffer", lambda _buf: None)
    monkeypatch.setattr(claude_hook, "set_transcript_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claude_hook, "send_turn", lambda payload: sent.append(payload) or {"action": "created"})
    monkeypatch.setattr(claude_hook, "log_debug", lambda *_args, **_kwargs: None)

    claude_hook.handle_user_prompt_submit(
        "claude-new",
        {"prompt": "hello", "transcript_path": "C:/tmp/active.jsonl"},
    )

    assert calls["ensure_session"] == 0
    assert aliased == [("claude-new", "vibe-existing", "claude-main", "C:/tmp/active.jsonl")]
    assert len(sent) == 1
    assert sent[0]["session_id"] == "vibe-existing"
    assert sent[0]["user_message"] == "hello"
