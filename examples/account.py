"""A local endpoint-style adapter with state, rejection, and a daily limit.

No network or external account is involved. ``snapshot`` deliberately returns
the internal dictionary: the harness must copy evidence before execution.
"""


class Account:
    def __init__(self) -> None:
        self._state = {"balance": 100, "spent": 0, "limit": 80, "owner": "dharmesh"}

    def snapshot(self) -> dict:
        return self._state

    def withdraw(self, *, amount: int) -> dict:
        if amount < 0:
            raise ValueError("amount must be nonnegative")
        if amount > self._state["balance"]:
            raise ValueError("insufficient balance")
        if self._state["spent"] + amount > self._state["limit"]:
            raise ValueError("daily limit exceeded")
        self._state["balance"] -= amount
        self._state["spent"] += amount
        return {"withdrawn": amount, "balance": self._state["balance"]}
