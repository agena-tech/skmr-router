"""Explicit vault capabilities shared by SKMR and agentcomm.

No identity, rank, storage location, or transport role implies write access.
Missing, unreadable, or malformed grants resolve to READ.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

READ, WRITE = 'read', 'write'


def config_path():
    return Path(os.environ.get('AGENTCOMM_CONF', '/root/agentcomm/agent.conf'))


def _configuration(conf):
    if isinstance(conf, dict):
        return conf
    try:
        value = json.loads(config_path().read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def permission(actor=None, vault=None, conf=None):
    conf = _configuration(conf)
    actor = str(actor if actor is not None else conf.get('self', '')).strip().casefold()
    vault = str(vault if vault is not None else conf.get('vault_name') or conf.get('smb_vault_share') or '')
    mapping = conf.get('vault_permissions', {})
    grants = mapping.get(vault, {}) if isinstance(mapping, dict) else {}
    if not isinstance(grants, dict):
        return READ
    matches = [value for key, value in grants.items() if isinstance(key, str) and key.strip().casefold() == actor]
    if len(matches) != 1 or not isinstance(matches[0], str):
        return READ
    return WRITE if matches[0].strip().casefold() == WRITE else READ


def can_write(actor=None, vault=None, conf=None):
    return permission(actor, vault, conf) == WRITE


def validate_permissions(mapping, agents):
    """Return normalized complete grants; reject invalid or writerless vaults."""
    identities = set()
    for agent in agents:
        name = agent.get('name') if isinstance(agent, dict) else getattr(agent, 'name', agent)
        if not isinstance(name, str) or not name.strip():
            raise ValueError('agent identity cannot be empty')
        key = name.strip().casefold()
        if key in identities:
            raise ValueError(f'duplicate agent identity: {name}')
        identities.add(key)
    if not identities or not isinstance(mapping, dict) or not mapping:
        raise ValueError('vault_permissions must contain at least one named vault and agent')
    normalized = {}
    for vault, grants in mapping.items():
        if not isinstance(vault, str) or not vault.strip() or any(ord(c) < 32 or ord(c) == 127 for c in vault):
            raise ValueError('vault name must be nonempty and contain no control characters')
        if not isinstance(grants, dict):
            raise ValueError(f'permissions for vault {vault} must be a mapping')
        values = {key: READ for key in identities}
        seen = set()
        for actor, value in grants.items():
            if not isinstance(actor, str) or actor.strip().casefold() not in identities:
                raise ValueError(f'unknown agent in vault {vault}: {actor}')
            key = actor.strip().casefold()
            if key in seen:
                raise ValueError(f'duplicate grant identity in vault {vault}: {actor}')
            seen.add(key)
            if not isinstance(value, str) or value.strip().casefold() not in (READ, WRITE):
                raise ValueError(f'invalid vault access for {actor} in {vault}; use read or write')
            values[key] = value.strip().casefold()
        if WRITE not in values.values():
            raise ValueError(f'at least one agent needs write vault access for {vault}; every agent read-only is invalid')
        normalized[vault] = values
    return normalized
