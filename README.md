# keel

**A transactional policy boundary for agent tool calls.**

Before an agent spends money or invokes a tool, make the decision explicit,
reserve its budget atomically, and leave evidence you can replay later.
Python 3.11+, SQLite, zero runtime dependencies. No model API required.

Keel is not another agent framework. It sits between an existing agent and its
tool dispatcher: `proposal -> policy -> durable decision -> your dispatcher`.

## Features

- Deny-first, case-sensitive tool globs, actor allowlists and required argument keys.
- Integer-microdollar per-call and per-run limits, plus accepted-call quotas.
- SQLite `BEGIN IMMEDIATE` transactions prevent concurrent budget overspending.
- Request IDs are idempotent within a run; conflicting reuse fails closed.
- SHA-256 chained decisions, with both hash and policy verification on reads.
- Offline counterfactual replay, including downstream budget effects.
- JSONL export, Python API, JSON CLI and an executable unittest suite.

## Quick start

```sh
git clone https://github.com/9osiris/keel.git
cd keel
python -m keel --db demo.db new demo --policy examples/policy.json
printf '%s\n' '{"id":"read-1","tool":"fs.read","args":{"path":"README.md"},"cost_micros":2500}' > request.json
python -m keel --db demo.db gate demo --request request.json
python -m keel --db demo.db verify demo
python -m keel --db demo.db export demo > decisions.jsonl
python -m keel --db demo.db replay demo --policy examples/policy.json
python -m unittest discover -s tests -v
```

Or run `python examples/demo.py` for an isolated temporary-database demo.
Optional installation: `python -m pip install .` exposes the `keel` command.

## Python API

```python
from keel import Journal, Policy, Request

policy = Policy.from_dict({
    "version": 1,
    "allowed_tools": ["fs.read", "search.*"],
    "denied_tools": ["search.private"],
    "max_spend_micros": 100_000,
    "max_call_micros": 20_000,
    "max_calls": 10,
    "actors": ["researcher"],
    "required_args": {"fs.read": ["path"]},
})
with Journal("agent.db") as journal:
    journal.create_run("research-42", policy)
    decision = journal.submit("research-42", Request(
        id="step-1", tool="fs.read", args={"path": "README.md"},
        cost_micros=2500, actor="researcher",
    ))
    if decision.allowed:
        # Dispatch here with your own validated tool implementation.
        # Forward the request ID to tools supporting idempotency.
        pass
    else:
        print(decision.reasons)
```

A run pins its policy. Recreating it with the same policy is safe; a different
policy raises `Conflict`. Identical retries return the original decision without
consuming more budget. Reusing an ID with different arguments, cost, actor or
tool raises `Conflict`. Rejections are recorded but consume no budget or quota.

## Policy contract

See [examples/policy.json](examples/policy.json). Limits are mandatory,
non-negative integers, not booleans. Unknown fields, non-finite numbers,
malformed JSON values and unsupported versions fail closed.
Empty `allowed_tools` denies all tools; empty `actors` allows all actors.
All matching required-argument rules apply. Required means a top-level key is
present, not type-checked or non-empty. Explicit tool denies always win.

One US dollar is `1_000_000` microdollars. Cost is a **reservation supplied by
trusted host code**, not model-reported actual billing. Accepted reservations
are never refunded, even if execution fails. There is no settlement API.

## Replay, not re-execution

```sh
python -m keel --db agent.db replay research-42 --policy stricter.json --fail-on-change
```

Replay processes recorded proposals in sequence with a fresh budget. It reports
old/new decisions, reasons, final reserved spend and decision flips. A newly
denied early proposal can make a later proposal affordable. Replay never calls
a model or tool and never changes the original journal. It cannot predict how
an agent would adapt its future proposals under the alternative policy.

CLI: `new RUN --policy FILE`, `gate RUN --request FILE` (`-` reads stdin),
`verify RUN`, `export RUN`, `replay RUN --policy FILE [--fail-on-change]`.
Place `--db PATH` before the command. JSON on stdout; errors on stderr.
Exit codes: `0` success, `1` invalid input/integrity/storage error, `2` denied gate
or changed replay decisions with `--fail-on-change`.

## Architecture

```text
keel/
  __init__.py       public API
  __main__.py       module entry point
  model.py          canonical JSON, requests, usage and decisions
  policy.py         strict parsing and pure policy evaluation
  store.py          transactions, idempotency and chained journal verification
  replay.py         counterfactual evaluation with downstream decision diff
  cli.py            argparse interface and exit-code contract
examples/
  policy.json       restrictive example policy
  demo.py           temporary-database walkthrough
tests/
  test_keel.py      validation, corruption, concurrency, replay and CLI tests
.github/workflows/ci.yml
pyproject.toml
```

The evaluator is pure. Each journal event binds its sequence, run, request,
decision, timestamp and predecessor hash. Verification checks contiguous
sequence numbers, the full hash chain and a fresh policy evaluation of every
event. Budget state is derived from verified events, not a second mutable
counter. This trades O(n) verification per submission for auditability. Rotate
runs for long-lived agents; this is not a high-throughput telemetry database.

## Security and integration boundaries

**A policy gate is not a sandbox or an exactly-once executor.** Keel does not
execute tools, constrain paths, inspect shell commands, authenticate actors,
verify price estimates or prevent dispatcher bypass. An allowed `fs.read` is
not a claim that its path is safe. Enforce those restrictions in host code,
OS isolation and tool implementations. Agents must not author their own
policies/costs or have direct write access to the journal.

A crash after reservation but before dispatch conservatively consumes budget.
A retry returns the same authorization; the host must prevent duplicate side
effects using downstream idempotency or its own execution state machine.
There is no durable execution status or distributed transaction with a tool.

The chain detects modification relative to an intact anchor, not a malicious
DB owner who can rewrite the whole run. Tail truncation requires an externally
retained head hash to detect. `verify` returns that head: checkpoint it elsewhere
if this matters. There are no signatures or encryption. Requests are stored
verbatim: do not submit secrets; protect databases and exports as sensitive.
SQLite locking assumes a supported local filesystem, not multi-host consensus.

## Why this exists

[Mule](https://github.com/9osiris/mule) executes tool loops,
[Council](https://github.com/9osiris/council) coordinates agents,
[Cognix](https://github.com/9osiris/cognix) makes cognitive state explicit, and
[Cassette](https://github.com/9osiris/cassette) replays API traffic.
Keel covers a different layer: **which actions were authorized, what budget
was reserved, and what would a policy change have done?** Wrap its API around
those systems without importing them. No native adapters are claimed.

## Development

```sh
python -m unittest discover -s tests -v
python -m compileall -q keel examples tests
```

CI runs tests on Python 3.11, 3.12 and 3.13, then smoke-tests the demo and CLI.
Runtime: Python standard library only. Packaging: setuptools. MIT licensed.
Changes should include a regression test and preserve fail-closed validation.
