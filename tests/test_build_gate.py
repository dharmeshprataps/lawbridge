"""End-to-end acceptance tests: every passing execution uses the real Lean checker."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "examples" / "contracts"
CHECKER = ROOT / "lean" / ".lake" / "build" / "bin" / "lawcheck"


class BuildGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not CHECKER.is_file():
            raise RuntimeError("Real Lean checker missing; run make build before make test")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lawbridge-test-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
        self.env["LAWBRIDGE_LEAN_CHECKER"] = str(CHECKER)

    def cli(self, *arguments: object, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "lawbridge", "check", *(str(x) for x in arguments)],
            cwd=ROOT, env=env or self.env, capture_output=True, text=True, timeout=30,
        )

    def contract(self, template: str = "add", **changes: object) -> Path:
        value = json.loads((CONTRACTS / f"{template}.laws.json").read_text())
        value.update(changes)
        path = self.work / f"{template}.laws.json"
        path.write_text(json.dumps(value))
        return path

    def fake_checker(self, body: str) -> dict:
        path = self.work / "fake_checker.py"
        path.write_text("#!/usr/bin/env python3\n" + body)
        path.chmod(0o700)
        return {**self.env, "LAWBRIDGE_LEAN_CHECKER": str(path)}

    def assert_code(self, result: subprocess.CompletedProcess, code: int) -> None:
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_passing_examples_check_actual_python_and_invalid_inputs(self) -> None:
        report = self.work / "report.json"
        result = self.cli(CONTRACTS, "--report", report)
        self.assert_code(result, 0)
        data = json.loads(report.read_text())
        self.assertEqual(data["status"], "passed")
        self.assertEqual(data["evidence"], "execution_checks")
        self.assertEqual(len(data["contracts"]), 3)
        by_name = {entry["name"]: entry for entry in data["contracts"]}
        addition = by_name["exact integer addition"]
        self.assertEqual(addition["cases_checked"], 81)
        self.assertEqual(addition["evidence"], "exhaustive_within_bounds")
        withdrawal = by_name["withdrawal arithmetic and rejection"]
        self.assertGreater(withdrawal["eligible_cases"], 0)
        self.assertGreater(withdrawal["ineligible_cases"], 0)
        account = by_name["account withdrawal transitions"]
        self.assertEqual(account["evidence"], "sampled_executions")
        self.assertGreater(account["eligible_cases"], 0)
        self.assertGreater(account["ineligible_cases"], 0)

    def test_mutated_implementation_fails_and_counterexample_replays(self) -> None:
        contract = self.contract(target="examples.broken:off_by_one_add")
        report = self.work / "failure.json"
        result = self.cli(contract, "--report", report)
        self.assert_code(result, 1)
        data = json.loads(report.read_text())
        failure = data["contracts"][0]["failures"][0]
        self.assertTrue(failure["failures"])
        self.assertIn("record", failure)
        self.assertIn("sequence", failure["replay"])
        replay = self.cli("--replay", report)
        self.assert_code(replay, 1)

    def test_stateful_frame_rule_catches_changed_owner(self) -> None:
        contract = self.contract(
            "account", target="examples.broken:ChangesOwner",
            inputs={"amount": {"type": "int", "min": 0, "max": 0}},
            testing={"seed": 1, "examples": 1, "steps": 1},
        )
        result = self.cli(contract)
        self.assert_code(result, 1)

    def test_rejected_operation_must_not_mutate_state(self) -> None:
        contract = self.contract(
            "account", target="examples.broken:MutatesOnReject",
            inputs={"amount": {"type": "int", "min": -1, "max": -1}},
            testing={"seed": 1, "examples": 1, "steps": 1},
        )
        report = self.work / "rejected.json"
        result = self.cli(contract, "--report", report)
        self.assert_code(result, 1)
        record = json.loads(report.read_text())["contracts"][0]["failures"][0]["record"]
        self.assertEqual(record["before"]["balance"], 100)
        self.assertEqual(record["after"]["balance"], 101)
        self.assert_code(self.cli("--replay", report), 1)

    def test_proved_model_remains_required_when_dsl_is_weakened(self) -> None:
        contract = self.contract(
            "account", target="examples.broken:WrongBalance",
            inputs={"amount": {"type": "int", "min": 1, "max": 1}},
            ensures=["after.balance >= 0"],
            testing={"seed": 1, "examples": 1, "steps": 1},
        )
        self.assert_code(self.cli(contract), 1)

    def test_wrong_exception_does_not_count_as_valid_rejection(self) -> None:
        contract = self.contract(
            "account", target="examples.broken:WrongException",
            inputs={"amount": {"type": "int", "min": -1, "max": -1}},
            testing={"seed": 1, "examples": 1, "steps": 1},
        )
        self.assert_code(self.cli(contract), 1)

    def test_boolean_does_not_masquerade_as_integer(self) -> None:
        contract = self.contract(
            target="examples.broken:boolean_add",
            inputs={"a": {"type": "int", "min": 0, "max": 0},
                    "b": {"type": "int", "min": 1, "max": 1}},
        )
        self.assertNotEqual(self.cli(contract).returncode, 0)

    def test_float_results_are_explicitly_unsupported(self) -> None:
        contract = self.contract(target="examples.broken:floats_add")
        self.assert_code(self.cli(contract), 2)

    def test_invalid_expression_never_executes(self) -> None:
        for expression in ["__import__('os').getcwd()", "input.a / input.b", "input.a[0]"]:
            with self.subTest(expression=expression):
                contract = self.contract(ensures=[expression])
                self.assert_code(self.cli(contract), 2)

    def test_missing_laws_no_cases_and_bad_generation_bounds_fail(self) -> None:
        variants = [
            {"ensures": []},
            {"testing": {"examples": 0}},
            {"inputs": {"a": {"type": "int", "min": False, "max": 1}}},
            {"testing": {"mode": "exhaustive", "max_cases": 1}},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                self.assert_code(self.cli(self.contract(**changes)), 2)

    def test_empty_directory_fails(self) -> None:
        self.assert_code(self.cli(self.work), 2)

    def test_missing_checker_fails(self) -> None:
        env = {**self.env, "LAWBRIDGE_LEAN_CHECKER": str(self.work / "not-installed")}
        self.assert_code(self.cli(CONTRACTS / "add.laws.json", env=env), 2)

    def test_checker_death_fails(self) -> None:
        env = self.fake_checker("raise SystemExit(0)\n")
        self.assert_code(self.cli(CONTRACTS / "add.laws.json", env=env), 2)

    def test_checker_timeout_fails(self) -> None:
        env = self.fake_checker("import time\ntime.sleep(5)\n")
        self.assert_code(self.cli(CONTRACTS / "add.laws.json", "--timeout", "0.2", env=env), 2)

    def test_python_execution_timeout_fails(self) -> None:
        contract = self.contract(target="examples.broken:slow_add")
        self.assert_code(self.cli(contract, "--timeout", "0.2"), 2)

    def test_only_rejection_cases_do_not_claim_postcondition_coverage(self) -> None:
        contract = self.contract(
            "withdraw_balance",
            inputs={"balance": {"type": "int", "min": 0, "max": 0},
                    "amount": {"type": "int", "min": 1, "max": 1}},
        )
        self.assert_code(self.cli(contract), 2)

    def test_mismatched_checker_id_fails(self) -> None:
        env = self.fake_checker(
            "import sys,json\n"
            "for line in sys.stdin:\n"
            " print(json.dumps({'id':'wrong','passed':True,'eligible':True,'failures':[]}),flush=True)\n"
        )
        self.assert_code(self.cli(CONTRACTS / "add.laws.json", env=env), 2)

    def test_invalid_checker_response_types_fail(self) -> None:
        env = self.fake_checker(
            "import sys,json\n"
            "for line in sys.stdin:\n"
            " r=json.loads(line)\n"
            " print(json.dumps({'id':r['id'],'passed':'true','eligible':True,'failures':[]}),flush=True)\n"
        )
        self.assert_code(self.cli(CONTRACTS / "add.laws.json", env=env), 2)

    def test_replay_rejects_changed_contract(self) -> None:
        contract = self.contract(target="examples.broken:off_by_one_add")
        report = self.work / "failure.json"
        self.assert_code(self.cli(contract, "--report", report), 1)
        changed = json.loads(contract.read_text())
        changed["ensures"] = ["result == input.a + input.b + 1"]
        contract.write_text(json.dumps(changed))
        self.assert_code(self.cli("--replay", report), 2)


def path(*parts: str) -> dict:
    return {"kind": "path", "value": list(parts)}


def integer(value: int) -> dict:
    return {"kind": "int", "value": value}


def binary(kind: str, left: dict, right: dict) -> dict:
    return {"kind": kind, "left": left, "right": right}


class LeanProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not CHECKER.is_file():
            raise RuntimeError("Real Lean checker missing; run make build before make test")

    def request(self, **changes: object) -> dict:
        request = {
            "id": "direct-test", "requires": [],
            "ensures": [binary("eq", path("result"), integer(1))],
            "record": {"input": {}, "before": None, "after": None,
                       "result": 1, "ok": True, "error": None},
        }
        request.update(changes)
        return request

    def check(self, request: dict) -> dict:
        result = subprocess.run(
            [str(CHECKER)], input=json.dumps(request) + "\n", text=True,
            capture_output=True, timeout=10, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        response = json.loads(lines[0])
        self.assertEqual(response["id"], request["id"])
        self.assertIs(type(response["passed"]), bool)
        return response

    def test_exact_arithmetic_beyond_machine_integer_range(self) -> None:
        large = 10 ** 100
        request = self.request(ensures=[binary("eq", path("result"), binary("add", path("input", "a"), path("input", "b")))])
        request["record"]["input"] = {"a": large, "b": large + 1}
        request["record"]["result"] = 2 * large + 1
        self.assertTrue(self.check(request)["passed"])

    def test_bool_and_integer_are_distinct(self) -> None:
        request = self.request()
        request["record"]["result"] = True
        self.assertFalse(self.check(request)["passed"])

    def test_unknown_ast_missing_path_and_bad_operand_fail(self) -> None:
        expressions = [
            {"kind": "call", "value": "anything"},
            binary("eq", path("result", "missing"), integer(1)),
            binary("add", {"kind": "bool", "value": True}, integer(1)),
        ]
        for expression in expressions:
            with self.subTest(expression=expression):
                result = self.check(self.request(ensures=[expression]))
                self.assertFalse(result["passed"])
                self.assertTrue(result.get("error"))

    def test_boolean_disjunction_short_circuits(self) -> None:
        expression = binary("or", {"kind": "bool", "value": True}, path("result", "missing"))
        self.assertTrue(self.check(self.request(ensures=[expression]))["passed"])

    def test_eligible_failure_and_ineligible_success_are_failures(self) -> None:
        eligible_failure = self.request()
        eligible_failure["record"].update(ok=False, result=None, error={"type": "ValueError", "message": "bad"})
        self.assertFalse(self.check(eligible_failure)["passed"])
        ineligible_success = self.request(requires=[{"kind": "bool", "value": False}], reject_exception="ValueError")
        self.assertFalse(self.check(ineligible_success)["passed"])

    def test_rejection_checks_exception_type_and_unchanged_state(self) -> None:
        request = self.request(requires=[{"kind": "bool", "value": False}], reject_exception="ValueError")
        request["record"].update(ok=False, result=None, before={"x": 1}, after={"x": 1}, error={"type": "ValueError", "message": "invalid"})
        self.assertTrue(self.check(request)["passed"])
        changed = copy.deepcopy(request)
        changed["record"]["after"]["x"] = 2
        self.assertFalse(self.check(changed)["passed"])
        wrong = copy.deepcopy(request)
        wrong["record"]["error"]["type"] = "RuntimeError"
        self.assertFalse(self.check(wrong)["passed"])

    def test_process_handles_multiple_requests(self) -> None:
        requests = [self.request(id="first"), self.request(id="second")]
        result = subprocess.run(
            [str(CHECKER)], input="\n".join(json.dumps(x) for x in requests) + "\n",
            capture_output=True, text=True, timeout=10, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([x["id"] for x in responses], ["first", "second"])
        self.assertTrue(all(x["passed"] for x in responses))

    def test_registered_model_rejects_a_misclassified_valid_operation(self) -> None:
        state = {"balance": 100, "spent": 0, "limit": 80, "owner": "dharmesh"}
        request = self.request(
            model="account.withdraw.v1", requires=[{"kind": "bool", "value": False}],
            ensures=[{"kind": "bool", "value": True}], reject_exception="ValueError",
        )
        request["record"].update(input={"amount": 1}, before=state, after=state,
                                 ok=False, result=None,
                                 error={"type": "ValueError", "message": "wrong precondition"})
        self.assertFalse(self.check(request)["passed"])

    def test_registered_model_rejects_invalid_initial_state(self) -> None:
        state = {"balance": 100, "spent": 81, "limit": 80, "owner": "dharmesh"}
        request = self.request(
            model="account.withdraw.v1", requires=[{"kind": "bool", "value": False}],
            ensures=[{"kind": "bool", "value": True}], reject_exception="ValueError",
        )
        request["record"].update(input={"amount": 0}, before=state, after=state,
                                 ok=False, result=None,
                                 error={"type": "ValueError", "message": "already invalid"})
        self.assertFalse(self.check(request)["passed"])


if __name__ == "__main__":
    unittest.main()
