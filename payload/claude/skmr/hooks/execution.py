"""Execution hook -- the one place the `Executing:` banner is produced.

No skill prints its own banner. The dispatcher calls `announce()` once per
command, which keeps the wording uniform and makes double-printing structurally
impossible.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..core import config, output

RUN_LOG_LIMIT = 200


def announce(skill: str) -> float:
    output.executing(skill)
    return time.time()


def finish(skill: str, started: float, ok: bool, detail: str = "") -> None:
    if ok:
        output.completed(skill)
    _record(skill, time.time() - started, ok, detail)


def _record(skill: str, duration: float, ok: bool, detail: str) -> None:
    """Append one line of run telemetry, capped so it never becomes a log dump."""
    try:
        target = Path(config.state_dir()) / "runs.jsonl"
        entry = {
            "skill": skill,
            "ok": ok,
            "seconds": round(duration, 3),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if detail:
            entry["detail"] = detail[:200]
        lines = []
        if target.exists():
            lines = target.read_text(encoding="utf-8").splitlines()[-(RUN_LOG_LIMIT - 1):]
        lines.append(json.dumps(entry, ensure_ascii=False))
        tmp = target.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        # Telemetry is never allowed to fail a command.
        pass
