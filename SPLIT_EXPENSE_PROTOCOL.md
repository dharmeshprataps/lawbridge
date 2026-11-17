# Split expense service and law protocol

Local, single-currency-per-group REST demo; Python stdlib HTTP + SQLite. Money is
integer minor units (e.g. paise), no floats or bool-as-int. Positive member net
balance means owed money; negative means owes money. No authentication is claimed.

## HTTP API

- GET /health -> 200 {"status":"ok"}
- POST /groups -> 201 snapshot. Body: {"name":"Trip","currency":"INR",
  "members":[{"id":"alice","name":"Alice"},{"id":"bob","name":"Bob"}]}.
  Require nonempty name, currency exactly three uppercase ASCII letters,
  2..50 members, unique public nonempty member IDs/names. Unknown fields reject.
- GET /groups -> 200 {"groups":[snapshot,...]}
- GET /groups/{id} -> 200 full snapshot below.
- GET /groups/{id}/balances -> 200 {"group_id":ID,"currency":"INR",
  "balances":[{"member_id":"alice","amount_minor":0},...],"total_minor":0}
- POST /groups/{id}/expenses -> 201 expense; exact replay -> 200 same expense.
  Body: {"idempotency_key":"meal-1","description":"Dinner","payer_id":"alice",
  "amount_minor":1001,"split":{"type":"equal","member_ids":["alice","bob"]}}.
  Alternative split: {"type":"exact","shares":[{"member_id":"alice",
  "amount_minor":501},{"member_id":"bob","amount_minor":500}]}.
  Amount positive <= 10^12; at least one unique participant, all in group;
  exact shares nonnegative <=10^12 and sum exactly amount. Equal split uses
  quotient/remainder, giving the first remainder participants one extra unit.
  Expense record: {"id":ID,"idempotency_key":KEY,"description":STRING,
  "payer_id":ID,"amount_minor":INT,"shares":[SHARE,...]}.
- GET /groups/{id}/expenses -> 200 {"expenses":[EXPENSE,...]}.
- POST /groups/{id}/settlements -> 201 settlement; exact replay -> 200 same.
  Body: {"idempotency_key":"settle-1","from_member":"bob",
  "to_member":"alice","amount_minor":500}.
  Members distinct and exist; amount positive <=10^12; sender currently owes,
  receiver is owed; amount <= min(-sender balance, receiver balance).
  Settlement record: body fields plus "id".
- GET /groups/{id}/settlements -> 200 {"settlements":[SETTLEMENT,...]}.

Malformed/invalid -> 400 {"error":{"code":STRING,"message":STRING}};
missing group/route ->404; reused key with different body ->409. Every rejected
mutation leaves the entire group snapshot unchanged. Maximum body 1MiB.
Idempotency keys required nonempty strings <=128 chars. Valid string fields
bounded (names/description/member IDs <=200). Exact JSON object shapes only.
Raw JSON floats, duplicate keys, NaN/Infinity rejected. Idempotency is scoped by
group + operation kind + key, compared to canonical complete request JSON.
Duplicate detection occurs before current-balance admissibility (retries after
settlement must still replay). A SQLite transaction makes effects and request
record atomic; concurrent same-key calls commit one effect.

## Snapshot shape

```
{"id":ID,"name":STRING,"currency":"INR","members":[{"id":ID,"name":STRING}],
 "balances":[{"member_id":ID,"amount_minor":INT}],
 "expenses":[EXPENSE,...],"settlements":[SETTLEMENT,...],
 "journal":[{"member_id":ID,"delta_minor":INT},...],
 "requests":[{"kind":"expense"|"settlement","key":KEY,"body":REQUEST_BODY,
               "response":EXPENSE_OR_SETTLEMENT},...]}
```

Preserve creation order for members, events, journal, requests. Balances are
computed from the journal and ordered as members. Expense journal suffix is
{payer,+amount} followed by each {participant,-share} in share order. Settlement
suffix is {from,+amount},{to,-amount}. Group identity/name/currency/members are
unchanged by mutations. Successful events append exactly one event and request
record and exactly their journal suffix. Replays change nothing. Conflicts and
invalid requests change nothing. No hidden aggregate metrics are trusted by laws.

## Lean law observations

`Checker.check` accepts an additional model `splitexpense.v1`; direct requests:

```
{"id":"case-1","model":"splitexpense.v1",
 "record":{"input":{"operation":"expense"|"settlement","body":REQUEST_BODY},
 "before":SNAPSHOT,"after":SNAPSHOT,
 "result":{"status":HTTP_STATUS,"body":ACTUAL_RESPONSE_JSON}}}
```

The split model dispatches before generic DSL checks; no requires/ensures fields
are needed. The response follows normal id/passed/eligible/failures protocol.
Lean derives valid/invalid/replay/conflict from actual request and before state;
it must check transitions, no-mutation rejection, and exact replay behavior.
Eligibility means a new valid operation or matching replay. Invalid JSON shapes
can be returned as failed checks/errors; harness must not count errors as passes.
Malformed raw HTTP parser cases are separately covered by HTTP tests.

Formal model uses a list of signed journal postings (member ID and Int delta).
Prove expense postings total zero when shares sum to amount, settlements total
zero, appending a balanced event preserves total, and arbitrary finite histories
preserve a zero total. Actual snapshot checking uses these same model definitions
and additionally checks per-member balances agree with the entire journal.
The runtime verifier validates actual response objects/event counts/unchanged
groups, not just total zero. No universal proof of the Python implementation.

## Python interface and ownership

Package src/splitexpense: store.py + server.py + __main__.py (service agent).
`ExpenseStore(path)` with create_group(body), list_groups(), snapshot(group_id),
balances(group_id), list_expenses(group_id), list_settlements(group_id),
add_expense(group_id, body), add_settlement(group_id, body), close(). Mutations
return (status, JSON body). Read methods return JSON body. Store may raise
`APIError(status, code, message)` for invalid requests. Store constructor default
path is ':memory:'. `create_server(store, host='127.0.0.1', port=0)` returns a
ThreadingHTTPServer ready for serve_forever; handler uses provided store.
`python -m splitexpense --host 127.0.0.1 --port 8791 --db PATH` serves locally.

src/splitexpense/laws.py + tests/test_splitexpense*.py + SPLIT_EXPENSE.md
(test/law agent). Law harness starts a real ephemeral localhost HTTP server;
checks nested payloads and operation sequences in Lean; writes JSON evidence and
counterexamples; CLI `python -m splitexpense.laws --report PATH` exits nonzero on
any checker error/violation/zero cases. Include a deliberate bad-transition probe
which Lean rejects without letting an expected rejection count as a passing law.
Report count those probes separately. Existing package generation needn't change.

lean/Lawbridge/SplitExpenseModel.lean + SplitExpenseChecker.lean and checker
dispatch/imports (Lean agent). Parent owns Makefile/audit script and root docs.
