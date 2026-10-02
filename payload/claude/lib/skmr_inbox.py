"""Compatibility wrapper for SKMR hooks.

AgentComm delivery is intentionally NOT consumed here.

The canonical inbound path is agent-message-watcher.py (asyncRewake).
A previous inbox-consuming implementation raced the watcher by advancing the
shared receive cursor before the awakened Claude turn processed the message.
Keep this wrapper transparent so existing hooks may still import
run_with_inbox() without becoming a second inbox consumer.
"""

from __future__ import annotations

import contextlib
import io


def run_with_inbox(main, event):
    """Run the wrapped hook and preserve stdout unchanged.

    `event` is accepted for backwards compatibility. Inter-agent delivery is
    owned exclusively by the authenticated asyncRewake watcher.
    """
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            main()
    finally:
        # Emitted in every path: when main() raised, the old code never reached
        # this line and silently swallowed output the hook had already produced,
        # including a decision JSON it had finished printing. The exception still
        # propagates; only the loss of produced output is fixed.
        print(captured.getvalue(), end="")
