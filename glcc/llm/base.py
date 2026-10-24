"""Provider-neutral LLM interface with the reliability features an agent system needs.

The LLM is OPTIONAL and UNTRUSTED in this system. Every call goes through here, every response is
schema-validated, and every agent that uses a model has a deterministic fallback, so a provider
outage, a malformed answer, or a hallucination degrades quality, never correctness or safety.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol


class LLMError(Exception):
    """The provider failed, timed out, or returned something unusable."""


class LLMUnavailable(LLMError):
    """The circuit breaker is open: recent calls failed, so calls are short-circuited until the cooldown ends."""


class SchemaError(LLMError):
    """The response did not match the expected schema."""


class LLMClient(Protocol):
    def complete_json(self, *, system: str, user: str, schema: dict, max_tokens: int = 1500) -> dict: ...


_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


def validate(value: Any, schema: dict, path: str = "$") -> None:
    """Validate a JSON value against a small JSON-Schema subset: type, required, properties, items, enum, maxLength, minItems."""
    t = schema.get("type")
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError(f"{path}: expected number")
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError(f"{path}: expected integer")
    elif t in _TYPES and not isinstance(value, _TYPES[t]):
        raise SchemaError(f"{path}: expected {t}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} not in {schema['enum']}")
    if t == "string" and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaError(f"{path}: longer than {schema['maxLength']}")
    if t == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaError(f"{path}: fewer than {schema['minItems']} items")
        for i, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{path}[{i}]")
    if t == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}: missing required key {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                validate(value[key], sub, f"{path}.{key}")


class ResilientLLM:
    """Wrap a provider with retries, exponential backoff, schema validation, and a circuit breaker."""

    def __init__(self, inner: LLMClient, *, max_retries: int = 2, backoff_s: float = 0.5, failure_threshold: int = 3,
                 cooldown_s: float = 30.0, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.inner, self.max_retries, self.backoff_s = inner, max_retries, backoff_s
        self.failure_threshold, self.cooldown_s = failure_threshold, cooldown_s
        self._sleep, self._clock = sleep, clock
        self._consecutive_failures = 0
        self._open_until = 0.0
        self.stats = {"calls": 0, "retries": 0, "failures": 0, "short_circuited": 0}

    @property
    def breaker_open(self) -> bool:
        return self._clock() < self._open_until

    def complete_json(self, *, system: str, user: str, schema: dict, max_tokens: int = 1500) -> dict:
        if self.breaker_open:
            self.stats["short_circuited"] += 1
            raise LLMUnavailable("LLM circuit breaker is open after repeated failures")
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.stats["retries"] += 1
                self._sleep(self.backoff_s * (2 ** (attempt - 1)))
            self.stats["calls"] += 1
            try:
                out = self.inner.complete_json(system=system, user=user, schema=schema, max_tokens=max_tokens)
                validate(out, schema)
                self._consecutive_failures = 0
                return out
            except LLMError as exc:
                last = exc
            except Exception as exc:                       # provider SDKs raise many types; none may escape unhandled
                last = LLMError(f"{type(exc).__name__}: {exc}")
        self.stats["failures"] += 1
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._open_until = self._clock() + self.cooldown_s
        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempt(s): {last}")
