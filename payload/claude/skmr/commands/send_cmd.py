"""`/skmr:send` -- the user-facing face of AgentComm.

The transport itself is unchanged: this forwards to `/root/agentcomm/send.py`,
which owns authentication, cursors and the tunnel. What changes is the interface --
the operator types a `/skmr:` command instead of a python path.

Delivery semantics are preserved deliberately: a successful send means the
transport accepted the message, and this command does not then poll `status` or
`recv` to "prove" delivery.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..core import config, output
from ..hooks import routing
from ..planning.planner import Plan

SEND = Path("/root/agentcomm/send.py")
PASSTHROUGH = {"message", "task", "connection", "health", "status", "recv", "show", "watch", "vault", "candidate"}
DIAGNOSTIC = {"recv", "show", "watch", "status"}


def _run(argv: list[str]) -> int:
    if not SEND.exists():
        output.error(f"AgentComm client missing at {SEND}")
        return 1
    routing.to("send")
    try:
        completed = subprocess.run([str(config.get("python")), str(SEND), *argv], check=False)
        return completed.returncode
    except OSError as exc:
        output.error(f"cannot run the AgentComm client: {exc}")
        return 1


def _agent_conf() -> dict:
    import json
    import os
    try:
        path = Path(os.environ.get("AGENTCOMM_CONF", "/root/agentcomm/agent.conf"))
        conf = json.loads(path.read_text(encoding="utf-8"))
        return conf if isinstance(conf, dict) else {}
    except (OSError, ValueError):
        return {}


def _configured_peer() -> tuple[str, set[str]]:
    """The peer agent.conf names, and the set of names accepted for it."""
    expected = str(_agent_conf().get("peer", ""))
    from ..agents import topology
    allowed = {a.name.casefold() for a in topology.load().remote}
    if expected:
        allowed = {expected.casefold()}
    return expected, allowed


def _self_name() -> str:
    return str(_agent_conf().get("self", "")).casefold()


def run(args: list[str], plan: Plan) -> int:
    if not args:
        output.error(
            'usage: /skmr:send "text" | <peer> "text" | message "text" | task "text" '
            "| connection [public|private] | health"
        )
        return 2

    head = args[0].casefold()

    if head in PASSTHROUGH:
        if head in DIAGNOSTIC:
            output.warning(
                f"`{head}` is a manual diagnostic, not the normal receive path; "
                "verified peer messages arrive through the asyncRewake watcher."
            )
        if head in {"message", "task"} and len(args) < 2:
            output.error(f'usage: /skmr:send {head} "text"')
            return 2
        return _run(args)

    # `/skmr:send sondra "text"` -- a peer name followed by the message body.
    if len(args) >= 2:
        peer = args[0]
        expected, allowed = _configured_peer()
        if peer.casefold() not in allowed:
            output.error(f"unknown peer '{peer}'; use the configured AgentComm peer")
            return 2
        body = " ".join(args[1:])
        output.info(f"Sending to peer '{peer}'.")
        return _run(["message", body])

    # `/skmr:send "text"` -- a single free-text argument is a message to the one
    # configured peer. There is exactly one, so requiring its name adds nothing,
    # and the obvious form was rejected with a list of subcommands instead.
    expected, allowed = _configured_peer()
    if head in allowed or head == _self_name():
        # The peer's own name with no body: the message is missing, not unknown.
        output.error(f'usage: /skmr:send {args[0]} "text"')
        return 2
    # A single bare word close to a subcommand is far more likely a typo than a
    # message. Without this, `/skmr:send heath` silently queued the word "heath"
    # to the peer instead of reporting that `health` was meant.
    import difflib
    near = difflib.get_close_matches(head, sorted(PASSTHROUGH), n=1, cutoff=0.75)
    if near and " " not in args[0]:
        output.error(f'unknown action "{args[0]}" -- did you mean `{near[0]}`? '
                     f'To send it as a message: /skmr:send message "{args[0]}"')
        return 2
    if expected:
        output.info(f"Sending to peer '{expected}'.")
        return _run(["message", args[0]])

    output.error(
        f'unknown action "{args[0]}". Use a peer name plus a message, or one of: '
        + ", ".join(sorted(PASSTHROUGH))
    )
    return 2
