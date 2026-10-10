"""Validated JSON value objects shared by storage and the pure evaluator."""
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any


class KeelError(Exception):
    """Expected application error, safe to surface through the CLI."""


class Conflict(KeelError):
    """An immutable identifier was reused with different content."""


class IntegrityError(KeelError):
    """The stored journal no longer verifies."""


def integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _json_value(value: Any) -> None:
    if value is None or type(value) in (bool, str, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("expected finite JSON values with string object keys")


def canonical(value: Any) -> str:
    _json_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Request:
    id: str
    tool: str
    args: dict
    cost_micros: int = 0
    actor: str = "agent"

    def __post_init__(self):
        text(self.id, "request id")
        text(self.tool, "tool")
        text(self.actor, "actor")
        integer(self.cost_micros, "cost_micros")
        if type(self.args) is not dict:
            raise ValueError("args must be an object")
        canonical(self.args)

    def to_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool,
                "args": json.loads(canonical(self.args)),
                "cost_micros": self.cost_micros, "actor": self.actor}

    @classmethod
    def from_dict(cls, data: dict) -> "Request":
        keys = {"id", "tool", "args", "cost_micros", "actor"}
        if type(data) is not dict or set(data) - keys:
            raise ValueError("invalid request fields")
        if not {"id", "tool", "args"} <= set(data):
            raise ValueError("request needs id, tool and args")
        return cls(**data)


@dataclass(frozen=True)
class Usage:
    spent_micros: int = 0
    calls: int = 0

    def __post_init__(self):
        integer(self.spent_micros, "spent_micros")
        integer(self.calls, "calls")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...]
    spent_micros: int
    calls: int

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": list(self.reasons),
                "spent_micros": self.spent_micros, "calls": self.calls}

    @classmethod
    def from_dict(cls, data: dict) -> "Decision":
        return cls(data["allowed"], tuple(data["reasons"]),
                   data["spent_micros"], data["calls"])

    @property
    def usage(self) -> Usage:
        return Usage(self.spent_micros, self.calls)
