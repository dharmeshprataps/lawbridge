"""Evidence-capture errors must never satisfy a target's rejection contract."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EvidenceIntegrityTests(unittest.TestCase):
    def test_copy_failure_is_not_an_expected_target_exception(self):
        with tempfile.TemporaryDirectory(prefix="lawbridge-evidence-") as directory:
            work = Path(directory)
            (work / "copy_target.py").write_text(
                "class Uncopyable:\n"
                "    def __deepcopy__(self, memo):\n"
                "        raise ValueError('evidence capture failed')\n"
                "def run(*, value):\n"
                "    return 0 if value == 0 else Uncopyable()\n"
            )
            contract = {
                "version": 1, "name": "capture must be trustworthy",
                "target": "copy_target:run", "mode": "pure",
                "inputs": {"value": {"type": "int", "min": 0, "max": 1}},
                "requires": ["input.value == 0"], "ensures": ["result == 0"],
                "reject_exception": "ValueError",
                "testing": {"mode": "exhaustive", "max_cases": 2},
            }
            path = work / "capture.laws.json"
            path.write_text(json.dumps(contract))
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT), str(work)])
            result = subprocess.run(
                [sys.executable, "-m", "lawbridge", "check", str(path)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("evidence capture failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
