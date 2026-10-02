# SKMR

Command dispatch, planning, agent orchestration and memory for this host.

```text
/skmr:<command>  ->  cli.py  ->  core/dispatcher.py
                                     |
        execution hook ──────────────┤
        planning hook ───────────────┤
        [subagent orchestration] ────┤
        command handler ─────────────┤
        state update ────────────────┤
        completion ──────────────────┘
```

## Command system

`core/registry.py` is the single source of truth for user-facing commands. A
command is declared once there; `/skmr:help` and the dispatcher both read it, and
`commands/skmr/*.md` are thin wrappers that call `cli.py`.

Adding a command means adding one `Skill(...)` entry and one handler with the
signature `run(args: list[str], plan: Plan) -> int`. Handlers are imported lazily
from their `module:function` string, so a subsystem that fails to import breaks
only its own command.

Never print an `Executing:`/`Planning:` banner or re-implement planning inside a
handler — the hooks in `hooks/` own that, centrally.

## Memory architecture

Two stores, different jobs:

| | Purpose | Owner |
|---|---|---|
| `MEMORY.md` | working state: where did we stop, next action, subagent status | `memory/native.py` |
| Obsidian vault | permanent durable knowledge | `obsidian_memory.py` (canonical writer) |

`memory/native.py` is the only supported writer of `MEMORY.md`. Every write is a
read → merge → atomic replace under an exclusive lock, and sections SKMR does not
own are preserved verbatim. Subagents report through `agents/orchestrator.py`,
which funnels into that locked path, so concurrent agents cannot overwrite each
other's rows.

**Reads of the vault happen here; writes never do.** `commands/obsidian_memory_cmd.py`
delegates `route`/`preview`/`commit`/`validate`/`lint`/`init`/`log` straight to the
canonical writer, which remains the sole routing and mutation authority.

### Retrieval

```text
query
  ├── BM25 (memory/retrieval/bm25.py)      lexical: exact ids, commands, paths, CVEs
  └── vector (memory/retrieval/vector.py)  semantic: different wording, same meaning
            │
      fusion (hybrid.py): RRF (default) or normalized weighted
            │
      dedupe -> feature rerank -> final context
```

Raw BM25 scores and cosine similarities are never added directly — they live on
different scales. RRF compares ranks instead; `weighted` min/max-normalises first.
Set `hybrid_strategy` in `config/skmr.json`.

Degradation is explicit: if the vector backend is down, BM25 answers and a warning
says so. Losing both is an error. A missing vault mount is an error, never an empty
result.

### Index

`memory/indexing/index.py` keeps chunks, BM25 postings and embeddings in one
SQLite file. Only files whose content hash changed are reprocessed — the vault is
never re-embedded per query. Changing `embedding_model`, `embedding_version` or
`chunking_version` invalidates the affected vectors instead of silently mixing
vectors from different models.

Chunking is heading-aware (`memory/retrieval/chunking.py`): thin sections merge
forward, oversized ones split on paragraph boundaries.

Embeddings come from `bge-m3` over Ollama, behind the `EmbeddingProvider`
interface in `memory/retrieval/embeddings.py`.

## Planning and subagents

`planning/planner.py` classifies a task as simple / medium / complex and builds the
plan. Subagent count is **derived**, never fixed: units come from explicit
enumeration in the task. Explicit sequential cues keep work serial; ambiguous
prose still requires the parent to confirm independence before dispatch. A verifier agent is added only when there is more than one
result to reconcile, and it depends on all of them.

`agents/orchestrator.py` enforces the state machine
(`PLANNED → QUEUED → RUNNING → BLOCKED → COMPLETED / FAILED / CANCELLED`) and gates
transitions on dependencies: an agent whose prerequisites are unfinished cannot
enter `RUNNING`. Results are structured, duplicates across agents are surfaced, and
completed-but-unverified work is flagged.

## Identity and topology

`agents/topology.py` persists agent identity so it survives sessions. Validation
runs before anything is written: names, roles, IP addresses, hostnames, duplicate
identities, self-reference and reporting cycles. `/skmr:assign-role` then updates
the topology file, `MEMORY.md`, and a delimited block in `CLAUDE.md` — taking a
backup and never rewriting the instruction file wholesale.

## Configuration

`config/skmr.json` holds every tunable (paths, search mode and weights, top-k,
chunk sizes, embedding model, max subagents). Missing keys fall back to `DEFAULTS`
in `core/config.py`; a corrupt file degrades to defaults rather than failing.

## Tests

```bash
/usr/bin/python3 /root/.claude/skmr/tests/run_all.py
```

Individual suites: `test_index.py` (incremental indexing, version invalidation),
`test_policy_coverage.py` (CLAUDE.md behaviour coverage), `test_system.py`
(commands, hooks, planning, orchestration, intent, fallback).

## Runtime integration (audit repair)

`bin/skmr-runtime.py` handles real Claude events through `hooks/runtime.py`.
UserPromptSubmit reads native state for continuation questions; SessionStart
loads persisted identity. PreToolUse(Skill) supplies one plan as valid hook JSON.
The parent uses Claude's Agent tool to dispatch useful independent work; the hook
does not launch model processes itself. Planned agents are reported as planned.
SubagentStart/Stop track actual agent IDs, structured results and blockers.
Include `SKMR_AGENT_ID: <planned-id>` in a dispatched prompt so the Agent result
binds actual execution to the plan. Dependencies are enforced before dispatch.
Tool completion and agent exit never certify the underlying technical findings.
The parent must verify critical results before setting `verified`.

Report milestones with `/skmr:state agent <id> <status> <progress>` and structured
results with `/skmr:state result '<JSON>'`. Result JSON and MEMORY.md writes share
an orchestrator lock. Illegal state transitions, duplicate IDs and cycles fail
before persistence. Role updates roll back if a later store write fails.

`run_all.py` uses temporary config and runtime paths (`SKMR_CONFIG`,
`SKMR_RUNTIME_STATE`, `SKMR_MEMORY_PATH`, `SKMR_AGENTS_PATH`, `SKMR_CLAUDE_MD`).
It tests the current host files, including Profile and security suites, instead
of a stale peer staging directory. Index tests use their own temporary vault.
The root policy references automatically loaded Profile/AgentComm rules; its
coverage test validates all 107 inherited behavior contracts across those files.

Profile search options are forwarded as separate arguments to the canonical
writer. Preview returns a token: `/skmr:obsidian-memory commit <token>` commits
that preview. A candidate path is not a commit token. Learning candidates never
invent an explicit user save request; only a user-invoked learn command supplies it.
