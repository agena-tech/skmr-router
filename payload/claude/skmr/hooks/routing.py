"""Routing hook -- announces a hand-off to another SKMR subsystem.

Deliberately stateful within a process: the same target is announced once, so a
command that consults Obsidian three times does not print three identical
routing lines. Routing is only announced when a real hand-off happens.
"""
from __future__ import annotations

from ..core import output

_announced: set[str] = set()


def to(target: str) -> None:
    key = (target or "").strip()
    if not key or key in _announced:
        return
    _announced.add(key)
    output.routing(key)


def reset() -> None:
    _announced.clear()
