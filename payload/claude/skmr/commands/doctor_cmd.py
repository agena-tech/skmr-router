"""`/skmr:doctor` -- one health report for every SKMR subsystem.

Each subsystem is probed independently so a single outage is visible as exactly
that, instead of looking like a total failure.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..core import config, output
from ..planning.planner import Plan

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "ok", WARN: "warn", FAIL: "FAIL"}


def _vault() -> tuple[str, str]:
    """Separate three states; never call an unreachable vault an empty one.

    ``index.vault_available()`` answers a narrower question on purpose: local
    indexing needs a real filesystem, so it only looks at the mount. Reporting
    its False as "empty or unreadable" was the same conflation the retrieval
    side already fixed -- on a peer whose SMB mount had dropped, doctor
    announced an empty vault while the authenticated read route was serving
    that vault's notes. Retrieval and indexing keep requiring the mount; only
    the diagnosis is corrected here.
    """
    import os
    import sys
    from pathlib import Path
    sys.path.insert(0, os.environ.get('AGENTCOMM_LIB', str(Path(__file__).resolve().parents[2] / 'lib')))
    from skmr_agent_policy import vault_http_readable, vault_mount_usable
    from ..memory.indexing import index
    root = index.vault_root()
    if vault_mount_usable() and root.exists() and index.vault_available():
        return OK, str(root)
    if vault_http_readable():
        return WARN, (f"{root} mount unavailable; readable over the authenticated route "
                      "(local indexing and retrieval unavailable)")
    if not root.exists():
        return FAIL, f"{root} is unreachable (mount missing, no authenticated read route)"
    return FAIL, f"{root} is unreachable over both the mount and the authenticated read route"


def _index_state() -> tuple[str, str]:
    from ..memory.indexing import index
    try:
        stats = index.stats()
    except Exception as exc:
        return FAIL, f"index unreadable: {exc}"
    if not stats["chunks"]:
        return WARN, "empty — run /skmr:obsidian-memory index"
    detail = f"{stats['files']} files, {stats['chunks']} chunks, {stats['vectors']} vectors"
    if not stats["vectors"]:
        return WARN, detail + " (no vectors: BM25 only)"
    if stats["vectors"] < stats["chunks"]:
        return WARN, detail + " (partial vector coverage)"
    return OK, detail


def _embedding() -> tuple[str, str]:
    from ..memory.retrieval import vector
    state = vector.status()
    if not state.available:
        return WARN, state.reason
    return OK, f"{config.get('embedding_provider')}:{config.get('embedding_model')}"


def _native() -> tuple[str, str]:
    from ..memory import native
    path = native.memory_path()
    if not path.exists():
        return WARN, f"{path} missing (will be created on first write)"
    state = native.load()
    task = state.get("Active Task").strip() or "-"
    return OK, f"{path} (active task: {task})"


def _topology() -> tuple[str, str]:
    from ..agents import topology
    current = topology.load()
    if current.local is None:
        return WARN, "no local identity assigned — run /skmr:assign-role"
    try:
        topology.validate(current)
    except topology.ValidationError as exc:
        return FAIL, f"invalid topology: {exc}"
    remote = ", ".join(agent.label() for agent in current.remote) or "none"
    return OK, f"local {current.local.label()}; remote: {remote}"


def _agentcomm() -> tuple[str, str]:
    # Honour AGENTCOMM_CONF like every other consumer of this file. Hardcoded,
    # doctor reported an installation under another prefix as broken while
    # reading a file belonging to a different account entirely.
    import os
    conf = Path(os.environ.get("AGENTCOMM_CONF", "/root/agentcomm/agent.conf"))
    if not conf.exists():
        return WARN, "agent.conf missing; /skmr:send unavailable"
    try:
        data = json.loads(conf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return FAIL, f"agent.conf unreadable: {exc}"
    identity = data.get("identity") or data.get("self") or "?"
    state_file = conf.parent / "data" / "connection.json"
    mode = "private"
    if state_file.exists():
        try:
            mode = json.loads(state_file.read_text(encoding="utf-8")).get("mode", "private")
        except (json.JSONDecodeError, OSError):
            mode = "unknown"
    return OK, f"identity={identity}, transport={mode}"


def _writer() -> tuple[str, str]:
    writer = Path(str(config.get("writer")))
    if not writer.exists():
        return FAIL, f"canonical writer missing at {writer}"
    try:
        completed = subprocess.run(
            [str(config.get("python")), str(writer), "validate"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return WARN, f"writer present but validate failed to run: {exc}"
    if completed.returncode != 0:
        first = (completed.stderr or completed.stdout or "").strip().splitlines()
        return WARN, f"validate exit={completed.returncode}: {first[0] if first else 'no output'}"
    try:
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict) or not payload.get("ok"):
            return FAIL, "canonical validator reports integrity problems"
    except (ValueError, TypeError):
        return FAIL, "canonical validator returned invalid JSON"
    return OK, "present, validate passed"


def _peer_admin() -> tuple[str, str]:
    """Is the recorded peer administration endpoint actually usable right now?

    Role sync posts the administrative secret to this address. A quick-tunnel URL
    expires when the tunnel restarts, so a stale value looks configured while
    every --sync-peer against it fails. Nothing reported this before you tried.
    """
    conf = Path("/root/agentcomm/agent.conf")
    try:
        data = json.loads(conf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return WARN, "agent.conf unreadable; peer role sync state unknown"
    from .assign_role_cmd import peer_admin_endpoint
    endpoint = peer_admin_endpoint(data)
    if not endpoint:
        return WARN, ("no peer route: no active public tunnel and no peer LAN address; "
                      "assign the remote agent's LAN address with /skmr:assign-role --remote-lan")
    import urllib.error
    import urllib.request
    probe = urllib.request.Request(endpoint.rstrip("/") + "/api/health/", method="GET")
    try:
        with urllib.request.urlopen(probe, timeout=8) as response:
            if response.status == 200:
                return OK, f"peer admin endpoint reachable ({endpoint})"
            return WARN, f"peer admin endpoint answered HTTP {response.status} ({endpoint})"
    except urllib.error.HTTPError as error:
        # An authenticated route refusing anonymous access still proves it is up.
        if error.code in (401, 403):
            return OK, f"peer admin endpoint reachable, auth required ({endpoint})"
        return WARN, f"peer admin endpoint HTTP {error.code} ({endpoint})"
    except Exception as error:
        return WARN, (f"peer route unreachable ({endpoint}): {type(error).__name__}; "
                      "check the peer LAN address (/skmr:assign-role --remote-lan) or have the host open a tunnel")


CHECKS = (
    ("vault", _vault),
    ("index", _index_state),
    ("embedding", _embedding),
    ("native state", _native),
    ("agent topology", _topology),
    ("agentcomm", _agentcomm),
    ("canonical writer", _writer),
    ("peer admin", _peer_admin),
)


def run(args: list[str], plan: Plan) -> int:
    worst = OK
    print()
    for name, check in CHECKS:
        try:
            status, detail = check()
        except Exception as exc:
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        print(f"  [{MARK[status]:>4}] {name:<17} {detail}")
        if status == FAIL or (status == WARN and worst == OK):
            worst = status
    print()
    if worst == FAIL:
        output.error("one or more subsystems are down")
        return 1
    if worst == WARN:
        output.warning("degraded, but operational")
    return 0
