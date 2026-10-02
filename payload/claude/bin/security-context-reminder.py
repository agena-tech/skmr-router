#!/usr/bin/env python3
"""Pass only minimal active SKMR state to a spawned subagent."""

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
    if event.get("hook_event_name") != "SubagentStart":
        return
    session_id = str(event.get("session_id") or "")
    path = STATE_DIR / f"{hashlib.sha256(session_id.encode()).hexdigest()[:24]}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not state.get("active") or state.get("session_id") != session_id:
        return
    message = "SKMR active in the parent turn. Use the parent's routed context; retrieve only for a named gap and never persist private target data."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": message}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
