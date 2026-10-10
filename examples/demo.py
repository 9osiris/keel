"""Run directly from a checkout; never uses a model, network or real tool."""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from keel import Journal, Policy, Request, replay


def main():
    config = {
        "version": 1, "allowed_tools": ["search.*"],
        "max_spend_micros": 10, "max_call_micros": 10, "max_calls": 5,
    }
    with tempfile.TemporaryDirectory() as directory:
        with Journal(Path(directory) / "demo.db") as journal:
            journal.create_run("demo", Policy(config))
            for request in (Request("first", "search.web", {"query": "a"}, 7),
                            Request("later", "search.local", {"query": "b"}, 5)):
                print(request.id, journal.submit("demo", request).to_dict())
            config["denied_tools"] = ["search.web"]
            result = replay(journal, "demo", Policy(config))
            assert result["changed"] == ["first", "later"]
            assert result["spent_micros"] == 5
            assert journal.verify("demo")["spent_micros"] == 7
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
