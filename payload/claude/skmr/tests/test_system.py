#!/usr/bin/env python3
"""End-to-end checks for commands, hooks, planning, orchestration and intent.

Every case asserts observable behaviour of the real system: the CLI is invoked as
a subprocess the way a command file invokes it, and the state file is the real one,
snapshotted and restored around the tests that mutate it.

    /usr/bin/python3 /root/.claude/skmr/tests/test_system.py
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import sys

# <claude_dir>/skmr/tests/<this file> -- resolved so an installation under a
# different prefix exercises its own CLI instead of the packaging host's.
CLAUDE_DIR = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CLAUDE_DIR))

CLI = str(CLAUDE_DIR / "skmr" / "cli.py")
PYTHON = "/usr/bin/python3"
failures: list[str] = []
checks = 0


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run([PYTHON, CLI, *args], capture_output=True, text=True,
                          timeout=timeout, env=merged, check=False)


def check(name: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  [ ok ] {name}")
    else:
        failures.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


print("\n=== command surface ===")
result = run(["help"])
check("help lists commands", "/skmr:obsidian-memory" in result.stdout and result.returncode == 0)
check("help emits one Executing banner", result.stdout.count("Executing:") == 1)
check("simple task prints no plan body", "Plan (" not in result.stdout)

result = run(["help", "obsidian-memory"])
check("help <command> shows usage", "Usage:" in result.stdout)

result = run(["nonexistent-command"])
check("unknown command is a controlled error",
      result.returncode == 2 and "unknown command" in result.stderr)

print("\n=== output format ===")
result = run(["help"], env={"NO_COLOR": "1"})
check("NO_COLOR yields plain text", "\033[" not in result.stdout)
result = run(["help"], env={"SKMR_FORCE_COLOR": "1"})
check("forced colour emits ANSI", "\033[32m" in result.stdout)
check("banner prefix is standardized", result.stdout.lstrip().startswith("[SKMR]:"))

print("\n=== planning depth ===")
result = run(["plan", "rename one variable"])
check("trivial task -> no subagents", "No subagents required." in result.stdout)
result = run(["plan", "audit auth endpoints, review access control, analyze logic, check CORS"])
check("multi-unit task -> subagents", "subagents planned (not started)." in result.stdout)
check("subagent table rendered", "| Agent | Role |" in result.stdout)
check("dependencies shown", "verifier-agent" in result.stdout and "unit-01-agent" in result.stdout)
derived = [line for line in result.stdout.splitlines() if "subagents planned" in line]
check("count is derived not fixed", bool(derived) and "5 subagents" in derived[0], derived[0] if derived else "")

print("\n=== routing announced only on real routing ===")
result = run(["state", "show"])
check("state routes to native-memory", 'Routing: "native-memory"' in result.stdout)
check("routing announced once", result.stdout.count('Routing: "native-memory"') == 1)
result = run(["help"])
check("help announces no routing", "Routing:" not in result.stdout)

print("\n=== retrieval fallback matrix ===")
# Exercise the actual index/provider on a disposable vault on both machines.
# Sondra intentionally supports public access without any local SMB mount.
from skmr.core import config as retrieval_config
from skmr.memory.indexing import index as retrieval_index
retrieval_snapshot = retrieval_config.CONFIG_PATH.read_bytes()
retrieval_fixture = tempfile.TemporaryDirectory(prefix="skmr-system-retrieval-")
retrieval_root = pathlib.Path(retrieval_fixture.name)
(retrieval_root / 'profile.md').write_text(
    '# Anezatra profile\n\n## Canonical memory\n'
    'The Anezatra profile uses obsidian_memory.py for canonical memory validation. '
    'Preview and commit protect durable notes. Retrieval combines the BM25 index '
    'with semantic vector search over the profile and technical identifiers.\n', encoding='utf-8')
retrieval_config.write({'vault_path': str(retrieval_root), 'index_path': str(retrieval_root / 'index.sqlite3')})
retrieval_index.sync()
retrieval_vault_before = os.environ.get('SKMR_VAULT')
os.environ['SKMR_VAULT'] = str(retrieval_root)
result = run(["obsidian-memory", "search", "anezatra", "--limit", "2", "--no-plan"],
            env={"OLLAMA_HOST": "http://127.0.0.1:9"})
check("vector down -> BM25 fallback + warning",
      "Falling back to BM25" in result.stderr and "result(s)" in result.stdout)
result = run(["obsidian-memory", "search", "anezatra", "--mode", "vector", "--no-plan"],
            env={"OLLAMA_HOST": "http://127.0.0.1:9"})
check("mode=vector down -> controlled error",
      result.returncode == 1 and "vector search unavailable" in result.stderr)
result = run(["obsidian-memory", "search", "anything", "--no-plan"],
            env={"SKMR_VAULT": "/nonexistent-vault-xyz"})
check("missing mount is an error not empty success",
      "vault unavailable" in result.stderr and "not an empty result" in result.stderr)

print("\n=== search modes ===")
for mode in ("bm25", "vector", "hybrid"):
    result = run(["obsidian-memory", "search", "anezatra profile", "--mode", mode,
                  "--limit", "2", "--no-plan"])
    check(f"mode={mode} returns results", "result(s) for" in result.stdout, result.stderr[:120])
result = run(["obsidian-memory", "search", "obsidian_memory.py", "--mode", "bm25",
              "--limit", "3", "--no-plan"])
check("exact identifier token matches", "result(s) for" in result.stdout)
retrieval_config.CONFIG_PATH.write_bytes(retrieval_snapshot)
retrieval_config.load(refresh=True)
if retrieval_vault_before is None:
    os.environ.pop('SKMR_VAULT', None)
else:
    os.environ['SKMR_VAULT'] = retrieval_vault_before
retrieval_fixture.cleanup()

print("\n=== writer authority ===")
result = run(["obsidian-memory", "remember", "something", "--no-plan"])
check("direct remember is refused, preview/commit required",
      result.returncode == 2 and "preview" in result.stderr and "commit" in result.stderr)

print("\n=== knowledge-base guards ===")
result = run(["bugskills-ai", "read", "MASTER_SYSTEM_PROMPTS/x.md", "--no-plan"])
check("behavioural prompt source blocked",
      result.returncode == 2 and "behavioural instruction source" in result.stderr)
result = run(["bugskills-ai", "read", "SKILL_FILES/00_core_identity_and_behavior.md", "--no-plan"])
check("core identity file blocked", result.returncode == 2)
result = run(["bugskills-ai", "read", "../../../etc/passwd", "--no-plan"])
check("path traversal blocked", "escapes the knowledge base" in result.stderr)

print("\n=== identity and topology validation ===")
result = run(["assign-role", "--show", "--no-plan"])
# Assert the shape, never the installed names: this host's agents may be called
# anything, and an assertion on "Eliza" passes or fails for the wrong reason.
topology_before = result.stdout
check("topology persisted and readable",
      result.returncode == 0 and "### Local" in topology_before
      and "### Remote" in topology_before)
for label, args in (
    ("invalid IP rejected", ["--name", "A", "--role", "R", "--remote-name", "B",
                             "--remote-role", "R2", "--remote-host", "999.1.1.1"]),
    ("invalid hostname rejected", ["--name", "A", "--role", "R", "--remote-name", "B",
                                   "--remote-role", "R2", "--remote-host", "bad_host!"]),
    ("empty role rejected", ["--name", "A", "--role", ""]),
    ("duplicate identity rejected", ["--name", "Z", "--role", "R", "--remote-name", "z",
                                     "--remote-role", "L", "--remote-host", "10.0.0.5"]),
):
    result = run(["assign-role", "--non-interactive", *args, "--no-plan"])
    check(label, result.returncode == 2 and "Error" in result.stderr)
result = run(["assign-role", "--show", "--no-plan"])
# The real invariant is that a rejected assignment changed nothing, so compare
# the rendering with the one taken before the rejections rather than looking for
# particular names. This also catches a partial write that kept both names.
check("rejected input did not corrupt topology",
      result.stdout == topology_before)

print("\n=== intent detection ===")
from skmr.memory import intent  # noqa: E402
INTENT_CASES = [
    ("Nerede kalmıştık?", "previous_state"), ("where did we stop?", "previous_state"),
    ("En son ne yaptık?", "last_action"), ("what did we do last?", "last_action"),
    ("Devam edelim.", "continue_task"), ("continue from where we left off", "continue_task"),
    ("Hangi aşamadayız?", "current_state"), ("Subagentler ne yaptı?", "agent_progress"),
    ("what are the agents doing?", "agent_progress"), ("Adın ne?", "identity_self"),
    ("Ben kimim?", "identity_user"), ("who is the other agent", "identity_peer"),
    ("SQL injection nedir?", None), ("write a python function", None),
]
wrong = [(text, want, (d.intent if (d := intent.detect(text)) else None))
         for text, want in INTENT_CASES
         if (intent.detect(text).intent if intent.detect(text) else None) != want]
check(f"intent detection {len(INTENT_CASES) - len(wrong)}/{len(INTENT_CASES)}",
      not wrong, str(wrong)[:200])
check("state intents route to native-memory",
      all(intent.detect(t).route == "native-memory" for t, w in INTENT_CASES if w))

print("\n=== orchestration state machine ===")
from skmr.agents import orchestrator as orc      # noqa: E402
from skmr.core.registry import Skill             # noqa: E402
from skmr.memory import native                   # noqa: E402
from skmr.planning import planner                # noqa: E402

MEMORY = native.memory_path()
backup = MEMORY.read_text(encoding="utf-8") if MEMORY.exists() else None
try:
    # Count what is already registered instead of assuming an empty MEMORY.md.
    # The absolute count made this suite order-dependent: run after anything
    # that had registered a subagent, it failed for a reason unrelated to the
    # behaviour under test.
    subagents_before = len(native.load().subagents())
    skill = Skill(name="t", summary="t", handler="x:y", planning="medium", subagents=True)
    plan = planner.build(skill, "alpha work, beta work, gamma work")
    rows = orc.register(plan)
    check("register creates rows", len(rows) == 4)
    check("dependent agent starts QUEUED",
          any(r.agent_id == "verifier-agent" and r.status == "QUEUED" for r in rows))

    gated = False
    try:
        orc.transition("verifier-agent", "RUNNING")
    except orc.TransitionError:
        gated = True
    check("dependency gate blocks premature RUNNING", gated)

    orc.transition("unit-01-agent", "RUNNING", "in progress")
    illegal = False
    try:
        orc.transition("unit-01-agent", "PLANNED")
    except orc.TransitionError:
        illegal = True
    check("illegal transition rejected", illegal)

    orc.transition("unit-02-agent", "RUNNING")
    orc.transition("unit-02-agent", "BLOCKED", "waiting on input")
    orc.record(orc.Result("unit-01-agent", "alpha work", "COMPLETED", summary="done",
                          findings=["shared"], verified=True))
    orc.transition("unit-03-agent", "RUNNING")
    orc.record(orc.Result("unit-03-agent", "gamma work", "COMPLETED", summary="done",
                          findings=["shared"], open_problems=["needs token"]))
    merged = orc.aggregate()
    check("blocker preserved", merged["blocked"] == ["unit-02-agent"])
    check("duplicate findings merged", "shared" in merged["duplicated"])
    check("unverified completion flagged", "unit-03-agent" in merged["unverified"])
    check("open problem recorded in state", "needs token" in native.load().get("Open Problems"))
    check("state survives a fresh read",
          len(native.load().subagents()) - subagents_before == 4)
finally:
    orc.clear()
    if backup is not None:
        MEMORY.write_text(backup, encoding="utf-8")
    print("  (MEMORY.md restored)")

print("\n=== learning filter ===")
from skmr.memory.learning import ERROR, Episode, assess, candidate  # noqa: E402

noise = Episode(topic="Typo", problem="misspelled a flag",
                lesson="I made a typo in this run and fixed it")
check("one-off noise rejected", not assess(noise).save_worthy)
transient = Episode(topic="Timeout", problem="request failed",
                    lesson="The endpoint timed out right now so I retried it once")
check("transient episode rejected", not assess(transient).save_worthy)
unproven = Episode(topic="Claim", problem="p", verified=True,
                   lesson="Always check the WSL network boundary because NAT breaks connections")
check("verified without evidence rejected", not assess(unproven).save_worthy)
durable = Episode(
    topic="Reference analysis before deletion", kind=ERROR,
    problem="A file was archived before dependency analysis ran.",
    solution="Grep hooks, skills, commands and tests for the name first.",
    lesson="Always run reference analysis before deleting a file, because a test-only "
           "consumer makes a file look dead when it is not.")
verdict = assess(durable)
check("generalized lesson accepted", verdict.save_worthy, verdict.explain())
payload = candidate(durable)
check("unverified lesson offered as explicit save, not automatic",
      payload["automatic"] is False and payload.get("explicit_user_request") is False)
check("candidate carries no destination path",
      not any(k in payload for k in ("path", "relative", "target", "destination")))
verified = Episode(topic="T", problem="p", solution="s", verified=True,
                   evidence=[{"kind": "test", "locator": "tests/test_system.py"}],
                   lesson="Always verify the boundary first because the failure is silent otherwise.")
check("verified lesson with evidence is automatic", candidate(verified)["automatic"] is True)

print("\n=== lifecycle hook for external skills ===")
HOOK = "/root/.claude/bin/skmr-skill-lifecycle.py"


# The lifecycle hook really does write working state, and these cases assert only
# on its stdout. Pointing the subprocess at scratch paths keeps the hook itself
# real while making contamination of the operator's live MEMORY.md impossible --
# unlike a snapshot/restore pair, there is no cleanup step an early failure can
# skip. The orchestration section above still exercises the real file on purpose.
_HOOK_STATE = pathlib.Path(tempfile.mkdtemp(prefix="skmr-hook-test-"))
_HOOK_ENV = {**os.environ,
             "SKMR_MEMORY_PATH": str(_HOOK_STATE / "MEMORY.md"),
             "SKMR_RUNTIME_STATE": str(_HOOK_STATE / "state"),
             "SKMR_AGENTS_PATH": str(_HOOK_STATE / "state" / "agents.json")}


def hook(payload: dict) -> subprocess.CompletedProcess:
    result = subprocess.run([PYTHON, HOOK], input=json.dumps(payload), capture_output=True,
                            text=True, timeout=120, check=False, env=_HOOK_ENV)
    if result.stdout.strip():
        result.stdout = json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")
    return result


out = hook({"tool_name": "Skill", "tool_input": {
    "skill": "claude-bughunter:hunt", "args": "recon, auth, access control, logic"}})
check("external hunting skill gets the lifecycle",
      'Executing: "claude-bughunter:hunt"' in out.stdout and "Planning:" in out.stdout)
check("external skill gets a subagent decision", "subagents planned (not started)." in out.stdout)
out = hook({"tool_name": "Skill", "tool_input": {"skill": "hackerone-intelligence", "args": "oauth"}})
check("simple external skill gets no plan spam",
      "No subagents required." in out.stdout and "Plan (" not in out.stdout)
out = hook({"tool_name": "Skill", "tool_input": {"skill": "skmr:help"}})
check("/skmr: skills are not double-announced", out.stdout.strip() == "")
out = hook({"tool_name": "Skill", "tool_input": {"skill": "ecc:python-review"}})
check("unmonitored skill is silent", out.stdout.strip() == "")
out = subprocess.run([PYTHON, HOOK], input="not json", capture_output=True, text=True,
                     timeout=60, check=False)
check("malformed hook input never fails the tool call", out.returncode == 0)
out = hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
check("non-Skill tools ignored", out.returncode == 0 and out.stdout.strip() == "")

print("\n=== config resilience ===")
from skmr.core import config                     # noqa: E402
CONFIG = pathlib.Path(os.environ.get("SKMR_CONFIG", "/root/.claude/skmr/config/skmr.json"))
snapshot = CONFIG.read_text(encoding="utf-8")
try:
    CONFIG.write_text("{ this is not json", encoding="utf-8")
    values = config.load(refresh=True)
    check("corrupt config falls back to defaults",
          values["search_mode"] == config.DEFAULTS["search_mode"])
finally:
    CONFIG.write_text(snapshot, encoding="utf-8")
    config.load(refresh=True)

print(f"\n{checks - len(failures)}/{checks} checks passed")
for name in failures:
    print(f"  FAILED: {name}")
sys.exit(1 if failures else 0)
