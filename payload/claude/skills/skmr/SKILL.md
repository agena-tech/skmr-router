---
name: skmr
description: Route, recall, save, migrate, or validate durable knowledge in the ElizaMemory Obsidian vault. Use when the user asks to remember/save something, asks what was previously learned, or when a concrete gap may be answered by prior durable knowledge. Do not use for ordinary security questions already answerable from current context.
---

# SKMR permanent memory

Obsidian is the sole permanent-memory backend. Native Claude auto-memory remains working continuity only.

Do not invoke this skill merely because a task is security-related. Invoke it only when its save, recall, routing, migration, or knowledge-maintenance behavior is actually needed.

Start with current conversation, authorized evidence, source code, tool output, and your own knowledge. Retrieve only when you can name the missing information and why the selected source is likely to fill it.

- For recall/search, read [references/routing.md](references/routing.md).
- For permanent save or update, read [references/save-policy.md](references/save-policy.md) and [references/obsidian-schema.md](references/obsidian-schema.md).

Use `/skmr:obsidian-memory` for Obsidian search, routing, preview, commit, validation, lint, init, and log. Never bypass the preview/commit flow with direct file writes. Never write under `.obsidian/`.

<!-- SKMR_PROFILE_ROUTING_AUTHORITY_V1 -->
Profile routing is semantic-only: provide `semantic_class: user-profile`, `profile_username`, and `profile_section`. Never construct or choose a physical Profile path/filename from documentation; `obsidian_memory.py route` is authoritative. All accepted permanent mutations remain guarded by preview -> commit.

`validate` checks that the vault is well-formed. `lint` checks whether it still makes sense: orphans, notes whose cited source no longer exists, notes naming each other without linking, topic siblings that share tags but no edge, and thin pages. Its findings are candidates for judgement, not defects — read the pairs it surfaces for contradictions and superseded claims, since no structural check can see those. Run it when the vault has grown or when a session added several notes.

Permanent notes must use meaningful wikilinks when a genuine canonical relationship exists. Never invent a link solely to satisfy validation; if no truthful relationship exists, do not promote the note yet.

Durable user facts use the writer-owned `user-profile` domain. Supply `semantic_class: user-profile`, `profile_username`, and a supported `profile_section`; never construct a Profile path or filename. The canonical writer owns all physical Profile routing and note identity. Their gate is novel + reusable + valid provenance; `status: user-provided` needs no technical evidence, while `status: verified` keeps its strong meaning. Updates are read-merge-preview-commit so prior content is preserved; recall is profile-scoped via `search --profile <username>`. See [references/obsidian-schema.md](references/obsidian-schema.md) and [references/save-policy.md](references/save-policy.md).

Report only retrievals and commits that actually occurred. Keep any activity summary short and omit it for trivial work.


Agent boundary: effective assigned vault permission controls access independently of identity, rank and messaging host. Unassigned agents default to READ. READ agents retrieve narrowly and send qualifying candidates to the configured WRITE peer with `/skmr:send candidate <file>`. WRITE agents independently evaluate, merge, preview and commit through the guarded writer. A delivered candidate is not a saved note. Both agents use the same writer; it uses the configured canonical mount when physically usable, otherwise the authenticated canonical API. Consult `agent.conf` for `vault_local`, `vault_windows_path`, `vault_name` and `vault_permissions`; never write directly to either alias.

<!-- SKMR_PROFILE_HARDENING_V1 -->
Profile hardening: canonical profile titles and section→Profile-Index backlinks are writer-enforced. Existing profile updates additionally require `profile_full_merge: true` plus a matching `base_sha256`. Profile recall may be narrowed with `search --profile <username> --profile-section <section>`.
