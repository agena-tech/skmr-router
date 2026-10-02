---
description: 'Communicate with the remote peer agent over the authenticated AgentComm link'
allowed-tools: Bash(/usr/bin/python3 /root/.claude/skmr/cli.py send*)
---

# /skmr:send

!`/usr/bin/python3 /root/.claude/skmr/cli.py send $ARGUMENTS`

Forms: `/skmr:send <peer> "text"`, `message "text"`, `task "text" [--body <file>]`,
`connection [public|private]`, `health`, `vault ls|cat <path>`.

Delivery semantics — do not break these:

- a successful send means the transport **accepted and queued** the message; it is
  not proof the peer processed it,
- after a successful send do **not** run `status`, `recv`, `show` or `watch`, and
  do not offer the user an optional delivery check,
- give one concise confirmation, then end the turn and let the asyncRewake
  watcher deliver the reply,
- `recv`/`show`/`watch`/`status` are manual diagnostics only.
