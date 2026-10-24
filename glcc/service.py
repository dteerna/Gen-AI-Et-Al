"""Application service: the one place that wires corpus, orchestrator, store, and role directory together.

The API and the CLI are thin shells over this class, so they cannot behave differently.
"""

from __future__ import annotations

from .agents.orchestrator import Orchestrator
from .corpus import Corpus
from .governance import authority
from .governance.store import Store, StoreError
from .retrieval.retrievers import Retrievers

__all__ = ["ExceptionService", "StoreError"]


def perform_action(content: dict) -> dict:
    """The irreversible side effect. STUB: wire this to your payment, claims, or customs systems.

    The proposal id is the idempotency key, so a retried call must not pay twice.
    """
    pid = authority.proposal_id_for(authority.content_hash(content))
    verbs = {"approve_settlement": "settlement released", "partial_settlement": "settlement released",
             "deny_claim": "claim denial recorded", "clear_customs_hold": "customs hold release requested"}
    return {"confirmation_id": f"CONF-{pid[5:]}", "idempotency_key": pid, "action": verbs.get(content["decision_type"], "recorded"),
            "amount": content["amount"], "currency": content["currency"],
            "note": "Stub action. Wire perform_action to real systems using the idempotency key."}


class ExceptionService:
    def __init__(self, corpus: Corpus, store: Store, llm=None, directory: dict[str, list[str]] | None = None):
        self.corpus, self.store, self.llm = corpus, store, llm
        self.directory = directory or {}
        self.orchestrator = Orchestrator(corpus, store, llm)

    @property
    def retrievers(self) -> Retrievers:
        return self.orchestrator.retrievers

    # ---- analysis ------------------------------------------------------------------------------------------------------
    def analyze(self, case_id: str, actor: str = "agent:glcc") -> dict:
        return self.orchestrator.run(case_id, actor)

    @staticmethod
    def summarize_run(r: dict) -> dict:
        a = r.get("analysis") or {}
        p = r.get("proposal")
        return {"run_id": r["run_id"], "case_id": r["case_id"], "status": r["status"], "mode": r["mode"], "error": r["error"],
                "decision": r["decision"], "amount": r["amount"],
                "proposal_id": p["proposal_id"] if p else None,
                "required_approvals": p["content"]["required_approvals"] if p else [],
                "flags": a.get("flags", []), "blockers": [b["kind"] for b in a.get("blockers", [])],
                "memo_narrative": (r.get("memo_meta") or {}).get("narrative"),
                "agents": [{"agent": t["agent"], "status": t["status"], "ms": t.get("ms")} for t in r.get("trace", [])]}

    # ---- proposals and approvals -------------------------------------------------------------------------------------------
    def proposal_view(self, proposal_id: str) -> dict | None:
        p = self.store.get_proposal(proposal_id)
        if p is None:
            return None
        approvals = self.store.approvals_for(proposal_id)
        ok, reasons = authority.check_execution(p, approvals, p["status"] == "executed", self.directory)
        run = self.store.get_run(p["run_id"]) if p["run_id"] else None
        return {**p, "approvals": approvals, "ready_to_execute": ok and p["status"] != "executed",
                "blocked_reasons": [] if p["status"] == "executed" else reasons, "memo_available": bool(run and run.get("memo"))}

    def memo(self, proposal_id: str) -> str | None:
        p = self.store.get_proposal(proposal_id)
        run = self.store.get_run(p["run_id"]) if p and p["run_id"] else None
        return run["memo"] if run else None

    def approve(self, proposal_id: str, role: str, approver_id: str) -> dict:
        return self.store.add_approval(proposal_id, role, approver_id, self.directory)

    def revoke(self, approval_id: int, revoked_by: str) -> dict:
        return self.store.revoke_approval(approval_id, revoked_by)

    def execute(self, proposal_id: str, actor: str) -> dict:
        return self.store.execute(proposal_id, actor, self.directory, perform_action)

    # ---- audit and search ---------------------------------------------------------------------------------------------------
    def verify_audit(self) -> dict:
        return self.store.verify_audit()

    def search(self, kind: str, query: str, vendor: str = "any", port: str = "any", as_of: str = "2025-01-01", k: int = 5) -> list[dict]:
        if kind == "clause":
            return [{**h.item.to_dict(), **h.provenance(h.item.clause_id)} for h in self.retrievers.clauses.search(query, vendor=vendor, k=k)]
        if kind == "regulation":
            res = self.retrievers.regulations.search(query, port=port, as_of=as_of, k=k)
            return [{**h.item.to_dict(), **h.provenance(h.item.section_id)} for h in res.hits] + [{"excluded": e} for e in res.excluded]
        if kind == "ruling":
            return [{**h.item.to_dict(), **h.provenance(h.item.ruling_id)} for h in self.retrievers.rulings.search(query, k=k)]
        raise ValueError("kind must be clause, regulation, or ruling")
