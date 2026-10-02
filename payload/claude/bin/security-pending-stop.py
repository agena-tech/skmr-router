#!/usr/bin/env python3
"""Block Stop only for corrupt state or an unfinished atomic SKMR transaction."""

import json
import os
import sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("SKMR_STATE_DIR", "/root/.claude/state"))
QUEUE = STATE_DIR / "security-new-reports.json"
TRANSACTIONS = STATE_DIR / "skmr-obsidian-transactions"
REVIEW_TRANSACTIONS = STATE_DIR / "security-report-review-transactions"


def queue_error() -> str | None:
    if not QUEUE.exists():
        return None
    if QUEUE.is_symlink() or not QUEUE.is_file():
        return "HackerOne queue is not a regular file"
    try:
        payload = json.loads(QUEUE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return f"HackerOne queue is unreadable: {error}"
    reports = payload.get("reports") if isinstance(payload, dict) else None
    if not isinstance(reports, list) or any(not isinstance(item, dict) for item in reports):
        return "HackerOne queue has an invalid reports list"
    ids = [str(item.get("id") or "") for item in reports]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        return "HackerOne queue has empty or duplicate report IDs"
    return None


def pending_transactions() -> list[str]:
    result = []
    for directory in (TRANSACTIONS, REVIEW_TRANSACTIONS):
        if directory.is_dir():
            result.extend(str(path) for path in directory.glob("*.json") if path.is_file())
    return sorted(result)


def main() -> None:
    try:
        json.load(sys.stdin)
    except json.JSONDecodeError:
        pass
    error = queue_error()
    transactions = pending_transactions()
    if not error and not transactions:
        return
    reasons = []
    if error:
        reasons.append(error)
    if transactions:
        reasons.append("unfinished SKMR transaction: " + ", ".join(transactions[:4]))
    print(json.dumps({"decision": "block", "reason": " | ".join(reasons)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
