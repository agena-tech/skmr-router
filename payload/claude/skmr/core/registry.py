"""The SKMR skill registry -- the single source of truth for user-facing commands.

`/skmr:help` and the dispatcher both read this table, so a command is declared
once and never duplicated across documentation, settings or command files.

Metadata is deliberately thin: only fields something actually reads are present.
`planning` drives the depth the planning hook applies, `subagents` says whether
orchestration is even permitted, and `handler` names the module-level function
the dispatcher imports lazily so one broken subsystem cannot stop the others
from loading.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Skill:
    name: str
    summary: str
    handler: str                       # "module:function", imported lazily
    usage: str = ""
    aliases: tuple[str, ...] = ()
    planning: str = "simple"           # simple | medium | complex
    subagents: bool = False
    memory: tuple[str, ...] = ()       # native | obsidian
    routes: tuple[str, ...] = field(default=())


SKILLS: tuple[Skill, ...] = (
    Skill(
        name="help",
        summary="List the available SKMR commands.",
        handler="skmr.commands.help_cmd:run",
        usage="/skmr:help",
        aliases=("commands", "--help", "-h"),
        planning="simple",
    ),
    Skill(
        name="obsidian-memory",
        summary="Search, save and inspect permanent Obsidian knowledge (BM25 + vector hybrid).",
        handler="skmr.commands.obsidian_memory_cmd:run",
        usage='/skmr:obsidian-memory search "query" [--mode hybrid|bm25|vector] | remember | status | index | route | preview | commit | validate | lint | log',
        aliases=("memory", "obsidian"),
        planning="medium",
        subagents=False,
        memory=("obsidian",),
        routes=("obsidian-memory",),
    ),
    Skill(
        name="state",
        summary="Read or update the native MEMORY.md working state (where did we stop).",
        handler="skmr.commands.state_cmd:run",
        usage="/skmr:state show | set <section> <text> | <field> <text> | agent <id> <status> [progress] | result <json> | agents | clear-agents",
        aliases=("native-memory", "memory-state"),
        planning="simple",
        memory=("native",),
        routes=("native-memory",),
    ),
    Skill(
        name="send",
        summary="Communicate with the remote peer agent over the authenticated AgentComm link.",
        handler="skmr.commands.send_cmd:run",
        usage="/skmr:send <peer> \"text\" | task \"text\" | connection [public|private] | health | status",
        aliases=("agentcomm",),
        planning="simple",
        routes=("send",),
    ),
    Skill(
        name="assign-role",
        summary="Interactively assign local and remote agent identity, role and topology.",
        handler="skmr.commands.assign_role_cmd:run",
        usage="/skmr:assign-role [--show] [--non-interactive --name N --role R [--lan IP] [--remote-name N --remote-role R --remote-host H --remote-lan IP] [--vault V --local-access read|write --remote-access read|write] [--host-role host|consumer]]",
        aliases=("roles", "topology"),
        planning="simple",
        memory=("native",),
    ),
    Skill(
        name="hackerone-reports",
        summary="Search the local HackerOne disclosed-report dataset.",
        handler="skmr.commands.hackerone_cmd:run",
        usage="/skmr:hackerone-reports pending | review [id] | resolve <token> | latest | last <n> | search <terms> | report <id>",
        aliases=("h1", "hackerone"),
        planning="medium",
        subagents=True,
    ),
    Skill(
        name="bugskills-ai",
        summary="Consult the local BugBountySkills knowledge base for a named technique gap.",
        handler="skmr.commands.bugskills_cmd:run",
        usage="/skmr:bugskills-ai search <terms> | read <path>",
        aliases=("bugskills", "bugbountyskills"),
        planning="medium",
        subagents=True,
    ),
    Skill(
        name="learn",
        summary="Assess an episode and stage it as a durable-knowledge or failure-lesson candidate.",
        handler="skmr.commands.learn_cmd:run",
        usage='/skmr:learn [--error] --topic "T" --problem "P" --solution "S" --lesson "L" [--evidence kind=locator] [--verified] [--dry-run]',
        aliases=("lesson", "error-learning"),
        planning="simple",
        memory=("obsidian",),
        routes=("obsidian-memory",),
    ),
    Skill(
        name="plan",
        summary="Run the planning hook for an arbitrary task and print the execution plan.",
        handler="skmr.commands.plan_cmd:run",
        usage='/skmr:plan "task description"',
        aliases=("planning",),
        planning="simple",
        subagents=True,
    ),
    Skill(
        name="doctor",
        summary="Report health of every SKMR subsystem (vault, index, embedding, memory, agentcomm).",
        handler="skmr.commands.doctor_cmd:run",
        usage="/skmr:doctor",
        aliases=("health", "status"),
        planning="simple",
    ),
)

_BY_NAME: dict[str, Skill] = {}
for _skill in SKILLS:
    _BY_NAME[_skill.name] = _skill
    for _alias in _skill.aliases:
        _BY_NAME.setdefault(_alias, _skill)


def resolve(name: str) -> Skill | None:
    return _BY_NAME.get((name or "").strip().casefold().removeprefix("/skmr:").removeprefix("skmr:"))


def names() -> list[str]:
    return [skill.name for skill in SKILLS]


def all_skills() -> tuple[Skill, ...]:
    return SKILLS
