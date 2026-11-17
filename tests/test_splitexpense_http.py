"""HTTP acceptance tests for exact money, transactional rejection and retries."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import unittest

from splitexpense.laws import LocalService, equal_expense, settlement


class SplitExpenseHTTPTests(unittest.TestCase):
    def setUp(self):
        self.service = LocalService()
        self.service.__enter__()
        self.addCleanup(self.service.__exit__, None, None, None)
        self.group = self.service.group(3)
        self.group_id = self.group["id"]
        self.members = [member["id"] for member in self.group["members"]]
        self.base = f"/groups/{self.group_id}"

    def post(self, collection, body):
        return self.service.request("POST", f"{self.base}/{collection}", body)

    def assert_rejected_without_mutation(self, collection, body, status=400):
        before = self.service.snapshot(self.group_id)
        actual, response = self.post(collection, body)
        self.assertEqual(actual, status, response)
        self.assertIn("error", response)
        self.assertEqual(self.service.snapshot(self.group_id), before)

    def test_equal_rounding_and_real_settlement_balances(self):
        status, expense = self.post("expenses", equal_expense("meal", 1001, self.members))
        self.assertEqual(status, 201, expense)
        self.assertEqual([share["amount_minor"] for share in expense["shares"]], [334, 334, 333])
        status, balances = self.service.request("GET", self.base + "/balances")
        self.assertEqual(status, 200)
        self.assertEqual([item["amount_minor"] for item in balances["balances"]], [667, -334, -333])
        self.assertEqual(balances["total_minor"], 0)
        payment = settlement("payment", "m1", "m0", 334)
        self.assertEqual(self.post("settlements", payment)[0], 201)
        state = self.service.snapshot(self.group_id)
        self.assertEqual([item["amount_minor"] for item in state["balances"]], [333, 0, -333])
        self.assertEqual(sum(item["delta_minor"] for item in state["journal"]), 0)

    def test_exact_subset_and_zero_share_are_preserved(self):
        body = {
            "idempotency_key": "custom", "description": "Taxi", "payer_id": "m2",
            "amount_minor": 100, "split": {"type": "exact", "shares": [
                {"member_id": "m0", "amount_minor": 100},
                {"member_id": "m2", "amount_minor": 0},
            ]},
        }
        status, expense = self.post("expenses", body)
        self.assertEqual(status, 201, expense)
        state = self.service.snapshot(self.group_id)
        self.assertEqual([item["amount_minor"] for item in state["balances"]], [-100, 0, 100])
        self.assertEqual(expense["shares"], body["split"]["shares"])

    def test_bad_splits_types_members_and_unknown_fields_are_atomic(self):
        valid = equal_expense("invalid", 1001, self.members)
        bad = []
        for amount in [0, -1, True, 1.0, 10**12 + 1]:
            body = copy.deepcopy(valid)
            body["amount_minor"] = amount
            bad.append(body)
        body = copy.deepcopy(valid)
        body["payer_id"] = "outsider"
        bad.append(body)
        for participants in [[], ["m0", "m0"], ["m0", "outsider"]]:
            body = copy.deepcopy(valid)
            body["split"]["member_ids"] = participants
            bad.append(body)
        body = copy.deepcopy(valid)
        body["extra"] = "ignored?"
        bad.append(body)
        body = copy.deepcopy(valid)
        body["split"] = {"type": "exact", "shares": [{"member_id": "m0", "amount_minor": 1000}]}
        bad.append(body)
        for body in bad:
            with self.subTest(body=body):
                self.assert_rejected_without_mutation("expenses", body)

    def test_settlement_direction_and_overpayment_are_atomic(self):
        self.post("expenses", equal_expense("meal", 900, self.members))
        for body in [settlement("x", "m0", "m1", 1),
                     settlement("x", "m1", "m0", 301),
                     settlement("x", "m1", "m1", 1),
                     settlement("x", "outsider", "m0", 1),
                     settlement("x", "m1", "m0", 0)]:
            with self.subTest(body=body):
                self.assert_rejected_without_mutation("settlements", body)

    def test_replay_after_state_changes_and_conflicts(self):
        body = equal_expense("meal", 900, self.members)
        status, original = self.post("expenses", body)
        self.assertEqual(status, 201)
        payment = settlement("payment", "m1", "m0", 300)
        status, paid = self.post("settlements", payment)
        self.assertEqual(status, 201)
        before = self.service.snapshot(self.group_id)
        self.assertEqual(self.post("expenses", body), (200, original))
        self.assertEqual(self.post("settlements", payment), (200, paid))
        self.assertEqual(self.service.snapshot(self.group_id), before)
        changed = copy.deepcopy(body)
        changed["amount_minor"] += 1
        self.assert_rejected_without_mutation("expenses", changed, 409)
        semantic_conflict = copy.deepcopy(body)
        semantic_conflict["split"]["member_ids"] = []
        self.assert_rejected_without_mutation("expenses", semantic_conflict, 409)
        structural_error = copy.deepcopy(body)
        structural_error["unexpected"] = True
        self.assert_rejected_without_mutation("expenses", structural_error, 400)
        # Scope includes operation kind: an expense key is available to a settlement.
        self.assertEqual(self.post("settlements", settlement("meal", "m2", "m0", 300))[0], 201)

    def test_concurrent_same_key_has_one_committed_effect(self):
        body = equal_expense("race", 1001, self.members)
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(lambda _: self.post("expenses", body), range(8)))
        self.assertEqual(sorted(status for status, _ in responses), [200] * 7 + [201])
        self.assertTrue(all(response == responses[0][1] for _, response in responses))
        state = self.service.snapshot(self.group_id)
        self.assertEqual(len(state["expenses"]), 1)
        self.assertEqual(len(state["requests"]), 1)
        self.assertEqual(len(state["journal"]), 4)
        self.assertEqual([item["amount_minor"] for item in state["balances"]], [667, -334, -333])

    def test_group_isolation_and_idempotency_scope(self):
        other = self.service.group(2, name="Independent trip")
        before_other = self.service.snapshot(other["id"])
        first = equal_expense("shared-key", 1001, self.members)
        self.assertEqual(self.post("expenses", first)[0], 201)
        self.assertEqual(self.service.snapshot(other["id"]), before_other)
        second = equal_expense("shared-key", 101, ["m0", "m1"])
        self.assertEqual(self.service.request("POST", f"/groups/{other['id']}/expenses", second)[0], 201)
        self.assertEqual(len(self.service.snapshot(self.group_id)["expenses"]), 1)
        self.assertEqual(len(self.service.snapshot(other["id"])["expenses"]), 1)

    def test_raw_json_and_oversized_requests_are_rejected(self):
        before = self.service.snapshot(self.group_id)
        payloads = [b'{"amount_minor":1,"amount_minor":2}', b'{',
                    b'{"amount_minor":NaN}', b'{"amount_minor":Infinity}',
                    b'{"amount_minor":1.0}', b'[]']
        for raw in payloads:
            with self.subTest(raw=raw):
                status, response = self.service.request("POST", self.base + "/expenses", raw=raw)
                self.assertEqual(status, 400, response)
                self.assertEqual(self.service.snapshot(self.group_id), before)
        status, response = self.service.request(
            "POST", self.base + "/expenses", raw=b"{}",
            headers={"Content-Length": str(1024 * 1024 + 1)},
        )
        self.assertEqual(status, 400, response)
        self.assertEqual(self.service.snapshot(self.group_id), before)

    def test_group_validation_lists_and_unknown_routes(self):
        status, response = self.service.request("GET", "/health")
        self.assertEqual((status, response), (200, {"status": "ok"}))
        for body in [
            {"name": "", "currency": "INR", "members": self.group["members"]},
            {"name": "Trip", "currency": "inr", "members": self.group["members"]},
            {"name": "Trip", "currency": "INR", "members": self.group["members"][:1]},
            {"name": "Trip", "currency": "INR", "members": [self.group["members"][0]] * 2},
        ]:
            self.assertEqual(self.service.request("POST", "/groups", body)[0], 400)
        groups = self.service.request("GET", "/groups")[1]["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["id"], self.group_id)
        self.assertEqual(self.service.request("GET", self.base + "/expenses"), (200, {"expenses": []}))
        self.assertEqual(self.service.request("GET", self.base + "/settlements"), (200, {"settlements": []}))
        self.assertEqual(self.service.request("GET", "/groups/missing")[0], 404)
        self.assertEqual(self.service.request("GET", "/missing")[0], 404)


class SplitExpensePersistenceTests(unittest.TestCase):
    def test_database_reopen_keeps_balances_and_retry_identity(self):
        with tempfile.TemporaryDirectory(prefix="splitexpense-db-") as directory:
            path = Path(directory) / "expenses.sqlite3"
            with LocalService(path) as service:
                state = service.group(2)
                group_id = state["id"]
                body = equal_expense("persisted", 101, ["m0", "m1"])
                status, original = service.request("POST", f"/groups/{group_id}/expenses", body)
                self.assertEqual(status, 201)
                before = service.snapshot(group_id)
            with LocalService(path) as service:
                self.assertEqual(service.snapshot(group_id), before)
                self.assertEqual(service.request("POST", f"/groups/{group_id}/expenses", body), (200, original))
                self.assertEqual(service.snapshot(group_id), before)


if __name__ == "__main__":
    unittest.main()
