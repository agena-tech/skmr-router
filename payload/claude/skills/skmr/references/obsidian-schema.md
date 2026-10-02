# Obsidian note model

The model supplies semantic routing intent; the canonical writer alone maps
that intent to physical directories and filenames.

Supported non-profile semantic classes include:

- `system`, `system-skmr`
- `personal`
- `area-security`, `area-ai`, `area-education`, `area-technology`
- `bug-hunting-project`, `research-project`, `other-project`
- `completed-project`
- `vulnerability`, `methodology`, `technique`, `security-tool`,
  `security-protocol`, `security-report`, `security-map`
- `ai-resource`, `reference`

Never infer a physical destination from these names or from documentation.
Use the writer's `route` result when the destination must be inspected.

Prefer one canonical note per durable concept. Search for a canonical title before creating. Do not create suffix variants such as `OAuth 2`, `OAuth-new`, or `OAuth-final`.

Two vault layers are not notes and are excluded from validation, indexing, and wikilink resolution:

- `05 Sources/` — immutable raw source documents the wiki is compiled from. Read them, cite them as `kind: file` evidence, never edit them.
- `log.md` — the append-only chronological record, written by the writer on every commit.

`init` creates both if absent.

Every permanent note uses concise frontmatter plus meaningful `[[wikilinks]]`. Required metadata fields are `title`, `type`, `area`, `status`, and `updated`.

`status` is one of `verified`, `source-observed`, `user-provided`, or `synthesis`. `verified` requires at least one machine-checkable evidence locator; `synthesis` rests on cited vault notes instead and can never be `verified`.

## User profiles

Durable user facts are a distinct canonical domain, not ordinary `personal`
area notes. A profile candidate uses `semantic_class: user-profile` with a safe
`profile_username` and a supported `profile_section`.

Supported logical Profile sections are:

`index`, `identity`, `preferences`, `education-career`,
`interests-projects`, `people-relationships`, `life-events-timeline`,
`devices-technical`, `goals-plans`, `health-wellbeing`,
`locations-travel`, `financial-logistical`, `communication`,
`open-loops`, `provenance-corrections`.

The Profile root, section-to-file mapping, and physical filenames are
implementation details owned exclusively by `obsidian_memory.py`. Unknown
sections are rejected by the writer.

**Canonical identity is the frontmatter title, never a physical filename
stem.** Physical filenames may repeat across profiles; globally unique
frontmatter titles resolve canonical identity. `profile_username` is
normalized deterministically by the writer so capitalization variants resolve
to one canonical profile identity.

Profile status: `user-provided` (with provenance `user-direct`/`user-confirmed`) is the normal case and needs no external evidence; `source-observed` (provenance `external-observed`/`sondra-relay`/`imported-history`); `verified` keeps the strong evidence semantics unchanged. `provenance` is a required, validated field for profile notes; it is not required on non-profile notes (backward compatible). Profile frontmatter adds `profile`, `profile_section`, and `provenance`:

```yaml
---
title: "Anezatra — Devices and Technical Environment"
type: "user-profile"
area: "profile"
profile: "anezatra"
profile_section: "devices-technical"
status: "user-provided"
provenance: "user-direct"
updated: 2026-09-12
---
```

Only the writer-designated initial Profile Index may exist without an outgoing wikilink (bootstrap); section notes link back to the canonical index (`[[<User> — Profile]]`). Never store authentication secrets (passwords, keys, tokens, cookies, seed/recovery codes, CVV, full card secrets) in a profile — the writer's secret filter denies them even on an explicit save request. Profile facts are never auto-deleted for age; only a correction supersedes them.

```yaml
---
title: "OAuth Testing Methodology"
type: "methodology"
area: "security"
status: "verified"
updated: 2026-09-10
tags:
  - "security"
  - "oauth"
sources:
  - kind: "official-documentation"
    locator: "public or local source locator"
---
```

Do not put passwords, tokens, cookies, authorization headers, API keys, session identifiers, private credentials, or private target data in permanent notes. Public provenance may be retained concisely.

Links must express a real relationship. Reuse existing canonical note names; Obsidian supplies backlinks automatically.

<!-- SKMR_PROFILE_HARDENING_V1 -->
Profile integrity is writer-enforced: each section key resolves to one writer-owned canonical destination and deterministic canonical title (`<DisplayName> — <Section>`), non-index sections must link to `[[<DisplayName> — Profile]]`, and a linkless Profile Index is valid only before the first section exists. Existing-section updates require the read-merge proof fields `profile_full_merge: true` and matching `base_sha256`.

## User Profiles

User Profiles are a first-class semantic domain. Their physical root,
section filenames, section-to-file mapping, and canonical destinations are
owned exclusively by `obsidian_memory.py` and must not be duplicated here.

Candidates use:

- `semantic_class: user-profile`
- `profile_username`
- `profile_section`

Supported logical sections are: `index`, `identity`, `preferences`,
`education-career`, `interests-projects`, `people-relationships`,
`life-events-timeline`, `devices-technical`, `goals-plans`,
`health-wellbeing`, `locations-travel`, `financial-logistical`,
`communication`, `open-loops`, and `provenance-corrections`.

Canonical identity comes from the writer-enforced globally unique frontmatter
title, not from a physical filename stem. To inspect the current physical route,
call `obsidian_memory.py route`; the returned route is authoritative.

Profile frontmatter remains semantically equivalent to:

```yaml
---
title: "Anezatra — Devices and Technical Environment"
type: "user-profile"
area: "profile"
profile: "anezatra"
profile_section: "devices-technical"
status: "user-provided"
provenance: "user-direct"
updated: "YYYY-MM-DD"
---
```

The writer enforces Profile username safety, canonical title consistency,
section consistency, provenance/status compatibility, Profile-Index backlink
rules, and read-merge proof (`profile_full_merge` + `base_sha256`) for updates.
