"""Native working state -- the MEMORY.md reader/writer.

MEMORY.md answers exactly one question: *where did we stop?* It holds the active
task, phase, last completed action, next action, blockers, agent topology and
live subagent status. It is not a log: nothing here appends per-tool-call lines.

Concurrency: several agents may touch the file, so every write is
read -> merge -> atomic replace under an exclusive lock on a sidecar lockfile.
Sections SKMR does not own are preserved verbatim, which is what keeps
hand-written notes in the file from being destroyed by a state update.
"""
from __future__ import annotations

import fcntl
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..core import config

TITLE = "Working continuity"

# The sections SKMR manages, in the order they are rendered. Anything else found
# in the file is kept and re-emitted after these.
MANAGED = (
    "Active Task",
    "Phase",
    "Last Completed",
    "Current Work",
    "Next",
    "Active Agent Topology",
    "Active Subagents",
    "Open Problems",
    "Last State Update",
)

SUBAGENT_COLUMNS = ("Agent", "Task", "Status", "Progress")
STATES = ("PLANNED", "QUEUED", "RUNNING", "BLOCKED", "COMPLETED", "FAILED", "CANCELLED")

_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class Subagent:
    agent_id: str
    task: str
    status: str = "PLANNED"
    progress: str = ""

    def row(self) -> list[str]:
        return [self.agent_id, self.task, self.status, self.progress or "-"]


@dataclass
class State:
    preamble: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)

    # -- section access -------------------------------------------------
    def get(self, name: str, default: str = "") -> str:
        return self.sections.get(name, default)

    def set(self, name: str, body: str) -> None:
        body = (body or "").strip()
        if name not in self.sections:
            self.order.append(name)
        self.sections[name] = body

    def drop(self, name: str) -> None:
        self.sections.pop(name, None)
        if name in self.order:
            self.order.remove(name)

    # -- subagents -----------------------------------------------------
    def subagents(self) -> list[Subagent]:
        return _parse_table(self.get("Active Subagents"))

    def set_subagents(self, agents: list[Subagent]) -> None:
        if not agents:
            self.set("Active Subagents", "None.")
            return
        self.set("Active Subagents", _render_table(agents))

    def upsert_subagent(self, agent: Subagent) -> None:
        agents = self.subagents()
        for i, existing in enumerate(agents):
            if existing.agent_id == agent.agent_id:
                agents[i] = agent
                break
        else:
            agents.append(agent)
        self.set_subagents(agents)

    # -- rendering -----------------------------------------------------
    def render(self) -> str:
        parts: list[str] = [f"# {TITLE}", ""]
        if self.preamble.strip():
            parts += [self.preamble.strip(), ""]
        seen: set[str] = set()
        for name in MANAGED:
            if name in self.sections:
                seen.add(name)
                parts += [f"## {name}", "", self.sections[name].strip() or "-", ""]
        for name in self.order:
            if name in seen or name not in self.sections:
                continue
            parts += [f"## {name}", "", self.sections[name].strip() or "-", ""]
        return "\n".join(parts).rstrip() + "\n"


def _parse_table(body: str) -> list[Subagent]:
    agents: list[Subagent] = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip().replace("&#124;", "|").replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        if len(cells) < 3:
            continue
        head = cells[0].casefold()
        if head in {"agent", ""} or set(cells[0]) <= {"-", ":"}:
            continue
        agents.append(
            Subagent(
                agent_id=cells[0],
                task=cells[1],
                status=cells[2].upper() if cells[2].upper() in STATES else cells[2],
                progress=cells[3] if len(cells) > 3 and cells[3] != "-" else "",
            )
        )
    return agents


def _render_table(agents: list[Subagent]) -> str:
    lines = ["| " + " | ".join(SUBAGENT_COLUMNS) + " |",
             "|" + "|".join("---" for _ in SUBAGENT_COLUMNS) + "|"]
    for agent in agents:
        lines.append("| " + " | ".join(" ".join(c.splitlines()).replace("|", "&#124;") for c in agent.row()) + " |")
    return "\n".join(lines)


def memory_path() -> Path:
    return config.path("memory_path")


def parse(text: str) -> State:
    state = State()
    body = text or ""
    # Drop a leading H1 so the title is not mistaken for content.
    body = re.sub(r"\A\s*#\s+.*?\n", "", body, count=1)
    matches = list(_HEADING.finditer(body))
    if not matches:
        state.preamble = body.strip()
        return state
    state.preamble = body[: matches[0].start()].strip()
    for i, match in enumerate(matches):
        name = match.group(1).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        state.set(name, body[match.end(): end])
    return state


def load() -> State:
    try:
        return parse(memory_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return State()
    except OSError:
        return State()


@contextmanager
def _locked():
    """Hold an exclusive lock on a sidecar file for the read-merge-write cycle."""
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    handle = open(lock, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def save(state: State, stamp: bool = True) -> Path:
    """Write the state atomically. Callers normally use `mutate()` instead."""
    path = memory_path()
    if stamp:
        state.set("Last State Update", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(state.render(), encoding="utf-8")
    os.replace(tmp, path)
    return path


def mutate(fn, stamp: bool = True) -> State:
    """Apply `fn(state)` under the lock, re-reading first so writes never clobber.

    This is the only supported write path. Subagents report through the
    orchestrator, which funnels here, so two agents updating different sections
    cannot overwrite each other.
    """
    with _locked():
        state = load()
        fn(state)
        save(state, stamp=stamp)
        return state
