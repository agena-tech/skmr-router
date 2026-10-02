---
description: 'Search the local HackerOne disclosed-report dataset'
allowed-tools: Bash(/usr/bin/python3 /root/.claude/skmr/cli.py hackerone-reports*)
---

# /skmr:hackerone-reports

!`/usr/bin/python3 /root/.claude/skmr/cli.py hackerone-reports $ARGUMENTS`

Forms: `pending`, `review [id]`, `resolve <review-token>`, `review-result <review-token>`, `latest`, `last <n>`, `search <terms>`, `report <id>`, `stats`, or bare terms.

Use disclosed cases when the user asks for them, or when comparable real-world
evidence would materially improve an unresolved decision — not merely because a
vulnerability class was mentioned. Report only reports that actually appear above.

For `pending` show the queue, without claiming these were published today.
For `review`, follow the supplied sequential review protocol. Read each public report, consult canonical related notes, and apply NOVEL + REUSABLE + VERIFIED through the existing writer. Review one ID at a time; resolve its saved/skipped decision before moving on. Inaccessible or unfinished reports remain queued. Do not launch subagents or test external targets.

### New disclosed-report offer

At the first user message of a fresh session, if the New Reports queue is nonempty,
tell the user its actual pending count and newest report titles/programs/dates, then
ask: **Yeni raporları incelememi ister misin?** Follow the UserPromptSubmit hook's
metadata. Pending reports may be older; never call all of them today's publications.
Wait for the user's agreement. On "incele" or an affirmative answer to that offer,
use `/skmr:hackerone-reports pending` and `review <id>` to examine each report
sequentially. Read the full public source, then search for existing canonical knowledge.
Save only novel, reusable, verified lessons with public-report evidence through
`/skmr:obsidian-memory preview` then `commit`. This request does not weaken the
existing verification gate. Resolve each single-report queue decision only after
its real save or justified skip. Keep inaccessible/incomplete reports pending.
Do not start subagents, message the peer, or test report targets for this workflow.
Report actual saved/skipped/pending results. Do not repeat the offer every turn.

