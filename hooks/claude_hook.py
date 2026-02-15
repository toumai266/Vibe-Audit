#!/usr/bin/env python3
"""
Claude Code Hook for Vibe Audit.
Captures Claude Code session events and forwards to Vibe Audit daemon.

Events:
  UserPromptSubmit -> /api/turn (user turn + baseline/phase) + session creation
  PostToolUse      -> /events (tool_use for rule-based flags) + buffer for summary
  Stop             -> /api/turn (assistant_response for LLM analysis) + /events (stop for verdict)
  SubagentStop     -> /events (tool_use record for subagent results)

Usage in ~/.claude/settings.json (or .claude/settings.json):
  "hooks": {
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "python path/to/claude_hook.py UserPromptSubmit"}]}],
    "PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "python path/to/claude_hook.py PostToolUse"}]}],
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "python path/to/claude_hook.py Stop"}]}],
    "SubagentStop": [{"matcher": "", "hooks": [{"type": "command", "command": "python path/to/claude_hook.py SubagentStop"}]}]
  }
"""
import sys
import json
import os
import io
import hashlib
import tempfile
import time
from datetime import datetime
import urllib.request
import urllib.error

DAEMON_URL = os.getenv("VIBE_AUDIT_URL", "http://localhost:8000")
SESSIONS_FILE = os.path.join(tempfile.gettempdir(), "vibe-audit-sessions.json")
TOOL_BUFFER_FILE = os.path.join(tempfile.gettempdir(), "vibe-audit-claude-buffer.json")
TURN_STATE_FILE = os.path.join(tempfile.gettempdir(), "vibe-audit-claude-turns.json")
TRANSCRIPT_INDEX_FILE = os.path.join(tempfile.gettempdir(), "vibe-audit-claude-transcripts.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "claude_hook_debug.log")


# ============ Logging ============

def log_debug(msg):
    """Debug logging for troubleshooting (same pattern as Gemini hook)."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


# ============ Session Management ============

def load_sessions_map():
    """Load session ID mapping: {claude_session_id: vibe_audit_session_id}."""
    try:
        with open(SESSIONS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_sessions_map(sessions):
    """Save session ID mapping."""
    with open(SESSIONS_FILE, "w") as f:
        json.dump(sessions, f)


def get_vibe_session_id(claude_session_id):
    """Get Vibe Audit session ID for a Claude Code session."""
    sessions = load_sessions_map()
    return sessions.get(claude_session_id)


def set_vibe_session_id(claude_session_id, vibe_session_id):
    """Store mapping from Claude Code session to Vibe Audit session."""
    sessions = load_sessions_map()
    sessions[claude_session_id] = vibe_session_id
    # Keep only last 20 sessions to prevent file bloat
    if len(sessions) > 20:
        keys = list(sessions.keys())
        for old_key in keys[:-20]:
            del sessions[old_key]
    save_sessions_map(sessions)


def _normalize_transcript_path(transcript_path):
    """Normalize transcript path for stable keying across path variants."""
    if not transcript_path or not isinstance(transcript_path, str):
        return ""
    normalized = os.path.abspath(os.path.normpath(transcript_path))
    if os.name == "nt":
        normalized = normalized.lower()
    return normalized


def get_transcript_key(transcript_path):
    """Build a privacy-safe key from transcript path."""
    normalized = _normalize_transcript_path(transcript_path)
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def load_transcript_index():
    """
    Load transcript index map:
      {transcript_key: {"vibe_session_id": str, "owner_session_id": str, "updated_at": float}}
    """
    try:
        with open(TRANSCRIPT_INDEX_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_transcript_index(index):
    """Save transcript index map."""
    try:
        with open(TRANSCRIPT_INDEX_FILE, "w") as f:
            json.dump(index, f)
    except Exception:
        pass


def set_transcript_binding(transcript_path, vibe_session_id, owner_session_id=""):
    """Bind transcript key to a Vibe session for resilient re-association."""
    transcript_key = get_transcript_key(transcript_path)
    if not transcript_key or not vibe_session_id:
        return

    index = load_transcript_index()
    index[transcript_key] = {
        "vibe_session_id": vibe_session_id,
        "owner_session_id": owner_session_id,
        "updated_at": time.time(),
    }

    # Keep latest 50 transcript entries.
    if len(index) > 50:
        pairs = sorted(
            index.items(),
            key=lambda kv: float((kv[1] or {}).get("updated_at", 0) or 0),
            reverse=True,
        )
        index = dict(pairs[:50])

    save_transcript_index(index)


def get_transcript_binding(transcript_path):
    """Resolve transcript key to bound Vibe session metadata."""
    transcript_key = get_transcript_key(transcript_path)
    if not transcript_key:
        return None
    return load_transcript_index().get(transcript_key)


# ============ Tool Buffer ============

def load_tool_buffer():
    """Load buffered tool uses per session."""
    try:
        with open(TOOL_BUFFER_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_tool_buffer(buffer):
    """Save tool buffer."""
    try:
        with open(TOOL_BUFFER_FILE, "w") as f:
            json.dump(buffer, f)
    except Exception:
        pass


# ============ Turn State ============

def load_turn_state():
    """Load current turn state (prompt hash per session for turn_key)."""
    try:
        with open(TURN_STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_turn_state(state):
    """Save turn state."""
    try:
        with open(TURN_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def get_turn_key(claude_session_id):
    """Get turn_key for current turn (session_id:prompt_hash)."""
    state = load_turn_state()
    entry = state.get(claude_session_id, {})
    prompt_hash = entry.get("prompt_hash", "default")
    return f"{claude_session_id}:{prompt_hash}"


# ============ Prompt Filters ============

def is_ignored_user_prompt(prompt: str) -> bool:
    """
    Ignore non-user control prompts emitted by Claude internals.

    Example:
      <task-notification> ... </task-notification>
    """
    if not isinstance(prompt, str):
        return False
    normalized = prompt.strip().lower()
    return normalized.startswith("<task-notification>")


# ============ Sanitize ============

def _sanitize(obj):
    """Remove surrogate characters that break JSON serialization."""
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


# ============ HTTP Helpers ============

def post_event(event_data):
    """Send event to Vibe Audit daemon via /events."""
    try:
        data = json.dumps(_sanitize(event_data)).encode("utf-8")
        req = urllib.request.Request(
            f"{DAEMON_URL}/events",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log_debug(f"post_event error: {e}")
        return None


def send_turn(turn_data):
    """Send turn to /api/turn endpoint (triggers LLM analysis + Turn recording)."""
    try:
        data = json.dumps(_sanitize(turn_data)).encode("utf-8")
        req = urllib.request.Request(
            f"{DAEMON_URL}/api/turn",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            log_debug(f"send_turn OK: action={result.get('action', '?')}")
            return result
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log_debug(f"send_turn error: {e}")
        return None


def ensure_session(claude_session_id, prompt="", transcript_path=""):
    """Ensure a Vibe Audit session exists for this Claude Code session."""
    vibe_id = get_vibe_session_id(claude_session_id)
    if vibe_id:
        if transcript_path:
            set_transcript_binding(transcript_path, vibe_id, claude_session_id)
        return vibe_id

    # Create new session via /events
    result = post_event({
        "type": "session_start",
        "payload": {
            "cli_command": "claude-code:hook",
            "user_prompt": prompt,
        },
    })
    if result and result.get("session_id"):
        vibe_id = result["session_id"]
        set_vibe_session_id(claude_session_id, vibe_id)
        if transcript_path:
            set_transcript_binding(transcript_path, vibe_id, claude_session_id)
        log_debug(f"Created session: {vibe_id} for claude: {claude_session_id}")
        return vibe_id
    return None


def _alias_session(claude_session_id, vibe_id, owner_sid="", transcript_path=""):
    """Alias incoming session_id to known Vibe session with optional transcript binding."""
    set_vibe_session_id(claude_session_id, vibe_id)
    if transcript_path:
        set_transcript_binding(transcript_path, vibe_id, owner_sid or claude_session_id)


def resolve_vibe_session_for_non_user_event(claude_session_id, hook_data=None):
    """
    Resolve Vibe session for non-user hook events without creating a new session.

    Claude may emit a new internal session_id during long-context compaction or
    sub-agent transitions. In that case we should alias to the current active
    Vibe session instead of creating a ghost session.
    """
    hook_data = hook_data or {}
    transcript_path = hook_data.get("transcript_path", "")

    # Direct mapping exists.
    vibe_id = get_vibe_session_id(claude_session_id)
    if vibe_id:
        if transcript_path:
            set_transcript_binding(transcript_path, vibe_id, claude_session_id)
        return vibe_id

    # Prefer transcript binding when available.
    binding = get_transcript_binding(transcript_path) if transcript_path else None
    if binding:
        vibe_id = binding.get("vibe_session_id")
        owner_sid = binding.get("owner_session_id", "")
        if vibe_id:
            _alias_session(claude_session_id, vibe_id, owner_sid=owner_sid, transcript_path=transcript_path)
            log_debug(
                f"Aliased non-user claude session via transcript: {claude_session_id} -> {owner_sid or '?'} "
                f"(vibe={vibe_id})"
            )
            return vibe_id

    sessions = load_sessions_map()
    if not sessions:
        return None

    # Prefer same transcript owner from active turn state.
    state = load_turn_state()
    now = time.time()
    transcript_key = get_transcript_key(transcript_path) if transcript_path else ""
    if transcript_key:
        transcript_candidates = []
        for sid, entry in state.items():
            if sid not in sessions:
                continue
            if entry.get("transcript_key") != transcript_key:
                continue
            started_at = float(entry.get("started_at", 0) or 0)
            if started_at <= 0:
                continue
            if now - started_at > 2 * 3600:
                continue
            transcript_candidates.append((started_at, sid))
        if transcript_candidates:
            transcript_candidates.sort(reverse=True)
            owner_sid = transcript_candidates[0][1]
            vibe_id = sessions.get(owner_sid)
            if vibe_id:
                _alias_session(claude_session_id, vibe_id, owner_sid=owner_sid, transcript_path=transcript_path)
                log_debug(
                    f"Aliased non-user claude session via transcript_key state: {claude_session_id} -> {owner_sid} "
                    f"(vibe={vibe_id})"
                )
                return vibe_id

    # Fallback: most recent active turn owner.
    candidates = []
    for sid, entry in state.items():
        if sid not in sessions:
            continue
        started_at = float(entry.get("started_at", 0) or 0)
        if started_at <= 0:
            continue
        # Ignore stale sessions to reduce wrong cross-session aliasing.
        if now - started_at > 2 * 3600:
            continue
        candidates.append((started_at, sid))

    if candidates:
        candidates.sort(reverse=True)
        owner_sid = candidates[0][1]
        vibe_id = sessions.get(owner_sid)
        if vibe_id:
            _alias_session(claude_session_id, vibe_id, owner_sid=owner_sid, transcript_path=transcript_path)
            log_debug(
                f"Aliased non-user claude session: {claude_session_id} -> {owner_sid} "
                f"(vibe={vibe_id})"
            )
            return vibe_id

    # Secondary heuristic: if exactly one mapped session has buffered tools, reuse it.
    buffer = load_tool_buffer()
    buffered = [sid for sid, tools in buffer.items() if sid in sessions and isinstance(tools, list) and len(tools) > 0]
    if len(buffered) == 1:
        owner_sid = buffered[0]
        vibe_id = sessions.get(owner_sid)
        if vibe_id:
            _alias_session(claude_session_id, vibe_id, owner_sid=owner_sid, transcript_path=transcript_path)
            log_debug(
                f"Aliased non-user claude session via tool buffer: {claude_session_id} -> {owner_sid} "
                f"(vibe={vibe_id})"
            )
            return vibe_id

    # Final fallback: only when one session is known, reuse it.
    if len(sessions) == 1:
        owner_sid, vibe_id = next(iter(sessions.items()))
        _alias_session(claude_session_id, vibe_id, owner_sid=owner_sid, transcript_path=transcript_path)
        log_debug(
            f"Aliased non-user claude session via single-map fallback: {claude_session_id} -> {owner_sid} "
            f"(vibe={vibe_id})"
        )
        return vibe_id

    return None


# ============ Transcript Parsing ============

def extract_response_from_transcript(transcript_path):
    """Extract the latest assistant response from Claude Code transcript JSONL.

    Transcript format (per line):
      {"type": "user",      "message": {"role": "user", "content": "prompt text"}}
      {"type": "assistant", "message": {"role": "assistant", "content": [blocks]}}

    Content blocks: {"type": "text", "text": "..."} | {"type": "tool_use", ...} | {"type": "thinking", ...}

    We read backwards from the end, collecting all assistant text+tool_use blocks
    until we hit the user's prompt message (string content, not tool_result).
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return ""

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Only check last 200 lines for performance (single turn is ~20-50 lines)
        lines = lines[-200:]

        texts = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")

            # Stop at user prompt message (string content = actual prompt, not tool_result)
            if entry_type == "user":
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str):
                    break

            # Collect assistant content blocks
            if entry_type == "assistant":
                msg = entry.get("message", {})
                content_blocks = msg.get("content", [])
                if not isinstance(content_blocks, list):
                    continue
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text = block.get("text", "").strip()
                        if text:
                            texts.append(text)
                    elif block_type == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tool_input = block.get("input", {})
                        # Brief description from input
                        desc = str(
                            tool_input.get("description", "")
                            or tool_input.get("command", "")
                            or tool_input.get("file_path", "")
                            or ""
                        )[:200]
                        texts.append(f"[Tool: {tool_name}] {desc}")

        texts.reverse()  # Restore chronological order
        result = "\n".join(texts)
        log_debug(f"Transcript extracted: {len(texts)} blocks, {len(result)} chars")
        return result
    except Exception as e:
        log_debug(f"Transcript read error: {e}")
        return ""


# ============ Tool Summary (fallback) ============

def format_tool_summary(tool_history):
    """Format buffered tool uses into a readable assistant response summary.

    Fallback when transcript_path is unavailable.
    """
    if not tool_history:
        return ""
    parts = []
    for t in tool_history:
        tool_name = t.get("tool", "unknown")
        tool_input = t.get("input", "")
        tool_output = t.get("output", "")
        if tool_output:
            parts.append(f"[{tool_name}] {tool_input} -> {tool_output}")
        else:
            parts.append(f"[{tool_name}] {tool_input}")
    return "\n".join(parts)


# ============ Event Handlers ============

def handle_user_prompt_submit(claude_session_id, hook_data):
    """Handle UserPromptSubmit - user sends a prompt to Claude.

    Flow:
    1. Ensure Vibe Audit session exists
    2. Save turn state (prompt hash for turn_key)
    3. Clear tool buffer for new turn
    4. Send user_message via /api/turn (creates Turn + Event + triggers baseline/phase)
    """
    prompt = hook_data.get("prompt", "")
    if is_ignored_user_prompt(prompt):
        log_debug(
            f"UserPromptSubmit ignored: session={claude_session_id} "
            f"reason=task-notification"
        )
        return

    transcript_path = hook_data.get("transcript_path", "")
    vibe_id = get_vibe_session_id(claude_session_id)

    if not vibe_id and transcript_path:
        binding = get_transcript_binding(transcript_path)
        if binding and binding.get("vibe_session_id"):
            vibe_id = binding["vibe_session_id"]
            _alias_session(
                claude_session_id,
                vibe_id,
                owner_sid=binding.get("owner_session_id", ""),
                transcript_path=transcript_path,
            )
            log_debug(
                f"UserPromptSubmit rebound by transcript: session={claude_session_id} "
                f"vibe={vibe_id}"
            )

    if not vibe_id:
        # First prompt - create session
        vibe_id = ensure_session(claude_session_id, prompt, transcript_path=transcript_path)
        if not vibe_id:
            log_debug("Failed to create session")
            return

    # Save turn state for turn_key generation
    prompt_hash = hashlib.md5((prompt or "").encode()).hexdigest()[:12]
    state = load_turn_state()
    state[claude_session_id] = {
        "prompt_hash": prompt_hash,
        "started_at": time.time(),
        "transcript_key": get_transcript_key(transcript_path) if transcript_path else "",
    }
    # Clean stale entries (older than 1 hour)
    now = time.time()
    for key in list(state.keys()):
        if key != claude_session_id and now - state[key].get("started_at", 0) > 3600:
            del state[key]
    save_turn_state(state)

    # Clear tool buffer for new turn
    buffer = load_tool_buffer()
    buffer.pop(claude_session_id, None)
    save_tool_buffer(buffer)

    # Send user_message via /api/turn (dedup via turn_key + Turn recording + Event + processing)
    if prompt:
        turn_key = f"{claude_session_id}:{prompt_hash}"
        send_turn({
            "session_id": vibe_id,
            "turn_key": turn_key,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "user_message": prompt[:2000],
            "cli_command": "claude-code:hook",
        })

    if transcript_path:
        set_transcript_binding(transcript_path, vibe_id, claude_session_id)

    log_debug(f"UserPromptSubmit: session={claude_session_id} prompt={prompt[:50]}...")


def handle_post_tool_use(claude_session_id, hook_data):
    """Handle PostToolUse - agent used a tool.

    Flow:
    1. Send tool_use event via /events (triggers rule-based flag checks)
    2. Buffer tool info for assistant_response summary at Stop
    """
    vibe_id = resolve_vibe_session_for_non_user_event(claude_session_id, hook_data)
    if not vibe_id:
        log_debug(f"PostToolUse dropped (unresolved session): {claude_session_id}")
        return

    tool_name = hook_data.get("tool_name", hook_data.get("tool", "unknown"))
    tool_input = hook_data.get("tool_input", hook_data.get("input", {}))
    tool_output = hook_data.get("tool_output", hook_data.get("output", ""))

    # Truncate large outputs
    if isinstance(tool_output, str) and len(tool_output) > 1000:
        tool_output = tool_output[:1000] + "...[truncated]"

    # Send tool_use event via /events (rule-based flags)
    post_event({
        "session_id": vibe_id,
        "type": "tool_use",
        "payload": {
            "tool": tool_name,
            "tool_name": tool_name,
            "inputs": tool_input if isinstance(tool_input, dict) else {"raw": str(tool_input)[:500]},
            "output": tool_output if isinstance(tool_output, str) else str(tool_output)[:500],
        },
    })

    # Buffer for assistant_response summary at Stop
    buffer = load_tool_buffer()
    if claude_session_id not in buffer:
        buffer[claude_session_id] = []
    buffer[claude_session_id].append({
        "tool": tool_name,
        "input": str(tool_input)[:200] if tool_input else "",
        "output": str(tool_output)[:300] if tool_output else "",
    })
    save_tool_buffer(buffer)

    log_debug(f"PostToolUse: {tool_name}")


def handle_stop(claude_session_id, hook_data):
    """Handle Stop - agent response turn complete.

    Claude Code fires Stop after EACH response turn, NOT at session end.

    Flow:
    1. Build assistant_response from hook_data.response (if available) or tool buffer
    2. Send via /api/turn (triggers LLM analysis: snapshot -> alignment -> flags)
    3. Send stop event via /events (triggers verdict update)
    4. Keep session mapping alive for follow-up turns
    """
    vibe_id = resolve_vibe_session_for_non_user_event(claude_session_id, hook_data)
    if not vibe_id:
        log_debug(f"Stop dropped (unresolved session): {claude_session_id}")
        return

    # --- Build assistant_response (3-tier priority) ---

    # Priority 1: Read actual response from transcript file
    transcript_path = hook_data.get("transcript_path", "")
    if transcript_path:
        set_transcript_binding(transcript_path, vibe_id, claude_session_id)
    response_text = extract_response_from_transcript(transcript_path)

    # Priority 2: Check if Stop hook_data includes response text (future-proof)
    if not response_text:
        response_text = (
            hook_data.get("response", "")
            or hook_data.get("content", "")
            or hook_data.get("message", "")
        )

    # Priority 3: Fallback to buffered tool history
    buffer = load_tool_buffer()
    tool_history = buffer.pop(claude_session_id, [])
    save_tool_buffer(buffer)

    if not response_text and tool_history:
        response_text = format_tool_summary(tool_history)

    # Send assistant_response via /api/turn (LLM analysis pipeline)
    if response_text:
        turn_key = get_turn_key(claude_session_id)
        send_turn({
            "session_id": vibe_id,
            "turn_key": turn_key,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "assistant_response": response_text[:8000],
            "cli_command": "claude-code:hook",
        })
        log_debug(
            f"Stop: assistant_response sent "
            f"({len(response_text)} chars, {len(tool_history)} tools buffered)"
        )
    else:
        log_debug("Stop: no response_text or tool_history — skipping assistant_response")

    # Send stop event via /events (verdict update)
    post_event({
        "session_id": vibe_id,
        "type": "stop",
        "payload": {
            "reason": hook_data.get("stop_reason", "completed"),
        },
    })

    # NOTE: Do NOT remove session mapping here.
    # Claude Code calls Stop after each response cycle, not at conversation end.
    # Removing the mapping would cause a new session for the next user prompt.


def handle_subagent_stop(claude_session_id, hook_data):
    """Handle SubagentStop - a subagent (Task tool) completed.

    Records subagent result as a tool_use event for rule-based analysis.
    The main agent's Stop will handle the final assistant_response.
    """
    vibe_id = resolve_vibe_session_for_non_user_event(claude_session_id, hook_data)
    if not vibe_id:
        log_debug(f"SubagentStop dropped (unresolved session): {claude_session_id}")
        return

    response_text = (
        hook_data.get("response", "")
        or hook_data.get("content", "")
        or hook_data.get("message", "")
    )

    if response_text:
        post_event({
            "session_id": vibe_id,
            "type": "tool_use",
            "payload": {
                "tool": "SubAgent",
                "tool_name": "SubAgent",
                "inputs": {"stop_reason": hook_data.get("stop_reason", "completed")},
                "output": response_text[:1000],
            },
        })

        # Also buffer for assistant_response summary
        buffer = load_tool_buffer()
        if claude_session_id not in buffer:
            buffer[claude_session_id] = []
        buffer[claude_session_id].append({
            "tool": "SubAgent",
            "input": hook_data.get("stop_reason", "completed"),
            "output": response_text[:300],
        })
        save_tool_buffer(buffer)

    log_debug(f"SubagentStop: {len(response_text) if response_text else 0} chars")


# ============ Main ============

def main():
    if len(sys.argv) < 2:
        print("Usage: claude_hook.py <event_type>", file=sys.stderr)
        sys.exit(1)

    event_type = sys.argv[1]
    log_debug(f"=== Claude Hook: {event_type} ===")

    # Read hook data from stdin (force UTF-8 on Windows)
    try:
        if hasattr(sys.stdin, "buffer"):
            stdin_text = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8").read()
        else:
            stdin_text = sys.stdin.read()
        hook_data = json.loads(stdin_text) if stdin_text.strip() else {}
    except (json.JSONDecodeError, EOFError, AttributeError):
        hook_data = {}

    # Log hook_data keys for debugging (helps identify available fields)
    if event_type in ("Stop", "SubagentStop"):
        log_debug(f"{event_type} hook_data keys: {list(hook_data.keys())}")
        has_response = bool(hook_data.get("response"))
        log_debug(f"{event_type} has response field: {has_response}")

    # Get Claude Code's session_id from hook data
    claude_session_id = hook_data.get("session_id", "default")

    handlers = {
        "UserPromptSubmit": handle_user_prompt_submit,
        "PostToolUse": handle_post_tool_use,
        "Stop": handle_stop,
        "SubagentStop": handle_subagent_stop,
    }

    handler = handlers.get(event_type)
    if handler:
        handler(claude_session_id, hook_data)
    else:
        log_debug(f"Unknown event type: {event_type}")


if __name__ == "__main__":
    main()
