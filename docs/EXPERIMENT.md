# Checking a Python expense service against Lean laws

Lawbridge explores a practical question: can ordinary Python software carry an
explicit set of behavioral laws that block a build when an implementation breaks
them? The experiment combines proofs about a small Lean model with checks of
actual Python executions. It keeps those two kinds of evidence separate.

The application is a [split-expense REST service](../SPLIT_EXPENSE.md) implemented
with Python's standard library and SQLite. It supports groups, equal or exact
expense splits, member balances, settlements, and idempotent retries. Money uses
integer minor units: in an INR group, `1001` represents ₹10.01.

## The contract spans the request and the state change

A useful law describes relationships, including what must remain unchanged.
Consider a three-member expense request:

```json
{
  "idempotency_key": "dinner-1",
  "description": "Dinner",
  "payer_id": "alice",
  "amount_minor": 1001,
  "split": {
    "type": "equal",
    "member_ids": ["alice", "bob", "chris"]
  }
}
```

The specified shares are 334, 334, and 333 minor units. Remainder units go to
participants in the supplied order. Alice's net balance becomes +667, Bob's −334,
and Chris's −333. Positive balances mean a member is owed money.

The request's preconditions require a positive integer amount and a nonempty,
unique selection of group members. Exact splits additionally require
nonnegative shares whose sum equals the expense amount. Settlements require a
debtor, a creditor, and an amount within both outstanding net balances.

The postconditions specify the exact returned event, journal postings, resulting
balances, and retry record. Frame rules preserve the group's identity, name,
currency, members, and existing events. Rejection requires the complete observed
group state to remain unchanged. An identical retry returns the original event
without another effect; a changed request under the same scoped key conflicts.

These relationships live in the
[Lean observation checker](../lean/Lawbridge/SplitExpenseChecker.lean). The
[wire protocol](../SPLIT_EXPENSE_PROTOCOL.md) describes the request, response,
snapshot, validation order, and idempotency scope.

## How the build connects Lean to Python

The [HTTP harness](../src/splitexpense/laws.py) starts a real loopback server with
an isolated database. For each generated operation, it captures a group snapshot,
sends the HTTP request, captures the response and resulting snapshot, and sends
that observation to the persistent Lean checker.

Lean derives the expected outcome from the input and the prior state. It checks
new operations, invalid requests, exact retries, and conflicts. It also checks
that every reported member balance agrees with that member's journal postings.
It does not rely on the service returning an aggregate total of zero.

The checker uses the same posting constructors and balance functions defined in
the [formal journal model](../lean/Lawbridge/SplitExpenseModel.lean). This connects
observed transitions to the model whose algebraic properties are proved. The
Python implementation is not translated into Lean.

The implementation under test is in the [store](../src/splitexpense/store.py) and
[HTTP server](../src/splitexpense/server.py). The store commits an event, its
postings, and its retry record together in a SQLite transaction.

## What Lean proves

The split-expense model has eight theorems. Representative claims include:

| Theorem | Formal claim |
| --- | --- |
| `expense_balanced` | Expense postings total zero when the shares sum to the expense amount. |
| `settlement_balanced` | A settlement's opposite postings total zero. |
| `member_balance_append` | A member's balance after appending an event equals its prior balance plus that event's contribution. |
| `balanced_event_preserves_total` | Appending an event with zero total preserves the journal total. |
| `history_preserves_zero` | Any finite modeled history of balanced events preserves an initially zero journal total. |

The remaining theorems establish debit totals, addition of journal totals, and
total preservation across histories. The build
[rechecks and audits the theorem dependencies](../scripts/audit_proofs.py),
rejecting proof gaps and axioms outside the configured standard Lean whitelist.

These are universal statements about the formal journal model. They do not
prove that every possible Python request conforms to it. Equal-split rounding,
HTTP parsing, retry handling, SQLite behavior, and the observation mechanism
remain implementation concerns exercised by tests and execution checks.

## Results and counterexamples

The baseline suite produces the following distinct evidence:

These counts were rechecked on 18 November 2026; a compact, machine-readable
record is available in [results.json](results.json).

| Evidence | Result | Scope |
| --- | --- | --- |
| Generated HTTP observations | 216 passed: 108 eligible operations and 108 rejection/conflict cases | Sampled sequences across groups of 2–8 members, including nested equal/exact splits, settlements, and retries. |
| Deliberate observation-corruption probes | 2 rejected as expected | Incorrect individual balances with an unchanged aggregate total; incorrect rounding in a response. Counted separately from passing HTTP observations. |
| Split-expense model proofs | 8 rechecked | Formal signed-journal model. |
| Project model proofs | 14 rechecked | The split-expense model plus the separate account-withdrawal model. |
| Existing function/account execution checks | 329 passed | Explicitly bounded exhaustive checks and sampled stateful executions, as identified in their reports. |
| Project tests | 42 passed | Protocol failures, HTTP behavior, persistence, concurrency, rejection, replay, and mutation detection. |

The [implementation-mutation test](../tests/test_splitexpense_mutation.py) is a
particularly useful check. It runs an actual HTTP server with a deliberately
incorrect balance calculation: one member gains an extra minor unit while
another loses one. The total still equals zero, and the endpoint returns HTTP
201. Lean rejects the resulting observation because the individual balances
disagree with the journal. A conservation law alone would miss this bug.

The [HTTP tests](../tests/test_splitexpense_http.py) also exercise concurrent
same-key requests committing one effect, persistence across database reopen,
group isolation, malformed inputs, and retry identity after state changes.
Those are tested properties, not formal proofs of SQLite or concurrent execution.

## Reproduce the experiment

From a checkout with Python 3.12 or newer, `make`, `curl`, and a `tar` installation
that can extract Zstandard archives:

```sh
make bootstrap
make build
```

Bootstrap downloads the pinned official Lean toolchain into the project and
verifies its archive digest. No Python packages are needed for the prototype.
The [Makefile](../Makefile) builds Lean, audits the model proofs, checks the
function contracts and HTTP observations, and runs the tests. A failed or
incomplete required check makes the build fail.

To run the HTTP law suite independently after building Lean:

```sh
PYTHONPATH=src:. python3 -m splitexpense.laws \
  --seed 2026 --groups 12 --report .lawbridge/splitexpense-laws.json
```

Generated evidence is written under `.lawbridge/` and is excluded from Git.
The HTTP report records scope, seed, counts, source hashes, checker fingerprint,
and failed observations. `.lawbridge/model-proofs.json` records the checked
theorems and their dependencies. Reports are evidence for the recorded run, not
a claim of universal implementation correctness.

The service is a local demonstration without authentication. It stores group
snapshots as SQLite JSON documents to keep transactions and state observations
inspectable. The generic contract DSL still generates bounded scalar integers;
the expense service uses a dedicated nested-payload generator and native Lean
model. Broader generators, more reusable models, and a faithful Python-to-Lean
implementation bridge are separate extensions.
