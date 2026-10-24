"""Shared run context and the agent contract.

Coordination pattern: a BLACKBOARD. Agents do not call each other. Each reads named fields from the shared
RunContext and writes its own, and declares which (`reads`, `writes`). The orchestrator owns ordering,
parallelism, timeouts, retries, and failure policy; wiring tests verify that every agent's inputs are
produced by an earlier stage. Every agent's outcome lands in the trace and, for key events, the audit log.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..corpus import Corpus
from ..models import CaseFacts, CaseRecord


@dataclass
class RunContext:
    corpus: Corpus
    case_id: str
    llm: Any = None                                   # a ResilientLLM, or None for deterministic offline mode
    retrievers: Any = None                            # shared, read-only retrievers (built once per orchestrator)
    audit: Callable[..., Any] = lambda *a, **k: None  # audit(event, payload) -> record
    run_id: str = field(default_factory=lambda: "RUN-" + uuid.uuid4().hex[:10])

    # blackboard fields, written by agents
    case: CaseRecord | None = None
    facts: CaseFacts | None = None
    plan: dict | None = None
    retrieved: dict = field(default_factory=dict)        # clauses, regulations, rulings, provenance, gaps
    topic_rulings: dict | None = None
    analysis: dict | None = None
    memo: str | None = None
    memo_meta: dict = field(default_factory=dict)
    proposal: dict | None = None

    trace: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return "llm" if self.llm is not None else "offline"


class AgentError(Exception):
    """An agent could not do its job."""


class Agent:
    name = "agent"
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    critical = True                                   # if a critical agent fails, the run ends in a safe escalation

    def run(self, ctx: RunContext) -> dict:           # returns a small JSON-able summary for the trace
        raise NotImplementedError


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    t0 = time.perf_counter()
    result = fn()
    return result, round((time.perf_counter() - t0) * 1000, 1)
