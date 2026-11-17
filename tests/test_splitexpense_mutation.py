"""A real HTTP implementation mutation must be caught despite zero-sum totals."""
import unittest
from unittest.mock import patch

from lawbridge.checker import Checker
from splitexpense.laws import LocalService, equal_expense, observe
from splitexpense.store import ExpenseStore


class WrongMemberBalances(ExpenseStore):
    @staticmethod
    def _balances(state):
        balances = ExpenseStore._balances(state)
        if state["journal"]:
            balances[0]["amount_minor"] += 1
            balances[1]["amount_minor"] -= 1
        return balances


class HTTPMutationTests(unittest.TestCase):
    def test_lean_rejects_real_server_bug_that_preserves_zero_total(self):
        with patch("splitexpense.laws.ExpenseStore", WrongMemberBalances):
            with LocalService() as service, Checker() as checker:
                group = service.group(3)
                members = [member["id"] for member in group["members"]]
                observation = observe(
                    service, group["id"], "expense",
                    equal_expense("mutation-probe", 1001, members), "actual-http-mutation",
                )
                self.assertEqual(observation["record"]["result"]["status"], 201)
                balances = observation["record"]["after"]["balances"]
                self.assertEqual(sum(item["amount_minor"] for item in balances), 0)
                result = checker.check(observation)
                self.assertFalse(result["passed"])
                self.assertIn("balance", " ".join(result["failures"]))


if __name__ == "__main__":
    unittest.main()
