"""`/skmr:plan` -- run the planning hook against an arbitrary task.

Useful on its own (what would SKMR do with this task?) and as the way to register
a subagent set into MEMORY.md before orchestration starts, with `--register`.
"""
from __future__ import annotations

from ..agents import orchestrator
from ..core import output
from ..planning.planner import Plan


def run(args: list[str], plan: Plan) -> int:
    register = "--register" in args
    task = " ".join(arg for arg in args if arg != "--register").strip().strip('"')
    if not task:
        output.error('usage: /skmr:plan "task description" [--register]')
        return 2

    built = plan

    if not register:
        return 0
    if not built.subagents:
        output.info("Nothing to register: the plan needs no subagents.")
        return 0

    rows = orchestrator.register(built)
    output.info(f"Registered {len(rows)} subagent(s) in the working state.")
    orchestrator.summary()
    return 0
