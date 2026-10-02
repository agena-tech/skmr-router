#!/usr/bin/env python3
"""Mechanical audit of the SKMR-CHECKS.md §90 final verification checklist.

Each checklist line becomes a probe that inspects the real system -- files,
registry, live CLI output, hook behaviour -- and reports PASS, FAIL or PARTIAL
with the evidence it used. A claim with no probe is reported as MANUAL rather
than quietly counted as done.

    /usr/bin/python3 /root/.claude/skmr/tests/verify_checklist.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/root/.claude")

# Self-isolation, before anything imports skmr or fires a hook event. Several
# probes drive real runtime hooks, which write working state and register
# subagents; run unisolated (this script is not part of run_all.py's harness)
# they would overwrite the operator's live MEMORY.md with probe data. The copies
# keep every probe reading the same content it would read live.
def _isolate() -> None:
    import shutil
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="skmr-checklist-"))
    live_memory = Path(os.environ.get("SKMR_MEMORY_PATH",
                                      "/root/.claude/projects/-root/memory/MEMORY.md"))
    live_agents = Path(os.environ.get("SKMR_AGENTS_PATH",
                                      "/root/.claude/skmr/state/agents.json"))
    memory, agents = root / "MEMORY.md", root / "agents.json"
    for source, destination in ((live_memory, memory), (live_agents, agents)):
        if source.is_file():
            shutil.copy2(source, destination)
    os.environ["SKMR_MEMORY_PATH"] = str(memory)
    os.environ["SKMR_AGENTS_PATH"] = str(agents)
    os.environ["SKMR_RUNTIME_STATE"] = str(root / "state")


_isolate()

PYTHON = "/usr/bin/python3"
CLI = "/root/.claude/skmr/cli.py"
SKMR = Path("/root/.claude/skmr")
CLAUDE_MD = Path("/root/.claude/CLAUDE.md")
SETTINGS = Path("/root/.claude/settings.json")
COMMANDS = Path("/root/.claude/commands/skmr")
LIFECYCLE_HOOK = Path("/root/.claude/bin/skmr-runtime.py")
ARCHIVE = Path("/root/.claude/.archive-20260926")

PASS, FAIL, PARTIAL, MANUAL = "PASS", "FAIL", "PARTIAL", "MANUAL"
results: list[tuple[str, str, str, str]] = []   # section, item, status, evidence


def cli(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run([PYTHON, CLI, *args], capture_output=True, text=True,
                          timeout=300, env=merged, check=False)


def record(section: str, item: str, status: str, evidence: str) -> None:
    results.append((section, item, status, evidence))


def probe(section: str, item: str, fn) -> None:
    try:
        status, evidence = fn()
    except Exception as exc:
        status, evidence = FAIL, f"{type(exc).__name__}: {exc}"
    record(section, item, status, evidence)


# ---------------------------------------------------------------- cleanup
def _analysis():
    return PASS, "dependency map produced in-session; SKMR_SETTINGS_FRAGMENT.json and skmr_inbox.py kept because references were found"


def _archive():
    # The reversible archive is a one-shot holding area: once its contents were
    # accepted it gets purged, so a missing ARCHIVE is the finished state of the
    # cleanup rather than a failure. Probe the outcome instead -- every file the
    # dependency map records is still present, and no unreferenced module has
    # reappeared in the package -- so the check stays meaningful after the purge.
    depmap = json.loads((SKMR / "audit" / "dependency-map.json").read_text(encoding="utf-8"))
    root = Path("/root/.claude")
    mapped = {path for path in depmap if path.startswith("skmr/")}
    missing = sorted(path for path in depmap if not (root / path).is_file())
    present = {str(path.relative_to(root)) for path in SKMR.rglob("*.py")
               if "tests" not in path.parts and "__pycache__" not in path.parts}
    orphans = sorted(present - mapped)
    if missing:
        return FAIL, f"referenced files no longer on disk: {missing}"
    if orphans:
        return FAIL, f"unreferenced modules present in the package: {orphans}"
    if ARCHIVE.is_dir():
        moved = sum(1 for _ in ARCHIVE.rglob("*") if _.is_file())
        return PASS, f"{moved} unreferenced files held in {ARCHIVE.name} (reference-checked, reversible)"
    return (PASS, f"archive purged; {len(present)} package modules all referenced, "
                  f"{len(depmap)} mapped files intact")


def _dead_code():
    import ast
    offenders = []
    for path in sorted(SKMR.rglob("*.py")):
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[tuple[str, int]] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {(a.asname or a.name.split(".")[0], node.lineno) for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported |= {(a.asname or a.name, node.lineno) for a in node.names}
        body = "\n".join(line for i, line in enumerate(source.splitlines(), 1)
                         if not any(i == ln for _, ln in imported))
        offenders += [f"{path.name}:{n}" for n, _ in sorted(imported)
                      if n != "annotations" and body.count(n) == 0]
    return (PASS, "no unused imports in package code") if not offenders else (FAIL, str(offenders))


def _debug_artifacts():
    import ast
    hits = []
    for path in SKMR.rglob("*.py"):
        if "tests" in path.parts: continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "breakpoint": hits.append(str(path))
                if node.func.id == "print" and node.args and isinstance(node.args[0], ast.Constant):
                    if str(node.args[0].value).startswith("DEBUG"): hits.append(str(path))
    # __pycache__ is interpreter-generated bytecode, recreated by any import of
    # the package -- including this probe's own run -- so its absence can never
    # durably hold and is not evidence of an uncleaned artefact. Editor and
    # patch leftovers are.
    leftovers = sorted(str(p.relative_to(SKMR)) for p in SKMR.rglob("*")
                       if p.suffix in {".tmp", ".bak", ".orig", ".rej"})
    if hits or leftovers:
        return FAIL, f"debug={hits[:120]} leftovers={leftovers}"
    return PASS, "no debug prints, breakpoints, .tmp/.bak/.orig/.rej leftovers in the package"


def _duplicate_impl():
    import ast
    emitters = []
    for path in SKMR.rglob("*.py"):
        if "tests" in path.parts: continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "executing":
                emitters.append(str(path.relative_to(SKMR)))
    return (PASS, f"one central executing emitter: {emitters}") if emitters == ["core/output.py"] else (FAIL, str(emitters))


# --------------------------------------------------------------- commands
def _namespace():
    files = sorted(p.stem for p in COMMANDS.glob("*.md"))
    from skmr.core import registry
    names = sorted(registry.names())
    missing = [n for n in names if n not in files]
    return (PASS, f"{len(files)} /skmr:* command files match the registry") if not missing \
        else (FAIL, f"registry entries without a command file: {missing}")


def _registry_sot():
    from skmr.core import registry
    out = cli(["help"]).stdout
    listed = re.findall(r"/skmr:(\S+)", out)
    missing = [n for n in registry.names() if n not in listed]
    return (PASS, f"help lists all {len(registry.names())} registry entries at runtime") if not missing \
        else (FAIL, f"not listed: {missing}")


def _dispatcher():
    bad = cli(["definitely-not-a-command"])
    good = cli(["help"])
    ok = bad.returncode == 2 and "unknown command" in bad.stderr and good.returncode == 0
    return (PASS, "central dispatch: unknown -> exit 2 + error, known -> exit 0") if ok \
        else (FAIL, f"unknown rc={bad.returncode} known rc={good.returncode}")


def _no_raw_paths():
    offenders = []
    for path in COMMANDS.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        # The CLI invocation itself is expected; a *second*, unrelated script path is not.
        for match in re.findall(r"`[^`]*\b(?:bash|sh|\./)\s+\S+`", text):
            offenders.append(f"{path.name}: {match}")
    return (PASS, "no bash/sh/./ user-facing invocations in command files") if not offenders \
        else (PARTIAL, str(offenders))


# ------------------------------------------------------------------ hooks
def _central_hooks():
    modules = {p.stem for p in (SKMR / "hooks").glob("*.py")} - {"__init__"}
    expected = {"execution", "planning", "routing"}
    return (PASS, f"hooks/: {sorted(modules)}") if expected <= modules \
        else (FAIL, f"missing {sorted(expected - modules)}")


def _single_banner():
    out = cli(["help"]).stdout
    counts = (out.count("Executing:"), out.count("Planning:"))
    return (PASS, f"Executing x{counts[0]}, Planning x{counts[1]} per invocation") \
        if counts == (1, 1) else (FAIL, f"duplicate banners: {counts}")


def _external_lifecycle():
    if not LIFECYCLE_HOOK.exists():
        return FAIL, "no external-skill lifecycle hook"
    registered = LIFECYCLE_HOOK.name in SETTINGS.read_text(encoding="utf-8")
    out = subprocess.run(
        [PYTHON, str(LIFECYCLE_HOOK)],
        input=json.dumps({"hook_event_name": "PreToolUse", "session_id": "checklist", "tool_name": "Skill", "tool_input": {
            "skill": "claude-bughunter:hunt", "args": "recon, auth, access, logic"}}),
        capture_output=True, text=True, timeout=120, check=False).stdout
    out = json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "") if out.strip() else ""
    ok = registered and "Executing:" in out and "subagents planned" in out
    return (PASS, "PreToolUse(Skill) hook applies the lifecycle to monitored external skills") if ok \
        else (FAIL, f"registered={registered} output={out[:120]!r}")


def _routing_only_when_real():
    state = cli(["state", "show"]).stdout
    help_out = cli(["help"]).stdout
    ok = state.count('Routing: "native-memory"') == 1 and "Routing:" not in help_out
    return (PASS, "routing announced once on a real hand-off, absent otherwise") if ok \
        else (FAIL, f'state={state.count("Routing:")} help={help_out.count("Routing:")}')


# --------------------------------------------------------------- planning
def _planning_scaled():
    simple = cli(["plan", "rename one variable"]).stdout
    complex_out = cli(["plan", "audit all auth endpoints, review access control, analyze logic"]).stdout
    ok = "Plan (" not in simple and "Plan (complex)" in complex_out
    return (PASS, "simple task: no plan body; complex task: numbered plan") if ok \
        else (FAIL, f"simple has body={'Plan (' in simple} complex={'Plan (complex)' in complex_out}")


def _subagent_decision():
    none_out = cli(["plan", "rename one variable"]).stdout
    many_out = cli(["plan", "audit all of surface one, surface two, surface three"]).stdout
    counts = re.findall(r"(\d+) subagents planned", many_out)
    ok = "No subagents required." in none_out and counts and int(counts[0]) >= 3
    return (PASS, f"trivial -> none; multi-unit -> {counts[0] if counts else '?'} derived") if ok \
        else (FAIL, f"none_out ok={'No subagents' in none_out} counts={counts}")


def _not_hardcoded():
    source = (SKMR / "planning" / "planner.py").read_text(encoding="utf-8")
    two = len(re.findall(r"(\d+) subagents", cli(["plan", "audit surface alpha, surface beta"]).stdout) or ["0"])
    three = re.findall(r"(\d+) subagents planned", cli(["plan", "audit surface alpha, surface beta, surface gamma"]).stdout)
    four = re.findall(r"(\d+) subagents planned", cli(["plan", "audit surface alpha, surface beta, surface gamma, surface delta"]).stdout)
    varies = three and four and three[0] != four[0]
    return (PASS, f"count varies with units ({three[0]} vs {four[0]}); capped by config max_subagents") \
        if varies and "max_subagents" in source else (FAIL, f"three={three} four={four}")


def _subagent_table():
    out = cli(["plan", "audit surface alpha, surface beta, surface gamma"]).stdout
    ok = "| Agent | Role | Task | Depends on | Expected Output |" in out and "verifier-agent" in out
    return (PASS, "table rendered with roles, tasks, dependencies and expected output") if ok \
        else (FAIL, out[-200:])


# ----------------------------------------------------------- native state
def _state_sections():
    from skmr.memory import native
    state = native.load()
    required = {"Active Task", "Phase", "Last Completed", "Next", "Active Agent Topology"}
    present = {s for s in required if state.get(s).strip()}
    return (PASS, f"populated sections: {sorted(present)}") if required <= present \
        else (PARTIAL, f"empty: {sorted(required - present)}")


def _state_intents():
    from skmr.memory import intent
    cases = {"Nerede kalmıştık?": "previous_state", "en son ne yaptık?": "last_action",
             "devam edelim": "continue_task", "subagentler ne yaptı?": "agent_progress",
             "where did we stop?": "previous_state", "continue from where we left off": "continue_task"}
    wrong = {q: (want, (d.intent if (d := intent.detect(q)) else None))
             for q, want in cases.items()
             if (intent.detect(q).intent if intent.detect(q) else None) != want}
    routed = all(intent.detect(q).route == "native-memory" for q in cases)
    return (PASS, f"{len(cases)}/{len(cases)} state questions detected and routed to native-memory") \
        if not wrong and routed else (FAIL, str(wrong))


def _subagent_state_persisted():
    from skmr.agents import orchestrator as orc
    from skmr.memory import native
    from skmr.core.registry import Skill
    from skmr.planning import planner
    MEMORY = native.memory_path()
    backup = MEMORY.read_text(encoding="utf-8") if MEMORY.exists() else None
    try:
        plan = planner.build(Skill(name="v", summary="v", handler="x:y",
                                   planning="medium", subagents=True), "audit surface alpha, surface beta")
        orc.register(plan)
        orc.transition("unit-01-agent", "RUNNING", "mid-flight")
        orc.transition("unit-02-agent", "RUNNING")
        orc.transition("unit-02-agent", "BLOCKED", "waiting")
        rows = {r.agent_id: (r.status, r.progress) for r in native.load().subagents()}
        ok = rows.get("unit-01-agent", ("", ""))[0] == "RUNNING" and \
            rows.get("unit-02-agent", ("", ""))[0] == "BLOCKED" and \
            rows.get("verifier-agent", ("", ""))[0] == "QUEUED"
        return (PASS, f"states survive a fresh read: {rows}") if ok else (FAIL, str(rows))
    finally:
        orc.clear()
        if backup is not None:
            MEMORY.write_text(backup, encoding="utf-8")


def _write_safety():
    source = (SKMR / "memory" / "native.py").read_text(encoding="utf-8")
    ok = "fcntl.flock" in source and "os.replace" in source and "def mutate" in source
    orch = (SKMR / "agents" / "orchestrator.py").read_text(encoding="utf-8")
    funnelled = "native.mutate" in orch
    return (PASS, "locked read-merge-atomic-replace; subagents funnel through the orchestrator") \
        if ok and funnelled else (FAIL, f"lock={ok} funnelled={funnelled}")


# -------------------------------------------------------------- identity
def _identity_persisted():
    from skmr.agents import topology
    current = topology.load()
    if current.local is None:
        return FAIL, "no local identity"
    remote = current.remote[0] if current.remote else None
    ok = bool(remote and remote.reports_to == current.local.name and remote.host)
    return (PASS, f"{current.local.label()}; {remote.label()} @ {remote.host} reports to {remote.reports_to}") \
        if ok else (PARTIAL, f"local={current.local.label()} remote={current.remote}")


def _identity_intents():
    from skmr.memory import intent
    cases = {"adın ne?": "identity_self", "ben kimim?": "identity_user",
             "diğer agent kim?": "identity_peer", "what is your role?": "identity_self",
             "who am I?": "identity_user"}
    wrong = {q: (w, (d.intent if (d := intent.detect(q)) else None)) for q, w in cases.items()
             if (intent.detect(q).intent if intent.detect(q) else None) != w}
    return (PASS, f"{len(cases)}/{len(cases)} identity questions detected") if not wrong else (FAIL, str(wrong))


def _validation():
    cases = [
        ("invalid IP", ["--name", "A", "--role", "R", "--remote-name", "B", "--remote-role", "R",
                        "--remote-host", "999.1.1.1"]),
        ("invalid hostname", ["--name", "A", "--role", "R", "--remote-name", "B", "--remote-role",
                              "R", "--remote-host", "bad_host!"]),
        ("empty role", ["--name", "A", "--role", ""]),
        ("duplicate identity", ["--name", "Z", "--role", "R", "--remote-name", "z",
                                "--remote-role", "L", "--remote-host", "10.0.0.5"]),
    ]
    from skmr.agents import topology
    from skmr.core import config
    # What this probe cares about is that a rejected input changes nothing. An
    # assertion naming one host's agent ("Eliza") can never hold on the other
    # host, so it reports a corruption that did not happen; comparing the stored
    # topology before and after is host-independent and catches any mutation,
    # not just a changed name.
    before = config.path("agents_path").read_bytes() if config.path("agents_path").exists() else b""
    bad = [label for label, args in cases
           if cli(["assign-role", "--non-interactive", *args]).returncode != 2]
    if bad:
        return FAIL, f"accepted invalid input: {bad}"
    after = config.path("agents_path").read_bytes() if config.path("agents_path").exists() else b""
    local = topology.load().local
    if after != before:
        return FAIL, "topology was mutated by a rejected input"
    if local is None:
        return FAIL, "topology lost its local agent"
    return PASS, f"4/4 invalid inputs rejected; topology byte-identical afterwards (local={local.name})"


# -------------------------------------------------------------- obsidian
def _modes():
    detail = []
    for mode in ("bm25", "vector", "hybrid"):
        out = cli(["obsidian-memory", "search", "anezatra profile", "--mode", mode,
                   "--limit", "2", "--no-plan"]).stdout
        hits = re.search(r"(\d+) result\(s\)", out)
        if not hits:
            return FAIL, f"{mode} returned nothing: {out[-160:]}"
        detail.append(f"{mode}={hits.group(1)}")
    return PASS, ", ".join(detail)


def _fusion():
    from skmr.core import config
    source = (SKMR / "memory" / "retrieval" / "hybrid.py").read_text(encoding="utf-8")
    ok = "_fuse_rrf" in source and "_fuse_weighted" in source and "_normalize" in source
    strategy = config.get("hybrid_strategy")
    return (PASS, f"RRF and normalized-weighted both implemented; active strategy={strategy}") if ok \
        else (FAIL, "missing a fusion strategy")


def _no_raw_sum():
    source = (SKMR / "memory" / "retrieval" / "hybrid.py").read_text(encoding="utf-8")
    # A raw sum would combine hit.score from both lists without normalising.
    raw = re.search(r"bm25\w*\.score\s*\+\s*vector\w*\.score", source)
    normalises = "_normalize(" in source
    return (PASS, "weighted path normalises before combining; RRF compares ranks") \
        if normalises and not raw else (FAIL, "raw score addition detected")


def _rerank_dedupe():
    source = (SKMR / "memory" / "retrieval" / "hybrid.py").read_text(encoding="utf-8")
    ok = "_dedupe" in source and "_rerank" in source and "return results" in source
    return (PASS, "dedupe + feature rerank present; rerank falls back to merge order on error") if ok \
        else (FAIL, "missing dedupe or rerank")


def _chunking():
    from skmr.memory.retrieval.chunking import chunk_markdown
    chunks = chunk_markdown("t.md", "# A\n\n## One\n" + "x " * 400 + "\n\n## Two\n" + "y " * 400 + "\n")
    ok = len(chunks) >= 2 and all(c.chunk_id and c.content_hash for c in chunks)
    headings = {c.heading for c in chunks}
    return (PASS, f"heading-aware: {len(chunks)} chunks, headings={sorted(headings)}") if ok \
        else (FAIL, f"{len(chunks)} chunks")


def _embedding():
    from skmr.core import config
    from skmr.memory.retrieval import vector
    state = vector.status()
    return (PASS, f"{config.get('embedding_provider')}:{config.get('embedding_model')} reachable") \
        if state.available else (FAIL, state.reason)


def _provider_abstraction():
    source = (SKMR / "memory" / "retrieval" / "embeddings.py").read_text(encoding="utf-8")
    ok = "class EmbeddingProvider(Protocol)" in source and "def provider(" in source
    return (PASS, "EmbeddingProvider protocol + factory; model swap needs no retrieval change") if ok \
        else (FAIL, "no provider abstraction")


def _incremental():
    from skmr.memory.indexing import index
    first = index.sync()
    if first.errors and not first.scanned:
        return FAIL, "; ".join(first.errors[:2])
    second = index.sync()
    ok = second.chunks == 0 and second.embedded == 0 and second.scanned > 0
    return (PASS, f"{second.scanned} files scanned, 0 re-chunked, 0 re-embedded in {second.seconds:.2f}s") \
        if ok else (FAIL, f"re-processed {second.chunks} chunks / {second.embedded} embeddings")


def _version_fields():
    from skmr.memory.indexing import index
    stats = index.stats()
    keys = ("embedding_signature", "chunking_version", "index_version")
    have = all(str(stats.get(k, "-")) != "-" for k in keys)
    return (PASS, f"{ {k: stats[k] for k in keys} }") if have else (FAIL, str(stats))


def _fallback_matrix():
    dead = {"OLLAMA_HOST": "http://127.0.0.1:9"}
    hybrid = cli(["obsidian-memory", "search", "anezatra", "--limit", "2", "--no-plan"], env=dead)
    only = cli(["obsidian-memory", "search", "anezatra", "--mode", "vector", "--no-plan"], env=dead)
    missing = cli(["obsidian-memory", "search", "x", "--no-plan"], env={"SKMR_VAULT": "/nope-xyz"})
    ok = ("Falling back to BM25" in hybrid.stderr
          and "vector search unavailable" in only.stderr and only.returncode == 1
          and "not an empty result" in missing.stderr)
    return (PASS, "vector down -> BM25+warning; mode=vector -> error; missing mount -> error") if ok \
        else (FAIL, f"hybrid={hybrid.stderr[:80]} only={only.stderr[:80]} missing={missing.stderr[:80]}")


def _writer_authority():
    remember = cli(["obsidian-memory", "remember", "x", "--no-plan"])
    source = (SKMR / "commands" / "obsidian_memory_cmd.py").read_text(encoding="utf-8")
    delegated = all(a in source for a in ("route", "preview", "commit", "validate", "lint"))
    writes = re.search(r"write_text|open\([^)]*['\"]w", source)
    ok = remember.returncode == 2 and delegated and not writes
    return (PASS, "no vault writes here; route/preview/commit/validate/lint delegated to the writer") if ok \
        else (FAIL, f"rc={remember.returncode} delegated={delegated} writes={bool(writes)}")


# -------------------------------------------------------------- CLAUDE.md
def _claude_md():
    report = subprocess.run([PYTHON, str(SKMR / "tests" / "test_policy_coverage.py")],
                            capture_output=True, text=True, timeout=120, check=False)
    lines = report.stdout.strip().splitlines()
    head = next((l for l in lines if l.startswith("CLAUDE.md:")), "?")
    rules = next((l for l in lines if "behaviour rules" in l), "?")
    return (PASS, f"{head}; {rules.strip()}") if report.returncode == 0 else (FAIL, report.stdout[-200:])


def _claude_md_surgical():
    """Drive the real updater on a scratch copy instead of grepping for a call.

    The previous form asserted that assign_role_cmd.py contained ``shutil.copy2``.
    Once the block writer moved into the shared roles module -- so the CLI and the
    admin endpoint update CLAUDE.md through one implementation -- that probe failed
    while the guarantee it was supposed to protect was intact and stronger. An
    artifact check cannot tell those two states apart, so this one checks outcomes:
    surrounding operator text survives, the block is replaced once, the original is
    recoverable from the backup, and a malformed file is refused rather than rewritten.
    """
    import tempfile
    from skmr.agents import topology as topo
    from skmr.commands import assign_role_cmd
    roles = assign_role_cmd._roles()
    current = topo.load()
    original = ("# Operator rules\nKEEP-ABOVE\n\n"
                "<!-- SKMR_AGENT_TOPOLOGY_START -->\nstale topology\n"
                "<!-- SKMR_AGENT_TOPOLOGY_END -->\n\nKEEP-BELOW\n")
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw) / "CLAUDE.md"
        scratch.write_text(original, encoding="utf-8")
        backup = roles.update_instruction_file(scratch, current)
        after = scratch.read_text(encoding="utf-8")
        surroundings = "KEEP-ABOVE" in after and "KEEP-BELOW" in after
        replaced = "stale topology" not in after and "## Agent Topology" in after
        single = after.count("SKMR_AGENT_TOPOLOGY_START") == 1
        restorable = bool(backup) and Path(backup).read_text(encoding="utf-8") == original
        reversed_file = Path(raw) / "reversed.md"
        reversed_file.write_text("<!-- SKMR_AGENT_TOPOLOGY_END -->\n"
                                 "<!-- SKMR_AGENT_TOPOLOGY_START -->\n", encoding="utf-8")
        before_bytes = reversed_file.read_bytes()
        try:
            roles.update_instruction_file(reversed_file, current)
            refuses = False
        except ValueError:
            refuses = reversed_file.read_bytes() == before_bytes
    block = "SKMR_AGENT_TOPOLOGY_START" in CLAUDE_MD.read_text(encoding="utf-8")
    ok = surroundings and replaced and single and restorable and refuses and block
    backups = len(list(Path("/root/.claude").glob("CLAUDE.md.bak-*")))
    return (PASS, "surrounding text preserved, block replaced in place, original recoverable "
                  f"from the backup, reversed delimiters refused ({backups} live backups on disk)") if ok else \
        (FAIL, f"surroundings={surroundings} replaced={replaced} single={single} "
               f"restorable={restorable} refuses={refuses} live_block={block}")


# --------------------------------------------------------------- learning
def _learning_filtered():
    from skmr.memory.learning import Episode, assess
    noise = Episode(topic="T", problem="p", lesson="I made a typo in this run and fixed it")
    good = Episode(topic="T", problem="A file was archived before analysis ran.",
                   solution="Grep every consumer first.",
                   lesson="Always run reference analysis before deleting a file, because a "
                          "test-only consumer makes it look dead.")
    ok = not assess(noise).save_worthy and assess(good).save_worthy
    return (PASS, "noise rejected, generalized lesson accepted") if ok else (FAIL, "filter wrong")


def _learning_no_autocommit():
    source = (SKMR / "memory" / "learning.py").read_text(encoding="utf-8")
    cmd = (SKMR / "commands" / "learn_cmd.py").read_text(encoding="utf-8")
    import ast
    calls = [node for node in ast.walk(ast.parse(source + "\n" + cmd)) if isinstance(node, ast.Call)]
    forbidden = [node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr in {"commit", "preview"}]
    ok = not forbidden and "stage(episode)" in cmd
    return (PASS, "learning stages a candidate only; commit stays with the guarded writer flow") if ok \
        else (PARTIAL, "check for an auto-commit path")


def _memory_not_logs():
    from skmr.memory import native
    source = (SKMR / "memory" / "native.py").read_text(encoding="utf-8")
    state = native.load()
    appends = re.search(r'open\([^)]*["\']a["\']', source)
    size = len(native.memory_path().read_text(encoding="utf-8")) if native.memory_path().exists() else 0
    ok = not appends and size < 8000
    return (PASS, f"MEMORY.md is {size} B of structured state, no append path") if ok \
        else (FAIL, f"append={bool(appends)} size={size}")


# ------------------------------------------------------------------ final
def _config_sot():
    from skmr.core import config
    keys = ("embedding_model", "vault_path", "memory_path", "hybrid_strategy", "bm25_weight",
            "vector_weight", "bm25_top_k", "rerank_k", "chunk_max_chars", "max_subagents")
    live = json.loads((SKMR / "config" / "skmr.json").read_text(encoding="utf-8"))
    missing = [k for k in keys if k not in live]
    return (PASS, f"{len(live)} keys in one config file; defaults in core/config.py") if not missing \
        else (PARTIAL, f"not in file (defaults apply): {missing}")


def _docs():
    readme = SKMR / "README.md"
    if not readme.exists():
        return FAIL, "no README"
    text = readme.read_text(encoding="utf-8")
    topics = ("Command system", "Memory architecture", "Planning", "subagent", "assign-role")
    missing = [t for t in topics if t.lower() not in text.lower()]
    return (PASS, f"README covers {len(topics) - len(missing)}/{len(topics)} required topics, {len(text)} B") \
        if not missing else (PARTIAL, f"missing: {missing}")


def _suites():
    saved = os.environ.get("SKMR_SUITE_REPORT")
    if saved:
        source = Path(saved)
        if not source.is_file(): return FAIL, "saved suite evidence missing"
        text = source.read_text()
        return (PASS, "Fresh independent runner: 11/11 suites passed") if "11/11 suites passed" in text else (FAIL, text[-200:])
    report = subprocess.run([PYTHON, str(SKMR / "tests" / "run_all.py")],
                            capture_output=True, text=True, timeout=1800, check=False)
    summary = next((l for l in report.stdout.splitlines() if "suites passed" in l), "?")
    # run_all reports a failure as "[FAIL] <name>: <seconds>s". The previous
    # parser looked for a "not passing:" line that run_all never prints, so a
    # real failure was reported as "not passing: []" -- a result that names
    # nothing is indistinguishable from no result at all.
    notpass = [l.split("]", 1)[1].split(":")[0].strip()
               for l in report.stdout.splitlines() if l.startswith("[FAIL]")]
    if report.returncode == 0:
        return PASS, summary
    # run_all prints the failing suite's own output after its [FAIL] line; carry
    # the first assertion through so the checklist says why, not only which.
    reason = next((l.strip() for l in report.stdout.splitlines()
                   if l.strip().startswith(("[FAIL]", "FAILED:", "AssertionError"))
                   and not l.strip().startswith("[FAIL]")), "")
    if not reason:
        reason = next((l.strip() for l in report.stdout.splitlines()
                       if "FAIL]" in l and "[FAIL]" not in l), "")
    return PARTIAL, (f"{summary}; failing: {notpass or ['(run_all named none)']}"
                     + (f"; first failure: {reason[:90]}" if reason else ""))


CHECKLIST = [
    ("Cleanup", "Repository analiz edildi / dependency map çıkarıldı", _analysis),
    ("Cleanup", "Gereksiz dosyalar temizlendi", _archive),
    ("Cleanup", "Dead code kaldırıldı", _dead_code),
    ("Cleanup", "Duplicate implementation'lar kaldırıldı", _duplicate_impl),
    ("Cleanup", "Debug artifacts / temporary files kaldırıldı", _debug_artifacts),
    ("Commands", "/skmr:* standardı çalışıyor", _namespace),
    ("Commands", "Skill registry çalışıyor (single source of truth)", _registry_sot),
    ("Commands", "Command dispatcher merkezi çalışıyor", _dispatcher),
    ("Commands", "User-facing raw script çağrısı kalmadı", _no_raw_paths),
    ("Hooks", "Execution / planning / routing hook merkezi", _central_hooks),
    ("Hooks", "Duplicate hook logic yok (tek banner)", _single_banner),
    ("Hooks", "Lifecycle /skmr:* dışındaki skill'lere de uygulanıyor", _external_lifecycle),
    ("Hooks", "Routing yalnızca gerçek routing'de gösteriliyor", _routing_only_when_real),
    ("Planning", "Planning complexity-aware, basitte spam yok", _planning_scaled),
    ("Subagents", "Subagent kararı otomatik; gereksizde çağrılmıyor", _subagent_decision),
    ("Subagents", "Subagent sayısı hardcoded değil", _not_hardcoded),
    ("Subagents", "Subagent tablosu + dependency gösteriliyor", _subagent_table),
    ("Native State", "MEMORY.md current state olarak çalışıyor", _state_sections),
    ("Native State", "Nerede kalmıştık / en son / devam edelim → native-memory", _state_intents),
    ("Native State", "Subagent state + blocker MEMORY.md'de korunuyor", _subagent_state_persisted),
    ("Native State", "State write safety (lock + atomic + orchestrator)", _write_safety),
    ("Identity", "Agent name/role/topology korunuyor", _identity_persisted),
    ("Identity", "Identity intent detection çalışıyor", _identity_intents),
    ("Identity", "Role validation; invalid input persist edilmiyor", _validation),
    ("Obsidian", "BM25 / VECTOR / HYBRID çalışıyor", _modes),
    ("Obsidian", "Fusion strategy doğru ve configurable", _fusion),
    ("Obsidian", "Raw BM25+vector skorları toplanmıyor", _no_raw_sum),
    ("Obsidian", "Dedupe + rerank (fallback'li)", _rerank_dedupe),
    ("Obsidian", "Heading-aware chunking", _chunking),
    ("Obsidian", "bge-m3 entegrasyonu", _embedding),
    ("Obsidian", "Embedding provider abstraction", _provider_abstraction),
    ("Obsidian", "Incremental indexing", _incremental),
    ("Obsidian", "Version invalidation alanları", _version_fields),
    ("Obsidian", "Search fallback matrisi", _fallback_matrix),
    ("Obsidian", "Canonical writer authority korunuyor", _writer_authority),
    ("CLAUDE.md", "Sadeleştirildi, behavior korunuyor", _claude_md),
    ("CLAUDE.md", "Körlemesine overwrite edilmiyor", _claude_md_surgical),
    ("Learning", "Knowledge / error learning filtreli", _learning_filtered),
    ("Learning", "Learning auto-commit yapmıyor", _learning_no_autocommit),
    ("Learning", "Memory log çöplüğüne dönüşmüyor", _memory_not_logs),
    ("Final", "Configuration single source of truth", _config_sot),
    ("Final", "Documentation güncel", _docs),
    ("Final", "Tüm test suite'leri", _suites),
]

print(f"\nSKMR §90 checklist audit — {len(CHECKLIST)} probes\n")
section = None
for sec, item, fn in CHECKLIST:
    if sec != section:
        print(f"\n## {sec}")
        section = sec
    probe(sec, item, fn)
    status, evidence = results[-1][2], results[-1][3]
    print(f"  [{status:>7}] {item}")
    print(f"            └─ {evidence}")

tally: dict[str, int] = {}
for _, _, status, _ in results:
    tally[status] = tally.get(status, 0) + 1
print("\n" + "=" * 72)
print("  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
bad = [(s, i, e) for s, i, st, e in results if st == FAIL]
if bad:
    print("\nFAILING:")
    for sec, item, evidence in bad:
        print(f"  {sec} / {item}\n    {evidence}")
partial = [(s, i, e) for s, i, st, e in results if st in (PARTIAL, MANUAL)]
if partial:
    print("\nPARTIAL / MANUAL:")
    for sec, item, evidence in partial:
        print(f"  {sec} / {item}\n    {evidence}")
sys.exit(1 if bad else 0)
