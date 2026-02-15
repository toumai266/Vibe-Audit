"""
Rule-based flag detection engine.
Scans event payloads for risky patterns without LLM calls.
Returns flags instantly for known dangerous patterns.
"""
import re
from typing import Optional, List

RULES = [
    {
        "pattern": r"rm\s+(-[a-zA-Z]*)?r[a-zA-Z]*f|rm\s+(-[a-zA-Z]*)?f[a-zA-Z]*r",
        "category": "risky_action",
        "severity": "high",
        "title": "Destructive delete command (rm -rf)",
    },
    {
        "pattern": r"DROP\s+(TABLE|DATABASE|SCHEMA)",
        "category": "risky_action",
        "severity": "high",
        "title": "Database drop command",
    },
    {
        "pattern": r"TRUNCATE\s+TABLE",
        "category": "risky_action",
        "severity": "high",
        "title": "Database truncate command",
    },
    {
        "pattern": r"\.env\b",
        "category": "risky_action",
        "severity": "med",
        "title": "Environment file access",
    },
    {
        "pattern": r"(password|secret|api_key|token|credentials)\s*[:=]",
        "category": "risky_action",
        "severity": "med",
        "title": "Potential secret/credential exposure",
    },
    {
        "pattern": r"curl\s|wget\s",
        "category": "risky_action",
        "severity": "low",
        "title": "External network call",
        "scope": "bash_command",
    },
    {
        "pattern": r"chmod\s+777",
        "category": "risky_action",
        "severity": "med",
        "title": "Overly permissive file permissions",
    },
    {
        "pattern": r"eval\s*\(",
        "category": "risky_action",
        "severity": "med",
        "title": "Dynamic code evaluation",
    },
    {
        "pattern": r"subprocess\.call|os\.system|exec\s*\(",
        "category": "risky_action",
        "severity": "med",
        "title": "Shell command execution",
    },
    {
        "pattern": r"--force|--hard|--no-verify",
        "category": "risky_action",
        "severity": "med",
        "title": "Forced/unsafe git operation",
    },
    {
        "pattern": r"git\s+push\s+.*--force|git\s+reset\s+--hard",
        "category": "risky_action",
        "severity": "high",
        "title": "Destructive git operation",
    },
    {
        "pattern": r"pip\s+install\s+(?!-r\b)",
        "category": "scope_bloat",
        "severity": "low",
        "title": "New package installation",
    },
    {
        "pattern": r"npm\s+install\s+(?!--save-dev)",
        "category": "scope_bloat",
        "severity": "low",
        "title": "New npm package installation",
    },
]


def _extract_searchable_text(event) -> str:
    """Extract all text content from an event for pattern matching."""
    parts = []

    # Handle both dict and ORM object
    if hasattr(event, 'payload'):
        payload = event.payload if isinstance(event.payload, dict) else {}
        event_type = getattr(event, 'type', '')
    elif isinstance(event, dict):
        payload = event.get('payload', {})
        event_type = event.get('type', '')
    else:
        return ''

    parts.append(event_type)

    # Extract known fields
    for key in ['tool', 'tool_name', 'content', 'output', 'result',
                'inputs', 'outputs', 'command', 'user_prompt']:
        val = payload.get(key)
        if val:
            if isinstance(val, dict):
                parts.append(str(val))
            elif isinstance(val, list):
                parts.append(' '.join(str(v) for v in val))
            else:
                parts.append(str(val))

    return ' '.join(parts)


def _extract_bash_command_text(event) -> str:
    """Extract shell command text only when the event represents Bash tool_use."""
    payload = {}
    event_type = ""

    if hasattr(event, 'payload'):
        payload = event.payload if isinstance(event.payload, dict) else {}
        event_type = getattr(event, 'type', '')
    elif isinstance(event, dict):
        payload = event.get('payload', {}) or {}
        event_type = event.get('type', '')
    else:
        return ""

    if event_type != "tool_use":
        return ""

    tool_name = str(payload.get("tool") or payload.get("tool_name") or "")
    if tool_name.lower() != "bash":
        return ""

    inputs = payload.get("inputs")
    if isinstance(inputs, dict):
        cmd = inputs.get("command")
        if isinstance(cmd, str):
            return cmd

    cmd = payload.get("command")
    if isinstance(cmd, str):
        return cmd

    return ""


def check_rules(event) -> List[dict]:
    """
    Check event against all rules.
    Returns list of flag dicts for each matched rule.
    """
    text = _extract_searchable_text(event)
    if not text:
        return []

    flags = []
    for rule in RULES:
        search_text = text
        if rule.get("scope") == "bash_command":
            search_text = _extract_bash_command_text(event)

        if search_text and re.search(rule["pattern"], search_text, re.IGNORECASE):
            flags.append({
                "severity": rule["severity"],
                "category": rule["category"],
                "title": rule["title"],
                "rationale": f"Pattern matched: {rule['pattern'][:50]}",
            })

    return flags
