"""SKMR -- command dispatch, planning, agent orchestration and memory.

Layout:
    core/      config, standardized output, skill registry, dispatcher
    hooks/     execution / planning / routing hooks (central, never per-skill)
    planning/  complexity analysis and the subagent decision
    agents/    identity + topology store and the subagent orchestrator
    memory/    native MEMORY.md state, intent detection, Obsidian retrieval
    commands/  one handler per user-facing /skmr:<command>

Permanent Obsidian mutations are NOT performed here. This package reads the
vault and delegates every write to the canonical writer
(skills/skmr/scripts/obsidian_memory.py) through its preview -> commit flow.
"""

__version__ = "1.0.0"
