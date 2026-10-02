---
description: 'Health report for every SKMR subsystem: vault, index, embedding, state, topology, agentcomm, writer'
allowed-tools: Bash(/usr/bin/python3 /root/.claude/skmr/cli.py doctor*)
---

# /skmr:doctor

!`/usr/bin/python3 /root/.claude/skmr/cli.py doctor $ARGUMENTS`

Each subsystem is probed independently, so one outage shows as one `FAIL`/`warn`
rather than a total failure. Relay the table as-is; do not describe a subsystem as
healthy when its row says otherwise.
