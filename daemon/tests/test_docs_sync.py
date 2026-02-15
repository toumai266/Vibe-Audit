"""Guardrail test: runtime routes and docs must stay in sync."""
import subprocess
import sys
from pathlib import Path


def test_docs_sync_script_passes():
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "verify_docs_sync.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
