#!/usr/bin/env python3
"""Run a project-local Lean tool without changing the user's environment."""
from pathlib import Path
import os
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
tools = root / ".tools" / "lean" / "bin"
if len(sys.argv) < 2 or sys.argv[1] not in {"lean", "lake"}:
    raise SystemExit("Usage: lean_tool.py lean|lake [arguments...]")
executable = tools / sys.argv[1]
if not executable.exists():
    raise SystemExit("Project Lean toolchain missing. Run make bootstrap.")
env = dict(os.environ)
env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
raise SystemExit(subprocess.call([str(executable), *sys.argv[2:]], cwd=root / "lean", env=env))
