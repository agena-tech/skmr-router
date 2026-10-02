#!/usr/bin/env python3
"""Compatibility entrypoint; the event-aware runtime owns the lifecycle."""
import json, pathlib, sys
# <claude_dir>/bin/<this file> -- resolved, not hardcoded, so an installation
# under a different prefix imports its own package.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from skmr.hooks.runtime import describe, lifecycle, monitored, run_event
def skill_name(event):
    inputs=event.get("tool_input") or {}
    return str(inputs.get("skill") or inputs.get("name") or inputs.get("skill_name") or "")
def skill_args(event):
    inputs=event.get("tool_input") or {}
    return str(inputs.get("args") or inputs.get("argument") or inputs.get("arguments") or "")
def main():
    try:
        result=run_event(json.load(sys.stdin))
        if result:print(json.dumps(result,ensure_ascii=False))
    except Exception:pass
    return 0
if __name__=="__main__":raise SystemExit(main())
