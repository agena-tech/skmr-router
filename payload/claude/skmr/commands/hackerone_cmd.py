"""`/skmr:hackerone-reports` -- disclosed-report lookup over the local dataset.

A thin, stable surface over `search_reports.py`; the dataset and its search logic
are unchanged. The point is that the operator no longer types a python path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..core import config, output
from ..hooks import routing
from ..planning.planner import Plan

# Resolved from this package's own location so an installation under a
# different prefix reads its own knowledge tree, not the packaging host's.
CLAUDE_DIR = Path(__file__).resolve().parents[2]
SEARCH = CLAUDE_DIR / "knowledge" / "bugskill-ai" / "search_reports.py"
DATASET = CLAUDE_DIR / "knowledge" / "bugskill-ai-data" / "hackerone_public_reports.json"


def _run(argv: list[str]) -> int:
    if not SEARCH.exists():
        output.error(f"HackerOne dataset search tool missing at {SEARCH}")
        return 1
    # The tool resolves its dataset relative to the working directory, so calling
    # it from anywhere else fails with "dataset not found". Pass the path
    # explicitly unless the caller already chose one.
    if "-d" not in argv and "--dataset" not in argv:
        if not DATASET.exists():
            output.error(
                f"HackerOne dataset missing at {DATASET}; "
                f"download it with `{config.get('python')} {SEARCH.parent / 'hackerone_public.py'}`"
            )
            return 1
        argv = [*argv, "-d", str(DATASET)]
    routing.to("hackerone-reports")
    try:
        completed = subprocess.run(
            [str(config.get("python")), str(SEARCH), *argv],
            cwd=str(SEARCH.parent), check=False,
        )
        return completed.returncode
    except OSError as exc:
        output.error(f"cannot run the dataset search tool: {exc}")
        return 1


def _latest(count: int) -> int:
    import importlib.util, json
    from datetime import datetime, timezone
    if count <= 0:
        output.error("report count must be positive")
        return 2
    try:
        reports = json.loads(DATASET.read_text()).get("reports", [])
        def date_key(report):
            stamp = str(report.get("attributes", {}).get("disclosed_at") or "")
            try:
                moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if moment.tzinfo is None: moment = moment.replace(tzinfo=timezone.utc)
                return moment.timestamp()
            except ValueError: return float("-inf")
        reports = sorted(reports, key=date_key, reverse=True)
        spec = importlib.util.spec_from_file_location("skmr_report_renderer", SEARCH)
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        routing.to("hackerone-reports")
        renderer.search_reports(reports, limit=count)
        return 0
    except (OSError, ValueError, AttributeError) as exc:
        output.error(f"cannot read disclosed reports: {exc}")
        return 1


def run(args: list[str], plan: Plan) -> int:
    if not args:
        return _run(["--stats"])

    action = args[0].casefold()
    rest = args[1:]

    if action in {"pending", "review", "resolve", "review-result"}:
        from ..hooks import report_review
        import json
        if action == "pending":
            try:
                reports=report_review.load_queue()
                print(json.dumps({"count":len(reports),"reports":reports},ensure_ascii=False,indent=2))
                return 0
            except ValueError as exc:
                output.error(str(exc));return 1
        if action != "review" and len(rest) != 1:
            output.error("usage: /skmr:hackerone-reports " + action + " <review-token>");return 2
        if action == "review" and (len(rest)>1 or (rest and not rest[0].isdigit())):
            output.error("usage: /skmr:hackerone-reports review [report-id]");return 2
        queue=Path('/root/.claude/bin/security-report-queue.py')
        argv=[str(config.get('python')),str(queue),{'review':'show','resolve':'resolve','review-result':'result'}[action],*rest]
        result=subprocess.run(argv,check=False)
        if result.returncode==0 and action=='review':print(report_review.protocol())
        return result.returncode

    if action in {"latest", "last"}:
        count = "10"
        if rest:
            candidate = rest[0]
            if candidate.isdigit():
                count = candidate
            elif action == "last":
                output.error("usage: /skmr:hackerone-reports last <n>")
                return 2
        elif action == "latest":
            count = "10"
        # Sort by disclosure timestamp, independently of vendor JSON order.
        return _latest(int(count))

    if action in {"search", "find"}:
        if not rest:
            output.error("usage: /skmr:hackerone-reports search <terms>")
            return 2
        # Preserve the dataset tool's existing severity/CWE/program/bounty flags.
        split = next((i for i, token in enumerate(rest) if token.startswith("-")), len(rest))
        return _run(([" ".join(rest[:split])] if split else []) + rest[split:])

    if action == "report":
        if not rest:
            output.error("usage: /skmr:hackerone-reports report <id>")
            return 2
        return _run(["--id", rest[0]])

    if action == "stats":
        return _run(["--stats"])

    # Anything else is treated as a free-text query, which is what an operator
    # typing `/skmr:hackerone-reports oauth` means.
    return _run([" ".join(args)])
