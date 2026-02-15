#!/usr/bin/env python3
"""
Gemini CLI Hook for Vibe Audit

Gemini CLI fires AfterModel for each streaming chunk (~300ms apart).
We ACCUMULATE response chunks per user message, then send the full
response to Vibe Audit once streaming appears done (finishReason=STOP).

Flow:
  1. Chunks with stream_done=False → buffered to disk (fast path, no lock)
  2. stream_done=True → acquire lock, merge buffered chunks, send to backend
  3. Backend triggers LLM analysis on assistant_response arrival
"""

import sys
import json
import urllib.request
import os
import io
import hashlib
import tempfile
import time
from datetime import datetime

VIBE_AUDIT_URL = os.environ.get("VIBE_AUDIT_URL", "http://localhost:8000")
LOG_FILE = os.path.join(os.path.dirname(__file__), "hook_debug.log")
SESSIONS_FILE = os.path.join(tempfile.gettempdir(), "vibe-audit-gemini-sessions.json")
PENDING_FILE = os.path.join(tempfile.gettempdir(), "vibe-audit-pending.json")
LOCK_FILE = os.path.join(tempfile.gettempdir(), "vibe-audit-pending.lock")
CHUNK_BUF_DIR = os.path.join(tempfile.gettempdir(), "vibe-audit-chunks")

MAX_PENDING_AGE = 300.0


def chunk_buffer_path(key: str) -> str:
    """Build a session-scoped chunk buffer filename to avoid cross-session collisions."""
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in key)
    return os.path.join(CHUNK_BUF_DIR, f"{safe}.txt")


def log_debug(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except:
        pass


def acquire_lock(timeout=5):
    """Cross-process file lock using atomic create (O_CREAT|O_EXCL)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except (OSError, FileExistsError):
            try:
                if os.path.exists(LOCK_FILE):
                    age = time.time() - os.path.getmtime(LOCK_FILE)
                    if age > 10:
                        os.remove(LOCK_FILE)
                        continue
            except OSError:
                pass
            time.sleep(0.05)
    log_debug("WARN: lock acquisition timed out")
    return False


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def load_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return {}


def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except:
        pass


def get_or_create_session(gemini_session_id, user_content=""):
    """Map Gemini session → Vibe Audit session (cached in temp file)."""
    sessions = load_json(SESSIONS_FILE)

    vibe_id = sessions.get(gemini_session_id)
    if vibe_id:
        return vibe_id

    try:
        payload = {
            "type": "session_start",
            "payload": {
                "cli_command": "gemini-cli:hook",
            },
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{VIBE_AUDIT_URL}/events", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            vibe_id = result.get("session_id")
            if vibe_id:
                sessions[gemini_session_id] = vibe_id
                if len(sessions) > 20:
                    for k in list(sessions.keys())[:-20]:
                        del sessions[k]
                save_json(SESSIONS_FILE, sessions)
                log_debug(f"Created session: {vibe_id} for gemini: {gemini_session_id}")
                return vibe_id
    except Exception as e:
        log_debug(f"Session create error: {e}")

    return None


def send_turn(turn_data):
    try:
        data = json.dumps(turn_data).encode("utf-8")
        req = urllib.request.Request(
            f"{VIBE_AUDIT_URL}/api/turn", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        user_preview = (turn_data.get("user_message") or "")[:40]
        resp_len = len(turn_data.get("assistant_response") or "")
        log_debug(f"Sent turn OK ({resp.status}): user=\"{user_preview}...\" resp_len={resp_len}")
        return True
    except Exception as e:
        log_debug(f"Send error: {e}")
        return False


def send_event(event_data):
    """Send raw event to /events endpoint (tool_use, stop, ...)."""
    try:
        data = json.dumps(event_data).encode("utf-8")
        req = urllib.request.Request(
            f"{VIBE_AUDIT_URL}/events", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            log_debug(f"Sent event OK ({resp.status}): type={event_data.get('type')}")
        return True
    except Exception as e:
        log_debug(f"Send event error: {e}")
        return False


def _coerce_args(value):
    """Normalize tool call args into a JSON-serializable dict."""
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
            return {"raw": value}
    if value is None:
        return {}
    return {"args": str(value)}


def extract_tool_use_events(llm_response):
    """
    Best-effort extraction of tool/function calls from Gemini response payload.
    Returns normalized tool_use payloads suitable for /events.
    """
    if not isinstance(llm_response, dict):
        return []

    extracted = []
    seen = set()

    def add_tool_call(name, args):
        tool_name = str(name or "unknown_tool")
        inputs = _coerce_args(args)
        dedupe_key = f"{tool_name}:{json.dumps(inputs, sort_keys=True, ensure_ascii=False)}"
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        extracted.append({
            "tool": tool_name,
            "tool_name": tool_name,
            "inputs": inputs,
            "output": "",
        })

    # Common Gemini format: candidates[*].content.parts[*].functionCall
    for candidate in llm_response.get("candidates", []) or []:
        content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
        parts = content.get("parts", []) if isinstance(content, dict) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            fn_call = part.get("functionCall") or part.get("function_call")
            if isinstance(fn_call, dict):
                add_tool_call(fn_call.get("name"), fn_call.get("args"))

    # Additional fallback shapes occasionally used by wrappers.
    for tool_call in llm_response.get("toolCalls", []) or llm_response.get("tool_calls", []) or []:
        if not isinstance(tool_call, dict):
            continue
        name = tool_call.get("name") or tool_call.get("tool")
        args = tool_call.get("args", tool_call.get("arguments", {}))
        add_tool_call(name, args)

    return extracted


def extract_user_content(llm_request):
    """Extract the latest user message from the LLM request."""
    for msg in reversed(llm_request.get("messages", [])):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            break
    return ""


def is_stream_finished(llm_response):
    """Check if this chunk signals the end of streaming (finishReason=STOP)."""
    if not isinstance(llm_response, dict):
        return False
    if llm_response.get("finishReason") == "STOP":
        return True
    try:
        candidates = llm_response.get("candidates", [])
        if candidates and candidates[0].get("finishReason") == "STOP":
            return True
    except (KeyError, IndexError, TypeError):
        pass
    return False


def extract_assistant_chunk(llm_response):
    """Extract assistant response text from a streaming chunk.

    Gemini CLI streaming format:
      - Thinking chunks: {"text": "", ...}  → empty (skip)
      - Response chunks: {"text": "actual response", ...}  → capture
      - Final chunk:     {"text": "", "finishReason": "STOP"}  → skip
    """
    if not llm_response:
        return ""
    if isinstance(llm_response, str):
        return llm_response
    if isinstance(llm_response, dict):
        if "text" in llm_response:
            return llm_response["text"]

        if "candidates" in llm_response:
            try:
                parts = llm_response["candidates"][0]["content"]["parts"]
                texts = []
                for p in parts:
                    if isinstance(p, str):
                        texts.append(p)
                    elif isinstance(p, dict):
                        texts.append(p.get("text", ""))
                return " ".join(texts)
            except (KeyError, IndexError):
                pass

        if "content" in llm_response:
            c = llm_response["content"]
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in c
                )
        if "message" in llm_response:
            m = llm_response["message"]
            return m.get("content", "") if isinstance(m, dict) else str(m)
    return str(llm_response)[:1000]


def upsert_turn(pending, pkey):
    """Send accumulated content to backend."""
    entry = pending.get(pkey)
    if not entry:
        return
    accumulated = entry.get("accumulated", "")
    vibe_session_id = entry.get("vibe_session_id")
    user_content = entry.get("user_content")

    if not vibe_session_id:
        return
    if not accumulated and not user_content:
        return

    include_user = user_content and not entry.get("user_content_sent")

    payload = {
        "session_id": vibe_session_id,
        "turn_key": pkey,
        "timestamp": entry.get("timestamp", datetime.utcnow().isoformat() + "Z"),
        "user_message": (user_content or "")[:2000] if include_user else None,
        "assistant_response": accumulated[:8000] if accumulated else None,
        "cli_command": "gemini-cli:hook",
    }
    send_turn(payload)
    log_debug(
        f"Upsert {pkey}: {entry.get('chunks', 0)} chunks, "
        f"{len(accumulated)} chars"
    )
    entry["user_content_sent"] = True


def main():
    log_debug("=== Gemini Hook ===")

    # --- Parse stdin ---
    try:
        if hasattr(sys.stdin, "buffer"):
            raw = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8").read()
        else:
            raw = sys.stdin.read()
        input_data = json.loads(raw) if raw else {}
    except Exception as e:
        log_debug(f"Input error: {e}")
        print(json.dumps({}))
        return

    gemini_session_id = input_data.get("session_id", "default")
    llm_request = input_data.get("llm_request", {})
    llm_response = input_data.get("llm_response", {})

    user_content = extract_user_content(llm_request)
    chunk = extract_assistant_chunk(llm_response)
    stream_done = is_stream_finished(llm_response)

    msg_hash = hashlib.md5((user_content or "").encode()).hexdigest()[:12]
    key = f"{gemini_session_id}:{msg_hash}"
    now = time.time()

    log_debug(f"key={key} chunk_len={len(chunk)} stream_done={stream_done}")

    # --- FAST PATH: buffer chunk to disk, no lock needed ---
    if chunk and not stream_done:
        os.makedirs(CHUNK_BUF_DIR, exist_ok=True)
        buf_path = chunk_buffer_path(key)
        try:
            with open(buf_path, 'a', encoding='utf-8') as f:
                f.write(chunk)
        except:
            pass
        print(json.dumps({}))
        return

    # --- FULL PATH: stream_done=True → merge chunks and send ---
    if not acquire_lock():
        log_debug("Lock timeout, skipping")
        print(json.dumps({}))
        return

    try:
        pending = load_json(PENDING_FILE)

        # Clean up stale entries
        for pkey in list(pending.keys()):
            if pkey == key:
                continue
            entry = pending[pkey]
            age = now - entry.get("first_chunk_at", 0)
            if age >= MAX_PENDING_AGE:
                is_completed = bool(entry.get("stream_ended")) and bool(entry.get("user_content_sent"))
                if (not is_completed) and entry.get("accumulated"):
                    upsert_turn(pending, pkey)
                del pending[pkey]

        # Create pending entry if new
        if key not in pending:
            vibe_session_id = get_or_create_session(gemini_session_id, user_content)
            if not vibe_session_id:
                log_debug("Failed to get/create session, skipping")
                save_json(PENDING_FILE, pending)
                print(json.dumps({}))
                return

            pending[key] = {
                "vibe_session_id": vibe_session_id,
                "user_content": user_content,
                "user_content_sent": False,
                "accumulated": "",
                "first_chunk_at": now,
                "last_chunk_at": now,
                "chunks": 0,
                "cycle_count": 0,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            log_debug(f"New message key={key}")
        else:
            # Existing entry — check for new tool-call cycle
            entry = pending[key]
            if entry.get("stream_ended"):
                entry["cycle_count"] = entry.get("cycle_count", 0) + 1
                entry["stream_ended"] = False
                if entry.get("accumulated") and chunk:
                    entry["accumulated"] += "\n\n---\n\n"
                log_debug(f"New tool-call cycle #{entry['cycle_count']} for {key}")

        # Read buffered chunks from fast path.
        # Keep a one-shot fallback for legacy msg_hash-only file names.
        buf_path = chunk_buffer_path(key)
        legacy_buf_path = os.path.join(CHUNK_BUF_DIR, f"{msg_hash}.txt")
        buffered = ""
        try:
            for candidate in (buf_path, legacy_buf_path):
                if os.path.exists(candidate):
                    with open(candidate, 'r', encoding='utf-8') as f:
                        buffered += f.read()
                    os.remove(candidate)
        except:
            pass

        if buffered or chunk:
            pending[key]["accumulated"] += buffered + (chunk or "")
            pending[key]["chunks"] = pending[key].get("chunks", 0) + 1
        pending[key]["last_chunk_at"] = now

        if stream_done:
            pending[key]["stream_ended"] = True

        if stream_done:
            vibe_session_id = pending[key].get("vibe_session_id")
            for tool_payload in extract_tool_use_events(llm_response):
                send_event({
                    "session_id": vibe_session_id,
                    "type": "tool_use",
                    "payload": tool_payload,
                })

        # Send accumulated content on stream_done
        if stream_done and pending[key].get("accumulated"):
            upsert_turn(pending, key)

        if stream_done:
            send_event({
                "session_id": pending[key].get("vibe_session_id"),
                "type": "stop",
                "payload": {
                    "reason": "turn_complete",
                    "source": "gemini_hook",
                    "turn_key": key,
                },
            })
            # Turn is finalized; drop pending entry to prevent stale re-upserts and latency buildup.
            pending.pop(key, None)

        save_json(PENDING_FILE, pending)
        print(json.dumps({}))
        sys.stdout.flush()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
