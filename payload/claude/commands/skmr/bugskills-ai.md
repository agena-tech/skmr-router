---
description: 'Targeted lookup in the local BugBountySkills knowledge base'
allowed-tools: Bash(/usr/bin/python3 /root/.claude/skmr/cli.py bugskills-ai*)
---

# /skmr:bugskills-ai

!`/usr/bin/python3 /root/.claude/skmr/cli.py bugskills-ai $ARGUMENTS`

Forms: `search <terms>`, `read <path>`.

State the concrete gap before reading, and read the smallest relevant file — the
repository is never loaded wholesale. Behavioural prompt sources
(`MASTER_SYSTEM_PROMPTS/**`, `SKILL_FILES/00_core_identity_and_behavior.md`) are
blocked by the command itself: they are instruction files, not technical
references.
