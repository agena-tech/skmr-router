"""`/skmr:learn` -- assess an episode and stage it as a durable-knowledge candidate.

It filters first and stages second. A rejected episode prints why, so the outcome
is a judgement you can argue with rather than a silent discard. Nothing is
committed here: staging hands the candidate to the canonical writer's
`preview -> commit` flow, which still applies the acceptance gate.
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get('AGENTCOMM_LIB', str(Path(__file__).resolve().parents[2] / 'lib')))
from skmr_permissions import can_write

from ..core import config, output
from ..hooks import routing
from ..memory.learning import ERROR, KNOWLEDGE, Episode, assess, render, route_note, stage
from ..planning.planner import Plan

USAGE = (
    'usage: /skmr:learn [--error] --topic "T" --problem "P" --solution "S" --lesson "L"\n'
    '                   [--evidence kind=locator] [--link "Note"] [--tag t] [--verified] [--dry-run]'
)


def _flags(args: list[str]) -> tuple[dict[str, list[str]], set[str]]:
    values: dict[str, list[str]] = {}
    switches: set[str] = set()
    i = 0
    while i < len(args):
        token = args[i]
        if not token.startswith("--"):
            i += 1
            continue
        name = token[2:]
        if name in {"error", "verified", "dry-run"}:
            switches.add(name)
            i += 1
            continue
        if i + 1 < len(args):
            values.setdefault(name, []).append(args[i + 1])
            i += 2
            continue
        i += 1
    return values, switches


def run(args: list[str], plan: Plan) -> int:
    values, switches = _flags(args)
    if not values:
        output.info(USAGE)
        return 2

    missing = [k for k in ("topic", "problem", "lesson") if k not in values]
    if missing:
        output.error(f"missing required flag(s): {', '.join('--' + m for m in missing)}\n{USAGE}")
        return 2

    evidence = []
    for item in values.get("evidence", []):
        kind, _, locator = item.partition("=")
        if not kind or not locator:
            output.error(f"--evidence expects kind=locator, got {item!r}")
            return 2
        evidence.append({"kind": kind.strip(), "locator": locator.strip()})

    episode = Episode(
        topic=values["topic"][0],
        problem=values["problem"][0],
        solution=values.get("solution", [""])[0],
        lesson=values["lesson"][0],
        kind=ERROR if "error" in switches else KNOWLEDGE,
        evidence=evidence,
        verified="verified" in switches,
        explicit_user_request=True,
        links=values.get("link", []),
        tags=values.get("tag", []),
    )

    verdict = assess(episode)
    print()
    print(f"kind      : {episode.kind}")
    print(f"topic     : {episode.topic}")
    print(f"assessment: {'SAVE-WORTHY' if verdict.save_worthy else 'REJECTED'} — {verdict.explain()}")
    print()

    if not verdict.save_worthy:
        output.info("Not staged. Working context is the right home for this; "
                    "durable knowledge must be generalized and reusable.")
        return 0

    print(render(episode))
    print()

    if "dry-run" in switches:
        output.info("Dry run: nothing staged.")
        return 0

    routing.to("obsidian-memory")
    path = stage(episode)
    output.info(f"Candidate staged at {path}")
    output.info(route_note(episode))
    if not can_write():
        send = os.environ.get('AGENTCOMM_SEND', '/root/agentcomm/send.py')
        try:
            delivered = subprocess.run([str(config.get('python')), send, 'candidate', str(path)], check=False)
        except OSError as exc:
            output.error(f'Candidate remains staged; delegation failed: {exc}')
            return 1
        if delivered.returncode:
            output.error('Candidate remains staged; delegation was unavailable or rejected. No permanent save occurred.')
            return delivered.returncode
        output.info('Candidate delegated to the configured WRITE peer. Staging and delivery are not saving; wait for a confirmed canonical commit.')
        return 0
    print()
    print("Complete the guarded write with the canonical writer:")
    print(f"  {config.get('python')} {config.get('writer')} preview {path}")
    print("  /skmr:obsidian-memory commit <token returned by preview>")
    print()
    output.warning("Staging is not saving. The writer's acceptance gate still decides.")
    return 0
