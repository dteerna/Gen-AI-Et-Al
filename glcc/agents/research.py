"""Dynamic RAG: plan the retrieval, run it in parallel, check it was sufficient, and repair gaps.

  PlannerAgent        decides what to look for from the case type, the vendor's documents, and the extracted facts
  ClauseResearcher    topic-filtered hybrid search; if a REQUIRED topic comes back empty it reformulates the query
                      (synonym expansion, then the bare topic) and retries before declaring a coverage gap
  RegulationResearcher  returns only the regulation version in force on the incident date, and reports what it excluded
  PrecedentResearcher   similar past rulings, with a vendor boost and a lexical-overlap floor

After retrieval, `expand_by_supersession` follows document relationships: any clause that replaces, or is replaced
by, a retrieved clause is pulled in even if the search did not rank it. A lexical search can miss an amendment
whose wording differs; the contract's own "replaces Section X" link cannot.
"""

from __future__ import annotations

from ..corpus import Corpus
from ..models import Clause
from ..reasoning.analysis import NEEDED_TOPICS, REG_TOPIC
from ..retrieval.index import expand_query
from .base import Agent, RunContext

TOPICS = {
    "freight_damage": ["liability_cap", "notification_deadline", "insurance_coverage"],
    "vessel_delay": ["force_majeure", "notification_deadline"],
    "customs_hold": ["detention_demurrage", "customs_responsibility"],
}
TOPIC_QUERY = {
    "liability_cap": "limit of liability for loss or damage to cargo",
    "notification_deadline": "carrier must notify GLCC within hours of discovery",
    "insurance_coverage": "cargo insurance deductible coverage insurer notified",
    "force_majeure": "force majeure written notice within hours port closure delay",
    "detention_demurrage": "detention and demurrage charges borne by carrier or GLCC",
    "customs_responsibility": "customs documentation responsibility of shipper and carrier",
}
PRECEDENT_BASE = {
    "freight_damage": "freight damage claim liability cap insurance",
    "vessel_delay": "vessel delay claim force majeure port closure",
    "customs_hold": "customs hold documentation detention charges",
}
CLAUSE_K = 10


def expand_by_supersession(corpus: Corpus, clauses: list[Clause]) -> tuple[list[Clause], list[str]]:
    """Add every clause linked to the set by a replaces/replaced-by relationship, repeatedly, until nothing new appears."""
    by_id = {c.clause_id: c for c in clauses}
    added: list[str] = []
    changed = True
    while changed:
        changed = False
        for c in list(by_id.values()):
            linked = [corpus.clause(s) for s in c.supersedes] + [o for o in corpus.clauses if c.clause_id in o.supersedes]
            for other in linked:
                if other is not None and other.clause_id not in by_id:
                    by_id[other.clause_id] = other
                    added.append(other.clause_id)
                    changed = True
    return list(by_id.values()), added


class PlannerAgent(Agent):
    name, reads, writes, critical = "planner", ("case", "facts"), ("plan",), True

    def run(self, ctx: RunContext) -> dict:
        case, facts = ctx.case, ctx.facts
        topics = list(TOPICS[case.case_type])
        issues = []
        if facts.first("verbal_notice") or facts.first("written_notice"):
            issues.append("verbal notice versus written notice")
        if facts.first("insurer_notice"):
            issues.append("insurer notified late reduced coverage")
        if any(c.vendor == case.vendor and c.doc_type in ("amendment", "addendum") for c in ctx.corpus.clauses):
            issues.append("amendment supersedes earlier liability limit conflicting addenda")
        if facts.fault:
            issues.append("missing certificate documentation error")
        ctx.plan = {
            "clause_tasks": [{"topic": t, "query": TOPIC_QUERY[t]} for t in topics],
            "required_topics": NEEDED_TOPICS[case.case_type],
            "regulation_task": ({"topic": REG_TOPIC["customs_hold"], "query": "customs compliance hold required documents submission deadline release"}
                                if case.case_type == "customs_hold" else None),
            "precedent_task": {"query": " ".join([PRECEDENT_BASE[case.case_type], *issues]), "case_type": case.case_type},
            "issues_detected": issues,
        }
        return {"clause_topics": topics, "regulation": bool(ctx.plan["regulation_task"]), "issues": issues}


class ClauseResearcher(Agent):
    name, reads, writes, critical = "clause_researcher", ("case", "plan"), ("retrieved.clauses", "retrieved.gaps"), False

    def run(self, ctx: RunContext) -> dict:
        r, case, plan = ctx.retrievers.clauses, ctx.case, ctx.plan
        found: dict[str, Clause] = {}
        provenance: list[dict] = []
        by_topic: dict[str, set[str]] = {}

        def attempt(topic: str, query: str, iteration: int, reformulate: bool) -> int:
            hits = r.search(query, vendor=case.vendor, topic=topic, k=CLAUSE_K, reformulate=reformulate)
            provenance.append({"iteration": iteration, "topic": topic, "query": query, "reformulated": reformulate,
                               "hits": [h.provenance(h.item.clause_id) for h in hits]})
            for h in hits:
                found[h.item.clause_id] = h.item
                by_topic.setdefault(topic, set()).add(h.item.clause_id)
            return len(hits)

        for task in plan["clause_tasks"]:
            attempt(task["topic"], task["query"], 1, False)

        gaps: list[str] = []
        for topic in plan["required_topics"]:
            if by_topic.get(topic):
                continue
            # Repair loop: expanded query, then the bare topic name. Then give up and report a gap.
            if not (attempt(topic, expand_query(TOPIC_QUERY[topic]), 2, True) or attempt(topic, topic.replace("_", " "), 3, False)):
                gaps.append(f"No clause on {topic.replace('_', ' ')} could be retrieved for {case.vendor} after reformulating the query.")

        ctx.retrieved["clauses"] = list(found.values())
        ctx.retrieved["clause_provenance"] = provenance
        ctx.retrieved.setdefault("gaps", []).extend(gaps)
        return {"clauses": len(found), "iterations": max(p["iteration"] for p in provenance), "gaps": gaps,
                "reformulations": sum(1 for p in provenance if p["iteration"] > 1)}


class RegulationResearcher(Agent):
    name, reads, writes, critical = "regulation_researcher", ("case", "facts", "plan"), ("retrieved.regulations",), False

    def run(self, ctx: RunContext) -> dict:
        task = ctx.plan["regulation_task"]
        if task is None:
            ctx.retrieved["regulations"], ctx.retrieved["regulations_excluded"] = [], []
            return {"skipped": "not a customs hold"}
        hold = ctx.facts.first("hold_notice")
        as_of = hold.ts[:10] if hold else ctx.case.shipment_date
        res = ctx.retrievers.regulations.search(task["query"], port=ctx.case.port, as_of=as_of, k=6, topic=task["topic"])
        sections = []
        if res.hits:
            top = res.hits[0].item                       # keep the whole in-force version of the regulation that matched best
            sections = [s for s in ctx.corpus.reg_sections if s.reg_id == top.reg_id and s.version == top.version and s.in_force(as_of)]
        ctx.retrieved.update(regulations=sections, regulations_excluded=res.excluded, reg_as_of=as_of,
                             regulation_provenance=[h.provenance(h.item.section_id) for h in res.hits])
        return {"as_of": as_of, "sections": [s.section_id for s in sections], "excluded": [e["id"] for e in res.excluded]}


class PrecedentResearcher(Agent):
    name, reads, writes, critical = "precedent_researcher", ("case", "plan"), ("retrieved.rulings",), False

    def run(self, ctx: RunContext) -> dict:
        task = ctx.plan["precedent_task"]
        hits = ctx.retrievers.rulings.search(task["query"], case_type=task["case_type"], vendor=ctx.case.vendor, k=3)
        ctx.retrieved["rulings"] = [h.item for h in hits]
        ctx.retrieved["ruling_provenance"] = [{**h.provenance(h.item.ruling_id), "notes": h.notes} for h in hits]
        return {"rulings": [h.item.ruling_id for h in hits]}
