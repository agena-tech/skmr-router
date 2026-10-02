---
description: 'Permanent Obsidian knowledge: hybrid (BM25 + vector) search, index, status, writer delegation'
allowed-tools: Bash(/usr/bin/python3 /root/.claude/skmr/cli.py obsidian-memory*)
---

# /skmr:obsidian-memory

!`/usr/bin/python3 /root/.claude/skmr/cli.py obsidian-memory $ARGUMENTS`

Subcommands: `search "<query>" [--mode hybrid|bm25|vector] [--limit N]`, `status`,
`index [--force] [--no-embed]`, and the canonical-writer actions `route`,
`preview`, `commit`, `validate`, `lint`, `init`, `log`.

Reading is served by the SKMR retrieval engine; **every permanent write is
delegated to `obsidian_memory.py` through its preview → commit flow**. Never write
a note directly and never choose an Obsidian destination path yourself — the
canonical writer owns routing.

Report only what the output actually shows. A missing vault mount is an error, not
an empty vault.
