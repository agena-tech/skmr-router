#!/usr/bin/env python3
"""Event-aware SKMR hook entrypoint. Outputs only valid Claude context JSON."""
import json, sys
sys.path.insert(0,'/root/.claude')
def main():
    try:
        from skmr.hooks.runtime import run_event
        event=json.load(sys.stdin); result=run_event(event)
        if result:print(json.dumps(result,ensure_ascii=False),flush=True)
    except Exception as exc:
        # Optional integration must not crash Claude; failure is visible, never a success claim.
        print('[SKMR]: Warning: runtime hook unavailable: '+str(exc)[:200],file=sys.stderr)
    return 0
if __name__=='__main__':raise SystemExit(main())
