"""Tests for Codex hook parsing and turn/event forwarding."""
import importlib.util
import io
import json
from pathlib import Path


def _load_codex_hook_module():
    root = Path(__file__).resolve().parents[2]
    hook_path = root / "hooks" / "codex_hook.py"
    spec = importlib.util.spec_from_file_location("codex_hook_test_module", hook_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


codex_hook = _load_codex_hook_module()


def test_extract_tool_payload_normalization():
    payload = {
        "tool_name": "ReadFile",
        "input": {"path": "daemon/main.py"},
        "output": {"text": "ok"},
    }
    normalized = codex_hook._extract_tool_payload(payload)
    assert normalized["tool"] == "ReadFile"
    assert normalized["tool_name"] == "ReadFile"
    assert normalized["inputs"] == {"path": "daemon/main.py"}
    assert normalized["output"] == "ok"


def test_main_user_assistant_and_tool_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_hook, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(codex_hook, "TURN_STATE_FILE", str(tmp_path / "turns.json"))
    monkeypatch.setattr(codex_hook, "log_debug", lambda *_args, **_kwargs: None)

    sent_turns = []
    sent_events = []

    def fake_send_turn(payload):
        sent_turns.append(payload)
        return {"action": "created", "session_id": payload.get("session_id")}

    def fake_post_event(payload):
        sent_events.append(payload)
        if payload.get("type") == "session_start":
            return {"session_id": "vibe-session-1"}
        return {"event_id": "evt-1", "session_id": payload.get("session_id")}

    monkeypatch.setattr(codex_hook, "send_turn", fake_send_turn)
    monkeypatch.setattr(codex_hook, "post_event", fake_post_event)

    # userpromptsubmit
    monkeypatch.setenv("CODEX_HOOK_EVENT_NAME", "userpromptsubmit")
    monkeypatch.setenv("CODEX_SESSION_ID", "codex-session-1")
    monkeypatch.setattr(codex_hook.sys, "stdin", io.StringIO(json.dumps({"prompt": "summarize logs"})))
    codex_hook.main()

    # assistantmessage
    monkeypatch.setenv("CODEX_HOOK_EVENT_NAME", "assistantmessage")
    monkeypatch.setattr(codex_hook.sys, "stdin", io.StringIO(json.dumps({"message": "Done. Summary generated."})))
    codex_hook.main()

    # tooluseend
    monkeypatch.setenv("CODEX_HOOK_EVENT_NAME", "tooluseend")
    monkeypatch.setattr(
        codex_hook.sys,
        "stdin",
        io.StringIO(json.dumps({"tool": "run_shell_command", "input": {"command": "ls -la"}, "output": "ok"})),
    )
    codex_hook.main()

    assert len(sent_turns) == 2
    assert sent_turns[0]["user_message"] == "summarize logs"
    assert sent_turns[1]["assistant_response"] == "Done. Summary generated."
    assert sent_turns[0]["session_id"] == "vibe-session-1"
    assert sent_turns[1]["session_id"] == "vibe-session-1"

    # 1x session_start + 1x stop(from assistantmessage) + 1x tool_use
    assert [event["type"] for event in sent_events] == ["session_start", "stop", "tool_use"]
    assert sent_events[2]["payload"]["tool"] == "run_shell_command"
