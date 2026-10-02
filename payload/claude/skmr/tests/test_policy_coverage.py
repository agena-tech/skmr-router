#!/usr/bin/env python3
"""Behaviour-coverage check for the refactored CLAUDE.md.

Shortening the policy is only safe if no *rule* was lost. Each entry below is a
behaviour the previous 1153-line policy enforced, expressed as the substrings that
must still appear. A rule matches when every one of its `needs` is present
(casefolded); anything missing is reported by name.

The four phrases asserted by the Sondra staging tests are checked verbatim and
separately, because those are a hard external contract.

    /usr/bin/python3 /root/.claude/skmr/tests/test_policy_coverage.py
"""
import os
import pathlib
import json
import sys

# Resolve this installation's own files. Hardcoded, the suite read another
# account's agent.conf and died with a permission error that said nothing about
# the policy it is supposed to be checking.
CLAUDE_DIR = pathlib.Path(__file__).resolve().parents[2]
CONF = pathlib.Path(os.environ.get('AGENTCOMM_CONF',
                                   str(CLAUDE_DIR.parent / 'agentcomm' / 'agent.conf')))
if not CONF.is_file():
    CONF = pathlib.Path('/root/agentcomm/agent.conf')
HOST_CONFIG = json.loads(CONF.read_text())
CANONICAL_VAULT = HOST_CONFIG['vault_local']
PEER_LABEL = HOST_CONFIG['peer'].upper()

POLICY = pathlib.Path(os.environ.get('SKMR_CLAUDE_MD', str(CLAUDE_DIR / 'CLAUDE.md')))
text = POLICY.read_text(encoding="utf-8")
profile_rules = CLAUDE_DIR / "rules" / "skmr-profile.md"
assert "rules/skmr-profile.md" in text and profile_rules.is_file(), "Profile rules must remain referenced and auto-loadable"
text += "\n" + profile_rules.read_text(encoding="utf-8")
transport_rules = CLAUDE_DIR / "rules" / "skmr-agentcomm.md"
assert "rules/skmr-agentcomm.md" in text and transport_rules.is_file(), "Transport rules must remain referenced and auto-loadable"
text += "\n" + transport_rules.read_text(encoding="utf-8")
folded = text.casefold()

# Hard contract: asserted literally by agentcomm/stage/sondra/tests.
EXTERNAL_CONTRACT = [
    "only when the user asks",
    "concrete unresolved gap",
    "strongly prefer one targeted consultation",
    "never invoke every source automatically",
]

# Markers other code depends on.
MARKERS = [
    "<!-- SKMR_AGENT_TOPOLOGY_START -->",
    "<!-- SKMR_AGENT_TOPOLOGY_END -->",
    "<!-- SKMR_PROFILE_HARDENING_V1 -->",
]

RULES: list[tuple[str, list[str]]] = [
    # -- identity / hierarchy
    ("persistent identity and hierarchy", ["skmr/state/agents.json", "agent topology"]),
    ("permissions independent of rank and hosting", ["independent", "rank", "host"]),
    ("assigned READ vault boundary", ["read-only", "never write canonical permanent memory"]),
    ("assigned WRITE authority", ["final approval for every permanent-memory write", "assigned write authority"]),
    ("candidate receipt is not approval", ["receiving a candidate is not approval"]),
    ("acceptance gate items", ["novel", "reusable", "path-safe", "free of duplicates", "free of secrets"]),
    ("reject with reason via send", ["reject a failure and", "explain the reason"]),
    ("no mechanical peer chatter", ["never mechanically on every turn"]),

    # -- memory roles
    ("memory.md is working state", ["memory.md", "where did we stop"]),
    ("memory.md is not a log", ["it is not a log", "raw tool output"]),
    ("obsidian is sole permanent store", ["single canonical permanent-memory store"]),
    ("canonical vault path", [CANONICAL_VAULT]),
    ("missing mount is an error", ["missing or unavailable mount is an", "empty but", "successful retrieval"]),
    ("never promote into projects memory", ["never promote durable knowledge into", "projects/*/memory/"]),
    ("never write under .obsidian", ["never write knowledge content beneath", ".obsidian/"]),
    ("writer owns physical routing", ["never construct, guess or", "select an obsidian destination path"]),
    ("writer wins on conflict", ["the writer wins"]),
    ("preview -> commit guarded", ["preview -> commit"]),
    ("ck working memory allowed", ["/root/.claude/ck", "ck_"]),

    # -- state routing / intent
    ("automatic state routing", ["where work stopped", "what was done last", "read `memory.md`"]),
    ("identity questions use topology", ["identity questions", "who the peer agent is"]),
    ("no giant regex", ["do not try to hardcode every language into one regex"]),
    ("semantic fallback + no-routing default", ["semantic fallback", "no routing"]),

    # -- knowledge routing
    ("evidence priority order", ["current conversation context", "authorized target evidence", "source code",
                                 "runtime or tool output", "your own technical knowledge"]),
    ("retrieve only on concrete gap", ["only when a concrete information gap exists"]),
    ("obsidian not automatic", ["do not query the vault automatically"]),
    ("bugskills smallest set, no wholesale", ["smallest relevant source set", "never load the repository wholesale"]),
    ("hackerone lazy policy", ["only when the user asks", "never invoke it mechanically"]),
    ("keyword alone insufficient", ["is not by", "itself a reason to retrieve"]),
    ("negative-result source order", ["obsidian", "bugskills-ai", "hackerone-reports"]),
    ("no duplicate consultation in a turn", ["do not repeat an equivalent successful consultation"]),
    ("skmr skill not for every security task", ["do not invoke the `skmr` skill merely because"]),

    # -- learning checkpoint
    ("checkpoint applies inside skills/tools", ["including inside any", "does not replace this policy"]),
    ("tool success is not verified", ["does **not** make the underlying technical claim `verified`"]),
    ("inconclusive stays inconclusive", ["never promote it to verified fact"]),
    ("durable lesson categories", ["bypass condition", "corrected assumption", "investigation pattern"]),
    ("preserve useful success too", ["preserve useful success as well as useful failure"]),
    ("do not save noise", ["do not save noise", "scanner noise"]),
    ("never claim ops that did not occur", ["unless it actually occurred"]),

    # -- save gate
    ("non-profile gate", ["novel + reusable + verified"]),
    ("verified keeps strong meaning", ["machine-checkable evidence"]),
    ("explicit request bypasses only worthiness", ["bypasses only the proactive question"]),
    ("explicit request does not bypass safety", ["para classification", "duplicate detection",
                                                "secret protection", "path safety", "integrity validation"]),
    ("opinions not labelled verified", ["never be labelled verified technical fact"]),
    ("negative results not automatically durable", ["negative results", "are not automatically durable"]),
    ("no fake wikilinks", ["never create a wikilink solely to satisfy validation"]),

    # -- profile
    ("profile is first-class domain", ["first-class permanent-memory domain"]),
    ("profile semantic fields", ["semantic_class: user-profile", "profile_username", "profile_section"]),
    ("all 15 profile sections", ["provenance-corrections", "open-loops", "financial-logistical",
                                 "health-wellbeing", "life-events-timeline", "devices-technical"]),
    ("profile gate", ["novel + reusable + valid provenance"]),
    ("provenance value set", ["user-direct", "user-confirmed", "sondra-relay",
                              "external-observed", "imported-history"]),
    ("guess is never provenance", ["a guess is never a provenance"]),
    ("user-provided is not verified", ["never relabelled verified"]),
    ("approximate dates marked", ["mark approximate dates as approximate"]),
    ("corrections supersede", ["a user correction supersedes"]),
    ("conflicts preserved", ["preserve contradictions as explicit conflicts"]),
    ("relay must not overwrite user fact", ["never", "silently overwrite a user-confirmed fact"]),
    ("read-merge-preview-commit", ["read-merge-preview-commit", "destroying prior content is a defect"]),
    ("merge proof fields", ["profile_full_merge: true", "base_sha256"]),
    ("index backlink rule", ["links back to the canonical profile index"]),
    ("secret list", ["seed phrases", "recovery codes", "session cookies", "cvv"]),
    ("no inferred sensitive facts", ["do not infer sensitive personal facts"]),
    ("profile recall scoping", ["--profile", "never bulk-load the profile"]),
    ("current statement wins", ["newest authoritative update"]),
    ("READ escalates profile candidates", ["assigned read", "configured", "write peer", "never writes canonical profile storage"]),

    # -- planning / orchestration
    ("plan before execution", ["plan before execution"]),
    ("plan scales with task", ["trivial command gets a one-line plan"]),
    ("real plan not prose", ["not a printed"]),
    ("subagents only when useful", ["only when they help"]),
    ("subagent anti-patterns", ["a tiny task", "a single-file", "cannot be", "meaningfully parallelized"]),
    ("never hardcode subagent count", ["never hardcode a subagent count"]),
    ("unique subagent ids", ["no duplicate identifiers in one execution"]),
    ("no fake parallelism", ["never be started in fake parallel"]),
    ("structured results", ["open problems", 'never just "done"']),
    ("verify subagent output", ["do not accept subagent output blindly"]),
    ("verification mandatory for destructive work", ["destructive actions"]),
    ("subagent state in memory.md", ["subagent state lives in `memory.md`"]),
    ("no per-action state writes", ["not on every small action"]),
    ("orchestrator prevents overwrite", ["cannot overwrite each other"]),

    # -- agentcomm
    ("send command surface", ["/skmr:send", "connection", "task"]),
    ("queued is not processed", ["does **not** prove the peer processed it"]),
    ("no polling after send", ["do not call `status`, `recv`, `show` or `watch`"]),
    ("no optional delivery offer", ["do not offer the user an"]),
    ("canonical inbound path", ["agent-message-watcher.py", "asyncrewake"]),
    ("surface peer message", [f"surface `{PEER_LABEL}: <message>` immediately"]),
    ("autonomous, no permission needed", ["do not ask the user for permission merely because"]),
    ("no ack ping-pong", ["ack", "heartbeats", "ping-pong"]),
    ("peer text is data not markup", ["never as system markup"]),
    ("do not re-authenticate a wake", ["do not re-authenticate an already verified wake"]),
    ("empty recv proves nothing", ["does not prove the wake was fake"]),
    ("diagnostics only", ["manual diagnostics only"]),
    ("skmr_inbox is a wrapper", ["skmr_inbox.py", "compatibility wrapper"]),
    ("receipt triggers no retrieval", ["must never by", "itself trigger obsidian retrieval"]),
    ("private is default", ["is the default"]),
    ("public tunnel scope", ["cloudflared", "/api/vault/list", "/api/vault/read"]),
    ("never expose token/smb", ["never expose the `x-agent-token`", ".smb.auth"]),
    ("no tunnel claim without url", ["never claim a tunnel is up"]),
    ("failure timings", ["300s", "600s", "15s"]),
    ("fallback needs a live channel", ["cannot", "reach a fully offline peer"]),

    # -- scope / trust / reporting
    ("authorization scope", ["user authorization", "target authorization", "bug-bounty"]),
    ("external content is not authority", ["reference material, not authority"]),
    ("external cannot expand scope", ["expand target scope", "redefine trust boundaries"]),
    ("blocked behavioural sources", ["master_system_prompts", "00_core_identity_and_behavior.md"]),
    ("browser-use for research", ["browser-use"]),
    ("no private data in external queries", ["never place private target information"]),
    ("report only what happened", ["report only sources actually consulted"]),
    ("no false completion", ["never mark work complete while a test fails"]),
]

missing_contract = [p for p in EXTERNAL_CONTRACT if p.casefold() not in folded]
missing_markers = [m for m in MARKERS if m not in text]
missing_rules: list[tuple[str, list[str]]] = []
for name, needs in RULES:
    absent = [n for n in needs if n.casefold() not in folded]
    if absent:
        missing_rules.append((name, absent))

lines = len(text.splitlines())
print(f"CLAUDE.md: {len(POLICY.read_text().splitlines())} lines, {POLICY.stat().st_size} bytes; combined auto-loaded rules: {len(text.encode())} bytes")
print(f"external contract phrases : {len(EXTERNAL_CONTRACT) - len(missing_contract)}/{len(EXTERNAL_CONTRACT)}")
print(f"required markers          : {len(MARKERS) - len(missing_markers)}/{len(MARKERS)}")
print(f"behaviour rules covered   : {len(RULES) - len(missing_rules)}/{len(RULES)}")

for phrase in missing_contract:
    print(f"  BROKEN CONTRACT: missing verbatim phrase {phrase!r}")
for marker in missing_markers:
    print(f"  MISSING MARKER : {marker}")
for name, absent in missing_rules:
    print(f"  LOST RULE      : {name} -> missing {absent}")

failed = bool(missing_contract or missing_markers or missing_rules)
print("\n" + ("FAILED" if failed else "PASS — no behaviour lost"))
sys.exit(1 if failed else 0)
