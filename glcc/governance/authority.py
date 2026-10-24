"""Who must approve what, and the single gate every irreversible action passes through.

The agents only PROPOSE. Execution requires human approvals that are:
  * bound to the exact proposal content (SHA-256), so editing a proposal voids earlier approvals;
  * given for EVERY role the authority matrix requires, by DIFFERENT people (dual control);
  * given by someone who currently holds that role in the role directory (checked again at execution);
  * not revoked, and not older than the approval time-to-live;
  * never from the system itself, and never from the person/agent that created the proposal.

All functions are pure (state is passed in), so the gate is easy to test and to reason about.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

ALLOWED_DECISIONS = {"approve_settlement", "partial_settlement", "deny_claim", "clear_customs_hold",
                     "request_information", "escalate_to_legal"}
EXECUTABLE_DECISIONS = {"approve_settlement", "partial_settlement", "deny_claim", "clear_customs_hold"}
SETTLEMENT_DECISIONS = {"approve_settlement", "partial_settlement"}

COORDINATOR_LIMIT = 10_000       # USD: up to this, a logistics coordinator may approve
UNDERWRITER_LIMIT = 100_000      # USD: up to this, an underwriter; above it, dual control
APPROVAL_TTL_HOURS = 72
SYSTEM_IDENTITIES = {"agent", "system", "ai", "glcc-agent", ""}


def required_approvals(decision_type: str, amount: float, flags: list[str] | tuple = ()) -> list[str]:
    """Roles that must all approve. Risk flags can only ADD reviewers, never remove them."""
    if decision_type in SETTLEMENT_DECISIONS:
        if amount <= COORDINATOR_LIMIT:
            roles = ["logistics_coordinator"]
        elif amount <= UNDERWRITER_LIMIT:
            roles = ["underwriter"]
        else:
            roles = ["senior_underwriter", "legal_counsel"]
    elif decision_type == "deny_claim":
        roles = ["underwriter"]
    elif decision_type == "clear_customs_hold":
        roles = ["compliance_officer"]
    elif decision_type == "request_information":       # not executable: names who must follow up
        roles = ["logistics_coordinator"]
    else:                                              # escalate_to_legal and anything unknown
        roles = ["legal_counsel"]
    if any(f in flags for f in ("injection_suspected", "material_discrepancy")) and "legal_counsel" not in roles:
        roles.append("legal_counsel")
    return roles


def content_hash(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def proposal_id_for(digest: str) -> str:
    return f"PROP-{digest[:10]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def validate_approval_request(role: str, approver_id: str, created_by: str, directory: dict[str, list[str]]) -> str | None:
    """Reason the approval may NOT be recorded, or None if it may."""
    who = (approver_id or "").strip()
    if who.lower() in SYSTEM_IDENTITIES:
        return "The system cannot approve its own proposals."
    if who == created_by:
        return "The creator of a proposal cannot approve it."
    if role not in directory.get(who, []):
        return f"{who} does not hold the role {role!r}."
    return None


def check_execution(proposal: dict, approvals: list[dict], already_executed: bool, directory: dict[str, list[str]],
                    now: datetime | None = None) -> tuple[bool, list[str]]:
    """The execution gate. Returns (allowed, reasons_blocked)."""
    now = now or utcnow()
    reasons: list[str] = []
    content = proposal.get("content", {})

    if content_hash(content) != proposal.get("content_hash"):
        return False, ["Proposal content does not match its recorded hash; it may have been altered."]
    if content.get("decision_type") not in EXECUTABLE_DECISIONS:
        return False, [f"Decision type {content.get('decision_type')!r} is not executable; it needs human handling."]
    if already_executed:
        return False, [f"Proposal {proposal['proposal_id']} was already executed."]

    required = content.get("required_approvals", [])
    created_by = proposal.get("created_by", "")
    chosen: dict[str, str] = {}
    ttl = timedelta(hours=APPROVAL_TTL_HOURS)

    for role in required:
        problems: list[str] = []
        usable = None
        for a in approvals:
            if a.get("proposal_id") != proposal["proposal_id"] or a.get("role") != role:
                continue
            if a.get("revoked_at"):
                problems.append(f"{role} approval by {a.get('approver_id')} was revoked")
            elif a.get("content_hash") != proposal["content_hash"]:
                problems.append(f"{role} approval by {a.get('approver_id')} was for different content")
            elif now - _parse(a["created_at"]) > ttl:
                problems.append(f"{role} approval by {a.get('approver_id')} expired after {APPROVAL_TTL_HOURS}h")
            else:
                bad = validate_approval_request(role, a.get("approver_id", ""), created_by, directory)
                if bad:
                    problems.append(bad.rstrip(".") + " (re-checked at execution)")
                else:
                    usable = a
                    break
        if usable is None:
            reasons.append(f"Missing valid approval from {role}" + (f" ({'; '.join(problems)})" if problems else "") + ".")
        else:
            chosen[role] = usable["approver_id"]

    if not reasons and len(set(chosen.values())) != len(chosen):
        reasons.append("Each required role must be approved by a different person (dual control).")
    return (not reasons), reasons
