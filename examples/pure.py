"""The implementation remains ordinary Python; contracts live alongside it."""


def add(*, a: int, b: int) -> int:
    return a + b


def withdraw_balance(*, balance: int, amount: int) -> int:
    if balance < 0 or amount < 0 or amount > balance:
        raise ValueError("withdrawal must fit a nonnegative balance")
    return balance - amount
