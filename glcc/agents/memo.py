"""Deterministic rendering of the resolution memo from the structured analysis.

Everything that matters (decision, amount, governing clauses, calculation, citations, caveats) comes from
the analysis, not from a model. An LLM may add a short narrative (Summary / Explanation) that is verified by
the critic before it is shown; if it fails verification the memo is rendered without it.
"""

from __future__ import annotations

SENTENCE = "No action has been taken. This proposal requires human approval before anything is executed."


def money(x: float, currency: str = "USD") -> str:
    return f"{currency} {x:,.2f}"


def auto_summary(a: dict) -> str:
    d = a["decision"]
    kind = d["decision_type"]
    if kind in ("approve_settlement", "partial_settlement"):
        return f"The analysis supports a settlement of {money(d['amount'], d['currency'])} for {a['case_id']} ({a['case_type'].replace('_', ' ')}, {a['vendor']})."
    if kind == "deny_claim":
        return f"The analysis supports denying the claim for {a['case_id']}: the delay is excused."
    if kind == "clear_customs_hold":
        return f"The analysis supports clearing the customs hold for {a['case_id']} and allocating the detention charges as shown below."
    if kind == "request_information":
        return f"{a['case_id']} cannot be decided yet: required information is missing. The items below are needed."
    return f"{a['case_id']} must go to legal counsel: the evidence or the contracts do not allow an automatic recommendation."


def render_memo(a: dict, narrative: dict | None = None) -> str:
    d, ev = a["decision"], a["evidence"]
    cur = d["currency"]
    L: list[str] = [f"# Resolution proposal for {a['case_id']}", ""]
    L.append(f"**Decision:** `{d['decision_type']}`" + (f", amount {money(d['amount'], cur)}" if d["decision_type"] in ("approve_settlement", "partial_settlement") else ""))
    L.append(f"**Case:** {a['case_type'].replace('_', ' ')} | vendor {a['vendor']} | shipped {a['shipment_date']} | port {a['port']}")
    L.append(f"**Required review:** {', '.join(d['required_approvals'])}" + ("" if d["executable"] else " (this outcome is not executable; it routes the case to people)"))
    L += [f"**{SENTENCE}**", ""]

    L += ["## Summary", "", (narrative or {}).get("summary") or auto_summary(a), ""]
    if narrative and narrative.get("explanation"):
        L += ["## Explanation", "", narrative["explanation"], ""]

    if a["blockers"]:
        L += ["## Blockers", ""] + [f"- **{b['kind']}** -> {b['resolution']}: {b['detail']}" for b in a["blockers"]] + [""]

    L += ["## Reasoning chain", ""]
    for r in a["reasoning"]:
        refs = f" [{', '.join(r['evidence'])}]" if r["evidence"] else ""
        L.append(f"{r['step']}. {r['claim']}{refs}")
    L.append("")

    gov = [(t, g) for t, g in a["governing"].items() if g["governing_clause"]]
    if gov:
        L += ["## Governing clauses", ""]
        for t, g in gov:
            L.append(f"- **{t.replace('_', ' ')}**: {g['governing_clause']}: {g['governing_text']}")
        L.append("")
    overridden = [(t, o) for t, g in a["governing"].items() for o in g["overridden"]]
    if overridden:
        L += ["## Clauses that do not govern", ""] + [f"- {o['clause_id']} ({t.replace('_', ' ')}): {o['reason']}" for t, o in overridden] + [""]

    p = a.get("payout")
    if p:
        L += ["## Calculation", "", f"- Settlement basis: {money(p['supported_loss'], cur)} ({p['supported_loss_basis']})",
              f"- Carrier liability: {money(p['vendor_liability'], cur)} (cap {p['cap']['note']}; {p['liability_clause_id']})",
              f"- Insurance: {money(p['insurance_payable'], cur)} ({p['insurance_note']}; {p['insurance_clause_id']})",
              f"- **Total recovery: {money(p['total_recovery'], cur)}**; unrecovered, borne by GLCC: {money(p['unrecovered'], cur)}", ""]
    c = a.get("customs")
    if c and c.get("complete"):
        L += ["## Customs hold", "", f"- Submission completed in {c['business_days_used']} business day(s); allowed {c['allowed_business_days']}.",
              f"- Detention charges: {c['days_charged']} day(s) = {money(c['detention_charges'], cur)}, borne by {str(c['responsible_party']).upper()}.", ""]
    if ev["regulations"]:
        L += ["## Regulatory basis", ""] + [f"- {r}" for r in ev["regulations"]]
        L += [f"- Not applied: {e['id']} ({e['reason']})" for e in ev["regulations_excluded"]] + [""]
    if a["precedents"]:
        L += ["## Precedents", ""] + [f"- {p_['ruling_id']} ({p_['outcome']}): {p_['summary']} [{p_['relationship']}]" for p_ in a["precedents"]] + [""]

    L += ["## Evidence considered", "", f"- Clauses: {', '.join(ev['clauses']) or 'none'}",
          f"- Regulations: {', '.join(ev['regulations']) or 'none'}", f"- Rulings: {', '.join(ev['rulings']) or 'none'}", ""]
    return "\n".join(L).strip() + "\n"
