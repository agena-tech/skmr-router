---
description: 'Assign preserved identities, reporting relationships, per-vault READ/WRITE grants and communication host roles'
allowed-tools: Bash(/usr/bin/python3 /root/.claude/skmr/cli.py assign-role*)
---

# /skmr:assign-role

First run `/usr/bin/python3 /root/.claude/skmr/cli.py assign-role --show` to read
the persistent topology. Existing names, roles, hosts, reporting relationships,
remote agents, grants, and communication host selections are the defaults.
An omitted flag preserves its current value; it never clears remote agents.

The harness has no terminal, so collect choices in the conversation and then
commit once. If `$ARGUMENTS` already supplies complete choices, use them.

1. For `--show`, report the command output without making changes.
2. Show the existing local and remote identities. Ask only for intended changes
   to names, rank labels, hostnames, or `reports_to`. Keep their existing values
   unless the user explicitly changes them. Stable `agent.conf` self/peer ids
   and authentication credentials are preserved. Renaming an established id
   requires a separate credential migration and this command rejects it safely.
3. For **each named vault**, ask the local agent and remote agent separately:
   `READ (onlyread)` or `WRITE (read+write)`. Show the current grant as the
   default. Missing grants default to READ. Rank and machine names confer no
   permissions. READ/READ is invalid for a vault; WRITE/WRITE is valid.
4. Ask which machine should be the communication host when changing that choice
   or when it has never been assigned. Persist exactly one `host` and the other
   as `consumer`. This choice is independent of rank and vault grants; it does
   not move canonical storage or grant write access. It sets the configured
   default. An active connection keeps its existing role and URL until the
   explicit `connection public` workflow starts the selected host and the user
   relays its returned URL to the consumer. Report any pending host change.
5. Commit with the selected vault and only the fields the user changed:

```bash
/usr/bin/python3 /root/.claude/skmr/cli.py assign-role --non-interactive \
  --vault <vault-name> --local-access <read|write> --remote-access <read|write> \
  [--name <local-id> --role <local-role> --host <local-host> --reports-to <manager>] \
  [--remote-name <remote-id> --remote-role <remote-role> --remote-host <remote-host> \
   --remote-reports-to <manager>] \
  [--host-role <host|consumer> --remote-host-role <host|consumer>]
```

For multiple vaults, run one validated assignment for each vault with its own
choices. `--vault` changes grants for that name; it does not change the active
backend or `vault_name`. Omitted `--vault` uses `agent.conf.vault_name`, falling
back to `smb_vault_share`.

Paired assignments synchronize automatically. The local CLI and remote admin
endpoint use the same `apply_roles` validation and update mirrored topology,
`agent.conf` grants and transport role, connection state, MEMORY.md, and the
delimited CLAUDE.md topology block. Surrounding instruction text is preserved
and a backup is taken before changing the block.

Peer synchronization uses `POST /api/config/roles` with the separate
`X-Config-Token` administration secret; agent tokens cannot authorize it. A
communication host uses `peer_admin_url`; a consumer uses its active public
host endpoint or configured peer endpoint. Self endpoints and redirects are
rejected. If the peer does not acknowledge persistence, local updates roll back
and peer persistence is reported as unconfirmed. Retry the **same assignment
flags** with `--sync-peer` after resolving connectivity or provisioning the
admin token. Do not claim both machines were updated on failure.

`--local-only` is explicitly for provisioning or deliberate local staging. Its
summary says that the peer was not updated; do not describe it as synchronized.
Print the command's own summary and any pending/rollback message.
