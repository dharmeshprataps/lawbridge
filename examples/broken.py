"""Deliberate mutations used by tests; excluded from passing contract discovery."""

from examples.account import Account


def off_by_one_add(*, a: int, b: int) -> int:
    return a + b + 1


def boolean_add(*, a: int, b: int) -> int:
    return True  # type: ignore[return-value]


def floats_add(*, a: int, b: int) -> float:
    return float(a + b)


def slow_add(*, a: int, b: int) -> int:
    import time
    time.sleep(5)
    return a + b


class ChangesOwner(Account):
    def withdraw(self, *, amount: int) -> dict:
        result = super().withdraw(amount=amount)
        self._state["owner"] = "someone-else"
        return result


class MutatesOnReject(Account):
    def withdraw(self, *, amount: int) -> dict:
        try:
            return super().withdraw(amount=amount)
        except ValueError:
            self._state["balance"] += 1
            raise


class WrongException(Account):
    def withdraw(self, *, amount: int) -> dict:
        try:
            return super().withdraw(amount=amount)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc


class WrongBalance(Account):
    def withdraw(self, *, amount: int) -> dict:
        result = super().withdraw(amount=amount)
        self._state["balance"] += 1
        result["balance"] = self._state["balance"]
        return result
