"""`/skmr:help` -- the command list, derived from the registry at runtime.

Nothing here restates the command set; duplicating it in documentation is how
such lists go stale.
"""
from __future__ import annotations

from ..core import registry
from ..planning.planner import Plan


def run(args: list[str], plan: Plan) -> int:
    wanted = args[0].casefold() if args else ""
    if wanted:
        skill = registry.resolve(wanted)
        if skill is None:
            print(f'No SKMR command named "{wanted}".')
            return 2
        print(f"/skmr:{skill.name} — {skill.summary}")
        print(f"\nUsage:\n  {skill.usage or f'/skmr:{skill.name}'}")
        if skill.aliases:
            print(f"\nAliases: {', '.join(skill.aliases)}")
        print(f"Planning depth: {skill.planning}")
        print(f"Subagents: {'allowed' if skill.subagents else 'not used'}")
        if skill.memory:
            print(f"Memory: {', '.join(skill.memory)}")
        return 0

    print("\nSKMR commands\n")
    width = max(len(s.name) for s in registry.all_skills()) + 2
    for skill in registry.all_skills():
        print(f"  /skmr:{skill.name.ljust(width)}{skill.summary}")
    print("\nRun /skmr:help <command> for usage.\n")
    return 0
