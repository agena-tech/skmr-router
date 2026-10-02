# Permanent save policy

Automatic preservation requires all three:

- Novel: materially absent from the canonical note, not a paraphrase or payload variant.
- Reusable: likely to improve a future decision, investigation, or workflow.
- Verified: tied to reproducible evidence, authorized target evidence, source code, official documentation, or a public disclosed report.

If any automatic gate fails, do not write permanent memory.

For knowledge derived from authorized application or testing, classify the outcome before promotion:

- `succeeded / target-validated`: the claimed behavior was reproduced.
- `failed but yielded a verified reusable limitation`: the technique failed, but a generalized limitation was demonstrated and may still pass the novel + reusable + verified gate.
- `inconclusive`: evidence is insufficient; do not promote the observation as verified fact.

A negative result itself is never durable memory; only a generalized, verified, reusable lesson derived from that result may be preserved.

Preserve only the smallest generalized durable insight. Do not copy whole BugBountySkills files or HackerOne reports into Obsidian.

## Synthesis

A note whose warrant is the vault's own notes rather than an outside source uses `status: "synthesis"`. It is how a connection you drew, a comparison, or a thesis across existing notes stops disappearing into chat history. Synthesis still passes `novel` and `reusable`, must cite at least two existing canonical notes, and may never set `verified` or carry `status: "verified"` — its evidence ceiling is whatever the notes it cites already carry. Say so in the note itself.

An index or map note (`semantic_class: "security-map"`) makes no claim of its own and therefore needs no `evidence`; requiring it only forces unrelated provenance into the map's frontmatter.

An explicit user save request bypasses the proactive save decision only. It still requires correct PARA routing, canonical-note search, duplicate protection, secret/DLP checks, meaningful wikilinks, preview, and atomic commit. Store an explicitly requested opinion or personal fact as `user-provided`, not as verified technical fact.

Create a candidate JSON outside the vault. Include: `title`, `semantic_class`, `content`, `links`, `tags`, exactly one of `automatic` or `explicit_user_request`, and for automatic saves `novel`, `reusable`, `verified`, plus non-empty `evidence` entries with `kind` and `locator`.

Preview, inspect the diff, then commit:

```bash
/usr/bin/python3 /root/.claude/skills/skmr/scripts/obsidian_memory.py preview /tmp/candidate.json
/usr/bin/python3 /root/.claude/skills/skmr/scripts/obsidian_memory.py commit <preview-token>
/usr/bin/python3 /root/.claude/skills/skmr/scripts/obsidian_memory.py validate
```

Every commit appends one line to the vault's `log.md` automatically; never write that file by hand. Use `log <action> <detail>` only for events that touch no note, such as an ingest discussion or a query worth dating. The shell hook fails closed on shell metacharacters, so a log detail cannot contain `;` `&` `|` `<` `>` `$` or a backtick — write the detail in plain prose with commas.

The writer rejects path traversal, `.obsidian`, symlink escapes, duplicate canonical titles, stale previews, concurrent changes, secret patterns, oversized notes, and writes without wikilinks. It journals before atomic replacement; unresolved transactions are stop-blocking integrity failures.

Link targets must already exist, at both preview and commit. Never invent a relationship to pass validation. Update candidates contain the complete desired note body; inspect the diff to preserve existing knowledge. Link, evidence, metadata, and operator changes are meaningful updates.

If an existing canonical note belongs in a new PARA location, set `move_existing: true` on the candidate and inspect the destination in the preview. Without this explicit flag, a route conflict is rejected. A move journals the source and destination, writes and verifies the destination, and then removes the unchanged source. Interrupted moves retain their transaction for integrity review.

Duplicate detection compares canonical titles and identical bodies under different titles. Paraphrased duplicates still require model judgment and a narrow canonical-note search.

The shell hook rejects unsupported commands mentioning the vault or running inside it. It is a policy guard, not an operating-system sandbox: arbitrary root programs or tools outside the hook cannot be made read-only by shell-text inspection. Use the canonical writer for all permanent writes and Read for note inspection.


## User-profile saves

Durable user facts use `semantic_class: user-profile`, a safe `profile_username`, and a supported `profile_section`. The model selects the semantic Profile section; it never selects the physical path or filename. Physical Profile routing is exclusively writer-owned. The profile gate differs from the technical gate: **automatic preservation requires novel + reusable + valid provenance**, and `status: user-provided` does **not** require technical evidence — a direct user statement is authoritative evidence of what the user said, not externally-verified truth. `status: verified` still demands the full evidence semantics and is not conferred by a user statement. Provenance is a required, validated field: `user-direct`, `user-confirmed`, `peer-relay`, `sondra-relay` (legacy), `external-observed`, or `imported-history`. An explicit user save request bypasses only proactive save-worthiness — never secret protection, safe profile routing, path/symlink safety, integrity, valid provenance, duplicate/conflict handling, or the canonical writer.

**Profile updates are mandatory read-merge-preview-commit.** The writer renders the whole note from the candidate, so a candidate that carries only one new fact would overwrite the section. Before updating: read the current note, preserve every still-valid durable fact, merge the new information, deduplicate, apply explicit user corrections, mark superseded facts as historical (never leave an outdated fact presented as current, and never delete unrelated still-valid content), render the COMPLETE section, inspect the full preview diff, then commit. Contradictions between non-user sources are preserved as explicit conflicts, not silently resolved; a peer-relayed or external fact never overwrites a user-confirmed one.

Only the writer-designated initial Profile Index may be created without an outgoing wikilink (a narrow bootstrap exception); every other profile section note must link back to the canonical index (`[[<User> — Profile]]`), and the index gains truthful `[[...]]` links to sections as they come to exist. A profile note's canonical identity is its unique frontmatter title, never a physical path or filename stem.

An assigned READ agent builds and locally pre-validates a profile candidate (`send.py candidate candidate.json`) and delegates it to the configured WRITE peer. An assigned WRITE agent independently re-evaluates provenance, novelty, duplicates/conflicts, secret and path safety, performs the semantic merge, previews, and commits through the canonical writer. Receiving a candidate is not approval or a successful save.

Agent boundary: named-vault grants in `agent.conf` determine effective READ/WRITE access; missing grants default to READ. Identity, rank, reporting relationship and communication hosting confer no write authority. READ agents cannot preview, commit, initialize or log in the permanent vault. WRITE agents use the guarded writer against a physically writable canonical mount, or its authenticated canonical API when the mount is unavailable or read-only. The same merge, preview token, acceptance, path and secret protections apply over either transport. A request failure is reported as unavailable, never as saved.

<!-- SKMR_PROFILE_HARDENING_V1 -->
### Enforced profile update proof

For an existing `user-profile` note, policy is now enforced by the writer, not left to model discipline. The candidate MUST carry `profile_full_merge: true` and `base_sha256` equal to the SHA-256 of the exact current canonical note that was read before the merge. A stale/missing hash or missing full-merge flag is rejected before preview. Re-read and merge after any concurrent change.
