"""Agent identity and topology -- the persistent answer to "who am I?".

Identity must survive across sessions, so it lives in one JSON file rather than
being re-derived from prose. Topology is a graph, not a name list: a remote agent
records who it reports to, and that relationship is what delegation reads.

Validation runs before anything is persisted. An invalid identity must never
reach the config file or CLAUDE.md.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields as dataclass_fields

from ..core import config

HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
                      r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

# Vault access is an assigned capability, not an identity. Two values only, so
# there is nothing to interpret: "write" may also read, "read" may only read.
READ, WRITE = "read", "write"
VAULT_ACCESS = (READ, WRITE)


class ValidationError(ValueError):
    """Raised before any persistence when an identity is not well-formed."""


@dataclass
class Agent:
    name: str
    role: str
    host: str = ""
    reports_to: str = ""
    kind: str = "local"           # local | remote
    # Empty means "never assigned". It resolves to READ, because a permission
    # that was never granted must not be inherited by accident: an unassigned
    # agent may read permanent memory and nothing else.
    vault_access: str = ""
    # The machine's LAN address, and the only address this assignment persists.
    # A public tunnel URL is deliberately NOT stored anywhere durable: it is
    # regenerated whenever the tunnel restarts, so a saved one is wrong by the
    # next session. LAN is the configured path; the tunnel is what the host
    # opens when the LAN cannot carry the link.
    lan: str = ""
    vault_permissions: dict[str, str] = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.name} — {self.role}"

    def access(self, vault: str | None = None) -> str:
        raw = self.vault_permissions.get(vault, "") if vault else self.vault_access
        value = raw.strip().casefold() if isinstance(raw, str) else ""
        return value if value in VAULT_ACCESS else READ

    def can_write_vault(self, vault: str | None = None) -> bool:
        return self.access(vault) == WRITE

    # Both capabilities follow from write access rather than from rank: the agent
    # that owns the canonical store is the one that may hand out work and expose
    # the share it writes to.
    def can_create_tasks(self) -> bool:
        return self.can_write_vault()

    def can_share_smb(self) -> bool:
        return self.can_write_vault()


@dataclass
class Topology:
    local: Agent | None = None
    remote: list[Agent] = field(default_factory=list)
    updated_at: str = ""

    def agents(self) -> list[Agent]:
        return ([self.local] if self.local else []) + list(self.remote)

    def writers(self) -> list[Agent]:
        return [agent for agent in self.agents() if agent.can_write_vault()]

    def find(self, name: str) -> Agent | None:
        target = (name or "").casefold()
        for agent in self.agents():
            if agent.name.casefold() == target:
                return agent
        return None


def _require(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValidationError(f"{what} cannot be empty")
    return value


def validate_name(value: str, what: str = "agent name") -> str:
    value = _require(value, what)
    if not NAME.match(value):
        raise ValidationError(f"invalid {what}: use 1-64 chars of letters, digits, space, dot, underscore or hyphen")
    return value


def validate_role(value: str) -> str:
    value = _require(value, "agent role")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValidationError("agent role cannot contain control characters")
    if len(value) > 64:
        raise ValidationError("agent role must be 64 characters or fewer")
    return value


def validate_vault_access(value: str) -> str:
    value = _require(value, "vault access").casefold()
    if value not in VAULT_ACCESS:
        raise ValidationError(f"invalid vault access '{value}': use 'read' or 'write'")
    return value


def validate_host(value: str) -> str:
    value = _require(value, "remote host")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    # A value written like an address must BE a valid address. Without this,
    # `999.1.1.1` slips through as a "hostname" because every label is
    # syntactically legal, and a typo'd IP gets persisted as a real host.
    labels = value.split(".")
    if len(labels) > 1 and all(label.isdigit() for label in labels):
        raise ValidationError(
            f"invalid remote host '{value}': looks like an IP address but is not a valid one"
        )
    if ":" in value:
        raise ValidationError(
            f"invalid remote host '{value}': looks like an IPv6 address but is not a valid one"
        )
    if any(len(label) > 63 for label in labels) or not HOSTNAME.fullmatch(value):
        raise ValidationError(f"invalid remote host '{value}': not a valid IP address or hostname")
    return value


def validate(topology: Topology) -> Topology:
    """Full-graph validation: duplicates, self-reference and dangling managers."""
    agents = topology.agents()
    if not agents:
        raise ValidationError("topology must contain at least the local agent")

    seen: set[str] = set()
    for agent in agents:
        validate_name(agent.name)
        validate_role(agent.role)
        if agent.vault_access:
            validate_vault_access(agent.vault_access)
        if not isinstance(agent.vault_permissions, dict):
            raise ValidationError("vault_permissions must be a mapping")
        for vault, access in agent.vault_permissions.items():
            if not isinstance(vault, str) or not vault.strip() or any(ord(c) < 32 or ord(c) == 127 for c in vault):
                raise ValidationError("invalid vault name")
            validate_vault_access(access)
        key = agent.name.casefold()
        if key in seen:
            raise ValidationError(f"duplicate agent identity: {agent.name}")
        seen.add(key)
        if agent.kind == "remote":
            validate_host(agent.host)
        if agent.lan:
            validate_host(agent.lan)
        if agent.reports_to:
            if agent.reports_to.casefold() == key:
                raise ValidationError(f"{agent.name} cannot report to itself")
            if agent.reports_to.casefold() not in {a.name.casefold() for a in agents}:
                raise ValidationError(f"{agent.name} reports to unknown agent '{agent.reports_to}'")

    # Someone must be able to write, or permanent memory can never grow: every
    # agent read-only is the one combination the store cannot survive. Several
    # writers are fine -- the writer's own gates still apply to each of them.
    if not topology.writers():
        raise ValidationError(
            "at least one agent needs 'write' vault access; every agent read-only "
            "would leave permanent memory with no writer"
        )
    for vault in {v for a in agents for v in a.vault_permissions}:
        if not any(a.can_write_vault(vault) for a in agents):
            raise ValidationError(f"at least one agent needs 'write' vault access for {vault}")

    # Walk each chain to catch a cycle that no single check above would see.
    by_name = {a.name.casefold(): a for a in agents}
    for agent in agents:
        visited = {agent.name.casefold()}
        cursor = agent
        while cursor.reports_to:
            nxt = by_name.get(cursor.reports_to.casefold())
            if nxt is None:
                break
            if nxt.name.casefold() in visited:
                raise ValidationError(f"reporting cycle detected at {nxt.name}")
            visited.add(nxt.name.casefold())
            cursor = nxt
    return topology


def path():
    return config.path("agents_path")


def load() -> Topology:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return Topology()
    fields = {f.name for f in dataclass_fields(Agent)}

    def build(item):
        # Ignore keys this version does not know: a newer peer's file must not
        # crash an older reader.
        return Agent(**{k: v for k, v in item.items() if k in fields})

    local = raw.get("local")
    return Topology(
        local=build(local) if isinstance(local, dict) else None,
        remote=[build(item) for item in raw.get("remote", []) if isinstance(item, dict)],
        updated_at=str(raw.get("updated_at") or ""),
    )


def save(topology: Topology) -> Topology:
    validate(topology)
    topology.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "local": asdict(topology.local) if topology.local else None,
        "remote": [asdict(agent) for agent in topology.remote],
        "updated_at": topology.updated_at,
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return topology


def render_markdown(topology: Topology) -> str:
    """The topology block shared by MEMORY.md and CLAUDE.md."""
    lines: list[str] = []
    if topology.local:
        lines += ["### Local", f"- {topology.local.label()}"]
        if topology.local.host:
            lines[-1] += f", host {topology.local.host}"
        if topology.local.reports_to:
            lines[-1] += f", reports to {topology.local.reports_to}"
        lines[-1] += f", vault {topology.local.access().upper()}"
        for vault, access in sorted(topology.local.vault_permissions.items()):
            lines.append(f"  - {vault}: {access.upper()}")
    else:
        lines += ["### Local", "- Not assigned"]
    lines += ["", "### Remote"]
    if not topology.remote:
        lines.append("- None")
    else:
        for agent in topology.remote:
            detail = [agent.label()]
            if agent.host:
                detail.append(f"host {agent.host}")
            if agent.reports_to:
                detail.append(f"reports to {agent.reports_to}")
            detail.append(f"vault {agent.access().upper()}")
            lines.append("- " + ", ".join(detail))
            for vault, access in sorted(agent.vault_permissions.items()):
                lines.append(f"  - {vault}: {access.upper()}")
    return "\n".join(lines)
