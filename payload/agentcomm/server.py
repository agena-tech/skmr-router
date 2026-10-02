"""FastAPI communication service for the Eliza (L0) <-> Sondra (L1) agents.

Runs in Eliza's WSL on port 8080. Exposed to the LAN host 192.168.1.110 via a
Windows `netsh portproxy` mapping (set up separately, see README).

Routes
  POST /api/message/   {from,to,text}         -> queue a message for `to`
  POST /api/create/    {from,to,text,body?}   -> allocate next TASK-N, write it
                                                 to the Sondratasks SMB share,
                                                 and notify `to`
  GET  /api/inbox/{who}?since=<id>            -> messages newer than cursor
  GET  /api/health/                          -> liveness

All mutating routes require header  X-Agent-Token: <token from agent.conf>.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import contextlib
import fcntl
import hmac
import os
import re
import time
from typing import Literal, Annotated
from pathlib import Path
import sys
import uuid
import importlib.util
import urllib.request
import urllib.error

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

import common
sys.path.insert(0, os.environ.get('AGENTCOMM_LIB', str(common.BASE_DIR.parent / '.claude/lib')))
from skmr_permissions import can_write, permission
from skmr_http import http_open

CONF = common.load_conf()
_LOADED_CONF = CONF
_CONFIG_STAMP = common.CONF_PATH.stat().st_mtime_ns
MSG_DIR = common.BASE_DIR / "messages"
DATA_DIR = common.BASE_DIR / "data"
MSG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

_LOCK = threading.RLock()
app = FastAPI(title="AgentComm", version="1.1", docs_url=None, redoc_url=None)
Agent = Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')]


def _refresh_conf():
    """Apply local CLI grant changes on the next request without a restart."""
    global _CONFIG_STAMP
    # Tests inject a complete in-memory config; only the installed object owns
    # the on-disk configuration. Failed reads reject the request, never keep a
    # revoked capability alive under an earlier configuration.
    if CONF is not _LOADED_CONF: return
    with _LOCK:
        try:
            # Timestamp granularity and preserved timestamps cannot establish
            # permission freshness. This small local file is read every time.
            value = common.load_conf()
            if not isinstance(value, dict): raise ValueError('invalid configuration')
            CONF.clear(); CONF.update(value)
            _CONFIG_STAMP = common.CONF_PATH.stat().st_mtime_ns
        except (OSError, ValueError):
            raise HTTPException(503, 'agent configuration unavailable')


@contextlib.contextmanager
def task_lock():
    with (DATA_DIR / 'task.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def atomic_text(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _check_token(tok: str | None) -> str:
    _refresh_conf()
    for actor, expected in CONF.get('tokens', {}).items():
        if tok and hmac.compare_digest(tok, expected):
            return actor
    raise HTTPException(status_code=401, detail="bad or missing X-Agent-Token")


def _append(to: str, record: dict) -> int:
    # The log is streamed, not slurped. Rewriting the whole file on every send put
    # the entire message history inside one os.replace window and made each send
    # cost grow with the history behind it. Dedup, the 409 and id ordering are
    # unchanged; the exclusive flock still serialises writers.
    with _LOCK, (DATA_DIR / 'message.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = MSG_DIR / f'{to}.jsonl'
        highest = 0
        if path.exists():
            with path.open('r', encoding='utf-8') as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    previous = json.loads(line)
                    highest = max(highest, int(previous.get('id', 0)))
                    if record.get('request_id') and previous.get('request_id') == record['request_id'] and previous.get('from') == record['from']:
                        if any(previous.get(k) != record.get(k) for k in ('type', 'text', 'body')):
                            raise HTTPException(409, 'request_id already used for different content')
                        return previous['id']
        seq = highest + 1
        record['id'] = seq
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    return seq


def _read_after(who: str, since: int) -> list[dict]:
    path = MSG_DIR / f"{who}.jsonl"
    if not path.exists():
        return []
    out = []
    # Streamed line by line: the watcher polls every 2s, so slurping the whole
    # log into memory on each poll cost ~43k full reads a day.
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("id", 0) > since:
                out.append(rec)
    return out


# --- SMB task share helpers ------------------------------------------------
def _smb_base() -> list[str]:
    cred = CONF['smb_credfile']
    share = f"//{CONF['smb_host']}/{CONF['smb_tasks_share']}"
    return ["smbclient", share, "-A", cred]


def _next_task_number() -> int:
    """Fail closed when the share cannot be listed; never use a stale counter."""
    try:
        r = subprocess.run(_smb_base() + ["-c", "ls"], capture_output=True,
                           text=True, timeout=25)
        mx = 0
        for tok in r.stdout.split():
            if tok.upper().startswith("TASK-") and tok.upper().endswith(".MD"):
                try:
                    mx = max(mx, int(tok.split("-", 1)[1].split(".")[0]))
                except ValueError:
                    pass
        if r.returncode == 0:
            counter = DATA_DIR / 'task_counter.json'
            if counter.exists():
                try:
                    mx = max(mx, int(json.loads(counter.read_text())['n']))
                except (ValueError, KeyError, TypeError) as error:
                    raise HTTPException(503, 'task counter requires recovery') from error
            n = mx + 1
            atomic_text(counter, json.dumps({'n': n}))
            return n
    except (subprocess.SubprocessError, OSError) as error:
        raise HTTPException(502, 'SMB task share unavailable') from error
    raise HTTPException(502, 'SMB task share cannot be listed')


def _write_task(n: int, sender: str, text: str, body: str) -> None:
    md = (
        f"# TASK-{n}\n\n"
        f"- From: {sender.upper()}\n"
        f"- To: {CONF['peer'].upper() if sender==CONF['self'] else CONF['self'].upper()}\n"
        f"- Created: {common.now_iso()}\n\n"
        f"## Instruction\n\n{text}\n\n"
        f"## Details\n\n{body or '(none)'}\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(md)
        tmp = tf.name
    staging = '.agentcomm-' + os.path.basename(tmp) + '.pending'
    try:
        r = subprocess.run(_smb_base() + ["-c", f"put {tmp} {staging}; rename {staging} TASK-{n}.md"],
                           capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as error:
        raise HTTPException(502, 'SMB task write unavailable') from error
    finally:
        Path(tmp).unlink(missing_ok=True)
    if r.returncode != 0:
        raise HTTPException(status_code=502,
                            detail='SMB task write failed')


# --- models ----------------------------------------------------------------
class Message(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra='forbid')
    from_: Agent = Field(alias="from")
    to: Agent
    text: str = Field(min_length=1, max_length=65536)
    request_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{8,80}$')


class TaskReq(Message):
    body: str = Field(default='', max_length=262144)


def authorize(req, token):
    actor = _check_token(token)
    if req.from_ != actor or req.to == actor or req.to not in CONF.get('tokens', {}):
        raise HTTPException(403, 'sender identity or recipient mismatch')
    return actor


def _require_write(actor):
    if not can_write(actor, conf=CONF):
        raise HTTPException(403, 'READ vault access cannot create tasks or write permanent memory')


def _storage_proxy(path, actor, payload=None, *, origin=None):
    """Forward to the physical vault origin, never to our active messaging URL."""
    origin = str((CONF.get('vault_origin') if origin is None else origin) or '').rstrip('/')
    if not origin or not origin.startswith(('https://', 'http://')):
        raise HTTPException(503, 'canonical storage origin unavailable')
    headers = {'X-Agent-Token': CONF.get('tokens', {}).get(actor, '')}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(origin + path, data=data, headers=headers)
    try:
        with http_open(request, timeout=45) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise HTTPException(error.code, 'canonical storage rejected the request') from error
    except (OSError, ValueError) as error:
        raise HTTPException(503, 'canonical storage origin unreachable') from error


def _storage_local():
    return CONF.get('storage_host', True) is True


# --- routes ----------------------------------------------------------------
@app.get("/api/health/")
def health():
    _refresh_conf()
    return {"ok": True, "service": "agentcomm", "host": CONF["self"]}


@app.post("/api/message/")
def post_message(msg: Message, x_agent_token: str | None = Header(default=None)):
    actor = authorize(msg, x_agent_token)
    rec = {"type": "message", "from": actor, "to": msg.to,
           "text": msg.text, "ts": common.now_iso(), 'request_id': msg.request_id}
    mid = _append(msg.to, rec)
    return {"ok": True, "id": mid, 'delivery': 'queued'}


@app.post("/api/create/")
def post_create(req: TaskReq, x_agent_token: str | None = Header(default=None)):
    actor = authorize(req, x_agent_token)
    # Allocate on the single storage authority, then notify on THIS host's
    # active inbox. One receipt ledger deduplicates retries across host changes.
    peer_vault = CONF.get('peer_vault_name')
    if (actor == CONF.get('peer') and peer_vault
            and can_write(actor, vault=peer_vault, conf=CONF)):
        # Independent Commanders share messaging, but each task is allocated by
        # its owner's storage server. This grants no write access to our vault.
        result = _storage_proxy('/api/storage/task', actor, req.model_dump(by_alias=True),
                                origin=CONF.get('peer_api_lan') or '')
    else:
        _require_write(actor)
        result = (storage_task(req, x_agent_token) if _storage_local() else
                  _storage_proxy('/api/storage/task', actor, req.model_dump(by_alias=True)))
    rec = {'type':'task', 'task_no':result['task_no'], 'from':actor,
           'to':req.to, 'text':req.text, 'body':req.body, 'ts':common.now_iso(),
           'request_id':req.request_id}
    _append(req.to, rec)
    return result


@app.get("/api/inbox/{who}")
def get_inbox(who: Agent, since: int = Query(default=0, ge=0),
              ack: bool = Query(default=False),
              expected_epoch: str | None = Query(default=None, max_length=64),
              x_agent_token: str | None = Header(default=None)):
    actor = _check_token(x_agent_token)
    if who != actor:
        raise HTTPException(403, 'only own inbox is readable')
    with _LOCK:
        epoch_path = DATA_DIR / 'inbox-epoch'
        if not epoch_path.exists():
            atomic_text(epoch_path, uuid.uuid4().hex)
        epoch = epoch_path.read_text().strip()
        if ack and expected_epoch is not None and expected_epoch != epoch:
            raise HTTPException(409, 'inbox epoch changed')
    presence_path = DATA_DIR / f'{who}.presence.json'
    previous = {}
    if presence_path.exists():
        try:
            previous = json.loads(presence_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError, TypeError):
            previous = {}
    acknowledged = int(previous.get('acknowledged_through', 0) or 0)
    if ack:
        acknowledged = max(acknowledged, since)
    atomic_text(presence_path, json.dumps({'seen': time.time(), 'acknowledged_through': acknowledged}))
    return {"ok": True, "messages": _read_after(who, since), 'inbox_epoch': epoch}


@app.get('/api/status/')
def status(x_agent_token: str | None = Header(default=None)):
    actor = _check_token(x_agent_token)
    peer = next((name for name in CONF.get('tokens', {}) if name != actor), CONF.get('peer', ''))
    path = DATA_DIR / f'{peer}.presence.json'
    presence = json.loads(path.read_text()) if path.exists() else {}
    age = time.time() - presence['seen'] if presence else None
    state = 'unknown' if age is None else 'passive' if age >= CONF.get('passive_seconds', 600) else 'fallback_due' if age >= CONF.get('fail_seconds', 300) else 'recently_seen'
    # The caller's own grant, so a peer can tell what this server believes it
    # may do instead of trusting only its own local file.
    return {'ok': True, 'peer': peer, 'state': state, 'last_seen_seconds_ago': age,
            'acknowledged_through': presence.get('acknowledged_through', 0),
            'vault_access': permission(actor, conf=CONF)}


# --- vault (ElizaMemory) read access ----------------------------------------
# Token-gated, read-only view of the canonical vault. In PUBLIC transport mode
# these routes are what lets the peer read durable knowledge cross-network when
# the LAN SMB mount is unreachable (Sondra is read-only by policy; Eliza writes
# locally on the host). Access is confined to the vault, blocks path traversal,
# symlink escape, dotfiles, and the protected .obsidian/ tree.
VAULT_ROOT = Path(CONF['vault_local'])
MAX_VAULT_READ = 2_000_000  # bytes; refuse to stream very large files


def _safe_vault_path(rel: str) -> Path:
    if '\\' in rel:
        raise HTTPException(403, 'backslash path separator denied')
    from skmr_agent_policy import check_root
    try:
        check_root(VAULT_ROOT)
    except PermissionError as error:
        raise HTTPException(503, 'canonical vault mount unavailable') from error
    if not VAULT_ROOT.exists():
        raise HTTPException(503, 'vault mount unavailable')
    root = VAULT_ROOT.resolve()
    target = (root / (rel or '').lstrip('/'))
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError) as error:
        raise HTTPException(400, 'bad path') from error
    if resolved != root and root not in resolved.parents:
        raise HTTPException(403, 'path escapes vault')
    parts = () if resolved == root else resolved.relative_to(root).parts
    if any(part == '.obsidian' or part.startswith('.') for part in parts):
        raise HTTPException(403, 'protected path')
    return resolved


@app.get('/api/vault/list')
def vault_list(path: str = Query(default=''), x_agent_token: str | None = Header(default=None)):
    actor = _check_token(x_agent_token)
    if not _storage_local():
        from urllib.parse import quote
        return _storage_proxy('/api/vault/list?path=' + quote(path, safe=''), actor)
    root = VAULT_ROOT.resolve()
    directory = _safe_vault_path(path)
    if not directory.is_dir():
        raise HTTPException(404, 'not a directory')
    entries = []
    for child in sorted(directory.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name.startswith('.'):
            continue
        entries.append({'name': child.name, 'dir': child.is_dir(),
                        'size': child.stat().st_size if child.is_file() else None})
    rel = '' if directory == root else str(directory.relative_to(root))
    return {'ok': True, 'path': rel, 'entries': entries}


@app.get('/api/vault/read')
def vault_read(path: str = Query(...), x_agent_token: str | None = Header(default=None)):
    actor = _check_token(x_agent_token)
    if not _storage_local():
        from urllib.parse import quote
        return _storage_proxy('/api/vault/read?path=' + quote(path, safe=''), actor)
    root = VAULT_ROOT.resolve()
    file = _safe_vault_path(path)
    if not file.is_file():
        raise HTTPException(404, 'not a file')
    if file.stat().st_size > MAX_VAULT_READ:
        raise HTTPException(413, 'file too large')
    try:
        content = file.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as error:
        raise HTTPException(415, 'unreadable as text') from error
    return {'ok': True, 'path': str(file.relative_to(root)), 'content': content}


@app.post('/api/config/roles')
def configure_roles(payload: dict, x_config_token: str | None = Header(default=None)):
    _refresh_conf()
    expected = CONF.get('config_admin_token', '')
    if not expected or not x_config_token or not hmac.compare_digest(expected, x_config_token):
        raise HTTPException(401, 'bad or missing administrative token')
    import roles
    global_conf = CONF
    try:
        with _LOCK:
            result = roles.apply_roles(payload)
            global_conf.clear(); global_conf.update(common.load_conf())
        return result
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except OSError as error:
        raise HTTPException(503, 'assignment could not be persisted') from error


@app.post('/api/storage/task')
def storage_task(req: TaskReq, x_agent_token: str | None = Header(default=None)):
    """Allocate on shared storage without enqueuing into an inactive inbox."""
    actor = authorize(req, x_agent_token)
    _require_write(actor)
    if not _storage_local():
        raise HTTPException(503, 'not the canonical storage host')
    with _LOCK, task_lock():
        receipts = DATA_DIR / 'storage-task-receipts.json'
        records = json.loads(receipts.read_text()) if receipts.exists() else {}
        key = actor + ':' + str(req.request_id or uuid.uuid4().hex)
        old = records.get(key)
        content = req.model_dump(by_alias=True)
        if old:
            if old['request'] != content:
                raise HTTPException(409, 'request_id already used for different content')
            return {'ok':True, 'task_no':old['n'], 'duplicate':True}
        if req.request_id:
            for old in _read_after(req.to, 0):
                if old.get('request_id') == req.request_id and old.get('from') == actor:
                    if old.get('type') != 'task' or old.get('text') != req.text or old.get('body','') != req.body:
                        raise HTTPException(409, 'request_id already used for different content')
                    return {'ok':True, 'task_no':old['task_no'], 'duplicate':True}
        n = _next_task_number()
        _write_task(n, actor, req.text, req.body)
        records[key] = {'n':n, 'request':content}
        atomic_text(receipts, json.dumps(records))
    return {'ok':True, 'task_no':n}


class WriterReq(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['route','search','preview','commit','validate','lint','init','log']
    candidate: dict | None = None
    token: str | None = Field(default=None, max_length=80)
    query: str = Field(default='', max_length=4096)
    mode: Literal['hybrid','bm25','vector'] | None = None
    limit: int = Field(default=10, ge=1, le=100)
    profile: str | None = Field(default=None, max_length=128)
    profile_section: str | None = Field(default=None, max_length=128)
    semantic_class: str | None = None
    title: str | None = Field(default=None, max_length=256)
    project: str | None = None
    profile_username: str | None = None
    entry_action: str = Field(default='', max_length=256)
    detail: str = Field(default='', max_length=4096)


_WRITER = None
def _writer_module():
    global _WRITER
    if _WRITER is None:
        path = Path(CONF.get('writer') or common.BASE_DIR.parent / '.claude/skills/skmr/scripts/obsidian_memory.py')
        spec = importlib.util.spec_from_file_location('agentcomm_canonical_writer', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _WRITER = module
    return _WRITER


def _writer_action(req, actor):
    if req.action == 'search' and req.mode is not None and not req.profile and not req.profile_section:
        from dataclasses import asdict
        sys.path.insert(0, str(common.BASE_DIR.parent/'.claude'))
        from skmr.memory.retrieval import hybrid
        outcome = hybrid.search(req.query, mode=req.mode, limit=req.limit)
        return dict(asdict(outcome), ok=outcome.ok)
    writer = _writer_module()
    from skmr_agent_policy import acting_as
    with acting_as(actor, CONF):
        store = writer.VaultStore(VAULT_ROOT, DATA_DIR/'writer-state')
        if req.action == 'preview':
            if not isinstance(req.candidate, dict):
                raise HTTPException(422, 'candidate object required')
            return store.preview(req.candidate)
        if req.action == 'commit': return store.commit(req.token or '')
        if req.action == 'search': return store.search(req.query, req.limit, req.profile, req.profile_section)
        if req.action == 'validate': return store.validate_vault()
        if req.action == 'lint': return store.lint_vault()
        if req.action == 'init': return store.init_layers()
        if req.action == 'log': return {'ok':True, 'logged':store.append_log(req.entry_action, req.detail)}
        relative, note_type, area = store.route(req.model_dump())
        return {'relative':str(relative), 'type':note_type, 'area':area}


def _execute_writer(req, actor):
    if req.action in {'preview','commit','init','log'}: _require_write(actor)
    if not _storage_local():
        return _storage_proxy('/api/vault/action', actor, req.model_dump())
    owner_path = DATA_DIR/'preview-owners.json'
    receipt_path = DATA_DIR/'commit-receipts.json'
    with _LOCK:
        owners = json.loads(owner_path.read_text()) if owner_path.exists() else {}
        receipts = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        if req.action == 'commit' and req.token in receipts:
            old = receipts[req.token]
            if old.get('actor') != actor:
                raise HTTPException(403, 'preview token does not belong to this actor')
            return dict(old['result'], duplicate=True)
        if req.action == 'commit' and owners.get(req.token) != actor:
            raise HTTPException(403, 'preview token does not belong to this actor')
        try:
            result = _writer_action(req, actor)
        except (ValueError, OSError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from error
        if req.action == 'preview' and result.get('token'):
            owners[result['token']] = actor
            atomic_text(owner_path, json.dumps(owners))
        if req.action == 'commit':
            receipts[req.token] = {'actor':actor, 'result':result}
            atomic_text(receipt_path, json.dumps(receipts))
            owners.pop(req.token, None)
            atomic_text(owner_path, json.dumps(owners))
    return result


@app.post('/api/vault/action')
def vault_action(req: WriterReq, x_agent_token: str | None = Header(default=None)):
    return _execute_writer(req, _check_token(x_agent_token))


@app.post('/api/vault/preview')
def vault_preview(payload: dict, x_agent_token: str | None = Header(default=None)):
    return _execute_writer(WriterReq(action='preview', candidate=payload.get('candidate')), _check_token(x_agent_token))


@app.post('/api/vault/commit')
def vault_commit(payload: dict, x_agent_token: str | None = Header(default=None)):
    return _execute_writer(WriterReq(action='commit', token=payload.get('token')), _check_token(x_agent_token))
