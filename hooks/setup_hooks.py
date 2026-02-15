#!/usr/bin/env python3
"""
Setup Vibe Audit hooks in Claude Code + Codex CLI settings.

Claude: writes ~/.claude/settings.json hook entries.
Codex: appends a managed notify block to ~/.codex/config.toml.

Usage: python setup_hooks.py [--remove] [--target all|claude|codex]
"""
import argparse
import json
import os
import sys
from pathlib import Path

CLAUDE_HOOK_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "claude_hook.py"))
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

HOOK_EVENTS = ["UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop"]

CODEX_HOOK_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "codex_hook.py"))
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
CODEX_BLOCK_START = "# >>> vibe-audit codex hooks >>>"
CODEX_BLOCK_END = "# <<< vibe-audit codex hooks <<<"
CODEX_NOTIFY_BLOCK_START = "# >>> vibe-audit codex notify >>>"
CODEX_NOTIFY_BLOCK_END = "# <<< vibe-audit codex notify <<<"


def get_hook_command(event_type: str) -> str:
    """Get the hook command for an event type."""
    return f"python \"{CLAUDE_HOOK_SCRIPT}\" {event_type}"


def build_hook_config() -> dict:
    """Build the hooks configuration."""
    hooks = {}
    for event_type in HOOK_EVENTS:
        hooks[event_type] = [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": get_hook_command(event_type),
                    }
                ],
            }
        ]
    return hooks


def load_settings() -> dict:
    """Load existing settings or create empty."""
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                print(f"Warning: Could not parse {SETTINGS_PATH}, backing up...")
                backup = SETTINGS_PATH.with_suffix(".json.bak")
                SETTINGS_PATH.rename(backup)
                return {}
    return {}


def install_hooks(target: str = "all"):
    """Install Vibe Audit hooks for selected target(s)."""
    targets = ("claude", "codex") if target == "all" else (target,)
    errors = []
    for name, fn, key in (
        ("Claude", install_claude_hooks, "claude"),
        ("Codex", install_codex_hooks, "codex"),
    ):
        if key not in targets:
            continue
        try:
            fn()
        except Exception as exc:
            errors.append((name, exc))
            print(f"[WARN] {name} hook install failed: {exc}")

    if errors and len(errors) == len(targets):
        raise RuntimeError(f"Hook installation failed for target: {target}")
    if errors:
        print("Hook installation completed with warnings.")


def install_claude_hooks():
    """Install Vibe Audit hooks into Claude settings."""
    settings = load_settings()

    # Merge hooks - preserve existing hooks for other events
    existing_hooks = settings.get("hooks", {})
    new_hooks = build_hook_config()

    for event_type, config in new_hooks.items():
        if event_type in existing_hooks:
            # Check if our hook already exists
            existing_commands = []
            for matcher_group in existing_hooks[event_type]:
                for hook in matcher_group.get("hooks", []):
                    existing_commands.append(hook.get("command", ""))

            our_command = get_hook_command(event_type)
            if any("claude_hook.py" in cmd for cmd in existing_commands):
                # Update existing hook command
                for matcher_group in existing_hooks[event_type]:
                    for hook in matcher_group.get("hooks", []):
                        if "claude_hook.py" in hook.get("command", ""):
                            hook["command"] = our_command
            else:
                # Append our hook
                existing_hooks[event_type].append(config[0])
        else:
            existing_hooks[event_type] = config

    settings["hooks"] = existing_hooks

    # Write back
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

    print(f"Hooks installed in {SETTINGS_PATH}")
    print(f"Hook script: {CLAUDE_HOOK_SCRIPT}")
    print(f"Events: {', '.join(HOOK_EVENTS)}")
    print("Restart Claude Code for changes to take effect.")


def _strip_managed_block(raw: str, start_marker: str, end_marker: str) -> str:
    """Remove a managed block if both markers are present."""
    start = raw.find(start_marker)
    if start == -1:
        return raw
    end = raw.find(end_marker, start)
    if end == -1:
        return raw
    end += len(end_marker)
    before = raw[:start].rstrip()
    after = raw[end:].lstrip("\r\n")
    if before and after:
        return before + "\n\n" + after
    if before:
        return before + "\n"
    if after:
        return after
    return ""


def _strip_codex_block(raw: str) -> str:
    """Remove legacy managed Codex event-hook block if present."""
    return _strip_managed_block(raw, CODEX_BLOCK_START, CODEX_BLOCK_END)


def _strip_codex_notify_block(raw: str) -> str:
    """Remove managed Codex notify block if present."""
    return _strip_managed_block(raw, CODEX_NOTIFY_BLOCK_START, CODEX_NOTIFY_BLOCK_END)


def _path_for_toml(path_value: str) -> str:
    """Use forward slashes in TOML paths for better Windows compatibility."""
    return str(path_value).replace("\\", "/")


def _build_codex_hooks_block() -> str:
    """Build legacy managed Codex event-hooks block for ~/.codex/config.toml."""
    python_cmd = f"\"{sys.executable}\" \"{CODEX_HOOK_SCRIPT}\""
    hook_inline = "{ type = \"command\", command = '" + python_cmd + "', timeout = 60000 }"
    return (
        f"{CODEX_BLOCK_START}\n"
        "[[hooks.userpromptsubmit]]\n"
        "matcher = \"*\"\n"
        f"hooks = [{hook_inline}]\n\n"
        "[[hooks.assistantmessage]]\n"
        "matcher = \"*\"\n"
        f"hooks = [{hook_inline}]\n\n"
        "[[hooks.tooluseend]]\n"
        "matcher = \"*\"\n"
        f"hooks = [{hook_inline}]\n\n"
        "[[hooks.sessionend]]\n"
        "matcher = \"*\"\n"
        f"hooks = [{hook_inline}]\n"
        f"{CODEX_BLOCK_END}\n"
    )


def _build_codex_notify_block() -> str:
    """Build managed Codex notify block for ~/.codex/config.toml."""
    python_path = _path_for_toml(sys.executable)
    hook_path = _path_for_toml(CODEX_HOOK_SCRIPT)
    return (
        f"{CODEX_NOTIFY_BLOCK_START}\n"
        f'notify = ["{python_path}", "{hook_path}", "--notify"]\n'
        f"{CODEX_NOTIFY_BLOCK_END}\n"
    )


def _insert_top_level_notify_block(raw: str, block: str) -> str:
    """
    Insert notify block at TOML top-level.

    Codex expects `notify = [...]` at top-level. Appending to the end can
    accidentally place it under the last table (e.g. `[windows]`).
    """
    text = raw.strip()
    if not text:
        return block + "\n"

    newline = "\r\n" if "\r\n" in raw else "\n"

    lines = raw.splitlines()
    first_table_idx = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            first_table_idx = idx
            break

    if first_table_idx is None:
        base = raw.rstrip()
        return base + newline + newline + block + newline

    top_lines = lines[:first_table_idx]
    table_lines = lines[first_table_idx:]
    top = newline.join(top_lines).rstrip()
    tables = newline.join(table_lines).lstrip("\r\n")

    if top:
        return top + newline + newline + block + newline + newline + tables + newline
    return block + newline + newline + tables + newline


def install_codex_hooks():
    """Install Vibe Audit managed notify hook block into Codex config.toml."""
    current = ""
    if CODEX_CONFIG_PATH.exists():
        current = CODEX_CONFIG_PATH.read_text(encoding="utf-8")
    # Keep legacy event-hook block for backwards compatibility, but upsert notify block.
    current = _strip_codex_notify_block(current)
    block = _build_codex_notify_block().rstrip()

    new_text = _insert_top_level_notify_block(current, block)

    CODEX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_CONFIG_PATH.write_text(new_text, encoding="utf-8")

    print(f"Codex notify hook installed in {CODEX_CONFIG_PATH}")
    print(f"Hook script: {CODEX_HOOK_SCRIPT}")
    print("Mode: notify (agent-turn-complete)")
    print("Fallback: legacy event-hook tables remain if already present")
    print("Restart Codex CLI for changes to take effect.")


def remove_hooks(target: str = "all"):
    """Remove Vibe Audit hooks for selected target(s)."""
    targets = ("claude", "codex") if target == "all" else (target,)
    errors = []
    for name, fn, key in (
        ("Claude", remove_claude_hooks, "claude"),
        ("Codex", remove_codex_hooks, "codex"),
    ):
        if key not in targets:
            continue
        try:
            fn()
        except Exception as exc:
            errors.append((name, exc))
            print(f"[WARN] {name} hook removal failed: {exc}")

    if errors and len(errors) == len(targets):
        raise RuntimeError(f"Hook removal failed for target: {target}")
    if errors:
        print("Hook removal completed with warnings.")


def remove_claude_hooks():
    """Remove Vibe Audit hooks from Claude settings."""
    settings = load_settings()
    hooks = settings.get("hooks", {})

    for event_type in HOOK_EVENTS:
        if event_type in hooks:
            hooks[event_type] = [
                mg for mg in hooks[event_type]
                if not any("claude_hook.py" in h.get("command", "") for h in mg.get("hooks", []))
            ]
            if not hooks[event_type]:
                del hooks[event_type]

    if not hooks:
        settings.pop("hooks", None)
    else:
        settings["hooks"] = hooks

    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

    print("Vibe Audit Claude hooks removed.")


def remove_codex_hooks():
    """Remove managed Codex notify block from ~/.codex/config.toml."""
    if not CODEX_CONFIG_PATH.exists():
        print("Codex config not found; nothing to remove.")
        return
    current = CODEX_CONFIG_PATH.read_text(encoding="utf-8")
    updated = _strip_codex_notify_block(current)
    if updated == current:
        print("No managed Codex notify block found.")
        return
    CODEX_CONFIG_PATH.write_text(updated, encoding="utf-8")
    print("Vibe Audit Codex notify hook removed.")


def parse_args():
    parser = argparse.ArgumentParser(description="Install/remove Vibe Audit hooks.")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove hooks instead of installing",
    )
    parser.add_argument(
        "--target",
        choices=("all", "claude", "codex"),
        default="all",
        help="Select target hook scope (default: all)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.remove:
        remove_hooks(args.target)
    else:
        install_hooks(args.target)


if __name__ == "__main__":
    main()
