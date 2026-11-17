"""Transactional SQLite-backed groups, expense splits, and settlements.

Each group is a small JSON document. One BEGIN IMMEDIATE transaction commits
the event, its balanced journal postings, and its idempotency record together.
This intentionally favors a transparent model over a production-scale schema.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

MAX_AMOUNT = 10**12
ASCII_WHITESPACE = " \t\n\r\v\f"


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    @property
    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def invalid(message: str):
    raise APIError(400, "invalid_request", message)


def json_safe(value, depth: int = 0):
    if depth > 64:
        invalid("JSON nesting exceeds 64 levels")
    if value is None or type(value) in (int, bool):
        return
    if type(value) is str:
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            invalid("Strings must contain valid Unicode scalar values")
        return
    if type(value) is list:
        for child in value:
            json_safe(child, depth + 1)
        return
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                invalid("Object keys must be strings")
            json_safe(key, depth + 1)
            json_safe(child, depth + 1)
        return
    invalid("Only JSON values with exact integer numbers are supported")


def object_shape(value, keys: set[str], label: str):
    if type(value) is not dict or set(value) != keys:
        invalid(f"{label} requires exactly these fields: {', '.join(sorted(keys))}")


def text(value, label: str, limit: int = 200) -> str:
    if type(value) is not str or not value.strip(ASCII_WHITESPACE) or len(value) > limit:
        invalid(f"{label} must be a nonempty string of at most {limit} characters")
    return value


def integer(value, label: str) -> int:
    if type(value) is not int:
        invalid(f"{label} must be an integer, not a Boolean or float")
    return value


def amount(value, label: str, *, zero: bool = False) -> int:
    integer(value, label)
    if not (0 if zero else 1) <= value <= MAX_AMOUNT:
        invalid(f"{label} must be between {0 if zero else 1} and {MAX_AMOUNT} minor units")
    return value


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ExpenseStore:
    def __init__(self, path=":memory:"):
        path = str(path)
        if path != ":memory:":
            path = str(Path(path).expanduser().resolve())
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self._connection.execute("PRAGMA busy_timeout = 10000")
        if path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("CREATE TABLE IF NOT EXISTS groups (id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self._connection.close()

    def _load(self, group_id: str) -> dict:
        row = self._connection.execute("SELECT state FROM groups WHERE id = ?", (group_id,)).fetchone()
        if row is None:
            raise APIError(404, "not_found", "Group does not exist")
        return json.loads(row[0])

    def _save(self, state: dict):
        self._connection.execute("UPDATE groups SET state = ? WHERE id = ?", (canonical(state), state["id"]))

    @staticmethod
    def _balances(state: dict) -> list[dict]:
        net = {member["id"]: 0 for member in state["members"]}
        for posting in state["journal"]:
            net[posting["member_id"]] += posting["delta_minor"]
        return [{"member_id": member["id"], "amount_minor": net[member["id"]]} for member in state["members"]]

    @staticmethod
    def _replay(state: dict, kind: str, body: dict):
        for request in state["requests"]:
            if request["kind"] == kind and request["key"] == body["idempotency_key"]:
                if canonical(request["body"]) != canonical(body):
                    raise APIError(409, "idempotency_conflict", "Idempotency key was already used with a different request")
                return request["response"]
        return None

    def create_group(self, body):
        json_safe(body)
        object_shape(body, {"name", "currency", "members"}, "Group")
        text(body["name"], "name")
        if type(body["currency"]) is not str or re.fullmatch(r"[A-Z]{3}", body["currency"]) is None:
            invalid("currency must be exactly three uppercase ASCII letters")
        members = body["members"]
        if type(members) is not list or not 2 <= len(members) <= 50:
            invalid("members must contain between 2 and 50 member objects")
        ids = []
        for member in members:
            object_shape(member, {"id", "name"}, "Member")
            ids.append(text(member["id"], "member.id"))
            text(member["name"], "member.name")
        if len(set(ids)) != len(ids):
            invalid("Member IDs must be unique")
        state = {
            "id": "g_" + uuid.uuid4().hex, "name": body["name"], "currency": body["currency"],
            "members": json.loads(canonical(members)), "balances": [], "expenses": [],
            "settlements": [], "journal": [], "requests": [],
        }
        state["balances"] = self._balances(state)
        with self._transaction():
            self._connection.execute("INSERT INTO groups(id,state) VALUES (?,?)", (state["id"], canonical(state)))
        return 201, state

    def snapshot(self, group_id):
        with self._lock:
            return self._load(group_id)

    def list_groups(self):
        with self._lock:
            rows = self._connection.execute("SELECT state FROM groups ORDER BY rowid").fetchall()
            return {"groups": [json.loads(row[0]) for row in rows]}

    def balances(self, group_id):
        state = self.snapshot(group_id)
        balances = self._balances(state)
        return {"group_id": state["id"], "currency": state["currency"], "balances": balances,
                "total_minor": sum(item["amount_minor"] for item in balances)}

    def list_expenses(self, group_id):
        return {"expenses": self.snapshot(group_id)["expenses"]}

    def list_settlements(self, group_id):
        return {"settlements": self.snapshot(group_id)["settlements"]}

    @staticmethod
    def _expense_shape(body):
        json_safe(body)
        object_shape(body, {"idempotency_key", "description", "payer_id", "amount_minor", "split"}, "Expense")
        text(body["idempotency_key"], "idempotency_key", 128)
        text(body["description"], "description")
        text(body["payer_id"], "payer_id")
        integer(body["amount_minor"], "amount_minor")
        split = body["split"]
        if type(split) is not dict:
            invalid("split must be an object")
        mode = split.get("type")
        if mode == "equal":
            object_shape(split, {"type", "member_ids"}, "Equal split")
            if type(split["member_ids"]) is not list:
                invalid("member_ids must be an array")
            for member_id in split["member_ids"]:
                text(member_id, "split member ID")
        elif mode == "exact":
            object_shape(split, {"type", "shares"}, "Exact split")
            if type(split["shares"]) is not list:
                invalid("shares must be an array")
            for share in split["shares"]:
                object_shape(share, {"member_id", "amount_minor"}, "Share")
                text(share["member_id"], "share.member_id")
                integer(share["amount_minor"], "share.amount_minor")
        else:
            invalid("split.type must be equal or exact")

    def add_expense(self, group_id, body):
        self._expense_shape(body)
        with self._transaction():
            state = self._load(group_id)
            replay = self._replay(state, "expense", body)
            if replay is not None:
                return 200, replay
            total = amount(body["amount_minor"], "amount_minor")
            members = {member["id"] for member in state["members"]}
            if body["payer_id"] not in members:
                invalid("payer_id must belong to the group")
            split = body["split"]
            if split["type"] == "equal":
                ids = split["member_ids"]
                if not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids) or any(x not in members for x in ids):
                    invalid("Split participants must be a nonempty unique selection of group members")
                quotient, remainder = divmod(total, len(ids))
                shares = [{"member_id": member_id, "amount_minor": quotient + (index < remainder)}
                          for index, member_id in enumerate(ids)]
            else:
                shares = split["shares"]
                ids = [share["member_id"] for share in shares]
                if not 1 <= len(shares) <= 50 or len(set(ids)) != len(ids) or any(x not in members for x in ids):
                    invalid("Shares must identify a nonempty unique selection of group members")
                for share in shares:
                    amount(share["amount_minor"], "share.amount_minor", zero=True)
                if sum(share["amount_minor"] for share in shares) != total:
                    invalid("Exact shares must sum to amount_minor")
                shares = json.loads(canonical(shares))
            expense = {"id": "e_" + uuid.uuid4().hex, "idempotency_key": body["idempotency_key"],
                       "description": body["description"], "payer_id": body["payer_id"],
                       "amount_minor": total, "shares": shares}
            state["expenses"].append(expense)
            state["journal"].append({"member_id": body["payer_id"], "delta_minor": total})
            state["journal"].extend({"member_id": share["member_id"], "delta_minor": -share["amount_minor"]} for share in shares)
            state["requests"].append({"kind": "expense", "key": body["idempotency_key"],
                                      "body": json.loads(canonical(body)), "response": expense})
            state["balances"] = self._balances(state)
            self._save(state)
            return 201, expense

    @staticmethod
    def _settlement_shape(body):
        json_safe(body)
        object_shape(body, {"idempotency_key", "from_member", "to_member", "amount_minor"}, "Settlement")
        text(body["idempotency_key"], "idempotency_key", 128)
        text(body["from_member"], "from_member")
        text(body["to_member"], "to_member")
        integer(body["amount_minor"], "amount_minor")

    def add_settlement(self, group_id, body):
        self._settlement_shape(body)
        with self._transaction():
            state = self._load(group_id)
            replay = self._replay(state, "settlement", body)
            if replay is not None:
                return 200, replay
            value = amount(body["amount_minor"], "amount_minor")
            balances = {item["member_id"]: item["amount_minor"] for item in self._balances(state)}
            sender, receiver = body["from_member"], body["to_member"]
            if sender == receiver or sender not in balances or receiver not in balances:
                invalid("Settlement members must be distinct members of the group")
            if balances[sender] >= 0 or balances[receiver] <= 0 or value > min(-balances[sender], balances[receiver]):
                invalid("Settlement must repay an existing debt without exceeding either member's net balance")
            settlement = {"id": "s_" + uuid.uuid4().hex, **body}
            state["settlements"].append(settlement)
            state["journal"].extend([{"member_id": sender, "delta_minor": value},
                                      {"member_id": receiver, "delta_minor": -value}])
            state["requests"].append({"kind": "settlement", "key": body["idempotency_key"],
                                      "body": json.loads(canonical(body)), "response": settlement})
            state["balances"] = self._balances(state)
            self._save(state)
            return 201, settlement
