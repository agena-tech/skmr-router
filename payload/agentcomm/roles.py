"""Transactional, machine-neutral role configuration used by CLI and admin API."""
from __future__ import annotations
import fcntl
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

# Installed beside /root/.claude, or in a relocatable isolated checkout.
LIB = Path(os.environ.get('AGENTCOMM_LIB', str(Path(__file__).resolve().parent.parent / '.claude/lib')))
if not (LIB / 'skmr_permissions.py').exists():
    LIB = Path('/root/.claude/lib')
sys.path.insert(0, str(LIB))
from skmr_permissions import config_path, validate_permissions


def agents_path(conf=None):
    conf = conf or {}
    return Path(os.environ.get('SKMR_AGENTS_PATH') or conf.get('agents_path') or '/root/.claude/skmr/state/agents.json')


def connection_path():
    return Path(os.environ.get('AGENTCOMM_CONNECTION_STATE') or config_path().parent / 'data/connection.json')


def instruction_block(current):
    topology = _topology_module()
    return '\n'.join(['<!-- SKMR_AGENT_TOPOLOGY_START -->', '## Agent Topology', '',
        topology.render_markdown(current), '',
        'READ (onlyread) reads the vault. WRITE reads and writes the vault.',
        'Vault grants are independent of agent rank and communication host role.', '',
        '<!-- SKMR_AGENT_TOPOLOGY_END -->'])


def update_instruction_file(path, current):
    start, end = '<!-- SKMR_AGENT_TOPOLOGY_START -->', '<!-- SKMR_AGENT_TOPOLOGY_END -->'
    original = path.read_text(encoding='utf-8') if path.exists() else ''
    if original.count(start) != original.count(end) or original.count(start) > 1:
        raise ValueError('CLAUDE.md topology delimiters are malformed')
    block = instruction_block(current)
    if start in original:
        if original.index(start) > original.index(end):
            raise ValueError('CLAUDE.md topology delimiters are reversed')
        head, _, rest = original.partition(start)
        _, _, tail = rest.partition(end)
        updated = head + block + tail
    else:
        updated = original.rstrip() + '\n\n' + block + '\n'
    if updated == original:
        return ''
    backup = ''
    if path.exists():
        backup = str(path.with_name(f'{path.name}.bak-{time.time_ns()}'))
        _atomic(Path(backup), original.encode())
    try:
        _atomic(path, updated.encode())
    except Exception:
        if backup:
            Path(backup).unlink(missing_ok=True)
        raise
    return backup


def _atomic(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def assignment_lock():
    """Shared lock lets CLI include its prose stores in the same transaction."""
    target = config_path().with_suffix('.roles.lock')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _topology_module():
    # The CLI already has this package; standalone API must not load CLI hooks.
    if 'skmr.agents.topology' in sys.modules:
        return sys.modules['skmr.agents.topology']
    root = Path(os.environ.get('SKMR_CODE_ROOT', str(Path(__file__).resolve().parent.parent / '.claude')))
    if not (root / 'skmr/agents/topology.py').exists():
        root = Path('/root/.claude')
    sys.path.insert(0, str(root))
    from skmr.agents import topology
    return topology


def transport_addresses(conf, agents, me, peer):
    """Return conf with the LAN addresses this assignment carries.

    The assignment owns the durable addressing. A public tunnel URL is not
    configuration: it is reissued whenever the tunnel restarts, so any copy kept
    in agent.conf is stale by the next session and sends role synchronisation at
    an endpoint that no longer exists. It is dropped here and resolved live from
    the connection state instead. An agent that supplies no LAN address leaves
    the existing value untouched rather than erasing a working one.
    """
    updated = dict(conf)
    updated.pop('peer_admin_url', None)
    port = int(conf.get('listen_port', 8080) or 8080)
    for item in agents:
        if not isinstance(item, dict):
            continue
        lan = str(item.get('lan') or '').strip()
        if not lan:
            continue
        key = str(item.get('name', '')).strip().casefold()
        field = 'api_lan' if key == me else ('peer_api_lan' if key == peer else '')
        if field:
            updated[field] = f'http://{lan}:{port}'
    return updated


def _apply(payload):
    conf = json.loads(config_path().read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('agents'), list):
        raise ValueError('roles payload requires an agents list')
    topology = _topology_module()
    me = str(conf.get('self', '')).strip().casefold()
    peer = str(conf.get('peer', '')).strip().casefold()
    expected = {me, peer} - {''}
    if not me:
        raise ValueError('agent.conf self identity is required')
    roster = {}
    for item in payload['agents']:
        if not isinstance(item, dict):
            raise ValueError('each agent must be an object')
        name = topology.validate_name(item.get('name', ''))
        key = name.casefold()
        if key in roster:
            raise ValueError(f'duplicate agent identity: {name}')
        roster[key] = dict(item, name=name)
    if set(roster) != expected:
        raise ValueError('complete self+peer roster required; identity rename needs separate credential migration')
    permissions = validate_permissions(payload.get('vault_permissions'), list(roster))
    hosts = payload.get('host_roles', {})
    if not isinstance(hosts, dict):
        raise ValueError('host_roles must be a mapping')
    normalized_hosts = {}
    for name, value in hosts.items():
        key = str(name).strip().casefold()
        if key not in roster or key in normalized_hosts or value not in ('host', 'consumer'):
            raise ValueError('host_roles must grant host or consumer once per known identity')
        normalized_hosts[key] = value
    if set(normalized_hosts) != expected or list(normalized_hosts.values()).count('host') != 1:
        raise ValueError('host_roles requires the complete roster with exactly one communication host')
    vault = str(conf.get('vault_name') or conf.get('smb_vault_share') or '')
    if vault not in permissions:
        raise ValueError('permissions must include the configured vault_name or smb_vault_share')
    built = {}
    for key, item in roster.items():
        built[key] = topology.Agent(name=item['name'], role=topology.validate_role(item.get('role', '')),
            host=item.get('host', ''), lan=str(item.get('lan') or '').strip(),
            reports_to=item.get('reports_to', ''),
            kind='local' if key == me else 'remote', vault_access=permissions[vault][key],
            vault_permissions={v: grants[key] for v, grants in permissions.items()})
        if built[key].host:
            topology.validate_host(built[key].host)
        if 'vault_access' in item and item['vault_access']:
            topology.validate_vault_access(item['vault_access'])
        if 'vault_permissions' in item:
            if not isinstance(item['vault_permissions'], dict):
                raise ValueError('per-agent vault_permissions must be a mapping')
            for access in item['vault_permissions'].values():
                topology.validate_vault_access(access)
    current = topology.Topology(local=built[me], remote=[a for key, a in built.items() if key != me],
                               updated_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    topology.validate(current)
    from dataclasses import asdict
    raw = {'local': asdict(current.local), 'remote': [asdict(a) for a in current.remote], 'updated_at': current.updated_at}
    updated = dict(transport_addresses(conf, payload['agents'], me, peer),
                   vault_permissions=permissions, host_roles=normalized_hosts,
                   transport_role=normalized_hosts[me])
    from skmr.memory import native
    instruction_path = topology.config.path('claude_md')
    paths = (config_path(), agents_path(conf), connection_path(), native.memory_path(), instruction_path)
    snapshots = {p: p.read_bytes() if p.exists() else None for p in paths}
    touched = []
    backup = ''
    try:
        for target, value in zip(paths[:2], (updated, raw)):
            _atomic(target, (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode())
            touched.append(target)
        try:
            state = json.loads(paths[2].read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        # send.py reads transport_role from HERE in preference to agent.conf, so
        # the assignment has to reach this file -- setdefault left a machine
        # reassigned from host to consumer still addressing its own dead service
        # and reporting "all endpoints unreachable (127.0.0.1)" while every real
        # endpoint answered 200.
        #
        # The one case that must NOT follow the assignment is a live public
        # route: tearing that down mid-session would strand the peer, so an
        # active tunnel keeps its role until the connection workflow changes it.
        live_public = state.get('mode') == 'public' and bool(state.get('public_url'))
        if live_public:
            state.setdefault('transport_role', updated['transport_role'])
        else:
            state['transport_role'] = updated['transport_role']
        state.setdefault('mode', 'private')
        state.setdefault('public_url', '')
        _atomic(paths[2], (json.dumps(state, indent=2) + '\n').encode())
        touched.append(paths[2])
        touched.append(paths[3])
        native.mutate(lambda state: state.set('Active Agent Topology', topology.render_markdown(current)))
        touched.append(paths[4])
        backup = update_instruction_file(paths[4], current)
    except Exception:
        for target in reversed(touched):
            data = snapshots[target]
            if data is None:
                target.unlink(missing_ok=True)
            else:
                if not target.exists() or target.read_bytes() != data:
                    _atomic(target, data)
        if backup:
            Path(backup).unlink(missing_ok=True)
        raise
    return {'ok': True, 'self': conf['self'], 'transport_role': updated['transport_role'], 'agents_path': str(paths[1]), 'backup': backup}


def apply_roles(payload, *, locked=False):
    """Validate then atomically replace conf + topology; rollback on failure.

    Stable conf.self/peer/token/tokens and all unrelated keys are retained.
    The administrative route must authenticate X-Config-Token before calling.
    """
    if locked:
        return _apply(payload)
    with assignment_lock():
        return _apply(payload)
