"""Mutation check: break the code on purpose; every mutation must make the test suite fail. Run: python scripts/mutation_check.py"""
import subprocess, sys, shutil, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
MUTATIONS = [
 # (label, file, old, new)
 ("critic: ignore invented figures", "glcc/agents/critic.py", "    extra = numbers_in(text) - allowed_numbers(analysis)\n    if extra:", "    extra = set()\n    if extra:"),
 ("critic: accept ids outside the case evidence", "glcc/agents/critic.py", "        elif cid not in allowed_ids(analysis):", "        elif False:"),
 ("critic: stop scanning narratives for executed claims", "glcc/agents/critic.py", "    issues += [i for i in check_text(narrative_sections(memo), analysis, known) if \"already carried out\" in i]", "    pass"),
 ("critic: drop the mandatory-statement check", "glcc/agents/critic.py", "    if SENTENCE not in memo:", "    if False:"),
 ("orchestrator: a memo failing review still becomes a proposal", "glcc/agents/orchestrator.py", "        if not ctx.memo_meta.get(\"critic_passed\"):", "        if False:"),
 ("orchestrator: researcher failures are not recorded as gaps", "glcc/agents/orchestrator.py", "            if e[\"status\"] != \"ok\":\n                if a.name == \"clause_researcher\":", "            if False:\n                if a.name == \"clause_researcher\":"),
 ("orchestrator: researchers run one after another", "glcc/agents/orchestrator.py", "ThreadPoolExecutor(max_workers=len(RESEARCHERS)", "ThreadPoolExecutor(max_workers=1"),
 ("proposals: allow invented citations", "glcc/proposals.py", "        if unknown:\n            errors.append(f\"Unknown {label}", "        if False:\n            errors.append(f\"Unknown {label}"),
 ("proposals: ignore the in-force regulation check", "glcc/proposals.py", "            if sec and not sec.in_force(reg_as_of):", "            if False:"),
 ("proposals: executable decisions need no citation", "glcc/proposals.py", "    if dt in authority.EXECUTABLE_DECISIONS and not (ev[\"relied_clauses\"] or ev[\"regulations\"]):", "    if False:"),
 ("proposals: skip the amount-vs-calculation check", "glcc/proposals.py", "abs(analysis[\"payout\"][\"total_recovery\"] - amount) > 0.005", "False"),
 ("api: no API key enforcement", "glcc/api/server.py", "            if api_key and path != \"/health\":", "            if False:"),
 ("api: trust the request body even when not allowed", "glcc/api/server.py", "            if not who and allow_body_identity:", "            if not who:"),
 ("api: body identity beats the authenticated header", "glcc/api/server.py", "            who = (self.headers.get(IDENTITY_HEADER) or \"\").strip()\n            if not who", "            who = \"\"\n            if not who"),
 ("api: leak internal error details", "glcc/api/server.py", "                self._send(500, {\"error\": \"internal error\"})", "                self._send(500, {\"error\": traceback_text()})"),
 ("api: no body size limit", "glcc/api/server.py", "            if length > MAX_BODY:", "            if False:"),
 ("store: refusals are no longer audited", "glcc/governance/store.py", "                self._audit(conn, f\"user:{approver_id}\", \"approval_rejected\"", "                (lambda *a, **k: None)(conn, f\"user:{approver_id}\", \"approval_rejected\""),
 ("store: execution does not check replays", "glcc/governance/store.py", "            if prior:\n                self._audit(conn, actor, \"execution_replay_ignored\"", "            if False:\n                self._audit(conn, actor, \"execution_replay_ignored\""),
 ("research: no supersession expansion", "glcc/agents/analysis_agents.py", "candidates, added = expand_by_supersession(ctx.corpus, ctx.retrieved.get(\"clauses\", []))", "candidates, added = list(ctx.retrieved.get(\"clauses\", [])), []"),
 ("research: no query reformulation", "glcc/agents/research.py", "            if not (attempt(topic, expand_query(TOPIC_QUERY[topic]), 2, True) or attempt(topic, topic.replace(\"_\", \" \"), 3, False)):", "            if True:"),
 ("intake: accept ungrounded model quotes", "glcc/agents/intake.py", "        elif not (quote_is_grounded(originals[e[\"doc_id\"]], e[\"quote\"]) or quote_is_grounded(redacted[e[\"doc_id\"]], e[\"quote\"])):", "        elif False:"),
 ("intake: send documents to the model unredacted", "glcc/agents/intake.py", "    redacted = {d.doc_id: _redacted(d) for d in docs}", "    redacted = {d.doc_id: d for d in docs}"),
 ("llm: circuit breaker never opens", "glcc/llm/base.py", "        if self._consecutive_failures >= self.failure_threshold:", "        if False:"),
 ("drafter: accept narratives without checking them", "glcc/agents/drafting.py", "                if issues:\n                    meta = {\"narrative\": \"rejected\"", "                if False:\n                    meta = {\"narrative\": \"rejected\""),
]

def run_suite():
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0

survivors = []
for label, rel, old, new in MUTATIONS:
    path = ROOT / rel
    original = path.read_text()
    if old not in original:
        print(f"!! cannot apply: {label}"); survivors.append(label + " (not applicable)"); continue
    mutated = original.replace(old, new, 1)
    if "traceback_text" in new:
        mutated = "import traceback\ndef traceback_text():\n    return traceback.format_exc()\n" + mutated
    path.write_text(mutated)
    try:
        caught = not run_suite()
    finally:
        path.write_text(original)
    print(("caught   " if caught else "SURVIVED ") + label)
    if not caught: survivors.append(label)
print("\nbaseline passes:", run_suite())
print("survivors:", survivors or "none")
