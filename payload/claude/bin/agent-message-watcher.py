#!/usr/bin/env python3
"""Canonical asyncRewake watcher for the Eliza <-> Sondra AgentComm link.

The watcher is generic: identity and peer come from /root/agentcomm/agent.conf.
It peeks at the authenticated inbox, wakes Claude for a substantive peer
message, and acknowledges that message only when a later Stop hook indicates
that the awakened turn completed. If the same Claude session is resumed after
a crash/suspend before Stop, the pending message is redelivered. Non-actionable
telemetry is acknowledged without waking the model.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
sys.path.insert(0, os.environ.get('AGENTCOMM_LIB', str(Path(__file__).resolve().parent.parent/'lib')))
from skmr_http import http_open

CONF_PATH = Path(os.environ.get("AGENTCOMM_CONF", "/root/agentcomm/agent.conf"))
STATE_DIR = Path(os.environ.get("SKMR_STATE_DIR", "/root/.claude/state"))
DATA_DIR = CONF_PATH.parent / "data"
PENDING_DIR = STATE_DIR / "agentcomm-pending"

POLL_SECONDS = max(1, int(os.environ.get("AGENTCOMM_WATCH_INTERVAL", "2")))
MAX_ENVELOPE_CHARS = 1200
TRUNCATION_MARKER = (
    " [... {withheld} of {total} chars withheld; full sha256:{digest};"
    " ask the peer to resend the rest]"
)
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
NO_WAKE_KINDS = {"ack", "heartbeat", "telemetry", "receipt", "seen", "status"}
NO_WAKE_TEXT = re.compile(
    r"^\s*(ack\b|seen\b|heartbeat\b|receipt\b|\[ack\]|delivery[- ]receipt)",
    re.I,
)
SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")
# The SessionStart/Stop hooks give this watcher timeout=86400. Exiting just under
# that lets the process leave on its own terms instead of being killed mid-request.
MAX_LIFETIME_SECONDS = max(60, int(os.environ.get("SKMR_WATCHER_MAX_LIFETIME", "82800")))
RETENTION_SECONDS = max(3600, int(os.environ.get("SKMR_STATE_RETENTION_SECONDS", str(2 * 86400))))
_EPOCH_RESETS: set[str] = set()
_OBSERVED_EPOCHS: dict[str, str] = {}


class InboxRejected(RuntimeError):
    """The inbox refused this agent's token; polling again cannot fix it."""


def load_conf() -> dict:
    payload = json.loads(CONF_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("agent.conf must contain a JSON object")
    if not payload.get("self") or not payload.get("peer") or not payload.get("token"):
        raise ValueError("agent.conf requires self, peer and token")
    return payload


def read_hook_event() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def safe_session_id(event: dict) -> str:
    sid = event.get("session_id")
    value = SAFE_KEY.sub("_", str(sid))[:64] if sid else "default"
    return value or "default"


def event_name(event: dict) -> str:
    return str(
        event.get("hook_event_name")
        or event.get("hookEventName")
        or event.get("event")
        or ""
    ).casefold()


def atomic_text(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict) -> None:
    atomic_text(path, json.dumps(payload, ensure_ascii=False) + "\n")


def read_int(path: Path, default: int = 0) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return default


def cursor_path(key: str) -> Path:
    return STATE_DIR / f"{key}-wake.cursor"


def shared_cursor_path(self_name: str) -> Path:
    return DATA_DIR / f"{self_name}.cursor"


def pending_path(key: str) -> Path:
    return PENDING_DIR / f"{key}.json"


def write_wake_cursor(key: str, value: int) -> None:
    atomic_text(cursor_path(key), str(max(0, int(value))))


def read_wake_cursor(key: str, self_name: str) -> int:
    own = read_int(cursor_path(key), -1)
    if own >= 0:
        return own
    return read_int(shared_cursor_path(self_name), 0)


def observe_inbox_epoch(self_name: str, epoch: object) -> bool:
    """Adopt legacy state once; archive prior-server delivery state on a change."""
    if not isinstance(epoch, str) or not epoch.strip():
        return False
    epoch = epoch.strip()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    epoch_path = STATE_DIR / f"{self_name}-inbox.epoch"
    with open(epoch_path.with_suffix('.epoch.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            previous = epoch_path.read_text(encoding='utf-8').strip()
        except OSError:
            previous = ''
        observed = _OBSERVED_EPOCHS.get(self_name, previous)
        changed = bool(observed and observed != epoch)
        if previous and previous != epoch:
            # Preserve journal contents, including original full delivery metadata.
            # Copy atomically before unlinking to work across configured filesystems.
            archive = (DATA_DIR / 'epoch-archive'
                       / (SAFE_KEY.sub('_', previous)[:128] or 'unknown') / uuid.uuid4().hex)
            files = list(STATE_DIR.glob(f'{self_name}-*-wake.cursor'))
            files.extend(PENDING_DIR.glob(f'{self_name}-*.json'))
            for path in files:
                target = archive / 'state' / path.relative_to(STATE_DIR)
                atomic_text(target, path.read_text(encoding='utf-8'))
                path.unlink(missing_ok=True)
                if path.name.endswith('-wake.cursor'):
                    atomic_text(path, '0')
            for path in (shared_cursor_path(self_name), DATA_DIR / f'{self_name}.feed.cursor'):
                if path.exists():
                    atomic_text(archive / 'data' / path.name, path.read_text(encoding='utf-8'))
                atomic_text(path, '0')
            changed = True
        if previous != epoch:
            atomic_text(epoch_path, epoch)
        _OBSERVED_EPOCHS[self_name] = epoch
        if changed:
            _EPOCH_RESETS.add(self_name)
        return changed


def highest_known_id(conf: dict, self_name: str) -> int | None:
    messages = inbox_request(conf, self_name, 0, ack=False)
    if messages is None:
        return None
    return max((int(msg.get('id', 0)) for msg in messages if isinstance(msg, dict)), default=0)


def resolve_start_cursor(conf: dict, key: str, self_name: str) -> int:
    """Repair impossible cursors without treating uncompleted journals as ACKed."""
    newest = highest_known_id(conf, self_name)
    cursor = read_wake_cursor(key, self_name)
    if newest is None:
        return cursor
    shared = read_int(shared_cursor_path(self_name), 0)
    if shared > newest:
        repaired = newest
        for path in PENDING_DIR.glob(f'{self_name}-*.json'):
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
                pending_id = int(payload.get('id', 0))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if pending_id > 0:
                repaired = min(repaired, pending_id - 1)
        atomic_text(shared_cursor_path(self_name), str(max(0, repaired)))
    if cursor > newest:
        cursor = newest
        write_wake_cursor(key, cursor)
    return cursor


def connection_state() -> dict:
    try:
        with (DATA_DIR / 'connection.json').open(encoding='utf-8') as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def public_endpoint(state: dict | None = None) -> str:
    """The peer's tunnel URL when the link is in public mode, else "".

    `send.py` chooses its endpoints from this same state, so reading only
    agent.conf here made the two halves of the link disagree: outbound went out
    over the tunnel while the wake path polled a LAN address that is unreachable
    precisely because the peer is off-LAN. Messages then sat in the inbox
    undelivered, with no error surfacing anywhere.
    """
    if state is None:
        state = connection_state()
    if not isinstance(state, dict) or state.get("mode") != "public":
        return ""
    url = str(state.get("public_url") or "").strip()
    return url if url.startswith("https://") else ""


def endpoints(conf: dict) -> list[str]:
    state = connection_state()
    local = [conf.get('api_base'), conf.get('api_lan')]
    public = public_endpoint(state)
    role = state.get('transport_role') or conf.get('transport_role')
    if role == 'host':
        return [str(conf.get('server_local_url') or 'http://127.0.0.1:8080').rstrip('/')]
    if state.get('mode') == 'public' and role == 'consumer':
        return [public.rstrip('/')] if public else []
    if role == 'consumer' and conf.get('peer_api_lan'):
        return [str(conf['peer_api_lan']).rstrip('/')]
    values = [public] + local
    return list(dict.fromkeys(str(value).rstrip("/") for value in values if value))


def inbox_request(conf: dict, self_name: str, since: int, *, ack: bool = False) -> list[dict] | None:
    suffix = "&ack=true" if ack else ""
    expected_epoch = _OBSERVED_EPOCHS.get(self_name, '') if ack else ''
    if expected_epoch:
        suffix += '&expected_epoch=' + urllib.parse.quote(expected_epoch, safe='')
    for base in endpoints(conf):
        url = f"{base}/api/inbox/{self_name}?since={since}{suffix}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-Agent-Token", str(conf["token"]))
        try:
            with http_open(req, timeout=6) as response:
                payload = json.loads(response.read().decode())
                changed = observe_inbox_epoch(self_name, payload.get('inbox_epoch'))
                if changed and (since != 0 or ack):
                    # The old request's filter/ACK refers to another server's ids.
                    return inbox_request(conf, self_name, 0, ack=False)
                messages = payload.get("messages", [])
                return messages if isinstance(messages, list) else []
        except urllib.error.HTTPError as error:
            if error.code == 409 and ack and expected_epoch:
                # A server swap between preflight and ACK must never acknowledge
                # the old id on the replacement inbox. Refresh without ACK.
                _EPOCH_RESETS.add(self_name)
                return inbox_request(conf, self_name, 0, ack=False)
            # Only an auth refusal is fatal: replaying a rejected token cannot fix
            # it, and silently polling on hid the failure for the whole 86400s
            # hook timeout. Everything else -- 404 included -- stays retryable,
            # because a restarting tunnel/proxy can answer 404 for a moment and a
            # wake path must not give up on a transient reply.
            if error.code in (401, 403):
                raise InboxRejected(f"HTTP {error.code} from {base}") from error
            continue
        except (
            urllib.error.URLError,
            OSError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ):
            continue
    return None


def acknowledge(conf: dict, key: str, self_name: str, message_id: int) -> bool:
    if message_id <= 0:
        return True
    # Server presence/ack state is updated only with ack=true.
    if inbox_request(conf, self_name, message_id, ack=True) is None:
        return False
    if self_name in _EPOCH_RESETS:
        return False
    current = read_int(shared_cursor_path(self_name), 0)
    if message_id > current:
        atomic_text(shared_cursor_path(self_name), str(message_id))
    write_wake_cursor(key, max(read_int(cursor_path(key), 0), message_id))
    return True


def acknowledge_completed_pending(conf: dict, key: str, self_name: str, hook_event: str) -> None:
    if hook_event != "stop":
        return
    path = pending_path(key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        message_id = int(payload.get("id", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    if acknowledge(conf, key, self_name, message_id):
        path.unlink(missing_ok=True)


def prune_acknowledged_pending(self_name: str) -> None:
    """Sweep this identity's pending journals against its shared ack cursor.

    Cleanup used to live in redeliver_pending_on_resume() and was keyed by
    session, so a session that died before its Stop hook left behind a pending
    file no later session could ever reach: never redelivered, never removed.
    Ids ABOVE the cursor are kept -- those are still awaiting delivery.
    """
    if not PENDING_DIR.is_dir():
        return
    acknowledged = read_int(shared_cursor_path(self_name), 0)
    for path in PENDING_DIR.glob(f"{self_name}-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            message_id = int(payload.get("id", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Unparseable: it can never be redelivered, so it is pure garbage.
            path.unlink(missing_ok=True)
            continue
        if 0 < message_id <= acknowledged:
            path.unlink(missing_ok=True)


def lock_is_held(path: Path) -> bool:
    """True if a live process still holds this watcher lock.

    Age alone never proves a session ended: the watcher never rewrites its lock,
    so a session alive for longer than the retention window looks just as stale
    as a dead one. Deleting a held lock would let a second watcher take a fresh
    lock for the same session and deliver the same message twice.
    """
    try:
        with open(path, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        return True  # cannot tell -- leave it alone


def sweep_stale_state(self_name: str, current_key: str) -> None:
    """Drop this watcher's own per-session leftovers past the retention window.

    Scoped deliberately to the files this watcher creates. Other components'
    session state is left to its owner.
    """
    if not STATE_DIR.is_dir():
        return
    cutoff = time.time() - RETENTION_SECONDS
    for suffix in ("-message-watcher.lock", "-wake.cursor"):
        for path in STATE_DIR.glob(f"{self_name}-*{suffix}"):
            if current_key in path.name:
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            if suffix.endswith(".lock") and lock_is_held(path):
                continue
            path.unlink(missing_ok=True)


def reject(error: Exception) -> int:
    sys.stderr.write(f"AgentComm inbox rejected the agent token: {error}\n")
    sys.stderr.flush()
    return 1


def is_wake_worthy(msg: dict) -> bool:
    if msg.get("type") == "task":
        return True
    if str(msg.get("kind", "")).casefold() in NO_WAKE_KINDS:
        return False
    return not bool(NO_WAKE_TEXT.match(str(msg.get("text", ""))))


def clean_peer_text(value: object) -> str:
    text = CONTROL_CHARS.sub("", str(value or ""))
    # The transport envelope is multi-line; peer-controlled text is forced to one
    # line so it cannot forge a second Message ID / sender header.
    text = " ".join(text.splitlines())
    total = len(text)
    if total <= MAX_ENVELOPE_CHARS:
        return text
    # Slicing silently cost two conversations: both sides answered half a message
    # without knowing any was missing. The cap and the one-line rule stay; only
    # the silence goes. The marker is paid for out of the same budget, so the
    # envelope stays bounded.
    # The counts alone are not enough, though not because they are unreliable:
    # measured against the sender's store they were exact every time. What they
    # cannot carry is WHICH message they describe. A peer that pairs "N of M" with
    # the wrong message resends the wrong slice and both ends believe the gap was
    # repaired -- an attribution error, and one this link made three times before
    # the numbers were checked against stored lengths. The digest is of the
    # complete one-line text, so a reassembled message can be verified rather
    # than assumed to match.
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]
    kept = MAX_ENVELOPE_CHARS
    while kept > 0 and kept + len(
        TRUNCATION_MARKER.format(withheld=total - kept, total=total, digest=digest)
    ) > MAX_ENVELOPE_CHARS:
        kept -= 1
    return text[:kept] + TRUNCATION_MARKER.format(withheld=total - kept, total=total, digest=digest)


def envelope(peer: str, msg: dict) -> str:
    name = str(msg.get("from") or peer or "peer").upper()
    message_id = int(msg.get("id", 0))
    text = clean_peer_text(msg.get("text", ""))
    if msg.get("type") == "task":
        text = f"[TASK-{msg.get('task_no')}] {text}"
    return (
        f"New verified message from {name}.\n"
        f"Message ID: {message_id}\n"
        f"{name}: {text}"
    )


def save_pending(key: str, msg: dict) -> None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    atomic_json(
        pending_path(key),
        {
            "id": int(msg.get("id", 0)),
            "from": str(msg.get("from", "")),
            "type": str(msg.get("type", "message")),
            "task_no": msg.get("task_no"),
            "text": clean_peer_text(msg.get("text", "")),
            "delivered_at": time.time(),
        },
    )



def load_pending_message(key: str) -> dict | None:
    """Return this session's unacknowledged pending message, if any."""
    path = pending_path(key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        message_id = int(payload.get("id", 0))
    except (TypeError, ValueError):
        return None
    if message_id <= 0:
        return None
    payload["id"] = message_id
    return payload


def redeliver_pending_on_resume(key: str, self_name: str, peer: str, hook_event: str) -> bool:
    """Redeliver a pending message after crash/suspend/resume.

    The per-session wake cursor may already equal the pending message id. Without
    this check, resuming the same Claude session could skip an unacknowledged
    message permanently. A completed turn is still acknowledged only by Stop.
    """
    if hook_event == "stop":
        return False

    pending = load_pending_message(key)
    if not pending:
        return False

    message_id = int(pending["id"])
    acknowledged = read_int(shared_cursor_path(self_name), 0)

    # If another valid completion path already ACKed it, clean stale pending state.
    if message_id <= acknowledged:
        pending_path(key).unlink(missing_ok=True)
        return False

    sys.stderr.write(envelope(peer, pending) + "\n")
    sys.stderr.flush()
    return True


def run_watcher(conf: dict, self_name: str, peer: str, key: str, hook_event: str) -> int:
    # Observe the server identity and repair cursors before replay, cleanup or ACK.
    # An outage still leaves durable pending messages available for replay.
    try:
        cursor = resolve_start_cursor(conf, key, self_name)
    except InboxRejected as error:
        return reject(error)
    _EPOCH_RESETS.discard(self_name)

    if redeliver_pending_on_resume(key, self_name, peer, hook_event):
        return 2

    try:
        acknowledge_completed_pending(conf, key, self_name, hook_event)
    except InboxRejected as error:
        return reject(error)

    prune_acknowledged_pending(self_name)
    sweep_stale_state(self_name, key)
    cursor = read_wake_cursor(key, self_name)
    started = time.monotonic()
    parent = os.getppid()
    supervised = parent > 1

    while True:
        if time.monotonic() - started > MAX_LIFETIME_SECONDS:
            return 0
        if supervised and os.getppid() != parent:
            return 0
        try:
            messages = inbox_request(conf, self_name, cursor, ack=False)
        except InboxRejected as error:
            return reject(error)
        if self_name in _EPOCH_RESETS:
            cursor = read_wake_cursor(key, self_name)
            _EPOCH_RESETS.discard(self_name)
        if messages is None:
            time.sleep(POLL_SECONDS)
            continue
        fresh = [msg for msg in messages if isinstance(msg, dict) and int(msg.get('id', 0)) > cursor]
        highest = max((int(msg.get('id', 0)) for msg in fresh), default=cursor)
        worthy = [msg for msg in fresh if is_wake_worthy(msg)]
        if worthy:
            first = min(worthy, key=lambda msg: int(msg.get('id', 0)))
            message_id = int(first.get('id', cursor))
            write_wake_cursor(key, message_id)
            save_pending(key, first)
            sys.stderr.write(envelope(peer, first) + '\n')
            sys.stderr.flush()
            return 2
        if highest > cursor:
            try:
                acknowledged = acknowledge(conf, key, self_name, highest)
            except InboxRejected as error:
                return reject(error)
            if acknowledged:
                cursor = highest
            else:
                time.sleep(POLL_SECONDS)
                continue
        time.sleep(POLL_SECONDS)


def main() -> int:
    try:
        conf = load_conf()
    except (OSError, ValueError, json.JSONDecodeError):
        return 0

    self_name = str(conf.get("self") or "").casefold()
    peer = str(conf.get("peer") or "peer")
    if not self_name:
        return 0

    event = read_hook_event()
    sid = safe_session_id(event)
    hook_event = event_name(event)
    key = f"{self_name}-{sid}"

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{key}-message-watcher.lock", "a") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        return run_watcher(conf, self_name, peer, key, hook_event)


if __name__ == "__main__":
    raise SystemExit(main())
