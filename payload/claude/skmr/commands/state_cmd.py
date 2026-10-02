"""`/skmr:state` -- read and update the native MEMORY.md working state.

This is the command behind "where did we stop?". It reads the state file rather
than guessing, and every write goes through the locked mutate path.
"""
from __future__ import annotations

from ..core import output
from ..hooks import routing
from ..memory import native
from ..planning.planner import Plan

FIELD_ALIASES = {
    "task": "Active Task",
    "phase": "Phase",
    "last": "Last Completed",
    "current": "Current Work",
    "next": "Next",
    "problems": "Open Problems",
    "blockers": "Open Problems",
}


def _show() -> int:
    routing.to("native-memory")
    state = native.load()
    path = native.memory_path()
    if not path.exists():
        output.warning(f"{path} does not exist yet; no working state recorded.")
        return 0
    print(f"\n# Working state ({path})\n")
    rendered = False
    for name in native.MANAGED:
        body = state.get(name).strip()
        if not body or body == "-":
            continue
        rendered = True
        print(f"## {name}\n{body}\n")
    for name in state.order:
        if name in native.MANAGED:
            continue
        body = state.get(name).strip()
        if body:
            print(f"## {name}\n{body}\n")
            rendered = True
    if state.preamble.strip():
        print(f"## Notes\n{state.preamble.strip()}\n")
        rendered = True
    if not rendered:
        output.info("Working state is empty.")
    return 0


def run(args: list[str], plan: Plan) -> int:
    action = (args[0].casefold() if args else "show")

    if action in {"show", "get", ""}:
        return _show()

    if action == "agent":
        from ..agents import orchestrator
        if len(args) < 3:
            output.error("usage: /skmr:state agent <id> <status> [progress]")
            return 2
        try:
            row = orchestrator.transition(args[1], args[2], " ".join(args[3:]))
        except orchestrator.TransitionError as exc:
            output.error(str(exc)); return 2
        output.info(f"{row.agent_id}: {row.status}")
        return 0
    if action == "result":
        import json
        from ..agents import orchestrator
        if len(args) != 2:
            output.error("usage: /skmr:state result '<structured result JSON>'")
            return 2
        try:
            result = orchestrator.Result(**json.loads(args[1]))
            orchestrator.record(result)
        except (ValueError, TypeError) as exc:
            output.error(str(exc)); return 2
        output.info("Structured agent result recorded; verify critical claims independently.")
        return 0
    if action == "agents":
        routing.to("native-memory")
        from ..agents import orchestrator
        orchestrator.summary()
        return 0

    if action == "clear-agents":
        from ..agents import orchestrator
        orchestrator.clear()
        output.info("Subagent state cleared.")
        return 0

    if action == "set":
        if len(args) < 3:
            output.error('usage: /skmr:state set <section> "<text>"')
            return 2
        section = FIELD_ALIASES.get(args[1].casefold(), args[1])
        body = " ".join(args[2:])
        native.mutate(lambda state: state.set(section, body))
        output.info(f'Updated "{section}".')
        return 0

    if action in FIELD_ALIASES:
        if len(args) < 2:
            output.error(f'usage: /skmr:state {action} "<text>"')
            return 2
        section = FIELD_ALIASES[action]
        body = " ".join(args[1:])
        native.mutate(lambda state: state.set(section, body))
        output.info(f'Updated "{section}".')
        return 0

    output.error(f'unknown action "{action}". Use: show | set <section> <text> | <field> <text> | agent <id> <status> [progress] | result <json> | agents | clear-agents')
    return 2
