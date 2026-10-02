"""Shared helpers for the Eliza <-> Sondra agent communication layer.

Identity, config, terminal formatting and small utilities used by both the
FastAPI server (server.py) and the CLI client (send.py). No vault paths are
hardcoded here; everything comes from agent.conf so the same code runs on
Eliza (L0) and Sondra (L1) with only the config differing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import zlib
from pathlib import Path

CONF_PATH = Path(os.environ.get("AGENTCOMM_CONF", "/root/agentcomm/agent.conf"))
BASE_DIR = CONF_PATH.parent

# --- terminal color --------------------------------------------------------
# Only the peer/self *label* ("SONDRA:", "ELIZA:") is ever colored, and the code
# is generated locally from this trusted table — never taken from the peer
# message, whose body is sanitized of control/escape bytes before display. This
# keeps a peer from injecting terminal escapes through its own text.
#
# Color is applied only on a real terminal-capable output path. Precedence:
#   NO_COLOR / AGENTCOMM_NO_COLOR set -> never color (explicit opt-out wins)
#   AGENTCOMM_FORCE_COLOR=1  -> always color (used by tests and forced feeds)
#   otherwise                -> color only when the target stream is a TTY
_ANSI_RESET = "\x1b[0m"
# Any agent name gets a colour: a fresh install names its agents freely, and a
# table keyed by two historical names leaves every other name unpainted. The
# index comes from crc32 rather than hash() because hash() is salted per process
# and would repaint the same agent on every run.
AGENT_PALETTE = (
    "32",          # green
    "38;5;208",    # orange
    "36",          # cyan
    "35",          # magenta
    "33",          # yellow
    "34",          # blue
    "38;5;141",    # violet
    "38;5;114",    # mint
)


def agent_color(agent: str) -> str:
    """Stable colour code for this name. Same name in, same colour out."""
    key = str(agent or "").strip().casefold().encode("utf-8", "replace")
    if not key:
        return AGENT_PALETTE[0]
    return AGENT_PALETTE[zlib.crc32(key) % len(AGENT_PALETTE)]
# Backwards-compatible module constants still referenced by send.py f-strings.
# DIM/RESET stay empty so existing status lines render plainly and unchanged.
RESET = ""
DIM = ""

# Strip C0 controls and DEL but keep tab (\x09) and newline (\x0a); ESC (\x1b)
# falls inside \x0b-\x1f and is therefore removed, so no escape can survive.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def load_conf() -> dict:
    return json.loads(CONF_PATH.read_text(encoding="utf-8"))


def sanitize(text: object) -> str:
    """Remove control/escape bytes from untrusted peer text before display."""
    return _CONTROL.sub("", str(text))


def color_enabled(stream=None) -> bool:
    """Whether the label may be colored for this output path (see table above).

    An explicit opt-out (NO_COLOR / AGENTCOMM_NO_COLOR) always wins, including
    over AGENTCOMM_FORCE_COLOR, matching the no-color.org convention.
    """
    if os.environ.get("NO_COLOR") or os.environ.get("AGENTCOMM_NO_COLOR"):
        return False
    if os.environ.get("AGENTCOMM_FORCE_COLOR") == "1":
        return True
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 - a stream without isatty is treated as non-TTY
        return False


def label(agent: str, *, color: bool | None = None) -> str:
    """'<NAME>:' label, colored locally only when color is enabled and known.

    The color code is derived from the agent name only, never from any message
    content.
    """
    name = f"{agent.upper()}:"
    use = color_enabled() if color is None else color
    code = agent_color(agent)
    if use and code:
        return f"\x1b[{code}m{name}{_ANSI_RESET}"
    return name


def format_incoming(sender: str, text: str, *, color: bool | None = None) -> str:
    """'SONDRA: <text>' — colored label only; body is sanitized and uncolored."""
    return f"{label(sender, color=color)} {sanitize(text)}"


def format_feed(msg: dict, *, color: bool | None = None) -> str:
    """Live-feed line '[HH:MM:SS] SONDRA: <text>' — colored label only."""
    sender = str(msg.get("from", "?"))
    text = " ".join(sanitize(msg.get("text", "")).split())  # collapse to one line
    if len(text) > 200:
        text = text[:200] + "…"
    if msg.get("type") == "task":
        text = f"[TASK-{msg.get('task_no')}] {text}"
    stamp = ""
    ts = msg.get("ts")
    if ts:
        try:
            from datetime import datetime
            t = datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")
            stamp = f"[{t}] "
        except ValueError:
            pass
    return f"{stamp}{label(sender, color=color)} {text}"


def format_task_created(n: int) -> str:
    """Plain 'Task N Created.' (no color)."""
    return f"Task {n} Created."


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
