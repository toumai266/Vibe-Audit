"""Tests for rule-based flag detection engine."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.rules import check_rules, _extract_searchable_text


class FakeEvent:
    def __init__(self, type, payload):
        self.type = type
        self.payload = payload


# --- _extract_searchable_text ---

def test_extract_from_tool_use():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "ls"}, "output": "file.txt"})
    text = _extract_searchable_text(e)
    assert "Bash" in text
    assert "ls" in text


def test_extract_from_dict():
    d = {"type": "tool_use", "payload": {"tool": "Write", "content": "hello"}}
    text = _extract_searchable_text(d)
    assert "Write" in text
    assert "hello" in text


def test_extract_empty():
    assert _extract_searchable_text(42) == ""


# --- check_rules: destructive commands ---

def test_rm_rf_detected():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "rm -rf /tmp/data"}})
    flags = check_rules(e)
    assert any(f["title"] == "Destructive delete command (rm -rf)" for f in flags)
    assert any(f["severity"] == "high" for f in flags)


def test_rm_rf_variant():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "rm -fr build/"}})
    flags = check_rules(e)
    assert any("rm -rf" in f["title"] for f in flags)


def test_drop_table():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "sqlite3 db.sqlite 'DROP TABLE users'"}})
    flags = check_rules(e)
    assert any("drop" in f["title"].lower() for f in flags)


def test_truncate_table():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "TRUNCATE TABLE logs"}})
    flags = check_rules(e)
    assert any("truncate" in f["title"].lower() for f in flags)


# --- check_rules: secrets ---

def test_env_file_access():
    e = FakeEvent("tool_use", {"tool": "Read", "inputs": {"file": ".env"}})
    flags = check_rules(e)
    assert any("Environment" in f["title"] for f in flags)


def test_credential_pattern():
    e = FakeEvent("tool_use", {"tool": "Write", "output": 'api_key="sk-12345"'})
    flags = check_rules(e)
    assert any("secret" in f["title"].lower() or "credential" in f["title"].lower() for f in flags)


# --- check_rules: git ---

def test_force_push():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "git push --force origin main"}})
    flags = check_rules(e)
    titles = [f["title"] for f in flags]
    assert any("git" in t.lower() for t in titles)


def test_git_reset_hard():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "git reset --hard HEAD~3"}})
    flags = check_rules(e)
    assert any("git" in f["title"].lower() for f in flags)


# --- check_rules: code execution ---

def test_eval():
    e = FakeEvent("tool_use", {"tool": "Write", "output": "result = eval(user_input)"})
    flags = check_rules(e)
    assert any("eval" in f["title"].lower() for f in flags)


def test_subprocess():
    e = FakeEvent("tool_use", {"tool": "Write", "output": "subprocess.call(['rm', 'file'])"})
    flags = check_rules(e)
    assert any("shell" in f["title"].lower() or "command" in f["title"].lower() for f in flags)


# --- check_rules: package installs ---

def test_pip_install():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "pip install requests"}})
    flags = check_rules(e)
    assert any("package" in f["title"].lower() for f in flags)


def test_npm_install():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "npm install lodash"}})
    flags = check_rules(e)
    assert any("npm" in f["title"].lower() for f in flags)


def test_external_network_call_detected_for_bash_command():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "curl http://localhost:8000/health"}})
    flags = check_rules(e)
    assert any("external network call" in f["title"].lower() for f in flags)


def test_external_network_call_not_detected_for_write_content():
    e = FakeEvent(
        "tool_use",
        {
            "tool": "Write",
            "inputs": {
                "file_path": "README.md",
                "content": "Example: curl http://localhost:8000/health",
            },
        },
    )
    flags = check_rules(e)
    assert not any("external network call" in f["title"].lower() for f in flags)


# --- check_rules: safe commands (no flags) ---

def test_safe_command_no_flags():
    e = FakeEvent("tool_use", {"tool": "Bash", "inputs": {"command": "ls -la"}})
    flags = check_rules(e)
    assert len(flags) == 0


def test_safe_read_no_flags():
    e = FakeEvent("tool_use", {"tool": "Read", "inputs": {"file": "src/app.py"}})
    flags = check_rules(e)
    assert len(flags) == 0


def test_user_message_not_checked():
    """User messages should not be passed to check_rules in the pipeline,
    but even if they are, verify the function doesn't crash."""
    e = FakeEvent("user_message", {"content": "please run rm -rf for me"})
    flags = check_rules(e)
    # Rules still match on content, but processor filters by event type
    assert isinstance(flags, list)
