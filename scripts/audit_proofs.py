#!/usr/bin/env python3
"""Recheck the model source and audit all exported theorem dependencies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "account.withdraw.v1": ("AccountModel", (
        "initialize_valid", "withdrawal_preserves_validity", "withdrawal_preserves_owner",
        "withdrawal_preserves_limit", "withdrawal_conserves_accounting", "trace_preserves_validity",
    )),
    "splitexpense.v1": ("SplitExpenseModel", (
        "debits_total", "expense_balanced", "settlement_balanced", "append_total",
        "member_balance_append", "balanced_event_preserves_total", "history_preserves_total", "history_preserves_zero",
    )),
}
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}


def main() -> int:
    tools = ROOT / ".tools" / "lean" / "bin"
    lean = tools / "lean"
    lake = tools / "lake"
    if not lean.is_file() or not lake.is_file():
        raise RuntimeError("Missing project toolchain; run make bootstrap.")
    env = dict(os.environ)
    env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
    dependencies = {}
    models = {}
    for model_id, (module, theorem_names) in MODELS.items():
        expected = {f"Lawbridge.{module}.{name}" for name in theorem_names}
        result = subprocess.run(
            [str(lake), "env", "lean", f"Lawbridge/{module}.lean"],
            cwd=ROOT / "lean", env=env, capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        if result.returncode or "warning:" in output:
            raise RuntimeError(f"{module} source did not recheck cleanly:\n" + output)
        found = {}
        for name, names in re.findall(r"'([^']+)' depends on axioms:\s*\[([^\]]*)\]", output):
            found[name] = [a.strip() for a in names.split(",") if a.strip()]
        for name in re.findall(r"'([^']+)' does not depend on any axioms", output):
            found[name] = []
        missing = expected - found.keys()
        if missing:
            raise RuntimeError("Missing axiom audit output for: " + ", ".join(sorted(missing)) + "\n" + output)
        for name in expected:
            unexpected = set(found[name]) - ALLOWED_AXIOMS
            if unexpected:
                raise RuntimeError(f"{name} uses unapproved axioms: {sorted(unexpected)}")
        checked = {name: found[name] for name in sorted(expected)}
        dependencies.update(checked)
        models[model_id] = {"theorems": checked, "source": f"lean/Lawbridge/{module}.lean"}
    version = subprocess.check_output([str(lean), "--version"], text=True).strip()
    sources = {}
    for path in sorted((ROOT / "lean").rglob("*.lean")):
        if ".lake" not in path.parts:
            sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {
        "kind": "proved_models",
        "models": models,
        "passed": True,
        "theorems": dependencies,
        "lean": version,
        "source_sha256": sources,
        "scope": "Formal state/journal models and their allowed transitions; Python and REST conformance are execution-tested separately.",
    }
    destination = ROOT / ".lawbridge" / "model-proofs.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(f"PROVED MODELS: {len(dependencies)} theorems across {len(models)} models rechecked; no proof gaps or unapproved axioms.")
    print(f"Evidence: {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"proof audit: {exc}", file=sys.stderr)
        raise SystemExit(2)
