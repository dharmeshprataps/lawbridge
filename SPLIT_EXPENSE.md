# Split expense REST demo

A small Python REST service with SQLite persistence and a Lean-backed build gate.
The service supports groups, equal or exact expense splits, balances, settlements,
and idempotent retries. It runs locally on `127.0.0.1:8791` by default.

All amounts are **integer minor units**. In an INR group, `1001` means 1,001 paise
(₹10.01). Positive member balances mean money is owed to that member; negative
balances mean that member owes money. Each group has one currency.

## Run and check

From this repository:

```sh
make build
make serve
```

The server persists data in `.lawbridge/splitexpense.sqlite3`. Stop it with Ctrl-C.
To choose another port or database:

```sh
PYTHONPATH=src:. python3 -m splitexpense --port 8792 --db /tmp/my-expenses.sqlite3
```

Run only the generated HTTP law checks:

```sh
PYTHONPATH=src:. python3 -m splitexpense.laws --report .lawbridge/splitexpense-laws.json
```

The law harness creates isolated in-memory databases and ephemeral loopback
servers. It does not change the database used by `make serve`.

## Try the endpoints

Create a group:

```sh
curl -sS http://127.0.0.1:8791/groups \
  -H 'Content-Type: application/json' \
  -d '{"name":"Weekend trip","currency":"INR","members":[{"id":"alice","name":"Alice"},{"id":"bob","name":"Bob"},{"id":"chris","name":"Chris"}]}'
```

Copy the returned `id` and replace `GROUP_ID` below. Record Alice paying ₹10.01,
shared equally by the three members:

```sh
curl -sS http://127.0.0.1:8791/groups/GROUP_ID/expenses \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"dinner-1","description":"Dinner","payer_id":"alice","amount_minor":1001,"split":{"type":"equal","member_ids":["alice","bob","chris"]}}'
```

The shares are 334, 334, and 333 paise. Equal splitting allocates remainder units
to participants in the supplied order. The balances are Alice +667, Bob −334,
Chris −333; they sum to zero.

```sh
curl -sS http://127.0.0.1:8791/groups/GROUP_ID/balances
```

Settle Bob's 334-paise debt to Alice:

```sh
curl -sS http://127.0.0.1:8791/groups/GROUP_ID/settlements \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"bob-pays-1","from_member":"bob","to_member":"alice","amount_minor":334}'
```

Exact splits use `"split":{"type":"exact","shares":[{"member_id":"alice","amount_minor":501},{"member_id":"bob","amount_minor":500}]}`.
Shares must sum to the expense amount; zero shares are allowed. A payer need not
be one of the participants, but every referenced member must belong to the group.

Identical retries return the original object with HTTP 200 and no new effect.
A new expense or settlement returns 201. Reusing a key with a different body
returns 409. Keys are scoped by group and operation kind. Structural validation
precedes lookup; semantic validation follows it so an old settlement can still
be replayed after its debt has been paid.

Other reads: `GET /health`, `GET /groups`, `GET /groups/GROUP_ID`,
`GET /groups/GROUP_ID/expenses`, and `GET /groups/GROUP_ID/settlements`.
The full group snapshot includes members, balances, event records, the signed
journal, and retry records so the law harness can inspect actual state.

## Laws enforced by the build

The Lean model and runtime checker describe these relationships:

- Each valid expense's exact shares sum to its amount; equal splitting obeys the
  specified quotient/remainder rule.
- An expense appends a credit for its payer and a debit for each participant's
  share. A settlement appends opposite postings for its two members.
- Journal postings sum to zero. Every member's reported balance equals that
  member's journal total, not just an aggregate returned by the service.
- A successful operation appends exactly its event, retry record, and postings;
  group identity, members, name, and currency remain unchanged.
- A settlement cannot exceed either the sender's debt or the receiver's credit.
- Invalid requests and conflicts leave the complete group snapshot unchanged.
- Exact retries return the original response and leave the snapshot unchanged.

The HTTP tests additionally check parser rejection, group isolation, persistence
across database reopen, and concurrent same-key requests committing one effect.
An implementation-mutation test runs a deliberately buggy server that shifts one
minor unit between two members while keeping the overall total zero. Lean rejects
that real HTTP observation because the individual balances disagree with the journal.

The generated suite samples groups of 2–8 members and nested equal/exact split
payloads, along with multi-operation histories. It checks actual HTTP observations
against Lean's `splitexpense.v1` model. Its separate corruption probes alter
per-member balances while preserving their total, and alter rounding in a response;
both must be rejected by the checker. Probes are reported separately from passing
execution checks.

## What is proved and what is tested

Lean proves conservation properties of the signed-journal model, including
balanced expenses, settlements, appending balanced events, and arbitrary finite
histories. The build audits theorems for proof gaps and unexpected axioms.

The Python implementation is checked through observed HTTP executions. This is
not a universal proof of the Python server, SQLite, or the snapshot mechanism.
The evidence file identifies its sampled scope, seed, counts, source hashes,
checker fingerprint, and any failed observations.

This is a local demo without authentication. Group bodies are stored as SQLite
JSON documents to keep transactions and model observations easy to inspect;
the API and storage schema are not intended as a production deployment design.
Limits include 50 members per group, 1 MiB request bodies, positive amounts up to
10^12 minor units, exact integer JSON numbers, and bounded strings. Floats,
duplicate JSON keys, nonfinite values, unknown fields, and invalid money types
are rejected.
