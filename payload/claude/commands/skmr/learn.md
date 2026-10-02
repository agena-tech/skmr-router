---
description: 'Assess an episode and stage it as a durable-knowledge or failure-lesson candidate'
allowed-tools: Bash(/usr/bin/python3 /root/.claude/skmr/cli.py learn*)
---

# /skmr:learn

!`/usr/bin/python3 /root/.claude/skmr/cli.py learn $ARGUMENTS`

```bash
/skmr:learn [--error] --topic "T" --problem "P" --solution "S" --lesson "L" \
            [--evidence kind=locator] [--link "Note"] [--tag t] [--verified] [--dry-run]
```

The filter decides, not you: a one-off failure, a typo, a transient error or a
lesson scoped to a single run is rejected and stays working context. Only a
generalized, reusable lesson is staged.

Staging is **not** saving. The command writes a candidate and prints the writer's
`preview` / `commit` commands; the canonical writer's acceptance gate still
decides. An unverified lesson is offered as an explicit user-requested save, and
`--verified` requires real `--evidence`.
