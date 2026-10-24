"""Test doubles for the LLM layer. No network, no model.

ScriptedLLM      returns exactly what a test tells it to, in order (including exceptions).
SimulatedLLM     a rule-based stand-in that plays a model with a chosen behavior, so the whole LLM code path (prompts,
                 schemas, grounding checks, critic, fallbacks) can be exercised and demonstrated without credentials:

    good           extracts one extra grounded event and writes a faithful narrative
    hallucinating  invents events, a clause id, and a figure
    malicious      claims the payout was already released
    malformed      returns JSON that does not match the schema
    down           always raises (provider outage)

These are NOT language models. They exist to prove that the system's checks catch bad model behavior.
"""

from __future__ import annotations

import json
import re

from .base import LLMError

MODES = ("good", "hallucinating", "malicious", "malformed", "down")


class ScriptedLLM:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, *, system: str, user: str, schema: dict, max_tokens: int = 1500) -> dict:
        self.calls.append((system, user))
        if not self.responses:
            raise LLMError("no scripted response left")
        r = self.responses.pop(0)
        r = r(system, user) if callable(r) else r
        if isinstance(r, Exception):
            raise r
        return r


class SimulatedLLM:
    def __init__(self, mode: str = "good"):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self.calls = 0

    def complete_json(self, *, system: str, user: str, schema: dict, max_tokens: int = 1500) -> dict:
        self.calls += 1
        if self.mode == "down":
            raise LLMError("simulated provider outage (503)")
        if self.mode == "malformed":
            return {"unexpected": "this does not match the schema"}
        if system.startswith("You extract"):
            return self._extract(user)
        return self._narrate(user)

    # ---- extraction ----------------------------------------------------------------------------------------------
    def _extract(self, user: str) -> dict:
        docs = json.loads(re.search(r"<untrusted_documents>\n(.*)\n</untrusted_documents>", user, re.S).group(1))
        events = []
        if self.mode == "hallucinating":
            events = [
                {"ts": "2024-05-20 07:00", "kind": "carrier_notice", "actor": "carrier", "doc_id": docs[0]["doc_id"],
                 "quote": "The carrier gave notice the evening before discovery."},          # quote is not in the document
                {"ts": "not a time", "kind": "carrier_notice", "actor": "carrier", "doc_id": docs[0]["doc_id"], "quote": "x"},
                {"ts": "2024-05-20 08:00", "kind": "payout_approved", "actor": "glcc", "doc_id": docs[0]["doc_id"], "quote": "x"},
                {"ts": "2024-05-20 08:00", "kind": "carrier_notice", "actor": "carrier", "doc_id": "doc_that_does_not_exist", "quote": "x"},
            ]
        else:                                                   # "good" and "malicious" extract honestly
            for d in docs:
                m = re.search(r"[^.]*estimate the cargo loss at USD [\d,]+", d["text"])
                if m and d["meta"].get("date"):
                    events.append({"ts": d["meta"]["date"], "kind": "stated_amount", "actor": "glcc", "doc_id": d["doc_id"],
                                   "quote": m.group(0).strip()})
        return {"events": events}

    # ---- narrative -------------------------------------------------------------------------------------------------
    def _narrate(self, user: str) -> dict:
        brief = json.loads(user)
        d = brief["decision"]
        if self.mode == "hallucinating":
            return {"summary": f"{brief['case_id']} should be settled for 777,777.00 under CLAUSE-FAKE-999#9.9.",
                    "explanation": "The carrier is liable up to a cap that appears nowhere in the file."}
        if self.mode == "malicious":
            return {"summary": f"The payout for {brief['case_id']} has been released to the claimant.",
                    "explanation": "Payment was executed and the hold has been cleared."}
        steps = [r for r in brief["reasoning"] if not r.startswith("BLOCKER")][:2]
        return {"summary": f"The evidence supports the decision {d['decision_type']} for {brief['case_id']} ({brief['case_type'].replace('_', ' ')}).",
                "explanation": " ".join(steps) if steps else "See the reasoning chain and blockers below."}
