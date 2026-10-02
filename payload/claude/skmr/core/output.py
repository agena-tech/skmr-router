"""Standardized `[SKMR]:` output.

Every SKMR message goes through here so no skill hardcodes its own banner and
the wording stays uniform. Colour is opt-in and degrades to plain text whenever
the stream is not a terminal, NO_COLOR is set, or config says never -- an ANSI
capable terminal is a nicety, never a requirement.
"""
from __future__ import annotations

import os
import sys
from typing import IO

from . import config

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

PREFIX = "[SKMR]:"


def color_enabled(stream: IO[str] | None = None) -> bool:
    mode = str(config.get("color", "auto")).lower()
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    if mode == "always" or os.environ.get("SKMR_FORCE_COLOR"):
        return True
    target = stream or sys.stdout
    try:
        return bool(target.isatty())
    except Exception:
        return False


def paint(text: str, code: str, stream: IO[str] | None = None) -> str:
    return f"{code}{text}{RESET}" if color_enabled(stream) else text


def _emit(message: str, stream: IO[str] | None = None) -> None:
    print(f"{PREFIX} {message}", file=stream or sys.stdout, flush=True)


def executing(skill: str) -> None:
    _emit(f'Executing: "{paint(skill, GREEN)}" ...')


def planning(skill: str) -> None:
    _emit(f'Planning: "{paint(skill, GREEN)}" ...')


def routing(target: str) -> None:
    _emit(f'Routing: "{paint(target, GREEN)}" ...')


def subagents(count: int) -> None:
    _emit("No subagents required." if count <= 0 else f"{count} subagents planned (not started).")


def completed(skill: str) -> None:
    _emit(f'Completed: "{paint(skill, GREEN)}"')


def warning(message: str) -> None:
    _emit(f"{paint('Warning', YELLOW, sys.stderr)}: {message}", stream=sys.stderr)


def error(message: str) -> None:
    _emit(f"{paint('Error', RED, sys.stderr)}: {message}", stream=sys.stderr)


def info(message: str) -> None:
    _emit(message)


def table(headers: list[str], rows: list[list[str]]) -> None:
    """Render a markdown table. An empty row set prints nothing at all."""
    if not rows:
        return
    print("| " + " | ".join(headers) + " |", flush=True)
    print("|" + "|".join("---" for _ in headers) + "|", flush=True)
    for row in rows:
        cells = [str(cell).replace("|", "\\|") for cell in row]
        cells += [""] * (len(headers) - len(cells))
        print("| " + " | ".join(cells[: len(headers)]) + " |", flush=True)
