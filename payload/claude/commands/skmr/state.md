---
description: 'Native working state (MEMORY.md): where did we stop, what is next, subagent status'
allowed-tools: Bash(/usr/bin/python3 /root/.claude/skmr/cli.py state*)
---

# /skmr:state

!`/usr/bin/python3 /root/.claude/skmr/cli.py state $ARGUMENTS`

Subcommands: `show` (default), `agents`, `set <section> "<text>"`, and the
shorthands `task`, `phase`, `last`, `current`, `next`, `problems`.
Report agent milestones with `agent <id> <status> "<progress>"` and structured
results with `result '<JSON>'`. These use the central dependency gate and locked
result store; never overwrite MEMORY.md directly.

This is the answer to "nerede kalmıştık?" / "where did we stop?" — read it rather
than reconstructing progress from the conversation. It holds working state only;
durable knowledge belongs in Obsidian via `/skmr:obsidian-memory`.
