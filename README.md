# Lawbridge

A build gate for Python functions and endpoint-style adapters, with laws checked
by Lean. The application stays Python. Contracts describe relationships between
inputs, outputs, and state before and after a call.

This prototype implements two distinct kinds of evidence:

- **Execution checks:** real Python calls are checked against their contracts.
  A report identifies sampled cases or exhaustive checking within explicit bounds.
- **Model proofs:** Lean proves initialization and invariant preservation for the
  account-withdrawal model and conservation of money in the split-expense journal
  model. The checker requires observations to satisfy the registered model's
  transition rules.

A proof about the model is not a universal proof about the Python implementation.
The wrapper establishes conformance for the executions it checks.

## The experiment

This project was built with AI assistance to explore how explicit behavioral laws
can guide acceptance of generated software. Its split-expense example runs real
HTTP requests against Python and SQLite, then checks the observed inputs,
responses, journal, and state transitions in Lean.

The most revealing test deliberately introduces a balance-calculation bug: one
member gains an extra minor unit and another loses one. The overall total still
equals zero, but Lean rejects the HTTP observation because the individual balances
do not match the journal.

The verified baseline on 18 November 2026 is:

| Evidence | Result | Scope |
| --- | --- | --- |
| Split-expense HTTP observations | 216 passed | 108 eligible operations and 108 rejection or conflict cases across sampled histories. |
| Split-expense model proofs | 8 checked | Universal claims about the signed-journal model under their stated assumptions. |
| All project model proofs | 14 checked | Split-expense and account-withdrawal models; no proof gaps or unapproved axioms. |
| Existing function and account checks | 329 passed | Bounded exhaustive checks and sampled executions, identified separately in reports. |
| Project tests | 42 passed | Includes real HTTP behavior, persistence, concurrent retries, protocol failures, and the buggy-server mutation. |

Two additional corrupted-observation probes are rejected as expected and are
counted separately from the 216 passing HTTP checks. See the
[experiment write-up](docs/EXPERIMENT.md), [recorded results](docs/results.json),
and [service walkthrough](SPLIT_EXPENSE.md) for the architecture, laws,
counterexamples, and reproduction commands.

## Quick start

Use Python 3.12 or newer. No Python packages are required for the prototype.
The bootstrap installs a pinned Lean toolchain inside this project.

```sh
make bootstrap
make build
make check
make test
```

`make check` runs the two pure-function contracts, the stateful account example,
and the split-expense service's HTTP law harness.
`make test` checks successful runs, deliberately broken implementations, rejection
behavior, replay, exact integers, and build failures caused by checker errors.

Run individual contracts from the project directory:

```sh
PYTHONPATH=src:. python3 -m lawbridge check examples/contracts/add.laws.json
PYTHONPATH=src:. python3 -m lawbridge check examples/contracts --report run.json
PYTHONPATH=src:. python3 -m lawbridge check --replay run.json
```

The replay command is useful for a report containing a failure. A replay creates
fresh adapter state and reruns the recorded input sequence. Changed contract or
target-file hashes invalidate the saved replay.

Exit codes are `0` for completed passing checks, `1` for law violations, and `2`
for setup, contract, protocol, or execution errors. Missing Lean, timeouts, empty
case sets, and malformed checker responses fail the gate.

## Split-expense REST service

The included application supports groups, equal/exact expense splits, balances,
and settlements. It stores a journal in SQLite and uses integer minor currency
units, such as INR paise. Start the local service with:

```sh
make serve
```

It listens at `http://127.0.0.1:8791` and stores data in
`.lawbridge/splitexpense.sqlite3`. Change the port with `make serve PORT=8792`.
See [the service walkthrough](SPLIT_EXPENSE.md) for requests and laws.

The dedicated HTTP harness generates nested multi-member requests and checks
actual before/after HTTP snapshots with the `splitexpense.v1` Lean model. This
does not expand the generic scalar-input DSL below; it is a richer, service-specific
model and generator built on the same checker protocol.

## Attach a Python function

An ordinary function accepts keyword arguments:

```python
def add(*, a: int, b: int) -> int:
    return a + b
```

Put its contract in a file ending in `.laws.json`:

```json
{
  "version": 1,
  "name": "exact addition",
  "target": "my_module:add",
  "mode": "pure",
  "inputs": {
    "a": {"type": "int", "min": -4, "max": 4},
    "b": {"type": "int", "min": -4, "max": 4}
  },
  "requires": [],
  "ensures": ["result == input.a + input.b"],
  "testing": {"mode": "exhaustive", "max_cases": 100}
}
```

The target module must be importable in the Python environment running Lawbridge.
The default Lean executable location is relative to this source checkout. When
using an installed package or checking another project, set
`LAWBRIDGE_LEAN_CHECKER` to the absolute path of the built `lawcheck` executable.
For exhaustive pure-function checking, the runner enumerates the bounded Cartesian
product. It fails when that exceeds `max_cases`, rather than silently sampling.
For larger domains, use `mode: "sample"`, a fixed `seed`, and `examples`.

The integer bounds control test generation. They do not silently add restrictions
to the meaning of a law. For instance, testing addition from -4 through 4 does not
establish its behavior for every integer.

## Relationships, rejection, and unchanged state

The account contract in
[`examples/contracts/account.laws.json`](examples/contracts/account.laws.json)
expresses these relationships:

```text
valid input:
  amount >= 0
  amount <= before.balance
  before.spent + amount <= before.limit

successful transition:
  after.balance = before.balance - amount
  after.spent = before.spent + amount
  owner and limit remain unchanged

invalid input:
  raises ValueError
  state remains unchanged
```

All generated inputs are executed, including inputs outside `requires`. Lean
evaluates the preconditions and selects the success or rejection obligations.
This exercises rejection behavior instead of filtering invalid cases away.

A stateful target is a zero-argument factory. Its returned adapter provides a
named method and `snapshot()`:

```python
class Account:
    def snapshot(self) -> dict:
        return self.state

    def withdraw(self, *, amount: int) -> dict:
        ...
```

The runner copies the input and snapshots before the call, then captures state
after the call even when the method raises. Each generated sequence starts with
a fresh adapter. Only the state exposed by `snapshot()` is covered by these
observations; external effects require their own test adapters and evidence.

`unchanged` adds equality checks on selected snapshot fields. Rejected calls must
leave the entire exposed state unchanged and raise the specified exception type.
An unexpected exception on a valid input is a failure.

## Proved state model

The optional contract setting `"model": "account.withdraw.v1"` registers the
included Lean account model as an additional requirement. It checks:

- Initial and subsequent states have nonnegative balance and spent, with spent
  within the limit.
- The amount is admissible under the model exactly when the contract says it is.
- Successful observations satisfy the model's exact withdrawal relation.

Lean proves that initialization is valid and every allowed withdrawal preserves
the state invariant. Selecting this model makes the same definitions part of
execution checking: weakening a JSON postcondition does not disable the model's
transition check.

The proof is in `lean/Lawbridge/AccountModel.lean`. The build audits its theorem
dependencies and rejects unfinished proofs or additional unapproved axioms.
Adding another proved model requires implementing and registering its Lean
definitions; arbitrary JSON contracts do not automatically acquire model proofs.

## Supported contract language

Contracts accept a deliberately small expression vocabulary:

- Integer, Boolean, and string literals.
- Paths rooted at `input`, `result`, `before`, or `after`.
- Exact integer `+`, `-`, `*`, unary minus, and ordered comparisons.
- Equality, inequality, `and`, `or`, and `not`; comparisons may be chained.

Booleans and integers are distinct. Strings support equality and inequality.
Calls, indexing, arbitrary Python evaluation, division, floating-point arithmetic,
and collection quantifiers are not supported in this version. Missing fields,
wrong operand types, and unsupported expressions are explicit errors.

Input generators currently support bounded scalar integers only. Scalar paths
inside nested JSON input, result, or state objects are understood by the checker,
but nested-object and collection input generators and collection quantifiers
remain future work.

The Python frontend parses the expressions into a restricted AST. A persistent
Lean executable independently evaluates that AST against each observed record.
The protocol is documented in [`PROTOCOL.md`](PROTOCOL.md).

## Evidence and scope

The report includes case counts, eligibility counts, seeds, hashes, and failure
inputs/traces. It identifies exhaustive checking as **within the declared bounds**.
Stateful sequence exploration is sampled, even if each action has a small domain.

Functions execute real Python. Use local test data and adapters for operations
that can affect persistent files, databases, or external services. A worker timeout
stops a stuck call; it is not a security sandbox.

The initial prototype intentionally has no result cache, no automatic proof of
arbitrary Python code, and no guarantee about dependencies or effects omitted
from the model and snapshots. Source hashes bind replay to the recorded files;
they do not prove the correctness of source-to-model translation.

The next extension points are richer dependent generators, nested collection
laws, more proved state models, and a supported Python-to-Lean implementation
bridge for selected functions.
