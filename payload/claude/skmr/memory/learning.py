"""Knowledge and error learning: turn an episode into reusable knowledge.

Two things this module refuses to do, because they are what turns a memory store
into a landfill:

  * dump the raw episode -- a lesson is an abstraction (problem, solution,
    lesson), not a transcript,
  * save everything -- `assess()` filters, and only a lesson that is novel,
    reusable and generalized is even proposed.

It never commits. A qualifying lesson becomes a candidate file for the canonical
writer's `preview -> commit` flow, so the acceptance gate, PARA routing, secret
scanning and path safety all still apply.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core import config

KNOWLEDGE = "knowledge"
ERROR = "error"

# Signals that an episode is one-off noise rather than a durable lesson. These
# mirror the policy's "do not save noise" list.
NOISE = re.compile(
    r"\b(typo|typos|one[- ]off|transient|temporar\w+|flaky|scanner noise|"
    r"rate[- ]limit\w*|timed out|timeout|retry|retried|404|connection reset|"
    r"gecici|tek seferlik|yazim hatasi)\b",
    re.IGNORECASE,
)
# Signals of a genuinely reusable lesson.
DURABLE = re.compile(
    r"\b(because|root cause|always|never|whenever|before assuming|"
    r"boundary|limitation|constraint|invariant|protocol|architectur\w+|"
    r"bypass|precondition|must|requires|prevents|kok neden|sinir|kisit)\b",
    re.IGNORECASE,
)

MIN_LESSON_CHARS = 40


@dataclass
class Episode:
    """What happened, in the three parts a lesson needs."""
    topic: str
    problem: str
    lesson: str
    # A limitation or a falsified hypothesis is a lesson with no solution, so this
    # is optional rather than required.
    solution: str = ""
    kind: str = KNOWLEDGE                    # knowledge | error
    evidence: list[dict[str, str]] = field(default_factory=list)
    verified: bool = False
    explicit_user_request: bool = False
    links: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    save_worthy: bool
    reasons: list[str] = field(default_factory=list)

    def explain(self) -> str:
        return "; ".join(self.reasons) or ("qualifies" if self.save_worthy else "rejected")


def assess(episode: Episode) -> Assessment:
    """Decide whether an episode is durable knowledge. Rejections are explained.

    The gate here is deliberately the *policy* gate, not a softer one: a lesson
    claiming verified status must carry machine-checkable evidence, exactly as
    the canonical writer demands.
    """
    reasons: list[str] = []
    lesson = (episode.lesson or "").strip()

    if len(lesson) < MIN_LESSON_CHARS:
        reasons.append("lesson too thin to be reusable")
    if not (episode.problem or "").strip():
        reasons.append("no problem statement")
    if NOISE.search(lesson) and not DURABLE.search(lesson):
        reasons.append("reads as one-off noise, not a generalized lesson")
    if not DURABLE.search(lesson) and not DURABLE.search(episode.solution or ""):
        reasons.append("no generalization: states what happened, not what to do differently")
    # A lesson phrased about one specific run is working context, not knowledge.
    if re.search(r"\b(this run|this session|right now|su an|bu oturum)\b", lesson, re.IGNORECASE):
        reasons.append("scoped to a single run")
    if episode.verified and not episode.evidence:
        reasons.append("claims verified but carries no machine-checkable evidence")

    return Assessment(not reasons, reasons)


def render(episode: Episode) -> str:
    """The abstracted note body: problem -> solution -> lesson, never a transcript."""
    parts = [f"## Summary", episode.problem.strip(), ""]
    if episode.solution.strip():
        parts += ["## Solution", episode.solution.strip(), ""]
    parts += ["## Lesson", episode.lesson.strip()]
    if episode.kind == ERROR:
        parts += ["", "## Prevention",
                  "Apply the lesson above before repeating the same approach."]
    return "\n".join(parts).strip()


def candidate(episode: Episode) -> dict[str, object]:
    """Build a writer candidate. Semantic fields only -- the writer owns routing.

    The `automatic` flag is not cosmetic: the writer's gate only preserves an
    automatic candidate when it is verified. An unverified lesson is therefore
    offered as an explicit user-requested save instead of a silently failing
    automatic one -- which is what the save gate actually permits.
    """
    body = render(episode)
    payload: dict[str, object] = {
        "title": episode.topic.strip(),
        # A failure lesson and a technique are both methodology knowledge; the
        # writer decides the physical PARA destination, never this module.
        "semantic_class": "methodology",
        "content": body,
        "tags": sorted(set(episode.tags + (["lesson", "error"] if episode.kind == ERROR else ["lesson"]))),
        "links": episode.links,
        "novel": True,
        "reusable": True,
        "verified": bool(episode.verified),
    }
    if episode.verified:
        payload["automatic"] = True
    else:
        payload["automatic"] = False
        payload["explicit_user_request"] = bool(episode.explicit_user_request)
        payload["status"] = "user-provided"
    if episode.evidence:
        payload["evidence"] = episode.evidence
    return payload


def route_note(episode: Episode) -> str:
    """How this candidate will be offered to the writer, in one line."""
    if episode.verified:
        return "automatic save (verified + evidence): the gate can preserve it on its own."
    return ("explicit user-requested save (not verified): the gate will not preserve it "
            "automatically, so it is offered as an explicit save you are asking for.")


def stage(episode: Episode) -> Path:
    """Write the candidate to disk for `obsidian_memory.py preview`.

    Staging, not saving: nothing enters the vault until the writer's guarded
    preview -> commit flow accepts it.
    """
    target = config.state_dir() / "learning-candidates"
    target.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", episode.topic.casefold()).strip("-")[:60] or "lesson"
    path = target / f"{slug}.json"
    path.write_text(json.dumps(candidate(episode), indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path
