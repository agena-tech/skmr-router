"""Planning hook: task analysis, plan construction and the subagent decision.

The planner is deterministic and heuristic -- it does not call a model. Its job
is to make the execution shape explicit before work starts: what the task is,
which steps it takes, which resources it needs, and whether splitting the work
across subagents would actually help.

Two rules drive the subagent decision and both matter:
  * parallelism is only proposed for work units that are genuinely independent,
  * the count follows from the units found, never from a constant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core import config
from ..core.registry import Skill

SIMPLE, MEDIUM, COMPLEX = "simple", "medium", "complex"

# Signals that a task is broader than its command's declared baseline.
_ESCALATE = re.compile(
    r"\b(all|every|entire|full|repository|repo[- ]wide|codebase|audit|refactor|"
    r"migrat\w*|comprehensive|deep|exhaustive|end[- ]to[- ]end|"
    r"tüm|hepsi|bütün|kapsamlı|derin|komple)\b",
    re.IGNORECASE,
)
_DEESCALATE = re.compile(r"^\s*(status|health|show|list|help|version)\s*$", re.IGNORECASE)

_DEPTH_ORDER = {SIMPLE: 0, MEDIUM: 1, COMPLEX: 2}


@dataclass
class SubagentSpec:
    agent_id: str
    role: str
    task: str
    expected_output: str
    depends_on: tuple[str, ...] = ()


@dataclass
class Plan:
    skill: str
    task: str
    complexity: str
    success: str
    steps: list[str] = field(default_factory=list)
    memory: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    subagents: list[SubagentSpec] = field(default_factory=list)
    rationale: str = ""

    @property
    def parallel(self) -> bool:
        return bool(self.subagents)


def _units(task: str) -> list[str]:
    """Split a task into candidate independent work units.

    Only explicit enumeration counts. Inferring units from prose would invent
    parallelism that does not exist, which is worse than running serially.
    """
    if not task.strip():
        return []
    parts = re.split(r"\s*(?:,|;|\bve\b|\band\b|\+)\s*", task.strip())
    return [p.strip() for p in parts if len(p.strip()) > 2]


def classify(skill: Skill, task: str) -> str:
    depth = MEDIUM if skill.name == "plan" else skill.planning
    if skill.name == "plan" and len(_units(task)) == 1 and not _ESCALATE.search(task) and re.search(r"\b(rename|format|lookup|show|list|replace one|tek dosya|yeniden adlandir)\b", task, re.I):
        return SIMPLE
    if _DEESCALATE.match(task or ""):
        return SIMPLE
    if _ESCALATE.search(task or "") and _DEPTH_ORDER[depth] < 2:
        depth = COMPLEX if depth == MEDIUM else MEDIUM
    units = _units(task)
    if len(units) >= 3 and _DEPTH_ORDER[depth] < 2 and skill.subagents:
        depth = COMPLEX
    return depth


def _steps_for(skill: Skill, task: str, complexity: str, parallel: bool = False) -> list[str]:
    """Steps describe the plan that was actually chosen.

    A complex task can still be serial -- dependent work is deliberately not
    split -- so the split/aggregate steps follow the subagent decision rather
    than the complexity label. Printing "dispatch them" under a plan with no
    agents tells the operator to do the one thing the planner just refused.
    """
    if complexity == SIMPLE:
        return [f"Run {skill.name} and report the result."]
    base = [
        f"Resolve inputs for {skill.name}" + (f': "{task}"' if task else ""),
        "Load the required memory and tool context",
        "Execute the skill",
        "Verify the result against the success condition",
    ]
    if complexity == COMPLEX and parallel:
        base.insert(2, "Split independent work units and dispatch them")
        base.insert(4, "Aggregate and de-duplicate the results")
    elif complexity == COMPLEX:
        base.insert(2, "Run the dependent steps in order; each waits on the previous result")
    return base


def _subagents_for(skill: Skill, task: str, complexity: str) -> tuple[list[SubagentSpec], str]:
    """Decide whether subagents help, and if so which ones.

    Returns the specs plus the reason, so a zero-subagent decision is still
    explainable rather than silent.
    """
    if not skill.subagents:
        return [], f"{skill.name} declares no subagent support."
    if complexity != COMPLEX:
        return [], "Task is not complex enough for orchestration overhead to pay off."
    units = _units(task)
    if re.search(r"\b(then|after|before|depends on|once|sonra|ardından|ardindan|önce|once)\b|[→⇒]", task, re.I):
        return [], "Sequential/dependent work must be planned serially."
    units = list(dict.fromkeys(units))
    if len(units) < 2:
        return [], "No independent work units found; parallelism would be artificial."

    cap = int(config.get("max_subagents", 10))
    units = units[:cap]
    specs = [
        SubagentSpec(
            agent_id=f"unit-{i:02d}-agent",
            role=f"{skill.name} specialist",
            task=unit,
            expected_output=f"Findings for: {unit}",
        )
        for i, unit in enumerate(units, start=1)
    ]
    # A verifier is only worth a slot when there is more than one result to
    # reconcile, and it genuinely depends on all of them.
    if len(specs) >= 2 and len(specs) < cap:
        specs.append(
            SubagentSpec(
                agent_id="verifier-agent",
                role="Verifier",
                task="Validate and reconcile the findings of the other agents",
                expected_output="Verification report",
                depends_on=tuple(s.agent_id for s in specs),
            )
        )
    return specs, f"{len(units)} independent work units detected."


def build(skill: Skill, task: str) -> Plan:
    task = (task or "").strip()
    complexity = classify(skill, task)
    specs, rationale = _subagents_for(skill, task, complexity)
    return Plan(
        skill=skill.name,
        task=task,
        complexity=complexity,
        success=f"{skill.summary.rstrip('.')} — completed and verified.",
        steps=_steps_for(skill, task, complexity, parallel=bool(specs)),
        memory=skill.memory,
        tools=(skill.handler.split(":")[0],),
        subagents=specs,
        rationale=rationale,
    )
