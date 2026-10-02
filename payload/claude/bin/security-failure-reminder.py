#!/usr/bin/env python3
"""Add minimal recovery context only when the current turn is security work."""

import hashlib
import json
import os
import sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("SKMR_STATE_DIR", "/root/.claude/state")) / "skmr-active-sessions"


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    session_id = str(event.get("session_id") or "")
    path = STATE_DIR / f"{hashlib.sha256(session_id.encode()).hexdigest()[:24]}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not state.get("active") or state.get("session_id") != session_id:
        return
    message = (
        "SKMR: distinguish an operational tool error from a failed security hypothesis. "
        "Fix operational errors locally; retrieve only if a named knowledge gap would change the next decision."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": message}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
