"""Central command dispatcher.

Every `/skmr:<command>` enters here, which is what lets the lifecycle live in one
place instead of being re-implemented per skill:

    command -> execution hook -> planning hook -> [orchestration] -> handler
            -> state update -> completion

Handlers are imported lazily by their `module:function` string so a subsystem
that fails to import (a missing optional dependency, say) only breaks its own
command.
"""
from __future__ import annotations

import importlib
import sys
from typing import Callable

from . import output, registry
from ..hooks import execution, planning, routing


class SkmrError(RuntimeError):
    """A controlled, reportable failure. Printed as `[SKMR]: Error: ...`."""


def _load(handler: str) -> Callable[..., int]:
    module_name, _, func_name = handler.partition(":")
    module = importlib.import_module(module_name)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise SkmrError(f"handler {handler} is not callable")
    return func


def dispatch(argv: list[str]) -> int:
    if not argv:
        argv = ["help"]
    name, args = argv[0], argv[1:]

    skill = registry.resolve(name)
    if skill is None:
        output.error(
            f'unknown command "{name}". Run /skmr:help for the command list.'
        )
        return 2

    started = execution.announce(skill.name)
    quiet_plan = "--no-plan" in args
    if quiet_plan:
        args = [a for a in args if a != "--no-plan"]

    ok = False
    detail = ""
    try:
        plan = planning.run(skill, " ".join(args), quiet=quiet_plan)
        handler = _load(skill.handler)
        code = handler(args, plan)
        ok = code == 0
        if not ok:
            detail = f"exit={code}"
    except SkmrError as exc:
        output.error(str(exc))
        code, detail = 1, str(exc)
    except KeyboardInterrupt:
        output.error("interrupted")
        code, detail = 130, "interrupted"
    except Exception as exc:  # pragma: no cover - surfaced, never swallowed
        output.error(f"{type(exc).__name__}: {exc}")
        code, detail = 1, f"{type(exc).__name__}: {exc}"
    finally:
        routing.reset()

    execution.finish(skill.name, started, ok, detail)
    return code


def main(argv: list[str] | None = None) -> int:
    return dispatch(list(argv if argv is not None else sys.argv[1:]))
