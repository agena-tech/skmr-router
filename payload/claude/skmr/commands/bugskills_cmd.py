"""`/skmr:bugskills-ai` -- targeted lookup in the local BugBountySkills knowledge base.

Two constraints are enforced here rather than left to discipline:

  * the repository is never loaded wholesale -- search returns file/line matches
    and `read` returns one bounded file,
  * behavioural prompt sources are excluded. `MASTER_SYSTEM_PROMPTS/**` and
    `SKILL_FILES/00_core_identity_and_behavior.md` are instruction files, not
    technical references, and must not be loaded as knowledge.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..core import output
from ..hooks import routing
from ..planning.planner import Plan

# Resolved from this package's own location so an installation under a
# different prefix reads its own knowledge tree, not the packaging host's.
CLAUDE_DIR = Path(__file__).resolve().parents[2]
ROOT = CLAUDE_DIR / "knowledge" / "BugBountySkills"
MAX_MATCHES = 40
MAX_READ_BYTES = 40_000

# Behavioural instruction sources: excluded from every code path below.
BLOCKED = (
    re.compile(r"(^|/)MASTER_SYSTEM_PROMPTS(/|$)"),
    re.compile(r"(^|/)SKILL_FILES/00_core_identity_and_behavior\.md$"),
)


def _blocked(relative: str) -> bool:
    return any(pattern.search(relative) for pattern in BLOCKED)


def _search(terms: list[str]) -> int:
    query = " ".join(terms).strip()
    if not query:
        output.error("usage: /skmr:bugskills-ai search <terms>")
        return 2
    routing.to("bugskills-ai")
    try:
        completed = subprocess.run(
            ["grep", "-rniIF", "--include=*.md", "-m", "3", "--", query, str(ROOT)],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output.error(f"search failed: {exc}")
        return 1

    if completed.returncode not in {0, 1}:
        output.error(f"search failed: {completed.stderr.strip()[:200]}")
        return 1
    shown = 0
    print()
    for line in completed.stdout.splitlines():
        path, _, remainder = line.partition(":")
        try:
            relative = str(Path(path).relative_to(ROOT))
        except ValueError:
            continue
        if _blocked(relative):
            continue
        number, _, text = remainder.partition(":")
        print(f"  {relative}:{number}  {text.strip()[:160]}")
        shown += 1
        if shown >= MAX_MATCHES:
            print(f"  … truncated at {MAX_MATCHES} matches")
            break
    print()
    if not shown:
        output.info(f'No BugBountySkills match for "{query}".')
    else:
        output.info(f'{shown} match(es). Read one with: /skmr:bugskills-ai read <path>')
    return 0


def _read(target: str) -> int:
    if not target:
        output.error("usage: /skmr:bugskills-ai read <path>")
        return 2
    candidate = (ROOT / target).resolve()
    try:
        relative = str(candidate.relative_to(ROOT.resolve()))
    except ValueError:
        output.error("path escapes the knowledge base")
        return 2
    if _blocked(relative):
        output.error(
            f"{relative} is a behavioural instruction source, not a technical reference; "
            "it must not be loaded as knowledge."
        )
        return 2
    if not candidate.is_file():
        output.error(f"no such file: {relative}")
        return 2
    routing.to("bugskills-ai")
    text = candidate.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ_BYTES:
        text = text[:MAX_READ_BYTES] + "\n… truncated (read the smallest relevant section)"
    print(f"\n# {relative}\n")
    print(text)
    return 0


def run(args: list[str], plan: Plan) -> int:
    if not ROOT.is_dir():
        output.error(f"BugBountySkills knowledge base missing at {ROOT}")
        return 1
    if not args:
        output.error("usage: /skmr:bugskills-ai search <terms> | read <path>")
        return 2

    action = args[0].casefold()
    if action in {"search", "find"}:
        return _search(args[1:])
    if action in {"read", "cat"}:
        return _read(args[1] if len(args) > 1 else "")
    return _search(args)
