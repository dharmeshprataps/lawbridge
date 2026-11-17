# Lawbridge prototype protocol

Python remains the implementation. A Python-friendly JSON contract compiles a
restricted expression syntax into JSON ASTs. A persistent Lean executable reads
one request per stdin line and returns exactly one JSON response line. It checks
observed executions, not universal correctness of arbitrary Python code.

## Request

```
{"id": "case-id", "requires": [EXPR], "ensures": [EXPR],
 "model": "account.withdraw.v1",
 "reject_exception": "ValueError",
 "record": {"input": OBJECT, "before": VALUE, "after": VALUE,
            "result": VALUE, "ok": BOOL,
            "error": {"type": STRING, "message": STRING}}}
```

For successful calls error is null. Pure functions use null before/after.
Stateful adapters have a zero-argument factory, a named method accepting keyword
arguments, and a snapshot() method returning a JSON-compatible value. Capture
deep copies so later mutation does not alter the evidence.

Lean computes whether every requires expression is true. If eligible, ok must
be true and all ensures expressions must hold. If ineligible, ok must be false,
error.type must match reject_exception, and after must equal before. An absent
reject_exception makes an ineligible observed input an explicit contract error.
Empty ensures is rejected by the Python contract loader. Invalid/missing types
or paths and malformed AST nodes are errors, never implicit truth.

## Expression AST

- {"kind":"int", "value": INTEGER}
- {"kind":"bool", "value": BOOLEAN}
- {"kind":"string", "value": STRING}
- {"kind":"path", "value": [STRING, ...]}
- Unary {"kind":"not"|"neg", "arg": EXPR}
- Binary {"kind": "add"|"sub"|"mul"|"eq"|"ne"|"lt"|"le"|"gt"|"ge"|"and"|"or", "left":EXPR, "right":EXPR}

Integer arithmetic is exact. Booleans are distinct from integers. Strings only
support equality/inequality. Comparisons and Boolean operations are typed.
The Python frontend accepts integer/Boolean/string literals, dotted paths rooted
at input/before/after/result, parentheses, unary minus/not, + - *, comparisons,
and/or. Comparisons may be chained. Everything else is rejected.

## Response

```
{"id":"case-id", "passed":true, "eligible":true, "failures":[]}
```

Failures are an array of strings. Protocol/type/evaluation errors additionally
have an `error` string and passed=false. Python must fail on errors, malformed
responses, mismatched ids, process exit, timeout, missing Lean, or zero cases.
Lean must keep diagnostics off stdout except JSON responses.

The generic contract-loader model identifier supports account.withdraw.v1.
It additionally checks the exact AccountModel predicates used in the proved
preservation theorem. Snapshots have balance/spent/limit integer fields and
owner string; input.amount is an integer. Eligible calls require a valid before
state, a permitted withdrawal, and an observed allowed transition. Rejected
calls require valid before state and unchanged state. Unknown identifiers fail.
This links execution observations to the model, without asserting universal
conformance of Python to that model.

The checker also dispatches direct `splitexpense.v1` observations from the richer
HTTP harness, whose shape and nested collection laws are specified in
SPLIT_EXPENSE_PROTOCOL.md. That model uses native Lean definitions rather than
the generic scalar expression DSL and does not use requires/ensures request fields.

## Contract format

```
{"version":1,"name":"withdraw","target":"examples.account:Account",
 "mode":"stateful","method":"withdraw","model":"account.withdraw.v1",
 "inputs":{"amount":{"type":"int","min":-2,"max":120}},
 "requires":["input.amount >= 0", "input.amount <= before.balance"],
 "ensures":["after.balance == before.balance - input.amount"],
 "unchanged":["owner","limit"],"reject_exception":"ValueError",
 "testing":{"seed":1,"examples":60,"steps":8}}
```

Pure mode targets a keyword-callable function; method/unchanged are absent.
Generation supports finite integer domains initially. Bounds are sampling bounds,
not implicit restrictions of the logical contracts. Generate boundary values,
seeded cases and stateful sequences, replay failures, and report checked counts.
Each sequence starts with a fresh adapter. Oversized domains are sampled; finite
domains may be explicitly enumerated within a configured case cap. Never label
sampled cases as exhaustive or as implementation proofs.

CLI: `python -m lawbridge check PATH... [--report PATH] [--timeout SECONDS]`
discovers *.laws.json files in directories. `--replay PATH` replays a saved failure.
Exit 0 only if every required check completed successfully; 1 for law failures;
2 for setup/contract/protocol errors. Report records contract hashes, target file
hashes, exact inputs/traces, seed, tool versions and evidence kind.

Lean project: executable `lawcheck`, default build under lean/.lake/build/bin/.
`Lawbridge/AccountModel.lean` proves initial-state validity and preservation of
balance, daily spent/limit, and unchanged owner through an allowed withdrawal.
Audit exported theorems with #print axioms; reject sorryAx and custom axioms.
Pin a Lean toolchain, no mathlib dependency. A model proof is explicitly separate
from execution evidence about Python.
