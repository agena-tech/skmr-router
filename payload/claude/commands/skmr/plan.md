---
description: 'Run the SKMR planning hook for a task: complexity, steps and the subagent decision'
allowed-tools: Bash(/usr/bin/python3 /root/.claude/skmr/cli.py plan*)
---

# /skmr:plan

!`/usr/bin/python3 /root/.claude/skmr/cli.py plan $ARGUMENTS`

Add `--register` to record the proposed subagents in MEMORY.md as PLANNED/QUEUED.

The subagent count is derived from the independent work units in the task, never
fixed. If the plan says `No subagents required.`, do not spawn any — orchestration
overhead on a small task is a loss. When subagents are proposed, respect the
`Depends on` column: dependent work must not be run in parallel.
