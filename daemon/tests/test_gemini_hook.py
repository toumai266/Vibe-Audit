"""Tests for Gemini hook event extraction and stream-done flow."""
import importlib.util
import io
import json
from pathlib import Path


def _load_gemini_hook_module():
    root = Path(__file__).resolve().parents[2]
    hook_path = root / "hooks" / "gemini_hook.py"
    spec = importlib.util.spec_from_file_location("gemini_hook_test_module", hook_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


gemini_hook = _load_gemini_hook_module()


def test_extract_tool_use_events_dedup_and_shape():
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": "search", "args": {"q": "drift"}}},
                        {"function_call": {"name": "search", "args": '{"q":"drift"}'}},
                        {"functionCall": {"name": "calc", "args": [1, 2, 3]}},
                    ]
                }
            }
        ],
        "toolCalls": [
            {"name": "search", "args": {"q": "drift"}},
            {"tool": "download", "arguments": "raw-value"},
        ],
    }

    events = gemini_hook.extract_tool_use_events(payload)
    assert len(events) == 3

    by_tool = {event["tool"]: event for event in events}
    assert set(by_tool.keys()) == {"search", "calc", "download"}
    assert by_tool["search"]["inputs"] == {"q": "drift"}
    assert by_tool["calc"]["inputs"] == {"args": [1, 2, 3]}
    assert by_tool["download"]["inputs"] == {"raw": "raw-value"}


def test_main_stream_done_emits_tool_and_stop_events(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_hook, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(gemini_hook, "PENDING_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(gemini_hook, "LOCK_FILE", str(tmp_path / "pending.lock"))
    monkeypatch.setattr(gemini_hook, "CHUNK_BUF_DIR", str(tmp_path / "chunks"))
    monkeypatch.setattr(gemini_hook, "log_debug", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gemini_hook, "acquire_lock", lambda timeout=5: True)
    monkeypatch.setattr(gemini_hook, "release_lock", lambda: None)
    monkeypatch.setattr(gemini_hook, "get_or_create_session", lambda *_args, **_kwargs: "vibe-session-1")

    sent_turns = []
    sent_events = []
    monkeypatch.setattr(gemini_hook, "send_turn", lambda payload: sent_turns.append(payload) or True)
    monkeypatch.setattr(gemini_hook, "send_event", lambda payload: sent_events.append(payload) or True)

    stdin_payload = {
        "session_id": "gemini-session-1",
        "llm_request": {"messages": [{"role": "user", "content": "summarize logs"}]},
        "llm_response": {
            "text": "Done. Summary generated.",
            "finishReason": "STOP",
            "toolCalls": [{"name": "Read", "args": {"path": "README.md"}}],
        },
    }
    monkeypatch.setattr(gemini_hook.sys, "stdin", io.StringIO(json.dumps(stdin_payload)))

    gemini_hook.main()

    assert len(sent_turns) == 1
    turn = sent_turns[0]
    assert turn["session_id"] == "vibe-session-1"
    assert turn["user_message"] == "summarize logs"
    assert turn["assistant_response"] == "Done. Summary generated."

    assert [event["type"] for event in sent_events] == ["tool_use", "stop"]
    assert sent_events[0]["session_id"] == "vibe-session-1"
    assert sent_events[0]["payload"]["tool"] == "Read"
    assert sent_events[1]["session_id"] == "vibe-session-1"
    assert sent_events[1]["payload"]["source"] == "gemini_hook"
