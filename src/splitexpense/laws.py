"""Check real HTTP executions of the split-expense app against its Lean model."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import random
import sys
import threading
import time

from lawbridge.checker import Checker
from lawbridge.errors import LawbridgeError

from .server import create_server
from .store import ExpenseStore


class LocalService:
    """An actual ephemeral loopback server with an isolated database."""

    def __init__(self, database=":memory:"):
        self.store = ExpenseStore(database)
        self.server = create_server(self.store, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.host, self.port = self.server.server_address[:2]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()

    def request(self, method, path, body=None, *, raw=None, headers=None):
        if raw is not None and body is not None:
            raise ValueError("Choose JSON body or raw bytes")
        payload = raw
        request_headers = dict(headers or {})
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            return response.status, json.loads(content)
        finally:
            connection.close()

    def group(self, count=3, *, name="Law checked trip"):
        members = [{"id": f"m{i}", "name": f"Member {i}"} for i in range(count)]
        status, state = self.request("POST", "/groups", {
            "name": name, "currency": "INR", "members": members,
        })
        if status != 201:
            raise RuntimeError(f"Group setup failed: HTTP {status}: {state}")
        return state

    def snapshot(self, group_id):
        status, state = self.request("GET", f"/groups/{group_id}")
        if status != 200:
            raise RuntimeError(f"Snapshot failed: HTTP {status}: {state}")
        return state


def equal_expense(key, amount, members, payer=None):
    return {
        "idempotency_key": key, "description": "Shared meal",
        "payer_id": payer or members[0], "amount_minor": amount,
        "split": {"type": "equal", "member_ids": list(members)},
    }


def settlement(key, source, destination, amount):
    return {
        "idempotency_key": key, "from_member": source,
        "to_member": destination, "amount_minor": amount,
    }


def observe(service, group_id, operation, body, case_id):
    before = service.snapshot(group_id)
    endpoint = "expenses" if operation == "expense" else "settlements"
    status, response = service.request("POST", f"/groups/{group_id}/{endpoint}", body)
    after = service.snapshot(group_id)
    return {
        "id": case_id, "model": "splitexpense.v1",
        "record": {
            "input": {"operation": operation, "body": copy.deepcopy(body)},
            "before": before, "after": after,
            "result": {"status": status, "body": response},
        },
    }


def _source_hashes():
    root = Path(__file__).parent
    return {str(path.relative_to(root.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.glob("*.py"))}


def run_suite(*, seed=2026, groups=12, timeout=10.0):
    if type(groups) is not int or not 1 <= groups <= 100:
        raise ValueError("groups must be between 1 and 100")
    started = time.monotonic()
    report = {
        "version": 1, "model": "splitexpense.v1", "status": "running",
        "evidence": "sampled_http_sequences", "implementation_proved": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed, "groups": groups, "cases_checked": 0,
        "eligible_cases": 0, "rejection_cases": 0,
        "operations": {}, "http_statuses": {}, "failures": [], "errors": [],
        "negative_probes": [], "source_sha256": _source_hashes(),
        "bounds": {"members": [2, 8], "random_expense_amount_minor": [1, 10001]},
        "scope": "Actual local HTTP observations checked against Lean; model proofs are separate.",
    }
    rng = random.Random(seed)
    operations = Counter()
    statuses = Counter()
    first_observation = None
    last_observation = None

    try:
        with LocalService() as service, Checker(timeout=timeout) as checker:
            report["checker"] = checker.metadata()

            def check(group_id, operation, body, label):
                nonlocal last_observation
                case_id = f"split:{report['cases_checked']}:{label}"
                observation = observe(service, group_id, operation, body, case_id)
                last_observation = observation
                try:
                    result = checker.check(observation)
                except Exception:
                    report["failures"].append({"id": case_id, "observation": observation,
                                               "error": "Lean check did not complete"})
                    raise
                report["cases_checked"] += 1
                report["eligible_cases" if result["eligible"] else "rejection_cases"] += 1
                operations[operation] += 1
                statuses[str(observation["record"]["result"]["status"])] += 1
                if not result["passed"]:
                    report["failures"].append({"id": case_id, "observation": observation,
                                               "failures": result["failures"]})
                    raise RuntimeError(f"Lean rejected {case_id}: {result['failures']}")
                return observation

            for group_number in range(groups):
                count = rng.randint(2, 8)
                state = service.group(count, name=f"Law trip {group_number}")
                group_id = state["id"]
                members = [member["id"] for member in state["members"]]
                first = equal_expense("first-meal", count * 100 + 1, members)
                observation = check(group_id, "expense", first, "equal-rounding")
                if first_observation is None:
                    first_observation = copy.deepcopy(observation)
                check(group_id, "expense", first, "expense-replay")
                conflict = copy.deepcopy(first)
                conflict["amount_minor"] += 1
                check(group_id, "expense", conflict, "expense-key-conflict")
                semantic_conflict = copy.deepcopy(first)
                semantic_conflict["split"]["member_ids"] = []
                check(group_id, "expense", semantic_conflict, "key-conflict-before-split-semantics")

                balance_map = {item["member_id"]: item["amount_minor"]
                               for item in observation["record"]["after"]["balances"]}
                debtor = next(member for member in members if balance_map[member] < 0)
                creditor = next(member for member in members if balance_map[member] > 0)
                payment = settlement("first-settlement", debtor, creditor,
                                     min(-balance_map[debtor], balance_map[creditor]))
                check(group_id, "settlement", payment, "valid-settlement")
                # The sender may now have zero debt: replay must precede current-state checks.
                check(group_id, "settlement", payment, "settlement-replay-after-payment")
                payment_conflict = copy.deepcopy(payment)
                payment_conflict["amount_minor"] += 1
                check(group_id, "settlement", payment_conflict, "settlement-key-conflict")
                overpayment = copy.deepcopy(payment)
                overpayment["idempotency_key"] = "too-much"
                overpayment["amount_minor"] = count * 1000
                check(group_id, "settlement", overpayment, "overpayment")
                check(group_id, "expense", first, "expense-replay-after-state-change")

                participants = rng.sample(members, rng.randint(1, count))
                amount = rng.randint(1, 10001)
                cuts = sorted([0, amount, *(rng.randint(0, amount) for _ in participants[1:])])
                shares = [{"member_id": member, "amount_minor": cuts[i + 1] - cuts[i]}
                          for i, member in enumerate(participants)]
                exact = {
                    "idempotency_key": "exact-shares", "description": "Custom split",
                    "payer_id": rng.choice(members), "amount_minor": amount,
                    "split": {"type": "exact", "shares": shares},
                }
                check(group_id, "expense", exact, "exact-subset")

                wrong_sum = copy.deepcopy(exact)
                wrong_sum["idempotency_key"] = "wrong-total"
                wrong_sum["split"]["shares"][0]["amount_minor"] += 1
                check(group_id, "expense", wrong_sum, "wrong-share-sum")
                duplicate = equal_expense("duplicate-member", 100, [members[0], members[0]])
                check(group_id, "expense", duplicate, "duplicate-participant")
                outsider = equal_expense("unknown-member", 100, [members[0], "outsider"])
                check(group_id, "expense", outsider, "nonmember")
                check(group_id, "expense", equal_expense("negative", -1, members), "negative-amount")
                check(group_id, "settlement", settlement("self", members[0], members[0], 1), "self-settlement")

                # Extend the history with differently-sized nested payloads and repeatable requests.
                for index in range(3):
                    subset = rng.sample(members, rng.randint(1, count))
                    body = equal_expense(f"history-{index}", rng.randint(1, 10001),
                                         subset, rng.choice(members))
                    check(group_id, "expense", body, "history-expense")

            if report["cases_checked"] == 0 or first_observation is None:
                raise RuntimeError("No HTTP law observations were checked")

            # These are checker sensitivity probes, kept separate from real execution evidence.
            balanced_error = copy.deepcopy(first_observation)
            balanced_error["id"] = "negative-probe:balanced-wrong-members"
            balances = balanced_error["record"]["after"]["balances"]
            balances[0]["amount_minor"] += 1
            balances[1]["amount_minor"] -= 1
            wrong_response = copy.deepcopy(first_observation)
            wrong_response["id"] = "negative-probe:wrong-rounding-response"
            response_shares = wrong_response["record"]["result"]["body"]["shares"]
            response_shares[0]["amount_minor"] -= 1
            response_shares[1]["amount_minor"] += 1
            for mutated in [balanced_error, wrong_response]:
                result = checker.check(mutated)
                report["negative_probes"].append({
                    "id": mutated["id"], "rejected": not result["passed"],
                    "failures": result["failures"],
                })
                if result["passed"]:
                    raise RuntimeError(f"Checker accepted deliberate corruption: {mutated['id']}")
        report["status"] = "passed"
    except (LawbridgeError, RuntimeError, OSError, ValueError, KeyError, StopIteration,
            http.client.HTTPException) as exc:
        report["status"] = "error"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        if last_observation is not None and not report["failures"]:
            report["last_observation"] = last_observation
    report["operations"] = dict(operations)
    report["http_statuses"] = dict(statuses)
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path(".lawbridge/splitexpense-laws.json"))
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--groups", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        report = run_suite(seed=args.seed, groups=args.groups, timeout=args.timeout)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"{report['status'].upper()} split expense: {report['cases_checked']} real HTTP law checks; "
          f"{len(report['negative_probes'])} separate checker probes")
    for error in report["errors"]:
        print(f"ERROR: {error}", file=sys.stderr)
    print(f"Evidence: {args.report}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
