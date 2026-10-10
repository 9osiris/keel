"""A local SQLite reservation ledger. No tools are executed by this module."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from .model import (Conflict, Decision, IntegrityError, KeelError,
                    Request, Usage, canonical, digest, text)
from .policy import Policy

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    policy TEXT NOT NULL,
    policy_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (run_id, seq),
    UNIQUE (run_id, request_id)
);
"""


def _chain(previous: str, payload: str) -> str:
    return hashlib.sha256((previous + "\n" + payload).encode("utf-8")).hexdigest()


class Journal:
    def __init__(self, path="keel.db"):
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def _transaction(self, write=False):
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create_run(self, run_id: str, policy: Policy):
        text(run_id, "run id")
        config = canonical(policy.to_dict())
        with self._transaction(write=True):
            old = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if old:
                if old["policy"] != config or old["policy_hash"] != policy.fingerprint:
                    raise Conflict("run already exists with a different policy")
                return
            self.db.execute("INSERT INTO runs VALUES (?, ?, ?)",
                            (run_id, config, policy.fingerprint))

    def _verified(self, run_id):
        run = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeelError(f"unknown run: {run_id}")
        try:
            policy = Policy.from_dict(json.loads(run["policy"]))
            if (canonical(policy.to_dict()) != run["policy"] or
                    policy.fingerprint != run["policy_hash"]):
                raise IntegrityError("run policy fingerprint mismatch")
            head = digest({"run": run_id, "policy": policy.to_dict()})
            usage = Usage()
            events = []
            seen = set()
            rows = self.db.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))
            for expected_seq, row in enumerate(rows, 1):
                p = json.loads(row["payload"])
                if set(p) != {"run", "seq", "at", "request", "decision"}:
                    raise IntegrityError("invalid event envelope")
                if (row["seq"] != expected_seq or p["seq"] != expected_seq or
                        type(p["seq"]) is not int or p["run"] != run_id):
                    raise IntegrityError("non-contiguous or mismatched sequence")
                text(p["at"], "event timestamp")
                if canonical(p) != row["payload"]:
                    raise IntegrityError("non-canonical event payload")
                if row["prev_hash"] != head or row["hash"] != _chain(head, row["payload"]):
                    raise IntegrityError("event hash chain mismatch")
                request = Request.from_dict(p["request"])
                if (canonical(request.to_dict()) != canonical(p["request"]) or
                        request.id != row["request_id"] or request.id in seen):
                    raise IntegrityError("request identity mismatch")
                decision = policy.evaluate(request, usage)
                if canonical(decision.to_dict()) != canonical(p["decision"]):
                    raise IntegrityError("stored decision violates pinned policy")
                usage = decision.usage
                head = row["hash"]
                seen.add(request.id)
                events.append(p)
            return policy, events, usage, head
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise IntegrityError(f"invalid journal data: {exc}") from exc

    def submit(self, run_id: str, request: Request) -> Decision:
        request = Request.from_dict(request.to_dict())
        with self._transaction(write=True):
            policy, events, usage, head = self._verified(run_id)
            for event in events:
                if event["request"]["id"] == request.id:
                    if canonical(event["request"]) != canonical(request.to_dict()):
                        raise Conflict("request id reused with different content")
                    return Decision.from_dict(event["decision"])
            decision = policy.evaluate(request, usage)
            payload = canonical({
                "run": run_id, "seq": len(events) + 1,
                "at": datetime.now(timezone.utc).isoformat(),
                "request": request.to_dict(), "decision": decision.to_dict(),
            })
            self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                            (run_id, len(events) + 1, request.id, payload,
                             head, _chain(head, payload)))
            return decision

    def events(self, run_id: str) -> list[dict]:
        with self._transaction():
            return self._verified(run_id)[1]

    def verify(self, run_id: str) -> dict:
        with self._transaction():
            policy, events, usage, head = self._verified(run_id)
            return {"run": run_id, "events": len(events), "head": head,
                    "spent_micros": usage.spent_micros, "calls": usage.calls,
                    "policy_hash": policy.fingerprint}
