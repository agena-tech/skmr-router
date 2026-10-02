#!/usr/bin/env python3
"""CLI client for the Eliza <-> Sondra agent link. Backs the /send skill.

Usage:
  send.py message "<text>"          send a message to the peer agent
  send.py task    "<text>" [--body <file>]   create a task for the peer
  send.py recv                      print new incoming messages (colored)
  send.py watch [interval_secs]     poll and print incoming continuously
  send.py health                    check API reachability (LAN + fallback)
  send.py connection [public|private] [--url <https-url>]
                                    show or switch the transport between the
                                    default private LAN and public cloudflared
  send.py vault <ls|cat> [path]     read the ElizaMemory vault over the active
                                    endpoint (used cross-network in public mode)

Identity, endpoints and token come from agent.conf. Endpoints are tried in
order (public tunnel when in public consumer mode -> local -> LAN ->
fallback); a total failure is reported, not hidden.

Transport mode is persisted in data/connection.json and defaults to PRIVATE
(LAN). PUBLIC mode is an explicitly authorized override that exposes both the
messaging API and token-gated vault read routes over a cloudflared quick tunnel.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
import os
import re
import tempfile
import fcntl
import importlib.util
import signal
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import common
sys.path.insert(0, os.environ.get('AGENTCOMM_LIB', str(common.BASE_DIR.parent / '.claude/lib')))
from skmr_permissions import can_write
from skmr_http import http_open

CONF = common.load_conf()
SELF = CONF["self"]
PEER = CONF["peer"]
TIMEOUT = 6


# --- transport mode (private LAN  <->  public cloudflared) -----------------
# Persisted transport state. Default is PRIVATE: the LAN endpoints from
# agent.conf. PUBLIC is an explicitly authorized override in which the server
# host (Eliza) runs a cloudflared quick tunnel over the FastAPI (port 8080),
# exposing both the messaging API and the token-gated vault read routes, and
# the peer (Sondra) points her client at that public URL. Which side does which
# is derived from identity, not a command argument.
CONNECTION_STATE = common.BASE_DIR / 'data' / 'connection.json'
CLOUDFLARED_LOG = common.BASE_DIR / 'data' / 'cloudflared.log'
CLOUDFLARED_PID = common.BASE_DIR / 'data' / 'cloudflared.pid'
VALID_MODES = ('private', 'public')
LOCAL_HTTP = str(CONF.get('server_local_url') or 'http://127.0.0.1:8080')
_QUICK_URL = re.compile(r'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com')


def _is_host() -> bool:
    """Hosting is an explicit transport choice, independent of identity/rank."""
    return _connection_state().get('transport_role', CONF.get('transport_role', 'consumer')) == 'host'


def _connection_state() -> dict:
    try:
        st = json.loads(CONNECTION_STATE.read_text(encoding='utf-8'))
        if not isinstance(st, dict):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        st = {}
    st.setdefault('mode', 'private')
    st.setdefault('public_url', '')
    return st


def _save_connection_state(st: dict) -> None:
    payload = {'mode': st.get('mode', 'private'),
               'public_url': st.get('public_url', ''),
               'transport_role': st.get('transport_role', CONF.get('transport_role', 'consumer')),
               'updated': common.now_iso()}
    _atomic(CONNECTION_STATE, json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def _endpoints() -> list[str]:
    st = _connection_state()
    eps = []
    if _is_host():
        return [LOCAL_HTTP.rstrip('/')]
    elif st.get('mode') == 'public':
        # A fallback to this machine's old API would enqueue into a different
        # epoch while appearing successful. Public consumers use the chosen host.
        return [st['public_url'].rstrip('/')] if st.get('public_url') else []
    elif CONF.get('peer_api_lan'):
        return [CONF['peer_api_lan'].rstrip('/')]
    eps += [CONF.get('api_base'), CONF.get('api_lan')]
    return list(dict.fromkeys(e.rstrip('/') for e in eps if e))


def _scan_log_for_url() -> str:
    try:
        m = _QUICK_URL.search(CLOUDFLARED_LOG.read_text(encoding='utf-8', errors='replace'))
        return m.group(0) if m else ''
    except OSError:
        return ''


def _cloudflared_running() -> int | None:
    try:
        pid = int(CLOUDFLARED_PID.read_text())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        command = Path(f'/proc/{pid}/cmdline').read_bytes()
        if b'cloudflared' not in command or b'tunnel' not in command:
            CLOUDFLARED_PID.unlink(missing_ok=True)
            return None
        return pid
    except OSError:
        CLOUDFLARED_PID.unlink(missing_ok=True)
        return None


def _cloudflared_start(local: str = LOCAL_HTTP, wait: int = 30) -> str:
    """Start (or reuse) a cloudflared quick tunnel and return its public URL."""
    if _cloudflared_running():
        url = _scan_log_for_url()
        if url:
            return url
    from shutil import which
    if which('cloudflared') is None:
        raise RuntimeError('cloudflared is not installed or not on PATH')
    CLOUDFLARED_LOG.parent.mkdir(parents=True, exist_ok=True)
    _atomic(CLOUDFLARED_LOG, '')  # truncate previous run's output
    with CLOUDFLARED_LOG.open('ab') as log:
        proc = subprocess.Popen(
            ['cloudflared', 'tunnel', '--no-autoupdate', '--url', local],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    _atomic(CLOUDFLARED_PID, str(proc.pid))
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            CLOUDFLARED_PID.unlink(missing_ok=True)
            raise RuntimeError(f'cloudflared exited early; see {CLOUDFLARED_LOG}')
        url = _scan_log_for_url()
        if url:
            return url
        time.sleep(0.5)
    raise RuntimeError(f'timed out waiting for public URL; see {CLOUDFLARED_LOG}')


def _cloudflared_stop() -> bool:
    pid = _cloudflared_running()
    if not pid:
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    CLOUDFLARED_PID.unlink(missing_ok=True)
    return True


def _probe(base: str) -> bool:
    try:
        req = urllib.request.Request(base.rstrip('/') + '/api/health/', method='GET')
        with http_open(req, timeout=TIMEOUT) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - reachability probe only
        return False


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _outage_age():
    path = common.BASE_DIR / 'data' / f'{SELF}.outage'
    try:
        started = float(path.read_text())
    except (OSError, ValueError):
        # Missing, empty or half-written marker: restart the outage clock instead
        # of turning a transient network problem into a traceback.
        started = time.time()
        _atomic(path, str(started))
    return max(0, time.time() - started)


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    last = None
    body = json.dumps(payload).encode() if payload is not None else None
    endpoints = _endpoints()
    fallback = CONF.get('api_fallback', '')
    outage = common.BASE_DIR / 'data' / f'{SELF}.outage'
    if fallback and _connection_state().get('mode') != 'public' and outage.exists() and _outage_age() >= CONF.get('fail_seconds', 300):
        if not fallback.startswith('https://'):
            raise RuntimeError('fallback requires HTTPS')
        endpoints.append(fallback.rstrip('/'))
    for base in endpoints:
        url = base.rstrip("/") + path
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("X-Agent-Token", CONF["token"])
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with http_open(req, timeout=TIMEOUT) as r:
                value = json.loads(r.read().decode())
                # Any endpoint that answered ends the outage. The old test asked
                # whether `base` was in _endpoints(), which is recomputed and
                # never contains the fallback, so a working fallback left the
                # marker in place and the link kept ageing into "passive" while
                # it was demonstrably carrying traffic.
                outage.unlink(missing_ok=True)
                return value
        except urllib.error.HTTPError as e:
            # A rejected request is not a transport failure and must not be replayed.
            raise RuntimeError(f'API rejected request (HTTP {e.code})') from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = f"{url}: {e}"
            continue
    age = _outage_age()
    state = 'peer passive; continue independently and retry later' if age >= CONF.get('passive_seconds', 600) else 'fallback eligible' if age >= CONF.get('fail_seconds', 300) else 'retry later'
    raise RuntimeError(f"all endpoints unreachable ({last}); outage {int(age)}s: {state}")


def cmd_message(text: str) -> int:
    try:
        r = _request("POST", "/api/message/", {"from": SELF, "to": PEER, "text": text, 'request_id': uuid.uuid4().hex})
    except RuntimeError as e:
        print(f"{common.DIM}[send] delivery failed: {e}{common.RESET}", file=sys.stderr)
        return 3
    print(f"{common.DIM}[send] queued -> {PEER.upper()} (id {r.get('id')}): {text}{common.RESET}")
    print("[send] next: await asyncRewake; do not immediately poll recv/status after a successful send")
    return 0


def cmd_task(text: str, body_file: str | None) -> int:
    if not can_write(SELF, conf=CONF):
        print('[send] READ vault access cannot create tasks', file=sys.stderr)
        return 3
    body = Path(body_file).read_text(encoding="utf-8") if body_file else ""
    try:
        r = _request("POST", "/api/create/", {"from": SELF, "to": PEER,
                                              "text": text, "body": body, 'request_id': uuid.uuid4().hex})
    except RuntimeError as e:
        print(f"{common.DIM}[send] task creation failed: {e}{common.RESET}", file=sys.stderr)
        return 3
    print(common.format_task_created(r.get("task_no")))
    print(common.format_incoming(SELF, text))  # mirror of what peer will see
    return 0


def _cursor() -> Path:
    return common.BASE_DIR / "data" / f"{SELF}.cursor"


def _inbox(since, ack=False):
    """Reconcile the manual/feed cursor with the serving queue's epoch."""
    data = common.BASE_DIR / 'data'
    marker = data / f'{SELF}.inbox-epoch'
    result = _request('GET', f'/api/inbox/{SELF}?since={since}')
    epoch = str(result.get('inbox_epoch') or '')
    if epoch and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', epoch):
        raise RuntimeError('invalid inbox epoch')
    try: previous = marker.read_text().strip()
    except OSError: previous = ''
    if epoch and previous and previous != epoch:
        archive = data/'epoch-archive'/re.sub(r'[^A-Za-z0-9_-]', '_', previous)[:64]/uuid.uuid4().hex/'manual'
        archive.mkdir(parents=True, exist_ok=True)
        for path in (_cursor(), data/f'{SELF}.feed.cursor'):
            if path.exists(): path.replace(archive/path.name)
        since = 0
        result = _request('GET', f'/api/inbox/{SELF}?since=0')
    if epoch: _atomic(marker, epoch)
    if ack:
        query = f'/api/inbox/{SELF}?since={since}&ack=true'
        if epoch: query += '&expected_epoch=' + urllib.parse.quote(epoch, safe='')
        try: result = _request('GET', query)
        except RuntimeError as error:
            if 'HTTP 409' not in str(error): raise
            return _inbox(0, ack=False)
    return result, since


def _recv_locked(quiet=False) -> int:
    since = int(_cursor().read_text()) if _cursor().exists() else 0
    try:
        r, since = _inbox(since, ack=True)
    except RuntimeError as e:
        print(f"{common.DIM}[send] inbox unreachable: {e}{common.RESET}", file=sys.stderr)
        return 3
    msgs = r.get("messages", [])
    for m in msgs:
        prefix = f"[TASK-{m['task_no']}] " if m.get("type") == "task" else ""
        text = m.get('text', '')
        # Never let a peer inject terminal control sequences.
        text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
        print(common.format_incoming(m.get("from", "?"), prefix + text))
        since = max(since, m.get("id", since))
    _cursor().parent.mkdir(exist_ok=True)
    _atomic(_cursor(), str(since))
    if not msgs and not quiet:
        print(f"{common.DIM}[send] no new messages{common.RESET}")
    return 0


def cmd_recv(quiet=False) -> int:
    _cursor().parent.mkdir(parents=True, exist_ok=True)
    with _cursor().with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _recv_locked(quiet)


def cmd_candidate(path):
    if not can_write(PEER, conf=CONF):
        print('[send] configured peer has no WRITE vault access; candidate not sent', file=sys.stderr)
        return 3
    candidate = json.loads(Path(path).read_text(encoding='utf-8'))
    writer_path = Path(CONF.get('writer') or common.BASE_DIR.parent / '.claude/skills/skmr/scripts/obsidian_memory.py')
    spec = importlib.util.spec_from_file_location('candidate_writer', writer_path)
    writer = importlib.util.module_from_spec(spec); spec.loader.exec_module(writer)
    try:
        writer.check_sensitive_values(candidate)
        # Validate the exact canonical gate for both profiles and technical
        # candidates; delegation never weakens it or pretends a save occurred.
        with tempfile.TemporaryDirectory() as tmp:
            writer.VaultStore(Path(tmp), Path(tmp)).validate_candidate(candidate)
    except (ValueError, writer.MemoryError) as error:
        print(f'[send] candidate rejected locally: {error}', file=sys.stderr)
        return 3
    return cmd_message('SKMR_SAVE_CANDIDATE\n' + json.dumps(candidate, ensure_ascii=False))


def cmd_watch(interval: int) -> int:
    """Live colored feed for a separate terminal/tmux pane. Uses its OWN feed
    cursor so it never touches the recv/ack cursor the Claude session relies on -
    the feed is display-only and cannot starve message processing."""
    if interval < 1:
        # A sub-second poll is rejected rather than silently clamped: clamping
        # turned an invalid interval into an unbounded foreground loop.
        print("[send] watch interval must be >= 1 second", file=sys.stderr)
        return 3
    feed_cur = common.BASE_DIR / "data" / f"{SELF}.feed.cursor"
    try:
        since = int(feed_cur.read_text())
    except (OSError, ValueError):
        # First run: start from the latest message so the pane shows only NEW
        # incoming messages (no dump of full history).
        since = 0
        try:
            msgs = _request("GET", f"/api/inbox/{SELF}?since=0").get("messages", [])
            since = max((m.get("id", 0) for m in msgs), default=0)
        except RuntimeError:
            pass
    print(f"{common.DIM}[agentcomm feed :: {SELF.upper()}] incoming peer messages (Ctrl-C to stop){common.RESET}",
          flush=True)
    try:
        while True:
            try:
                r, since = _inbox(since)
            except RuntimeError:
                time.sleep(interval)
                continue
            for m in r.get("messages", []):
                m = dict(m)
                m["text"] = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', str(m.get("text", "")))
                print(common.format_feed(m), flush=True)
                since = max(since, m.get("id", since))
            feed_cur.parent.mkdir(parents=True, exist_ok=True)
            _atomic(feed_cur, str(since))
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_health() -> int:
    ok = False
    for base in _endpoints():
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/api/health/", timeout=TIMEOUT) as r:
                print(f"OK  {base}  {r.read().decode()}")
                ok = True
        except Exception as e:  # noqa: BLE001 - report every endpoint's status
            print(f"XX  {base}  {e}")
    return 0 if ok else 3


def cmd_show(count: int) -> int:
    """Print the last `count` incoming messages in color WITHOUT touching the
    recv/ack cursor. Reliable display path for a wake: independent of whether
    the inbox hook already consumed (advanced) the cursor."""
    try:
        r = _request("GET", f"/api/inbox/{SELF}?since=0")
    except RuntimeError as e:
        print(f"{common.DIM}[send] inbox unreachable: {e}{common.RESET}", file=sys.stderr)
        return 3
    msgs = r.get("messages", [])
    if not msgs:
        print(f"{common.DIM}[send] no messages{common.RESET}")
        return 0
    for m in msgs[-max(1, count):]:
        prefix = f"[TASK-{m['task_no']}] " if m.get("type") == "task" else ""
        text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', m.get('text', ''))
        print(common.format_incoming(m.get("from", "?"), prefix + text))
    return 0


def _parse_conn(tokens: list[str]) -> tuple[str | None, str | None]:
    mode = url = None
    for tok in tokens:
        low = tok.lower()
        if low in VALID_MODES and mode is None:
            mode = low
        elif tok.startswith('https://') and url is None:
            url = tok
        else:
            raise ValueError(f'unexpected argument: {tok!r}')
    return mode, url


def _connection_status(st: dict) -> int:
    mode = st.get('mode', 'private')
    side = 'host' if _is_host() else 'consumer'
    print(f"[send] connection: mode={mode} self={SELF} ({side})")
    if mode == 'public':
        print(f"    public_url: {st.get('public_url') or '(unset)'}")
        if _is_host():
            pid = _cloudflared_running()
            print(f"    cloudflared: {'running (pid ' + str(pid) + ')' if pid else 'NOT running'}")
    print(f"    active endpoints: {', '.join(_endpoints())}")
    return 0


def _connection_go_public(st: dict, url_arg: str | None) -> int:
    if st.get('transport_role') == 'host':
        # The host brings messaging + token-gated vault routes onto the public
        # internet via a cloudflared quick tunnel over the FastAPI (8080).
        try:
            _ensure_server()
            url = _cloudflared_start(LOCAL_HTTP)
        except RuntimeError as error:
            print(f"[send] cloudflared start failed: {error}", file=sys.stderr)
            return 3
        st.update(mode='public', public_url=url)
        _save_connection_state(st)
        print("[send] PUBLIC active — messaging + vault exposed via cloudflared:")
        print(f"    {url}")
        print("    token-gated routes: /api/message/, /api/inbox/, /api/vault/list, /api/vault/read")
        print(f"    Give this URL to the peer:  /send:connection public {url}")
        print("    Revert to LAN with:  /send:connection private")
        return 0
    # Either agent can consume the other's endpoint; a supplied URL chooses it.
    url = url_arg
    if not url:
        try:
            url = input("Enter the host public cloudflared URL (https://...): ").strip()
        except EOFError:
            url = ''
    parsed = urllib.parse.urlsplit(url or '')
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        print("[send] connection: a valid https:// public URL is required", file=sys.stderr)
        return 3
    url = url.rstrip('/')
    st.update(mode='public', public_url=url)
    _save_connection_state(st)
    reachable = _probe(url)
    print("[send] PUBLIC active — peer endpoint set to:")
    print(f"    {url}")
    print(f"    health: {'reachable' if reachable else 'NOT reachable yet (check the host tunnel)'}")
    print("    Revert to LAN with:  /send:connection private")
    return 0


def _ensure_server():
    """Start the identically installed local service before opening a tunnel."""
    if _probe(LOCAL_HTTP):
        return
    result = subprocess.run(['systemctl', 'start', 'agentcomm.service'],
        capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('local agentcomm service could not start; run the installed bootstrap first')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _probe(LOCAL_HTTP): return
        time.sleep(.25)
    raise RuntimeError('local agentcomm service did not become healthy')


def _connection_go_private(st: dict) -> int:
    stopped = _cloudflared_stop() if _is_host() else False
    st.update(mode='private', public_url='')
    _save_connection_state(st)
    print(f"[send] PRIVATE active — LAN endpoints restored ({CONF.get('api_lan')}).")
    if stopped:
        print("[send] cloudflared tunnel stopped.")
    return 0


def cmd_connection(tokens: list[str], url_flag: str | None) -> int:
    choices = [value for value in tokens if value in ('--host','--consumer')]
    if len(choices) > 1:
        print('[send] choose one transport role: --host or --consumer', file=sys.stderr)
        return 3
    tokens = [value for value in tokens if value not in ('--host','--consumer')]
    try:
        mode, url = _parse_conn(tokens)
    except ValueError as error:
        print(f"[send] connection: {error}", file=sys.stderr)
        return 3
    url = url_flag or url
    st = _connection_state()
    if mode is None and url is None:
        return _connection_status(st)
    if mode is None:
        print("[send] connection: specify a mode (public|private)", file=sys.stderr)
        return 3
    if mode == 'public':
        st['transport_role'] = choices[0][2:] if choices else ('consumer' if url else 'host')
        if st['transport_role'] == 'host' and url:
            print('[send] --host does not accept a peer URL', file=sys.stderr)
            return 3
        return _connection_go_public(st, url)
    return _connection_go_private(st)


def cmd_vault(op: str, path: str) -> int:
    query = urllib.parse.quote(path or '', safe='')
    try:
        if op == 'ls':
            r = _request('GET', f"/api/vault/list?path={query}")
            for entry in r.get('entries', []):
                mark = 'd' if entry.get('dir') else '-'
                print(f"{mark} {entry.get('name')}")
        else:  # cat
            r = _request('GET', f"/api/vault/read?path={query}")
            sys.stdout.write(r.get('content', ''))
            if not r.get('content', '').endswith('\n'):
                sys.stdout.write('\n')
    except RuntimeError as error:
        print(f"[send] vault {op} failed: {error}", file=sys.stderr)
        return 3
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="send.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("message"); m.add_argument("text")
    t = sub.add_parser("task"); t.add_argument("text"); t.add_argument("--body")
    r = sub.add_parser("recv"); r.add_argument('--quiet', action='store_true')
    c = sub.add_parser('candidate'); c.add_argument('file')
    sub.add_parser('status')
    w = sub.add_parser("watch"); w.add_argument("interval", nargs="?", type=int, default=2)
    sh = sub.add_parser("show"); sh.add_argument("count", nargs="?", type=int, default=1)
    sub.add_parser("health")
    cn = sub.add_parser("connection"); cn.add_argument("args", nargs="*"); cn.add_argument("--url", default=None)
    vt = sub.add_parser("vault"); vt.add_argument("op", choices=["ls", "cat"]); vt.add_argument("path", nargs="?", default="")
    a = p.parse_args()
    if a.cmd == "message":
        return cmd_message(a.text)
    if a.cmd == "task":
        return cmd_task(a.text, a.body)
    if a.cmd == "recv":
        return cmd_recv(a.quiet)
    if a.cmd == 'candidate':
        return cmd_candidate(a.file)
    if a.cmd == 'status':
        try:
            print(json.dumps(_request('GET', '/api/status/'), ensure_ascii=False))
            return 0
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            return 3
    if a.cmd == "watch":
        return cmd_watch(a.interval)
    if a.cmd == "show":
        return cmd_show(a.count)
    if a.cmd == "health":
        return cmd_health()
    if a.cmd == "connection":
        return cmd_connection(a.args, a.url)
    if a.cmd == "vault":
        return cmd_vault(a.op, a.path)
    return 1


if __name__ == "__main__":
    sys.exit(main())
