"""Real SQLite, real subprocess CLI, no mocking of the transaction boundary."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from keel import Conflict, IntegrityError, Journal, KeelError, Policy, Request, Usage, replay
from keel.model import canonical


def policy(**changes):
    config = {"version": 1, "allowed_tools": ["*"],
              "max_spend_micros": 10, "max_call_micros": 10, "max_calls": 10}
    config.update(changes)
    return Policy.from_dict(config)


def request(id="a", tool="read", cost=3, **kwargs):
    return Request(id, tool, kwargs.pop("args", {}), cost, **kwargs)


class PolicyTests(unittest.TestCase):
    def test_deny_precedes_allow(self):
        p = policy(allowed_tools=["fs.*"], denied_tools=["fs.write"])
        self.assertIn("tool_denied", p.evaluate(request(tool="fs.write")).reasons)
        self.assertTrue(p.evaluate(request(tool="fs.read")).allowed)

    def test_empty_allowlist_and_case(self):
        self.assertFalse(policy(allowed_tools=[]).evaluate(request()).allowed)
        self.assertFalse(policy(allowed_tools=["read"]).evaluate(request(tool="Read")).allowed)

    def test_actor_allowlist(self):
        p = policy(actors=["planner"])
        self.assertFalse(p.evaluate(request()).allowed)
        self.assertTrue(p.evaluate(request(actor="planner")).allowed)

    def test_all_required_patterns_apply(self):
        p = policy(required_args={"*": ["trace"], "fs.*": ["path"]})
        d = p.evaluate(request(tool="fs.read"))
        self.assertEqual(d.reasons, ("missing_argument:path", "missing_argument:trace"))
        self.assertTrue(p.evaluate(request(tool="fs.read", args={"path": None, "trace": 1})).allowed)

    def test_exact_budget_boundary(self):
        self.assertTrue(policy().evaluate(request(cost=10)).allowed)
        d = policy().evaluate(request(cost=4), Usage(7, 1))
        self.assertIn("run_budget_exceeded", d.reasons)
        self.assertEqual(d.usage, Usage(7, 1))

    def test_per_call_limit(self):
        self.assertIn("call_cost_exceeded", policy(max_call_micros=2).evaluate(request()).reasons)

    def test_zero_cost_still_consumes_quota(self):
        p = policy(max_calls=1, max_spend_micros=0)
        first = p.evaluate(request(cost=0))
        self.assertTrue(first.allowed)
        self.assertFalse(p.evaluate(request(cost=0), first.usage).allowed)

    def test_invalid_limits(self):
        for value in (-1, True, 2.5, "10", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy(max_spend_micros=value)

    def test_unknown_fields_and_version(self):
        for changes in ({"typo": 1}, {"version": 2}, {"version": True}, {"actors": "agent"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                policy(**changes)
        with self.assertRaises(ValueError):
            Policy({"version": 1})

    def test_fingerprint_normalizes_sets_and_defaults(self):
        a = policy(allowed_tools=["b", "a"])
        b = policy(allowed_tools=["a", "b"], denied_tools=[], actors=[], required_args={})
        self.assertEqual(a.fingerprint, b.fingerprint)
        copied = a.to_dict()
        copied["allowed_tools"].clear()
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_bad_requests_fail_closed(self):
        for data in ({"id": "x", "tool": "read", "args": {}, "extra": 1},
                     {"id": "x", "tool": "read"},
                     {"id": "x", "tool": "read", "args": [], "cost_micros": 1},
                     {"id": "x", "tool": "read", "args": {}, "cost_micros": True},
                     {"id": "", "tool": "read", "args": {}},
                     {"id": "x", "tool": "read", "args": {"x": float("nan")}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                Request.from_dict(data)

    def test_non_json_python_values(self):
        for args in ({1: "key"}, {"x": (1, 2)}, {"x": object()}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                request(args=args)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ledger.db"
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)
        self.journal.create_run("run", policy())

    def test_empty_run_verifies(self):
        result = self.journal.verify("run")
        self.assertEqual(result["events"], 0)
        self.assertEqual(len(result["head"]), 64)

    def test_identical_retry_is_idempotent(self):
        a = self.journal.submit("run", request())
        self.assertEqual(a, self.journal.submit("run", request()))
        self.assertEqual(self.journal.verify("run")["calls"], 1)

    def test_conflicting_retry_rolls_back(self):
        self.journal.submit("run", request())
        with self.assertRaises(Conflict):
            self.journal.submit("run", request(cost=4))
        self.assertEqual(self.journal.verify("run")["events"], 1)
        self.assertTrue(self.journal.submit("run", request(id="b")).allowed)

    def test_denial_recorded_without_reservation(self):
        d = self.journal.submit("run", request(cost=11))
        self.assertFalse(d.allowed)
        result = self.journal.verify("run")
        self.assertEqual((result["events"], result["calls"], result["spent_micros"]), (1, 0, 0))

    def test_run_policy_is_pinned(self):
        self.journal.create_run("run", policy())
        with self.assertRaises(Conflict):
            self.journal.create_run("run", policy(max_calls=2))

    def test_unknown_run(self):
        with self.assertRaises(KeelError):
            self.journal.submit("missing", request())

    def test_isolated_runs(self):
        self.journal.create_run("other", policy())
        self.assertTrue(self.journal.submit("run", request(cost=10)).allowed)
        self.assertTrue(self.journal.submit("other", request(cost=10)).allowed)

    def test_reopen_and_export_snapshot(self):
        self.journal.submit("run", request())
        with Journal(self.path) as other:
            self.assertEqual(other.events("run"), self.journal.events("run"))
            events = other.events("run")
            events[0]["request"]["args"]["mutate"] = True
            self.assertEqual(other.events("run")[0]["request"]["args"], {})

    def test_tamper_is_detected_and_blocks_append(self):
        self.journal.submit("run", request())
        self.journal.db.execute("UPDATE events SET hash='broken'")
        with self.assertRaises(IntegrityError):
            self.journal.verify("run")
        with self.assertRaises(IntegrityError):
            self.journal.submit("run", request(id="b"))

    def test_gap_is_detected(self):
        self.journal.submit("run", request())
        self.journal.submit("run", request(id="b"))
        self.journal.db.execute("DELETE FROM events WHERE seq=1")
        with self.assertRaises(IntegrityError):
            self.journal.verify("run")

    def test_policy_tamper_is_detected(self):
        self.journal.db.execute("UPDATE runs SET policy_hash='broken'")
        with self.assertRaises(IntegrityError):
            self.journal.verify("run")

    def test_rehashed_invalid_decision_is_detected(self):
        self.journal.submit("run", request())
        row = self.journal.db.execute("SELECT * FROM events").fetchone()
        data = json.loads(row["payload"])
        data["decision"]["spent_micros"] = 0
        payload = canonical(data)
        new_hash = hashlib.sha256((row["prev_hash"] + "\n" + payload).encode()).hexdigest()
        self.journal.db.execute("UPDATE events SET payload=?, hash=?", (payload, new_hash))
        with self.assertRaises(IntegrityError):
            self.journal.verify("run")

    def test_concurrent_reservations_cannot_overspend(self):
        def reserve(index):
            with Journal(self.path) as journal:
                return journal.submit("run", request(id=str(index), cost=3)).allowed
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(16)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(self.journal.verify("run")["spent_micros"], 9)
        self.assertEqual(self.journal.verify("run")["events"], 16)

    def test_concurrent_identical_ids_reserve_once(self):
        def reserve(_):
            with Journal(self.path) as journal:
                return journal.submit("run", request()).allowed
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(reserve, range(16))))
        self.assertEqual(self.journal.verify("run")["events"], 1)

    def test_replay_cascades_and_preserves_original(self):
        self.journal.submit("run", request(id="early", tool="expensive", cost=7))
        self.journal.submit("run", request(id="late", tool="cheap", cost=5))
        before = self.journal.verify("run")
        result = replay(self.journal, "run", policy(denied_tools=["expensive"]))
        self.assertEqual(result["changed"], ["early", "late"])
        self.assertEqual(result["spent_micros"], 5)
        self.assertEqual(self.journal.verify("run"), before)

    def test_same_policy_replay_has_no_flips(self):
        self.journal.submit("run", request())
        self.assertEqual(replay(self.journal, "run", policy())["changed"], [])


class CLITests(unittest.TestCase):
    def test_cli_end_to_end_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "cli.db")
            config = Path(directory) / "policy.json"
            config.write_text(canonical(policy().to_dict()), encoding="utf-8")

            def run(*args, input=None):
                return subprocess.run([sys.executable, "-m", "keel", "--db", db, *args],
                                      input=input, text=True, capture_output=True)

            self.assertEqual(run("new", "r", "--policy", str(config)).returncode, 0)
            good = run("gate", "r", input=canonical(request().to_dict()))
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertTrue(json.loads(good.stdout)["allowed"])
            denied = run("gate", "r", input=canonical(request(id="b", cost=11).to_dict()))
            self.assertEqual(denied.returncode, 2)
            invalid = run("gate", "r", input="{}")
            self.assertEqual(invalid.returncode, 1)
            self.assertIn("error", json.loads(invalid.stderr))
            exported = run("export", "r")
            self.assertEqual(len(exported.stdout.splitlines()), 2)
            self.assertEqual(json.loads(run("verify", "r").stdout)["events"], 2)
            config.write_text(canonical(policy(allowed_tools=[]).to_dict()), encoding="utf-8")
            changed = run("replay", "r", "--policy", str(config), "--fail-on-change")
            self.assertEqual(changed.returncode, 2, changed.stderr)
            self.assertEqual(json.loads(changed.stdout)["changed"], ["a"])
            self.assertEqual(run("verify", "unknown").returncode, 1)


if __name__ == "__main__":
    unittest.main()
