#!/usr/bin/env python3
"""
Codex CLI Hook for Vibe Audit.

Captures Codex hook events and forwards conversation/tool signals to Vibe Audit.

Supported hook events (normalized):
  - userpromptsubmit  -> /api/turn (user_message)
  - assistantmessage  -> /api/turn (assistant_response) + /events (stop)
  - tooluseend        -> /events (tool_use)
  - sessionstart      -> ensure session mapping
  - sessionend        -> /events (stop) + cleanup

Codex passes event payload via stdin JSON and exposes event metadata
through environment variables (notably CODEX_HOOK_EVENT_NAME).
"""

import hashlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime
import urllib.error
import urllib.request

DAEMON_URL = os.getenv("VIBE_AUDIT_URL", "http://localhost:8000")
SESSIONS_FILE = os.path.join(tempfile.gettempdir(), "vibe-audit-codex-sessions.json")
TURN_STATE_FILE = os.path.join(tempfile.gettempdir(), "vibe-audit-codex-turns.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "codex_hook_debug.log")


# ============ Logging ============

def log_debug(msg):
    """Debug logging for Codex hook troubleshooting."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


# ============ JSON Helpers ============

def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _sanitize(obj):
    """Remove surrogate characters that break JSON serialization."""
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _normalize_event_name(name):
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def _coerce_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"args": value}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
            return {"args": parsed}
        except Exception:
            return {"raw": value[:2000]}
    if value is None:
        return {}
    return {"raw": str(value)[:2000]}


def _extract_text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            t = _extract_text(item)
            if t:
                parts.append(t)
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        for key in (
            "text", "message", "content", "response", "output",
            "assistant_message", "assistantResponse",
        ):
            if key in value:
                t = _extract_text(value.get(key))
                if t:
                    return t
    return ""


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _extract_event_name(hook_data):
    raw = _first_non_empty(
        os.getenv("CODEX_HOOK_EVENT_NAME"),
        os.getenv("CODEX_HOOK_EVENT"),
        hook_data.get("event_name"),
        hook_data.get("event"),
        hook_data.get("type"),
    )
    return _normalize_event_name(raw)


def _extract_session_id(hook_data):
    session_obj = hook_data.get("session", {})
    raw = _first_non_empty(
        os.getenv("CODEX_SESSION_ID"),
        os.getenv("CODEX_CONVERSATION_ID"),
        hook_data.get("session_id"),
        hook_data.get("sessionId"),
        session_obj.get("id") if isinstance(session_obj, dict) else None,
        hook_data.get("id"),
    )
    return str(raw or "default")


def _extract_notify_payload():
    """
    Parse Codex notify payload from argv (preferred) or stdin fallback.

    Codex notify typically calls:
      notify = ["python", "codex_hook.py", "--notify"]
    and passes the JSON payload as a single argument.
    """
    # argv payload (preferred)
    for arg in sys.argv[1:]:
        if arg == "--notify":
            continue
        try:
            parsed = json.loads(arg)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue

    # stdin fallback
    try:
        if hasattr(sys.stdin, "buffer"):
            raw = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8").read()
        else:
            raw = sys.stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _extract_notify_user_message(payload):
    messages = payload.get("input-messages") or payload.get("input_messages") or []
    if isinstance(messages, list):
        # Prefer latest explicit user role message.
        for msg in reversed(messages):
            if isinstance(msg, dict):
                role = str(msg.get("role", "")).lower()
                if role == "user":
                    content = _extract_text(msg.get("content"))
                    if content:
                        return content.strip()
        # Fallback: any text-like entry.
        for msg in reversed(messages):
            text = _extract_text(msg)
            if text:
                return text.strip()
    return ""


def _extract_notify_assistant_message(payload):
    text = _first_non_empty(
        _extract_text(payload.get("last-assistant-message")),
        _extract_text(payload.get("last_assistant_message")),
        _extract_text(payload.get("assistant_message")),
        _extract_text(payload.get("response")),
        _extract_text(payload.get("message")),
        _extract_text(payload.get("output_text")),
    )
    return (text or "").strip()


def _extract_notify_session_id(payload):
    """Build a stable session key for notify mode."""
    raw = _first_non_empty(
        payload.get("session-id"),
        payload.get("session_id"),
        payload.get("conversation-id"),
        payload.get("conversation_id"),
        payload.get("thread-id"),
        payload.get("thread_id"),
    )
    if raw:
        return str(raw)
    # Fallback to cwd-scoped key so separate projects don't collide.
    return f"cwd:{os.getcwd()}"


def _extract_notify_turn_key(payload, user_msg, assistant_msg):
    turn_id = _first_non_empty(payload.get("turn-id"), payload.get("turn_id"))
    if turn_id:
        return f"notify:{turn_id}"
    digest = hashlib.md5((f"{user_msg}\n{assistant_msg}").encode("utf-8")).hexdigest()[:12]
    return f"notify:{digest}"


def _extract_user_prompt(hook_data):
    payload = hook_data.get("payload", {})
    text = _first_non_empty(
        _extract_text(hook_data.get("prompt")),
        _extract_text(hook_data.get("user_message")),
        _extract_text(hook_data.get("message")),
        _extract_text(hook_data.get("input")),
        _extract_text(payload.get("prompt")) if isinstance(payload, dict) else "",
        _extract_text(payload.get("message")) if isinstance(payload, dict) else "",
    )
    return (text or "").strip()


def _extract_assistant_message(hook_data):
    payload = hook_data.get("payload", {})
    text = _first_non_empty(
        _extract_text(hook_data.get("assistant_message")),
        _extract_text(hook_data.get("response")),
        _extract_text(hook_data.get("message")),
        _extract_text(hook_data.get("content")),
        _extract_text(payload.get("assistant_message")) if isinstance(payload, dict) else "",
        _extract_text(payload.get("message")) if isinstance(payload, dict) else "",
        _extract_text(payload.get("content")) if isinstance(payload, dict) else "",
    )
    return (text or "").strip()


def _extract_tool_payload(hook_data):
    payload = hook_data.get("payload", {})
    tool_obj = hook_data.get("tool", {})
    sources = [hook_data, payload, tool_obj]

    def pick(*keys):
        for src in sources:
            if not isinstance(src, dict):
                continue
            for key in keys:
                if key in src and src.get(key) not in (None, ""):
                    return src.get(key)
        return None

    tool_name = _first_non_empty(
        pick("tool_name", "tool", "name"),
        tool_obj.get("name") if isinstance(tool_obj, dict) else None,
    )
    inputs = _first_non_empty(
        pick("inputs", "input", "args", "arguments", "tool_input"),
        tool_obj.get("input") if isinstance(tool_obj, dict) else None,
    )
    output = _first_non_empty(
        pick("output", "outputs", "result", "tool_output"),
        tool_obj.get("output") if isinstance(tool_obj, dict) else None,
    )

    output_text = _extract_text(output)
    if not output_text and output is not None:
        output_text = str(output)

    return {
        "tool": str(tool_name or "unknown_tool"),
        "tool_name": str(tool_name or "unknown_tool"),
        "inputs": _coerce_dict(inputs),
        "output": (output_text or "")[:4000],
    }


# ============ Session Management ============

def load_sessions_map():
    return _load_json(SESSIONS_FILE)


def save_sessions_map(sessions):
    _save_json(SESSIONS_FILE, sessions)


def get_vibe_session_id(codex_session_id):
    return load_sessions_map().get(codex_session_id)


def set_vibe_session_id(codex_session_id, vibe_session_id):
    sessions = load_sessions_map()
    sessions[codex_session_id] = vibe_session_id
    if len(sessions) > 50:
        keys = list(sessions.keys())
        for old_key in keys[:-50]:
            del sessions[old_key]
    save_sessions_map(sessions)


def clear_vibe_session_id(codex_session_id):
    sessions = load_sessions_map()
    if codex_session_id in sessions:
        del sessions[codex_session_id]
        save_sessions_map(sessions)


# ============ Turn State ============

def load_turn_state():
    return _load_json(TURN_STATE_FILE)


def save_turn_state(state):
    _save_json(TURN_STATE_FILE, state)


def set_turn_key(codex_session_id, prompt):
    prompt_hash = hashlib.md5((prompt or "").encode("utf-8")).hexdigest()[:12]
    state = load_turn_state()
    state[codex_session_id] = {
        "prompt_hash": prompt_hash,
        "started_at": datetime.utcnow().timestamp(),
    }
    # Keep only recent entries
    if len(state) > 100:
        keys = list(state.keys())
        for old_key in keys[:-100]:
            del state[old_key]
    save_turn_state(state)
    return f"{codex_session_id}:{prompt_hash}"


def get_turn_key(codex_session_id):
    state = load_turn_state()
    entry = state.get(codex_session_id, {})
    prompt_hash = entry.get("prompt_hash", "default")
    return f"{codex_session_id}:{prompt_hash}"


def clear_turn_key(codex_session_id):
    state = load_turn_state()
    if codex_session_id in state:
        del state[codex_session_id]
        save_turn_state(state)


# ============ HTTP Helpers ============

def post_event(event_data):
    try:
        data = json.dumps(_sanitize(event_data)).encode("utf-8")
        req = urllib.request.Request(
            f"{DAEMON_URL}/events",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log_debug(f"post_event error: {e}")
        return None


def send_turn(turn_data):
    try:
        data = json.dumps(_sanitize(turn_data)).encode("utf-8")
        req = urllib.request.Request(
            f"{DAEMON_URL}/api/turn",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            log_debug(f"send_turn OK: action={result.get('action', '?')}")
            return result
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log_debug(f"send_turn error: {e}")
        return None


def ensure_session(codex_session_id, prompt=""):
    vibe_id = get_vibe_session_id(codex_session_id)
    if vibe_id:
        return vibe_id

    result = post_event({
        "type": "session_start",
        "payload": {
            "cli_command": "codex-cli:hook",
            "user_prompt": (prompt or "")[:500],
        },
    })
    if result and result.get("session_id"):
        vibe_id = result["session_id"]
        set_vibe_session_id(codex_session_id, vibe_id)
        log_debug(f"Created session: {vibe_id} for codex: {codex_session_id}")
        return vibe_id
    return None


# ============ Event Handlers ============

def handle_user_prompt_submit(codex_session_id, hook_data):
    prompt = _extract_user_prompt(hook_data)
    vibe_id = ensure_session(codex_session_id, prompt)
    if not vibe_id:
        return

    turn_key = set_turn_key(codex_session_id, prompt or "prompt")
    if prompt:
        send_turn({
            "session_id": vibe_id,
            "turn_key": turn_key,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "user_message": prompt[:2000],
            "cli_command": "codex-cli:hook",
        })
    log_debug(f"userpromptsubmit: session={codex_session_id} prompt_len={len(prompt)}")


def handle_assistant_message(codex_session_id, hook_data):
    vibe_id = ensure_session(codex_session_id)
    if not vibe_id:
        return

    response_text = _extract_assistant_message(hook_data)
    if response_text:
        send_turn({
            "session_id": vibe_id,
            "turn_key": get_turn_key(codex_session_id),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "assistant_response": response_text[:8000],
            "cli_command": "codex-cli:hook",
        })

    post_event({
        "session_id": vibe_id,
        "type": "stop",
        "payload": {
            "reason": "turn_complete",
            "source": "codex_hook",
            "event": "assistantmessage",
        },
    })
    log_debug(f"assistantmessage: session={codex_session_id} resp_len={len(response_text)}")


def handle_tool_use_end(codex_session_id, hook_data):
    vibe_id = ensure_session(codex_session_id)
    if not vibe_id:
        return

    payload = _extract_tool_payload(hook_data)
    post_event({
        "session_id": vibe_id,
        "type": "tool_use",
        "payload": payload,
    })
    log_debug(f"tooluseend: tool={payload.get('tool_name')}")


def handle_session_start(codex_session_id, hook_data):
    prompt = _extract_user_prompt(hook_data)
    ensure_session(codex_session_id, prompt)
    log_debug(f"sessionstart: session={codex_session_id}")


def handle_session_end(codex_session_id, _hook_data):
    vibe_id = get_vibe_session_id(codex_session_id)
    if vibe_id:
        post_event({
            "session_id": vibe_id,
            "type": "stop",
            "payload": {
                "reason": "session_end",
                "source": "codex_hook",
            },
        })
    clear_vibe_session_id(codex_session_id)
    clear_turn_key(codex_session_id)
    log_debug(f"sessionend: session={codex_session_id}")


def handle_notify_event(payload):
    """
    Handle Codex `notify` callback payload.

    Typical payload example:
      {"type":"agent-turn-complete","turn-id":"...","input-messages":[...],
       "last-assistant-message":"..."}
    """
    notify_type = _normalize_event_name(payload.get("type"))
    codex_session_id = _extract_notify_session_id(payload)
    user_msg = _extract_notify_user_message(payload)
    assistant_msg = _extract_notify_assistant_message(payload)

    if notify_type and notify_type not in ("agentturncomplete", "turncomplete"):
        log_debug(f"notify ignored: type={notify_type}")
        return

    vibe_id = ensure_session(codex_session_id, user_msg)
    if not vibe_id:
        return

    turn_key = _extract_notify_turn_key(payload, user_msg, assistant_msg)
    turn_payload = {
        "session_id": vibe_id,
        "turn_key": turn_key,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "cli_command": "codex-cli:notify",
    }
    if user_msg:
        turn_payload["user_message"] = user_msg[:2000]
    if assistant_msg:
        turn_payload["assistant_response"] = assistant_msg[:8000]

    if "user_message" in turn_payload or "assistant_response" in turn_payload:
        send_turn(turn_payload)

    post_event({
        "session_id": vibe_id,
        "type": "stop",
        "payload": {
            "reason": "turn_complete",
            "source": "codex_notify",
            "event": "agent-turn-complete",
        },
    })
    log_debug(
        "notify: session=%s user_len=%d resp_len=%d type=%s"
        % (codex_session_id, len(user_msg), len(assistant_msg), notify_type or "unknown")
    )


# ============ Main ============

def main():
    log_debug("=== Codex Hook ===")

    # Notify mode: payload is passed as argv JSON (or stdin fallback).
    if "--notify" in sys.argv:
        payload = _extract_notify_payload()
        if payload:
            handle_notify_event(payload)
        else:
            log_debug("notify: empty payload")
        return

    try:
        if hasattr(sys.stdin, "buffer"):
            raw = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8").read()
        else:
            raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError, AttributeError):
        hook_data = {}

    # Fallback: if stdin payload looks like notify shape, handle it.
    if isinstance(hook_data, dict) and (
        "input-messages" in hook_data
        or "last-assistant-message" in hook_data
        or _normalize_event_name(hook_data.get("type")) == "agentturncomplete"
    ):
        handle_notify_event(hook_data)
        return

    event_name = _extract_event_name(hook_data)
    codex_session_id = _extract_session_id(hook_data)
    log_debug(f"event={event_name or 'unknown'} session={codex_session_id}")

    handlers = {
        "userpromptsubmit": handle_user_prompt_submit,
        "assistantmessage": handle_assistant_message,
        "tooluseend": handle_tool_use_end,
        "sessionstart": handle_session_start,
        "sessionend": handle_session_end,
    }

    handler = handlers.get(event_name)
    if handler:
        handler(codex_session_id, hook_data)
    else:
        log_debug(f"ignored event: {event_name or 'unknown'}")


if __name__ == "__main__":
    main()
