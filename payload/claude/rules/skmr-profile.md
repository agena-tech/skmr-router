# SKMR User Profile rules

## 7. User profile memory

All durable information about the user belongs to the canonical User Profile domain
in Obsidian. It is a first-class permanent-memory domain: information that
meaningfully preserves identity, continuity, preferences, history, relationships,
goals, technical context or future decision context is routed there.

Scope includes identity, locations, life events, personal timeline, family and
social context, education, career, security/technical experience, projects,
interests, preferences, goals and plans, routines, devices and environment,
communication conventions, voluntarily disclosed health or financial context,
constraints, stated opinions, emotional context around significant events, prior
decisions and their reasoning, corrections, and open loops. Do not discard a new
category of durable personal context merely because it is not listed.

**Routing.** Supply semantic fields only: `semantic_class: user-profile`,
`profile_username`, `profile_section`, plus the canonical title and content.
Supported sections: `index`, `identity`, `preferences`, `education-career`,
`interests-projects`, `people-relationships`, `life-events-timeline`,
`devices-technical`, `goals-plans`, `health-wellbeing`, `locations-travel`,
`financial-logistical`, `communication`, `open-loops`, `provenance-corrections`.
The writer owns the Profile root, physical filenames, section mapping, canonical
titles and destinations. Never derive a path from a section name or a documentation
example. A note's identity is its unique frontmatter title, never a filename stem.

**Save semantics.** The profile gate is **NOVEL + REUSABLE + VALID PROVENANCE** —
it does **not** require technical `verified` status. Provenance values are a finite
set: `user-direct`, `user-confirmed`, `sondra-relay`, `external-observed`,
`imported-history`. A guess is never a provenance.

- `status: user-provided` + `provenance: user-direct`/`user-confirmed` is the normal case: authoritative evidence of *what the user stated*, not externally verified truth. "I live in Istanbul" is stored this way, never relabelled verified.
- `status: source-observed` records a non-user source with `external-observed`, `sondra-relay` or `imported-history`.
- `status: verified` still requires the writer's full evidence semantics.

Record dates when known and mark approximate dates as approximate. Keep unknown
details unknown. Merge duplicates. A user correction supersedes an older
conflicting fact; keep the old value as historical context when useful but never
present it as current. Preserve contradictions as explicit conflicts until
resolved. A Sondra-relayed or external claim retains its provenance and must never
silently overwrite a user-confirmed fact. A directly stated personal fact need not
be a generalized technical lesson to qualify.

**Updating is read-merge-preview-commit, and destroying prior content is a defect.**
Read the current note, preserve every still-valid fact, merge, deduplicate, apply
corrections, mark superseded facts historical, render the COMPLETE resulting
section, inspect the full preview diff, then commit. The writer renders the whole
note from the candidate, so a one-fact candidate would overwrite the section.

<!-- SKMR_PROFILE_HARDENING_V1 -->
**Enforced update proof.** Before updating an existing canonical Profile section,
read the exact note and compute its SHA-256. The merged candidate must include
`profile_full_merge: true` and `base_sha256: <current-note-sha256>`. The writer
rejects stale, partial or unproven Profile updates before preview, and validates
title/backlink/file-section consistency.

Only the writer-designated initial Profile Index may exist without an outgoing
wikilink; every other section note links back to the canonical Profile Index.

**Secret protection.** The profile may hold sensitive context the user chose to
share, but never store passwords, private keys, seed phrases, recovery codes,
session cookies, API or access tokens, CVV values, full payment-card secrets,
authentication backup codes, or any credential that could grant account or system
access. Do not infer sensitive personal facts the user did not provide, and never
convert guesses, assumptions or third-party claims into user facts.

**Recall.** Obsidian is the source of truth for durable user facts. When a request
materially depends on the user's identity, history, preferences, relationships,
prior events, devices, projects, goals, constraints or previous decisions, consult
the canonical Profile before guessing or asking the user to repeat it. Do not rely
on native auto-memory for long-term user facts. Retrieve the smallest relevant note
set — never bulk-load the profile — and skip retrieval when the current
conversation already has what is needed. Scope recall with
`obsidian_memory.py search "<query>" --profile <username>` (optionally
`--profile-section <section>`). If the user's current statement conflicts with
stored information, the current statement is the newest authoritative update; route
the correction through the writer.

**Assigned READ and profile data.** A READ agent may learn a durable user fact
but must escalate it as a structured candidate with provenance to the configured
WRITE peer. It never writes canonical Profile storage or chooses a destination.
Any assigned WRITE agent evaluates, resolves duplicates and conflicts, and
performs accepted writes through the canonical writer, independently of rank.

---
