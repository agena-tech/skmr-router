# Symmetric AgentComm / SKMR

The installed Python, commands and skills are the same on both agents. Identity,
rank, named-vault grants, storage location and transport selection are persistent
configuration. Rank never implies a WRITE grant.

Either agent hosts with `/skmr:send connection public`. Give its returned URL to
the other agent, which selects `/skmr:send connection public <url>`. This selects
the active message host. `/skmr:assign-role` sets persistent grants and configured
host defaults; changing defaults preserves an active connection until this URL
workflow explicitly changes it. Inbox epochs archive old cursors and unacknowledged
deliveries when changing queues. SessionStart/Stop asyncRewake remains the receive path.

`/skmr:assign-role` offers READ or WRITE for each agent and named vault. WRITE includes
reading, guarded writing and task publication. WRITE/WRITE is valid; READ/READ is
rejected. Paired changes synchronize through a separate configuration secret, with
local rollback if the peer does not confirm. Ordinary message credentials cannot
grant WRITE. Omitted identity/rank/reporting options preserve their existing values.

The physical canonical SMB vault remains separate from the message host. A WRITE
agent without a writable mount uses authenticated canonical preview/commit. READ
agents may search and delegate validated candidates but cannot create tasks or commit.
A missing canonical backend is an error, not a successful empty result.

For a fresh pair, provision both `agent.conf` files before assigning roles:

- Choose stable lowercase `self` and `peer` ids using letters, digits, underscore
  or hyphen. Use those ids as display names at initial assignment. Existing ids
  cannot be renamed by assign-role without a separate credential migration.
- Generate two distinct random agent credentials and a third distinct random
  `config_admin_token`. Keep files mode 0600. Both hosts need the same `tokens`
  roster; `token` selects the local id. Never include credentials in a public URL.
- Set `vault_name`, `vault_local`, SMB connection/credential-file settings and
  `storage_host`. The non-storage machine sets `vault_origin` to the authenticated
  canonical API origin. Set `server_local_url`, `peer_api_lan` and `peer_admin_url`.
- Start the identical `agentcomm.service` and make the peer API reachable. Use
  assign-role with explicit names, roles, remote hostname, `--local-access`,
  `--remote-access`, `--host-role` and `--remote-host-role`. Without preexisting
  topology no identity or WRITE grant is inferred. `--local-only` allows deliberate
  initial staging; follow it with `--sync-peer` once the peer is reachable.

Vault grant maps can contain several names. `--vault` selects the grant map to
edit; it does not move the configured canonical `vault_name` or storage backend.

Validate with `/usr/bin/python3 /root/.claude/skmr/tests/run_all.py`. Test queues,
role stores, writer fixtures and retrieval fixtures are disposable; live vault
notes are never test output. Running Claude sessions load new hook code on their
next hook invocation; replacing files does not restart or kill a Claude process.
