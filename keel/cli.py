"""Machine-readable command line interface with stable exit codes."""
import argparse
import json
import sqlite3
import sys
from .model import KeelError, Request, canonical
from .policy import Policy
from .replay import replay
from .store import Journal


def _read(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="keel", description=__doc__)
    parser.add_argument("--db", default="keel.db", help="local SQLite journal")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="pin a policy to a new run")
    new.add_argument("run")
    new.add_argument("--policy", required=True)
    gate = sub.add_parser("gate", help="atomically reserve a proposal")
    gate.add_argument("run")
    gate.add_argument("--request", default="-", help="JSON file or - for stdin")
    for command in ("verify", "export"):
        sub.add_parser(command).add_argument("run")
    rep = sub.add_parser("replay", help="counterfactual evaluation, no execution")
    rep.add_argument("run")
    rep.add_argument("--policy", required=True)
    rep.add_argument("--fail-on-change", action="store_true")
    args = parser.parse_args(argv)
    try:
        with Journal(args.db) as journal:
            if args.command == "new":
                journal.create_run(args.run, Policy.from_dict(_read(args.policy)))
                result = journal.verify(args.run)
            elif args.command == "gate":
                decision = journal.submit(args.run, Request.from_dict(_read(args.request)))
                print(canonical(decision.to_dict()))
                return 0 if decision.allowed else 2
            elif args.command == "verify":
                result = journal.verify(args.run)
            elif args.command == "export":
                for event in journal.events(args.run):
                    print(canonical(event))
                return 0
            else:
                result = replay(journal, args.run, Policy.from_dict(_read(args.policy)))
                print(canonical(result))
                return 2 if args.fail_on_change and result["changed"] else 0
            print(canonical(result))
            return 0
    except (KeelError, ValueError, TypeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
