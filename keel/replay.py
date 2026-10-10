"""Counterfactual policy replay of fixed proposals, never tool re-execution."""
from .model import Request, Usage
from .policy import Policy
from .store import Journal


def replay(journal: Journal, run_id: str, policy: Policy) -> dict:
    usage = Usage()
    trace = []
    changes = []
    # One verified snapshot: concurrent appends cannot alter this replay.
    for event in journal.events(run_id):
        request = Request.from_dict(event["request"])
        after = policy.evaluate(request, usage)
        usage = after.usage
        before = event["decision"]
        if before["allowed"] != after.allowed:
            changes.append(request.id)
        trace.append({"seq": event["seq"], "request_id": request.id,
                      "before": before, "after": after.to_dict()})
    return {"run": run_id, "policy_hash": policy.fingerprint,
            "changed": changes, "spent_micros": usage.spent_micros,
            "calls": usage.calls, "trace": trace}
