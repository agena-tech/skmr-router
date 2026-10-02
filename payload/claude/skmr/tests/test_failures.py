#!/usr/bin/env python3
"""Failure-mode tests: every degradation must be controlled, not a crash.

Covers the cases the main system suite does not: a corrupted index, a missing
MEMORY.md, an unreachable remote peer, a failing skill handler, an unavailable
routing target, a FAILED subagent, and a single-specialist plan.

Anything this suite mutates (the index path, MEMORY.md, the config) is
snapshotted and restored in `finally`, including on failure.

    /usr/bin/python3 /root/.claude/skmr/tests/test_failures.py
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, "/root/.claude")

CLI = "/root/.claude/skmr/cli.py"
PYTHON = "/usr/bin/python3"
CONFIG = pathlib.Path(os.environ.get("SKMR_CONFIG", "/root/.claude/skmr/config/skmr.json"))

failures: list[str] = []
checks = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  [ ok ] {name}")
    else:
        failures.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run([PYTHON, CLI, *args], capture_output=True, text=True,
                          timeout=timeout, env=merged, check=False)


config_snapshot = CONFIG.read_text(encoding="utf-8")
from skmr.core import config          # noqa: E402
from skmr.memory import native        # noqa: E402

MEMORY = native.memory_path()
memory_snapshot = MEMORY.read_text(encoding="utf-8") if MEMORY.exists() else None

try:
    print("\n=== corrupted index ===")
    corrupt_dir = pathlib.Path(tempfile.mkdtemp(prefix="skmr-corrupt-"))
    corrupt = corrupt_dir / "index.sqlite3"
    corrupt.write_bytes(b"this is definitely not a sqlite database" * 20)
    cfg = json.loads(config_snapshot)
    cfg["index_path"] = str(corrupt)
    cfg["vault_path"] = str(corrupt_dir)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    result = run(["obsidian-memory", "search", "anything", "--no-plan"], env={'SKMR_VAULT': str(corrupt_dir)})
    check("corrupt index yields a controlled error, not a traceback",
          "Traceback" not in result.stderr and result.returncode != 0,
          result.stderr[:160])
    result = run(["doctor", "--no-plan"])
    check("doctor survives a corrupt index",
          "Traceback" not in result.stderr and "index" in result.stdout,
          result.stderr[:160])
    CONFIG.write_text(config_snapshot, encoding="utf-8")
    shutil.rmtree(corrupt_dir, ignore_errors=True)

    print("\n=== MEMORY.md missing ===")
    if MEMORY.exists():
        MEMORY.unlink()
    result = run(["state", "show", "--no-plan"])
    check("missing MEMORY.md warns instead of crashing",
          result.returncode == 0 and "does not exist yet" in result.stderr,
          (result.stderr or result.stdout)[:160])
    result = run(["state", "set", "task", "recreated by failure test", "--no-plan"])
    check("state write recreates the file", MEMORY.exists() and result.returncode == 0)
    check("recreated file is readable", "recreated by failure test" in native.load().get("Active Task"))
    result = run(["doctor", "--no-plan"])
    check("doctor reports native state without crashing", "native state" in result.stdout)

    print("\n=== unreachable remote peer ===")
    result = run(["send", "health", "--no-plan"], env={"AGENTCOMM_BASE": "http://127.0.0.1:9"},
                 timeout=120)
    check("send health against a dead endpoint is controlled",
          "Traceback" not in result.stderr, result.stderr[:160])
    # Identity is live state: validate the unreachable-host case in-process against
    # a temp topology file instead of running assign-role, which would rewrite the
    # real topology and CLAUDE.md.
    from skmr.agents import topology            # noqa: E402
    topology_scratch = pathlib.Path(tempfile.mkdtemp(prefix="skmr-topo-")) / "agents.json"
    scratch_cfg = json.loads(config_snapshot)
    scratch_cfg["agents_path"] = str(topology_scratch)
    CONFIG.write_text(json.dumps(scratch_cfg, indent=2), encoding="utf-8")
    config.load(refresh=True)
    config._cache["agents_path"] = str(topology_scratch)
    try:
        saved = topology.save(topology.Topology(
            local=topology.Agent(name="Probe", role="Tester", kind="local", vault_access="write"),
            remote=[topology.Agent(name="Ghost", role="Peer", host="10.255.255.1",
                                   reports_to="Probe", kind="remote")],
        ))
        check("unreachable remote host is still a valid topology entry",
              saved.remote[0].host == "10.255.255.1")
        check("scratch topology did not touch the real one",
              topology.path() == topology_scratch)
    finally:
        CONFIG.write_text(config_snapshot, encoding="utf-8")
        config.load(refresh=True)
        shutil.rmtree(topology_scratch.parent, ignore_errors=True)

    print("\n=== failing skill handler ===")
    from skmr.core import dispatcher    # noqa: E402
    from skmr.core.registry import Skill  # noqa: E402
    broken = Skill(name="broken", summary="s", handler="skmr.commands.does_not_exist:run")
    original_resolve = dispatcher.registry.resolve
    try:
        dispatcher.registry.resolve = lambda name: broken if name == "broken" else original_resolve(name)
        code = dispatcher.dispatch(["broken"])
        check("unimportable handler is a controlled failure", code == 1)
    finally:
        dispatcher.registry.resolve = original_resolve

    def explode(args, plan):
        raise RuntimeError("handler blew up")

    original_load = dispatcher._load
    try:
        dispatcher._load = lambda handler: explode
        dispatcher.registry.resolve = lambda name: broken if name == "broken" else original_resolve(name)
        code = dispatcher.dispatch(["broken"])
        check("raising handler is caught and reported", code == 1)
    finally:
        dispatcher._load = original_load
        dispatcher.registry.resolve = original_resolve

    print("\n=== routing target unavailable ===")
    result = run(["bugskills-ai", "search", "anything", "--no-plan"],
                 env={"SKMR_FORCE_MISSING_KB": "1"})
    check("bugskills routing target checked before use",
          "Traceback" not in result.stderr, result.stderr[:160])
    result = run(["hackerone-reports", "report", "000000", "--no-plan"])
    check("unknown report id is controlled", "Traceback" not in result.stderr)

    print("\n=== subagent FAILED and single specialist ===")
    from skmr.agents import orchestrator as orc   # noqa: E402
    from skmr.planning import planner             # noqa: E402

    orc.clear()
    native.mutate(lambda s: s.set_subagents([native.Subagent("solo-agent", "one surface", "PLANNED")]))
    orc.transition("solo-agent", "RUNNING")
    row = orc.transition("solo-agent", "FAILED", "tool unavailable")
    check("subagent can reach FAILED", row.status == "FAILED")
    check("FAILED state persisted",
          any(r.agent_id == "solo-agent" and r.status == "FAILED" for r in native.load().subagents()))
    requeued = orc.transition("solo-agent", "QUEUED")
    check("FAILED can be requeued for a retry", requeued.status == "QUEUED")

    terminal_blocked = False
    try:
        orc.transition("solo-agent", "RUNNING")
        orc.transition("solo-agent", "COMPLETED")
        orc.transition("solo-agent", "RUNNING")
    except orc.TransitionError:
        terminal_blocked = True
    check("COMPLETED is terminal", terminal_blocked)

    single = Skill(name="single", summary="s", handler="x:y", planning="medium", subagents=True)
    plan = planner.build(single, "one single indivisible surface")
    check("single-unit task proposes no subagents", not plan.subagents,
          f"got {len(plan.subagents)}")
    # Two units at medium depth stay serial on purpose: orchestration overhead is
    # only accepted once the task is actually complex.
    plan = planner.build(single, "surface one, surface two")
    check("two units at medium depth stay serial", not plan.subagents,
          f"got {len(plan.subagents)}")
    # ... but an explicitly broad task with two units does fan out, plus a verifier.
    plan = planner.build(single, "audit all of surface one, and surface two")
    check("keyword-escalated two-unit task fans out with a verifier",
          len(plan.subagents) == 3, f"got {len(plan.subagents)}")
    plan = planner.build(single, "surface one, surface two, surface three")
    check("three units escalate to complex with a verifier",
          len(plan.subagents) == 4, f"got {len(plan.subagents)}")

    print("\n=== embedding outage does not touch unrelated subsystems ===")
    dead = {"OLLAMA_HOST": "http://127.0.0.1:9"}
    check("assign-role unaffected by embedding outage",
          run(["assign-role", "--show", "--no-plan"], env=dead).returncode == 0)
    check("state unaffected by embedding outage",
          run(["state", "show", "--no-plan"], env=dead).returncode == 0)
    result = run(["doctor", "--no-plan"], env=dead)
    check("doctor flags embedding but stays operational",
          "embedding" in result.stdout and "Traceback" not in result.stderr)
finally:
    CONFIG.write_text(config_snapshot, encoding="utf-8")
    config.load(refresh=True)
    try:
        from skmr.agents import orchestrator as _orc
        _orc.clear()
    except Exception:
        pass
    if memory_snapshot is not None:
        MEMORY.write_text(memory_snapshot, encoding="utf-8")
    print("\n  (config and MEMORY.md restored)")

print(f"\n{checks - len(failures)}/{checks} checks passed")
for name in failures:
    print(f"  FAILED: {name}")
sys.exit(1 if failures else 0)
