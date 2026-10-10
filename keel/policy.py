"""Strict versioned policy parsing and deterministic, side-effect-free decisions."""
import json
from fnmatch import fnmatchcase
from .model import Decision, Request, Usage, canonical, digest, integer, text


def _strings(value, name):
    if type(value) is not list:
        raise ValueError(f"{name} must be an array")
    for item in value:
        text(item, name)
    if len(value) != len(set(value)):
        raise ValueError(f"{name} contains duplicates")
    return sorted(value)


class Policy:
    """Canonical configuration; to_dict returns a copy, not mutable policy state."""
    __slots__ = ("_json",)

    def __init__(self, data: dict):
        required = {"version", "allowed_tools", "max_spend_micros",
                    "max_call_micros", "max_calls"}
        optional = {"denied_tools", "actors", "required_args"}
        if type(data) is not dict or set(data) - required - optional:
            raise ValueError("unknown policy fields or non-object policy")
        if not required <= set(data):
            raise ValueError("policy is missing required fields")
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("unsupported policy version")
        c = {"version": 1}
        for name in ("allowed_tools", "denied_tools", "actors"):
            c[name] = _strings(data.get(name, []), name)
        for name in ("max_spend_micros", "max_call_micros", "max_calls"):
            c[name] = integer(data[name], name)
        rules = data.get("required_args", {})
        if type(rules) is not dict:
            raise ValueError("required_args must be an object")
        c["required_args"] = {}
        for pattern, names in rules.items():
            text(pattern, "required_args pattern")
            c["required_args"][pattern] = _strings(names, "required_args keys")
        self._json = canonical(c)

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        return cls(data)

    def to_dict(self) -> dict:
        return json.loads(self._json)

    @property
    def fingerprint(self) -> str:
        return digest(self.to_dict())

    def evaluate(self, request: Request, usage: Usage = Usage()) -> Decision:
        # Re-validate and snapshot even if caller mutated Request.args.
        request = Request.from_dict(request.to_dict())
        c = self.to_dict()
        reasons = []
        if any(fnmatchcase(request.tool, p) for p in c["denied_tools"]):
            reasons.append("tool_denied")
        if not any(fnmatchcase(request.tool, p) for p in c["allowed_tools"]):
            reasons.append("tool_not_allowed")
        if c["actors"] and request.actor not in c["actors"]:
            reasons.append("actor_not_allowed")
        missing = set()
        for pattern, names in c["required_args"].items():
            if fnmatchcase(request.tool, pattern):
                missing.update(set(names) - request.args.keys())
        reasons.extend(f"missing_argument:{key}" for key in sorted(missing))
        if request.cost_micros > c["max_call_micros"]:
            reasons.append("call_cost_exceeded")
        if usage.spent_micros + request.cost_micros > c["max_spend_micros"]:
            reasons.append("run_budget_exceeded")
        if usage.calls >= c["max_calls"]:
            reasons.append("call_quota_exceeded")
        allowed = not reasons
        return Decision(allowed, tuple(reasons),
                        usage.spent_micros + (request.cost_micros if allowed else 0),
                        usage.calls + int(allowed))
