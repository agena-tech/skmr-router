"""Preserving role/vault assignment with transactional stores and peer sync."""
from __future__ import annotations
import argparse
import copy
import ipaddress
import json
import os
import socket
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path
from ..core import config, output
from ..agents import topology
from ..agents.topology import Agent, ValidationError
from ..memory import native

START = '<!-- SKMR_AGENT_TOPOLOGY_START -->'
END = '<!-- SKMR_AGENT_TOPOLOGY_END -->'


def _interactive():
    return sys.stdin.isatty()


def _ask(prompt, default=''):
    try:
        answer = input(prompt + (f' [{default}]' if default else '') + ': ').strip()
    except EOFError:
        answer = ''
    return answer or default


def _roles():
    root = Path(os.environ.get('AGENTCOMM_CODE_ROOT', str(Path(__file__).resolve().parents[3] / 'agentcomm')))
    if not (root / 'roles.py').exists():
        root = Path('/root/agentcomm')
    sys.path.insert(0, str(root))
    import roles
    return roles


def _show():
    current = topology.load()
    if not current.local:
        output.warning('no topology assigned yet — run /skmr:assign-role')
        return 0
    print(topology.render_markdown(current))
    print(f'updated: {current.updated_at or "unknown"}\nsource: {topology.path()}')
    return 0


def _claude_md_block(current):
    return _roles().instruction_block(current)


def _update_claude_md(current):
    return _roles().update_instruction_file(config.path('claude_md'), current)


def _host_roles(conf, agents):
    ids = {a.name.casefold() for a in agents}
    existing = conf.get('host_roles', {})
    if isinstance(existing, dict) and {str(k).casefold() for k in existing} == ids:
        return {str(k).casefold(): v for k, v in existing.items()}
    me = str(conf.get('self', '')).casefold()
    role = conf.get('transport_role', 'consumer')
    if 'transport_role' not in conf and len(ids) > 1:
        raise ValueError('unassigned communication host; provide --host-role and --remote-host-role')
    return {key: role if key == me else ('consumer' if role == 'host' else 'host') for key in ids}


def _permissions(conf, current):
    vault = str(conf.get('vault_name') or conf.get('smb_vault_share') or '')
    if not vault:
        raise ValueError('agent.conf vault_name or smb_vault_share is required')
    raw = copy.deepcopy(conf.get('vault_permissions') or {})
    if vault not in raw:
        raw[vault] = {a.name.casefold(): a.access() for a in current.agents()}
    for agent in current.agents():
        for name, access in agent.vault_permissions.items():
            raw.setdefault(name, {}).setdefault(agent.name.casefold(), access)
    return raw, vault


def _sync_endpoint(conf):
    state_path = Path(os.environ.get('AGENTCOMM_CONNECTION_STATE') or _roles().config_path().parent / 'data/connection.json')
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    role = state.get('transport_role', conf.get('transport_role'))
    endpoint = peer_admin_endpoint(conf, state)
    if not endpoint:
        raise ValueError(
            'peer administration endpoint unavailable: no active public tunnel and no '
            "peer LAN address. Assign the remote agent's LAN address, then retry --sync-peer"
        )
    parsed = urllib.parse.urlsplit(str(endpoint))
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('invalid peer administration endpoint')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    if is_local_endpoint(parsed.hostname) and port == int(conf.get('listen_port', 8080)):
        raise ValueError('peer endpoint points to this machine; use a peer URL or distinct SSH tunnel port')
    # api_base is deliberately NOT compared: on a consumer it points at the PEER,
    # so matching against it rejected the one correct address and disabled
    # --sync-peer. api_lan and server_local_url are this machine by construction.
    if str(endpoint).rstrip('/') in {str(conf.get(key, '')).rstrip('/') for key in ('api_lan', 'server_local_url') if conf.get(key)}:
        raise ValueError('peer administration endpoint matches this host API; use the remote machine endpoint')
    reject_self_endpoint(endpoint, conf)
    return str(endpoint).rstrip('/') + '/api/config/roles'


def peer_admin_endpoint(conf, state=None):
    """Where role synchronisation should reach the peer, resolved live.

    Order matters and reflects what each address is worth. An active public
    tunnel is used while it is up, because that is the only route when the LAN
    is down -- but it is read from the connection state, never from a stored
    field, so an expired URL cannot be used. The configured LAN address is the
    durable path and the fallback. Returns None when neither exists, so the
    caller can say so instead of posting a secret into the void.
    """
    if state is None:
        try:
            state = json.loads(_roles().connection_path().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
    if not isinstance(state, dict):
        state = {}
    # Only the consumer may use it: on the host, public_url is the host's OWN
    # tunnel, so treating it as the peer's address points the secret at itself.
    hosting = state.get('transport_role', conf.get('transport_role', 'consumer')) == 'host'
    if not hosting and state.get('mode') == 'public' and state.get('public_url'):
        return str(state['public_url']).rstrip('/')
    lan = str(conf.get('peer_api_lan') or '').strip()
    return lan.rstrip('/') or None


def local_addresses() -> set[str]:
    """Every address this machine answers on, loopback included.

    Recognising only the loopback names would let a host whose endpoint is
    written as its OWN LAN address pass the self-check and administer itself
    through what it thinks is the peer.
    """
    found = {'127.0.0.1', '::1'}
    for name in {'localhost', socket.gethostname()}:
        try:
            for info in socket.getaddrinfo(name, None):
                found.add(str(info[4][0]).split('%')[0])
        except (OSError, UnicodeError):
            continue
    # The hostname often resolves to loopback only, which would hide the very
    # address a peer endpoint is most likely written as. Asking the routing table
    # which source address would be used reveals it without sending a packet.
    for probe in (('198.51.100.1', socket.AF_INET), ('2001:db8::1', socket.AF_INET6)):
        try:
            with socket.socket(probe[1], socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.2)
                sock.connect((probe[0], 9))
                found.add(str(sock.getsockname()[0]).split('%')[0])
        except OSError:
            continue
    return found


def is_local_endpoint(host: str) -> bool:
    """True when this hostname or address belongs to this machine."""
    value = str(host or '').strip().strip('[]').casefold()
    if not value:
        return False
    if value in {'localhost', socket.gethostname().casefold()}:
        return True
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return False
    if parsed.is_loopback or parsed.is_unspecified:
        return True
    for address in local_addresses():
        try:
            if ipaddress.ip_address(address) == parsed:
                return True
        except ValueError:
            continue
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('peer endpoint redirected; refusing to forward the administration secret')


def _endpoint_identity(endpoint):
    """Who does this endpoint say it is? None when it will not say.

    /api/health/ needs no credentials, so asking costs nothing and leaks nothing.
    """
    probe = urllib.request.Request(str(endpoint).rstrip('/') + '/api/health/', method='GET')
    try:
        with urllib.request.build_opener(_NoRedirect()).open(probe, timeout=8) as response:
            payload = json.loads(response.read(4096))
    except Exception:
        return None
    value = payload.get('host') if isinstance(payload, dict) else None
    return str(value).strip().casefold() if isinstance(value, str) and value.strip() else None


def reject_self_endpoint(endpoint, conf):
    """Guard against ACCIDENTALLY posting the administrative secret to ourselves.

    This is a footgun guard, NOT authentication, and the difference matters. The
    answer comes from whoever controls the endpoint, so a hostile one simply will
    not claim to be us and sails past this check; nothing here authorises
    anything. The only thing protecting the administrative secret is the
    config_admin_token comparison on the receiving side.

    What it does buy: an address test cannot answer "is this me" at all, because
    behind NAT or a WSL port proxy this host's own public address belongs to no
    local interface -- it looks remote while the traffic comes straight back. For
    that misconfiguration the service's own answer is exactly the right signal. A
    silent endpoint proves nothing and is left to fail later on its own terms.
    """
    # Normalised here as well as at the source: this comparison must hold however
    # the answer reached it.
    reported = _endpoint_identity(endpoint)
    reported = str(reported).strip().casefold() if reported else None
    mine = str(conf.get('self') or '').strip().casefold()
    if reported and mine and reported == mine:
        raise ValueError(
            'peer administration endpoint identifies as this machine '
            f'({reported}); use the remote agent endpoint'
        )


def _sync_peer(payload, conf):
    secret = conf.get('config_admin_token', '')
    if not isinstance(secret, str) or not secret:
        raise ValueError('config_admin_token is not provisioned; retry --sync-peer after provisioning it')
    if secret == conf.get('token') or secret in (conf.get('tokens') or {}).values():
        raise ValueError('config_admin_token must be separate from agent tokens')
    request = urllib.request.Request(_sync_endpoint(conf), data=json.dumps(payload).encode(), method='POST',
        headers={'Content-Type': 'application/json', 'X-Config-Token': secret})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
        reply = json.loads(response.read(65536))
    if not isinstance(reply, dict) or reply.get('ok') is not True or str(reply.get('self', '')).casefold() != str(conf.get('peer', '')).casefold():
        raise ValueError('peer did not acknowledge persisted roles for the expected identity')


def _persist(current, permissions, host_roles, sync_peer=False):
    roles = _roles()
    payload = {'agents': [asdict(a) for a in current.agents()], 'vault_permissions': permissions, 'host_roles': host_roles}
    backup = ''
    peer_status = 'local only; peer not updated'
    with roles.assignment_lock():
        paths = {roles.config_path(), topology.path(), native.memory_path(), config.path('claude_md'), roles.connection_path()}
        snapshots = {p: p.read_bytes() if p.exists() else None for p in paths}
        try:
            conf = json.loads(roles.config_path().read_text(encoding='utf-8'))
            result = roles.apply_roles(payload, locked=True)
            backup = result.get('backup', '')
            current = topology.load()
            if sync_peer:
                _sync_peer(payload, conf)
                peer_status = 'peer persisted'
        except Exception as exc:
            for path, contents in snapshots.items():
                if contents is None:
                    path.unlink(missing_ok=True)
                elif not path.exists() or path.read_bytes() != contents:
                    roles._atomic(path, contents)
            if backup:
                Path(backup).unlink(missing_ok=True)
            output.error(f'role assignment rolled back locally: {exc}. Peer persistence unconfirmed; retry with the same flags and --sync-peer.')
            return 2 if isinstance(exc, ValidationError) else 1
    output.info(f'Roles assigned ({peer_status}).')
    print(topology.render_markdown(current))
    print(f'configured communication roles: {host_roles}')
    try:
        active = json.loads(roles.connection_path().read_text(encoding='utf-8'))
        print(f'active connection: {active.get("mode", "private")} / {active.get("transport_role", "unassigned")}')
        if active.get('transport_role') != host_roles.get(current.local.name.casefold()):
            print('Configured host change pending: use the connection public workflow to activate the selected host and relay its URL.')
    except (OSError, ValueError):
        pass
    if backup:
        print(f'CLAUDE.md backup: {backup}')
    return 0


def run(args, plan):
    if args and args[0].casefold() == 'show':
        args = ['--show', *args[1:]]
    parser = argparse.ArgumentParser(prog='assign-role', exit_on_error=False)
    for flag in ('name', 'role', 'host', 'lan', 'reports-to', 'remote-name', 'remote-role', 'remote-host', 'remote-lan', 'remote-reports-to', 'vault'):
        parser.add_argument('--' + flag)
    for flag in ('local-access', 'remote-access'):
        parser.add_argument('--' + flag, choices=('read', 'write'), type=str.casefold)
    for flag in ('host-role', 'remote-host-role'):
        parser.add_argument('--' + flag, choices=('host', 'consumer'))
    for flag in ('non-interactive', 'show', 'sync-peer', 'local-only'):
        parser.add_argument('--' + flag, action='store_true')
    try:
        options = parser.parse_args(args)
    except (argparse.ArgumentError, SystemExit) as exc:
        output.error(f'invalid assignment arguments: {exc}')
        return 2
    if options.show:
        return _show()
    if options.local_only and options.sync_peer:
        output.error('--local-only and --sync-peer cannot be combined')
        return 2
    current = copy.deepcopy(topology.load())
    interactive = not options.non_interactive and _interactive()
    try:
        if current.local is None:
            current.local = Agent('', '')
        for key in ('name', 'role', 'host', 'lan', 'reports_to'):
            value = getattr(options, key)
            if value is not None:
                setattr(current.local, key, value)
        if interactive:
            current.local.name = _ask('Agent adı ne olsun?', current.local.name)
            current.local.role = _ask('Agent rolü ne olsun?', current.local.role)
        topology.validate_name(current.local.name)
        topology.validate_role(current.local.role)
        if current.local.lan:
            topology.validate_host(current.local.lan)
        remote_requested = any(getattr(options, 'remote_' + key) is not None for key in ('name', 'role', 'host', 'lan', 'reports_to', 'access', 'host_role'))
        if len(current.remote) > 1 and remote_requested:
            raise ValueError('multiple remotes exist; this paired command cannot select one implicitly')
        if not current.remote and (remote_requested or (interactive and _ask('Uzak agent eklensin mi? (e/h)', 'h').casefold() in ('e', 'evet', 'yes', 'y'))):
            current.remote.append(Agent('', '', kind='remote', reports_to=current.local.name))
        if current.remote:
            remote = current.remote[0]
            for key in ('name', 'role', 'host', 'lan', 'reports_to'):
                value = getattr(options, 'remote_' + key)
                if value is not None:
                    setattr(remote, key, value)
            if interactive:
                remote.name = _ask('Remote agent name', remote.name)
                remote.role = _ask('Remote agent role', remote.role)
                remote.host = _ask('Remote machine name', remote.host)
                # The LAN address is asked for; the public tunnel URL never is.
                # A tunnel is reissued on every restart, so it cannot be an
                # answer the operator types once -- the host opens one when the
                # LAN cannot carry the link.
                remote.lan = _ask('Remote machine LAN address (IP or hostname)', remote.lan)
        roles = _roles()
        conf = json.loads(roles.config_path().read_text(encoding='utf-8'))
        permissions, configured_vault = _permissions(conf, current)
        selected = options.vault or configured_vault
        permissions.setdefault(selected, {a.name.casefold(): 'read' for a in current.agents()})
        for vault in (list(permissions) if interactive and options.vault is None else [selected]):
            grants = {str(k).casefold(): v for k, v in permissions[vault].items()}
            permissions[vault] = grants
            for index, agent in enumerate(current.agents()):
                supplied = options.local_access if index == 0 else options.remote_access
                access = supplied if supplied is not None and vault == selected else grants.get(agent.name.casefold(), 'read')
                if interactive:
                    access = _ask(f'{agent.name} / {vault}: READ (onlyread) or WRITE (read+write)', access).casefold()
                grants[agent.name.casefold()] = topology.validate_vault_access(access)
                if vault == configured_vault:
                    agent.vault_access = grants[agent.name.casefold()]
                agent.vault_permissions[vault] = grants[agent.name.casefold()]
        conf_for_hosts = dict(conf)
        if 'transport_role' not in conf_for_hosts and (options.host_role is not None or options.remote_host_role is not None):
            conf_for_hosts['transport_role'] = options.host_role or ('consumer' if options.remote_host_role == 'host' else 'host')
        host_roles = _host_roles(conf_for_hosts, current.agents())
        if options.host_role is not None:
            host_roles[current.local.name.casefold()] = options.host_role
        if current.remote and options.remote_host_role is not None:
            host_roles[current.remote[0].name.casefold()] = options.remote_host_role
        topology.validate(current)
        permissions = roles.validate_permissions(permissions, current.agents())
        if list(host_roles.values()).count('host') != 1:
            raise ValueError('select exactly one communication host using --host-role and --remote-host-role')
    except (ValueError, OSError, TypeError) as exc:
        output.error(str(exc))
        return 2
    return _persist(current, permissions, host_roles, sync_peer=(options.sync_peer or bool(conf.get('peer')) and not options.local_only))
