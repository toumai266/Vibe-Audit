"""
Turn-level evidence aggregator.
Extracts structured evidence from tool_use events within a turn.
"""
import re

READ_TOOLS = {
    "read", "cat", "head", "tail", "view", "open",
    "readfile", "read_file", "readfolder", "read_folder",
    "list_directory", "listdir"
}
WRITE_TOOLS = {
    "write", "create", "write_file", "writefile", "create_file"
}
EDIT_TOOLS = {
    "edit", "replace", "sed", "patch", "str_replace_editor",
    "apply_patch"
}
SHELL_TOOLS = {
    "bash", "shell", "terminal", "execute",
    "run_shell_command", "run_shell"
}


def aggregate_turn_evidence(events: list) -> dict:
    """
    Extract structured evidence from a list of tool_use event payloads.

    Args:
        events: List of event dicts with {type, payload} or raw payload dicts
                with {tool, inputs, outputs/output}

    Returns:
        {
            "files_read": [...],
            "files_modified": [...],
            "files_created": [...],
            "commands_run": [...],
            "packages_installed": [...],
            "errors": [...],
            "tool_count": N
        }
    """
    files_read = set()
    files_modified = set()
    files_created = set()
    commands_run = []
    packages_installed = []
    errors = []
    tool_count = 0

    for event in events:
        # Normalize: accept both Event-like dicts and raw payload dicts
        payload = event.get("payload", event)
        tool = payload.get("tool", payload.get("tool_name", ""))
        inputs = payload.get("inputs", {}) or {}
        output_raw = payload.get("outputs", payload.get("output", ""))
        output = str(output_raw) if output_raw is not None else ""

        if not tool:
            continue

        tool_count += 1
        tool_lower = tool.lower()
        path = _extract_path(inputs)

        # File read tools
        if tool_lower in READ_TOOLS:
            if path:
                files_read.add(path)

        # File write/create tools
        elif tool_lower in WRITE_TOOLS:
            if path:
                files_created.add(path)

        # File edit tools
        elif tool_lower in EDIT_TOOLS:
            if path:
                files_modified.add(path)

        # Bash/shell commands
        elif tool_lower in SHELL_TOOLS:
            command = _extract_command(inputs)
            if command:
                commands_run.append(command[:200])
                _extract_packages(command, packages_installed)
                _extract_file_ops(command, files_read, files_modified, files_created)

        # Glob/Grep (read-like)
        elif tool_lower in ("glob", "grep", "search", "find"):
            pass  # These are search ops, not file modifications

        # Check output for errors
        if output:
            _extract_errors(output, errors)

    return {
        "files_read": sorted(files_read),
        "files_modified": sorted(files_modified),
        "files_created": sorted(files_created),
        "commands_run": commands_run,
        "packages_installed": packages_installed,
        "errors": errors[:10],  # Cap at 10 errors
        "tool_count": tool_count
    }


def _extract_path(inputs: dict) -> str:
    """Extract a likely file path from tool args."""
    if not isinstance(inputs, dict):
        return ""
    for key in ("file_path", "path", "file", "filepath", "absolute_path", "relative_path", "target"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_command(inputs: dict) -> str:
    """Extract a shell command from tool args."""
    if not isinstance(inputs, dict):
        return ""
    for key in ("command", "cmd"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested_args = inputs.get("args")
    if isinstance(nested_args, dict):
        for key in ("command", "cmd"):
            value = nested_args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raw = inputs.get("raw")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return ""


def _extract_packages(command: str, packages: list):
    """Extract package install commands from bash commands."""
    # pip install — capture everything after install up to command separator
    pip_match = re.search(r'pip\s+install\s+(.+?)(?:\s*[&|;]|$)', command)
    if pip_match:
        for token in pip_match.group(1).split():
            if not token.startswith('-'):
                packages.append(f"pip:{token}")

    # npm install
    npm_match = re.search(r'npm\s+(?:install|i)\s+(.+?)(?:\s*[&|;]|$)', command)
    if npm_match:
        for token in npm_match.group(1).split():
            if not token.startswith('-'):
                packages.append(f"npm:{token}")

    # yarn add
    yarn_match = re.search(r'yarn\s+add\s+(.+?)(?:\s*[&|;]|$)', command)
    if yarn_match:
        for token in yarn_match.group(1).split():
            if not token.startswith('-'):
                packages.append(f"yarn:{token}")


def _extract_file_ops(command: str, files_read: set, files_modified: set, files_created: set):
    """Extract file operations from bash commands."""
    # cat/less/more -> read
    cat_match = re.findall(r'(?:cat|less|more|head|tail)\s+([^\s&|;>]+)', command)
    for f in cat_match:
        if not f.startswith('-'):
            files_read.add(f)

    # Redirect -> create/modify
    redirect_match = re.findall(r'>\s*([^\s&|;]+)', command)
    for f in redirect_match:
        files_created.add(f)


def _extract_errors(output: str, errors: list):
    """Extract error messages from command output."""
    output_lower = output.lower()
    error_patterns = [
        r'(?:error|Error|ERROR)[:\s](.{10,80})',
        r'(?:failed|Failed|FAILED)[:\s](.{10,80})',
        r'(?:traceback|Traceback).+',
        r'(?:ModuleNotFoundError|ImportError|SyntaxError|TypeError|ValueError)[:\s](.{10,80})',
    ]
    for pattern in error_patterns:
        matches = re.findall(pattern, output[:2000])
        for match in matches:
            err_text = match.strip() if isinstance(match, str) else str(match).strip()
            if err_text and err_text not in errors:
                errors.append(err_text[:200])
                if len(errors) >= 10:
                    return
