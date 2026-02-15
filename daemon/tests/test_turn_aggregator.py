"""Tests for turn-level evidence aggregation."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.turn_aggregator import aggregate_turn_evidence


def test_write_event_detected_as_created():
    events = [{"tool": "Write", "inputs": {"file_path": "src/App.jsx"}, "outputs": "ok"}]
    result = aggregate_turn_evidence(events)
    assert "src/App.jsx" in result["files_created"]
    assert result["tool_count"] == 1


def test_edit_event_detected_as_modified():
    events = [{"tool": "Edit", "inputs": {"file_path": "src/index.css"}, "outputs": "ok"}]
    result = aggregate_turn_evidence(events)
    assert "src/index.css" in result["files_modified"]


def test_read_event_detected():
    events = [{"tool": "Read", "inputs": {"file_path": "README.md"}, "outputs": "content"}]
    result = aggregate_turn_evidence(events)
    assert "README.md" in result["files_read"]


def test_bash_command_captured():
    events = [{"tool": "Bash", "inputs": {"command": "npm test"}, "outputs": "All tests passed"}]
    result = aggregate_turn_evidence(events)
    assert "npm test" in result["commands_run"]


def test_pip_install_detected():
    events = [{"tool": "Bash", "inputs": {"command": "pip install requests flask"}, "outputs": "ok"}]
    result = aggregate_turn_evidence(events)
    assert "pip:requests" in result["packages_installed"]
    assert "pip:flask" in result["packages_installed"]


def test_npm_install_detected():
    events = [{"tool": "Bash", "inputs": {"command": "npm install axios"}, "outputs": "ok"}]
    result = aggregate_turn_evidence(events)
    assert "npm:axios" in result["packages_installed"]


def test_error_extraction():
    events = [{"tool": "Bash", "inputs": {"command": "python app.py"}, "outputs": "ImportError: no module named 'flask'"}]
    result = aggregate_turn_evidence(events)
    assert len(result["errors"]) >= 1
    assert any("flask" in e for e in result["errors"])


def test_mixed_events():
    events = [
        {"tool": "Read", "inputs": {"file_path": "src/main.py"}, "outputs": "content"},
        {"tool": "Edit", "inputs": {"file_path": "src/main.py"}, "outputs": "ok"},
        {"tool": "Write", "inputs": {"file_path": "src/utils.py"}, "outputs": "ok"},
        {"tool": "Bash", "inputs": {"command": "pip install requests"}, "outputs": "ok"},
        {"tool": "Bash", "inputs": {"command": "python -m pytest"}, "outputs": "3 passed"},
    ]
    result = aggregate_turn_evidence(events)
    assert "src/main.py" in result["files_read"]
    assert "src/main.py" in result["files_modified"]
    assert "src/utils.py" in result["files_created"]
    assert len(result["commands_run"]) == 2
    assert "pip:requests" in result["packages_installed"]
    assert result["tool_count"] == 5


def test_empty_events():
    result = aggregate_turn_evidence([])
    assert result["tool_count"] == 0
    assert result["files_read"] == []
    assert result["files_modified"] == []
    assert result["files_created"] == []
    assert result["commands_run"] == []
    assert result["packages_installed"] == []
    assert result["errors"] == []


def test_payload_wrapper_format():
    """Events with {type, payload} wrapper format should also work."""
    events = [
        {"type": "tool_use", "payload": {"tool": "Write", "inputs": {"file_path": "foo.py"}, "outputs": "ok"}},
    ]
    result = aggregate_turn_evidence(events)
    assert "foo.py" in result["files_created"]


def test_gemini_readfile_tool_detected():
    events = [{"tool": "ReadFile", "inputs": {"absolute_path": "testhook/main.py"}, "outputs": "ok"}]
    result = aggregate_turn_evidence(events)
    assert "testhook/main.py" in result["files_read"]


def test_gemini_writefile_tool_detected():
    events = [{"tool": "WriteFile", "inputs": {"path": "testhook/star_pattern.py"}, "outputs": "ok"}]
    result = aggregate_turn_evidence(events)
    assert "testhook/star_pattern.py" in result["files_created"]


def test_run_shell_command_tool_detected():
    events = [{"tool": "run_shell_command", "inputs": {"command": "pytest -q"}, "outputs": "2 passed"}]
    result = aggregate_turn_evidence(events)
    assert "pytest -q" in result["commands_run"]
