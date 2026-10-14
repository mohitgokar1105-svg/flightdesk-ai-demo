"""Command-line scripts run as the trusted system principal (regression: they must not fail the permission checks)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_seed_script_runs_against_a_fresh_database(tmp_path):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{(tmp_path / 'seed.db').as_posix()}", "FLIGHTDESK_DISABLE_DOTENV": "1",
           "OPENAI_API_KEY": "", "DEMO_MODE": "true", "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_demo_data.py")], env=env, capture_output=True,
                         text=True, timeout=180, cwd=ROOT)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "'inquiries':" in out.stdout and "PermissionDenied" not in out.stderr
