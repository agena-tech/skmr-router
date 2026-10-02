"""Planning hook -- central, so no skill implements planning for itself.

Output scales with the plan: a simple command gets one banner line and nothing
else, while a complex one gets numbered steps and the subagent table. This is
what keeps `/skmr:help` from printing a plan larger than its own output.
"""
from __future__ import annotations

from ..core import output
from ..core.registry import Skill
from ..planning import planner
from ..planning.planner import Plan


def run(skill: Skill, task: str, quiet: bool = False) -> Plan:
    plan = planner.build(skill, task)
    if quiet:
        return plan

    output.planning(skill.name)
    if plan.complexity == planner.SIMPLE:
        # A lightweight plan is a single line; printing six headings for
        # `/skmr:help` would be planning spam, which the policy forbids.
        output.subagents(0)
        return plan

    print()
    print(f"Plan ({plan.complexity}):")
    for i, step in enumerate(plan.steps, start=1):
        print(f"{i}. {step}")
    print()
    output.subagents(len(plan.subagents))
    if plan.subagents:
        print()
        output.table(
            ["Agent", "Role", "Task", "Depends on", "Expected Output"],
            [
                [s.agent_id, s.role, s.task, ", ".join(s.depends_on) or "-", s.expected_output]
                for s in plan.subagents
            ],
        )
        print()
    return plan
