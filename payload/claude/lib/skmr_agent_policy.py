"""Persistent SKMR agent boundary. Temp test vaults are not permanent memory."""
import json
import math
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from skmr_permissions import READ, WRITE, can_write, permission, config_path
from skmr_http import http_open
CONFIG_PATH = config_path()
CONFIG = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
ROLE = CONFIG['self']
VAULT = Path(CONFIG['vault_local'])
DATA_DIR = CONFIG_PATH.parent / 'data'
CONNECTION_STATE = DATA_DIR / 'connection.json'
_authenticated_actor = ContextVar('skmr_authenticated_actor', default=None)
VAULT_PROBE_PATH = '/api/vault/list?path='
def _probe_number(name, default, *, positive=False):
    try:
        value = float(os.environ.get(name, default))
        if not math.isfinite(value) or (positive and value <= 0):return default
        return max(0, value)
    except ValueError:return default


VAULT_PROBE_TIMEOUT = _probe_number('SKMR_VAULT_PROBE_TIMEOUT', 8, positive=True)
VAULT_PROBE_TTL_DEFAULT = 30
# One probe per call would turn every availability check into network latency.
def vault_probe_ttl():
    return _probe_number('SKMR_VAULT_PROBE_TTL', VAULT_PROBE_TTL_DEFAULT)


_probe_cache = {'at': 0.0, 'ok': False, 'value': False}
_PROBE_CACHE = _probe_cache

SMB_BACKING = 'smb'
LOCAL_BACKING = 'local'


def vault_backing():
    """How the canonical vault is physically provided: 'smb' (default) or 'local'.

    Declared in agent.conf. It is read fresh rather than captured at import, so
    changing it takes effect on the next call, like the vault grants. An
    unrecognised value falls back to 'smb', the stricter of the two.
    """
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding='utf-8')).get('vault_backing')
    except (OSError, ValueError):
        value = CONFIG.get('vault_backing')
    value = str(value or SMB_BACKING).casefold()
    return LOCAL_BACKING if value == LOCAL_BACKING else SMB_BACKING


def _probe_enumerable(vault):
    """Raise OSError if the mount exists but its contents cannot be listed.

    A stale CIFS mount keeps its mountpoint and dentry, so is_mount() and
    os.access() both still pass while reading the contents fails with
    ENODEV/EIO. Path.rglob() swallows that OSError mid-traversal and yields
    nothing, which is what turned an unreachable vault into "notes: 0, ok: true".
    Enumerating one entry is the only probe that separates "holds no notes" from
    "cannot be reached".
    """
    with os.scandir(vault) as entries:
        next(entries, None)


@contextmanager
def acting_as(actor, conf):
    """Server-only scope after authenticated WRITE authorization.

    No environment variable can select this actor. Context-local state prevents
    simultaneous authenticated requests from sharing authorization.
    """
    token = _authenticated_actor.set((actor, conf))
    try:
        yield
    finally:
        _authenticated_actor.reset(token)


def check_root(vault, writable=False):
    vault = Path(vault)
    if vault.absolute() == VAULT.absolute() or vault.resolve(strict=False) == VAULT.resolve(strict=False):
        authenticated = _authenticated_actor.get()
        # conf=None on purpose: the grant is re-read from the file on every call,
        # so revoking WRITE takes effect on the next request instead of surviving
        # in a cached copy until the process restarts.
        actor, conf = authenticated if authenticated else (ROLE, None)
        if writable:
            if authenticated:
                # A server decided this on its own configuration: that IS the
                # authority, so it is not second-guessed here.
                if not can_write(actor, conf=conf):
                    raise PermissionError('Assigned READ vault access cannot write permanent memory; delegate a candidate to the configured WRITE peer via /skmr:send.')
            elif not can_write(actor, conf=conf):
                raise PermissionError('Assigned READ vault access cannot write permanent memory; delegate a candidate to the configured WRITE peer via /skmr:send.')
            elif authoritative_write_grant() == READ:
                # Local grant says write, the canonical service says read. The
                # local file is self-declared, so the authoritative answer wins.
                raise PermissionError('Local grant claims WRITE but the authoritative canonical service reports READ for this agent; refusing a self-granted write.')
        if vault_backing() == LOCAL_BACKING:
            # A single-machine installation has no SMB mount at all, so the
            # is_mount() rule below would leave its vault permanently unwritable.
            # That rule still holds for its real purpose: an unmounted stub
            # directory must never pass as an empty vault. For a local vault the
            # discriminator is the Obsidian marker, which a stub cannot have and
            # which the installer verifies before recording this backing. The
            # backing is declared in agent.conf and never inferred from the
            # filesystem; inferring it would silently reintroduce exactly the
            # failure this refuses.
            if not vault.is_dir():
                raise PermissionError('Local vault directory does not exist: ' + str(vault))
            if not (vault / '.obsidian').is_dir():
                raise PermissionError(
                    'Local vault has no .obsidian directory, so it is not an Obsidian '
                    'vault; refusing to treat it as canonical memory: ' + str(vault))
        elif not vault.is_mount():
            raise PermissionError('SMB vault is not mounted; refusing an empty local directory: ' + str(vault))
        try:
            _probe_enumerable(vault)
        except OSError as error:
            raise PermissionError(
                'SMB vault is mounted but cannot be enumerated ('
                + (error.strerror or str(error))
                + '); refusing to treat an unreachable vault as empty: '
                + str(vault)
            ) from error
        if not os.access(vault, os.R_OK | (os.W_OK if writable else 0)):
            raise PermissionError('Canonical vault lacks physical ' + ('read/write' if writable else 'read') + ' access: ' + str(vault))


VAULT_GRANT_TTL = 30
_GRANT_CACHE = {"at": 0.0, "value": None}


def _probe_write_grant(base, token):
    """Ask an endpoint what IT thinks this agent may do. None when it cannot say."""
    import urllib.request
    request = urllib.request.Request(base + '/api/status/', method='GET')
    request.add_header('X-Agent-Token', str(token or ''))
    try:
        with urllib.request.urlopen(request, timeout=VAULT_PROBE_TIMEOUT) as response:
            payload = json.loads(response.read().decode())
    except Exception:
        return None
    value = payload.get('vault_access') if isinstance(payload, dict) else None
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def authoritative_write_grant(force=False):
    """What the canonical API says this agent's grant is, or None if nobody says.

    The local config file is the weakest layer: an agent that can edit it can
    claim any grant. Where a canonical service answers, its word outranks the
    local file. Silence is not a denial, so an unreachable service leaves the
    local decision intact instead of blocking a legitimate writer.
    """
    import time
    ttl = vault_probe_ttl()
    if not force and ttl and (time.time() - _GRANT_CACHE["at"]) < ttl:
        return _GRANT_CACHE["value"]
    answer = None
    for base in vault_read_endpoints():
        answer = _probe_write_grant(base, CONFIG.get('token'))
        if answer is not None:
            break
    _GRANT_CACHE.update(at=time.time(), value=answer)
    return answer


def may_write_vault():
    """Local grant AND, where an authority answers, its agreement."""
    if not can_write(conf=CONFIG):
        return False                       # a read grant is never upgraded
    return authoritative_write_grant() != READ


def vault_mount_usable():
    if vault_backing() == LOCAL_BACKING:
        # Same discriminator as check_root: a declared local vault is usable
        # when it is a real Obsidian vault, and unusable when it is a bare stub.
        if not (VAULT.is_dir() and (VAULT / '.obsidian').is_dir()):
            return False
        if not os.access(VAULT, os.R_OK):
            return False
        try:
            _probe_enumerable(VAULT)
        except OSError:
            return False
        return True
    if not (VAULT.is_mount() and VAULT.is_dir()):
        return False
    if not os.access(VAULT, os.R_OK):
        return False
    try:
        _probe_enumerable(VAULT)
    except OSError:
        return False
    return True


def vault_mount_writable():
    """Physical capability only; assigned permission is checked separately."""
    try:
        return vault_mount_usable() and not (os.statvfs(VAULT).f_flag & os.ST_RDONLY) and os.access(VAULT, os.W_OK)
    except OSError:
        return False


def _connection_state():
    try:
        state = json.loads(CONNECTION_STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def vault_read_endpoints():
    """Same ordering send.py uses: in public mode the peer reaches the host only
    through the tunnel, so it leads and the LAN bases stay behind it."""
    # Independent vaults use their own storage authority. A shared messaging
    # host may legitimately grant this actor READ to a different vault.
    origin = CONFIG.get('vault_origin')
    if isinstance(origin, str) and origin.startswith(('http://', 'https://')):
        return [origin.rstrip('/')]
    state = _connection_state()
    endpoints = []
    host = state.get('transport_role', CONFIG.get('transport_role', 'consumer')) == 'host'
    if host:
        endpoints.append(CONFIG.get('server_local_url') or 'http://127.0.0.1:8080')
    elif state.get('mode') != 'public' and CONFIG.get('peer_api_lan'):
        endpoints.append(CONFIG['peer_api_lan'])
    if state.get('mode') == 'public' and state.get('public_url') and not host:
        endpoints.append(state['public_url'])
    endpoints += [CONFIG.get('api_base'), CONFIG.get('api_lan'), CONFIG.get('api_fallback')]
    return list(dict.fromkeys(e.rstrip('/') for e in endpoints if e))


def _probe_vault_route(base):
    request = urllib.request.Request(base.rstrip('/') + VAULT_PROBE_PATH, method='GET')
    request.add_header('X-Agent-Token', CONFIG['token'])
    try:
        with http_open(request, timeout=VAULT_PROBE_TIMEOUT) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    return bool(payload.get('ok')) and isinstance(payload.get('entries'), list)


def vault_http_readable(force=False):
    """A readable route proves availability, never assigned WRITE permission."""
    now = time.monotonic()
    ttl = vault_probe_ttl()
    if not force and ttl and now - _probe_cache['at'] < ttl:
        return _probe_cache['ok']
    ok = any(_probe_vault_route(base) for base in vault_read_endpoints())
    _probe_cache['at'] = now
    _probe_cache['ok'] = ok
    _probe_cache['value'] = ok
    return ok


def vault_available():
    # An unreachable mount is not an empty vault, and it is no longer the only
    # way in: when SMB is down the authenticated HTTP route still serves reads.
    return vault_mount_usable() or vault_http_readable()
