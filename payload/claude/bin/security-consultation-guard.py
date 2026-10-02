#!/usr/bin/env python3
"""Protect the canonical Obsidian write path and deduplicate consultations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from skmr_security_registry import is_security_skill, normalize  # noqa: E402
from skmr_permissions import config_path

BASE_STATE = Path(os.environ.get("SKMR_STATE_DIR", "/root/.claude/state"))
ACTIVE = BASE_STATE / "skmr-active-sessions"
CONSULTATIONS = BASE_STATE / "skmr-consultations"
CONFIG = json.loads(config_path().read_text(encoding='utf-8'))
VAULT = Path(CONFIG['vault_local'])
VAULT_ROOT = VAULT.resolve(strict=False)
# /mnt/c is a case-insensitive DrvFs mount: a case-differing spelling names the
# same inode, so every vault comparison is casefolded.
VAULT_KEY = str(VAULT_ROOT).casefold()
WRITER = os.environ.get('SKMR_WRITER', str(Path(__file__).resolve().parent.parent / 'skills/skmr/scripts/obsidian_memory.py'))
WINDOWS_VAULT = str(CONFIG.get('vault_windows_path') or '').replace('\\', '/')

def _portable_path(value):
    text = str(value or '').replace('\\', '/')
    match = re.match(r'^([A-Za-z]):/(.*)', text)
    return '/mnt/' + match[1].lower() + '/' + match[2] if match else text

VAULT_ALIASES = tuple(dict.fromkeys(key.casefold().rstrip('/') for key in
    (str(VAULT), str(VAULT_ROOT), WINDOWS_VAULT, _portable_path(WINDOWS_VAULT)) if key))
# Every subcommand the canonical writer exposes. Each one is either read-only or
# routes through the guarded preview/commit path; "log" and "init" only touch the
# vault's non-note layers (the chronological log and the raw-source directory)
# and can never write a canonical note.
WRITER_ACTIONS = {"route", "search", "preview", "commit", "validate", "lint", "init", "log"}
REPORT_SEARCH = "/root/.claude/knowledge/bugskill-ai/search_reports.py"
CONSULTATION_SKILLS = {"bugbountyskills", "hackerone-intelligence"}
# Native auto-memory is working continuity only. A durable Obsidian note has a
# distinctive frontmatter shape, and the documented native format does not, so
# the store boundary can be enforced instead of merely written down in policy.
NATIVE_MEMORY = re.compile(r"/projects/[^/]+/memory(?:/|$)")
DURABLE_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
DURABLE_KEYS = {"title", "area", "status", "updated"}
MUTATING_SHELL = re.compile(
    r"(?:^|[;&|\n])\s*(?:rm|mv|cp|install|mkdir|rmdir|touch|truncate|sed\s+-i|perl\s+-i|tee)\b|(?:^|[^<])>>?|\bos\.replace\b|\bwrite_text\b",
    re.I,
)
WRITER_NAME = Path(WRITER).name
# Mutation aimed at the writer itself. Copying the writer somewhere else is
# deliberately absent: that is a read.
WRITER_MUTATION = re.compile(
    r"(?:^|[;&|\n])\s*(?:rm|mv|install|truncate|shred|tee|chmod|chown|ln|dd)\b"
    r"|\bsed\s+-i|\bperl\s+-i"
    r"|>>?\s*[^\s;&|]*" + re.escape(WRITER_NAME),
    re.I,
)
# A token in program position is judged by basename, so relative spellings count.
INTERPRETERS = {"python", "python2", "python3", "py", "sh", "bash", "zsh", "dash", "ksh", "perl", "ruby", "node"}
WRAPPERS = {"sudo", "env", "command", "nice", "nohup", "time", "stdbuf", "timeout", "exec"}
# Modules whose whole job is to read/compile, so -m with one is not opaque.
READ_ONLY_MODULES = {"py_compile", "compileall", "pydoc", "tokenize", "ast", "json.tool", "dis"}
# Tools whose effect is bounded by argv[0] alone. find (-delete/-exec), awk, sed
# and xargs are deliberately excluded: their argv[0] proves nothing.
READ_TOOLS = {
    "ls", "cat", "head", "tail", "stat", "wc", "sha256sum", "md5sum", "pwd", "cd",
    "grep", "egrep", "fgrep", "file", "du", "sort", "uniq", "cut", "nl", "cmp",
    "diff", "realpath", "basename", "dirname", "mountpoint", "readlink", "echo", "true",
}
LONE_AMPERSAND = re.compile(r"(?<!&)&(?!&)")
OPERATORS = {";", "|", "||", "&&", "&", "\n"}


def _segments(command: str) -> list[list[str]] | None:
    """Split a command into stages, respecting quoting.

    Splitting the raw string on operators first would treat a ';' or '&&' that
    merely sits inside a quoted argument as a real operator, which is how the
    previous rule manufactured the very false positives it was meant to stop.
    shlex with punctuation_chars emits operators as their own tokens only when
    they are genuinely unquoted. Returns None when the command cannot be parsed.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in OPERATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _is_python(name: str) -> bool:
    return name == "py" or name.startswith("python")


def _is_interpreter(name: str) -> bool:
    return name in INTERPRETERS


def _program_index(tokens: list[str]) -> int:
    """Index of the real program, skipping VAR=value prefixes and wrappers."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if Path(token).name in WRAPPERS or ("=" in token and not token.startswith("-")):
            index += 1
            continue
        return index
    return len(tokens)


def invokes_writer(command: str) -> bool:
    """True when the command would EXECUTE the writer, not merely name it.

    Judged on basenames, so a relative spelling ("cd scripts && python3
    obsidian_memory.py commit") is caught as well. The old absolute-substring
    test missed that entirely, which made the rule friction instead of a
    boundary, while denying plain reads that only mentioned the path.
    """
    segments = _segments(command)
    if segments is None:
        return WRITER_NAME in command  # unparseable: cannot prove it is a read
    for tokens in segments:
        index = _program_index(tokens)
        if index >= len(tokens):
            continue
        if Path(tokens[index]).name == WRITER_NAME:
            return True  # argv[0] position: executed directly
        if _is_interpreter(Path(tokens[index]).name):
            for token in tokens[index + 1:]:
                if token.startswith("-"):
                    continue
                if Path(token).name == WRITER_NAME:
                    return True  # first non-flag argument is the script
                break
    return False


def inline_program(command: str) -> bool:
    """True when an interpreter carries opaque inline source or an opaque module.

    Only consulted for interpreters, so "wc -c <writer>" stays a read.
    """
    segments = _segments(command)
    if segments is None:
        return True
    for tokens in segments:
        index = _program_index(tokens)
        if index >= len(tokens) or not _is_interpreter(Path(tokens[index]).name):
            continue
        rest = tokens[index + 1:]
        for position, option in enumerate(rest):
            if option in ("-c", "-e"):
                return True
            if option == "-m":
                module = rest[position + 1] if position + 1 < len(rest) else ""
                if module not in READ_ONLY_MODULES:
                    return True
    return False


def read_only_shell(command: str) -> bool:
    """True when every stage of a chained command is a plain read.

    Chaining alone is not a mutation: a command built only of reads stayed denied
    merely because it contained ';' or '|'. Redirection, command substitution and
    backgrounding are still refused outright; '&&' is preserved.
    """
    if any(marker in command for marker in ("<", ">", "$", "`")):
        return False
    if LONE_AMPERSAND.search(command):
        return False
    segments = _segments(command)
    if not segments:
        return False
    for tokens in segments:
        if not tokens or Path(tokens[0]).name not in READ_TOOLS:
            return False
    return True


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def digest(value: str, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def active_state(session_id: str) -> dict:
    return load_json(ACTIVE / f"{digest(session_id)}.json")


def skill_name(event: dict) -> str:
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    return normalize(tool_input.get("skill") or tool_input.get("name"))


def mark_security_active(session_id: str) -> None:
    if not session_id:
        return
    state = active_state(session_id)
    if state.get("session_id") != session_id:
        state = {"session_id": session_id, "turn_id": secrets.token_hex(12)}
    state["active"] = True
    # Sticky: the security work itself happens in the turns that follow the
    # triggering prompt, so downstream reminders must stay armed for the session.
    state["session_active"] = True
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(ACTIVE / f"{digest(session_id)}.json", state)


def normalized_path(raw: object, cwd: object = "/") -> Path:
    path = Path(_portable_path(raw))
    if not path.is_absolute():
        path = Path(str(cwd or "/")) / path
    return path.resolve(strict=False)


def in_vault(path: Path) -> bool:
    candidate = str(path).casefold()
    aliases = VAULT_ALIASES
    if any(candidate == key or candidate.startswith(key + os.sep) for key in aliases):
        return True
    # Inode identity also catches alternate mounts and hard links to the vault.
    for ancestor in (path, *path.parents):
        try:
            if ancestor.exists() and ancestor.samefile(VAULT_ROOT):
                return True
        except OSError:
            continue
    return False


def mentions_vault(command: str) -> bool:
    return any(key in command.replace('\\', '/').casefold() for key in VAULT_ALIASES)


def written_texts(tool_input: dict) -> list[str]:
    """Every payload this call would write, kept separate.

    Joining them would hide a durable note that is not the first edit, since
    the frontmatter shape is only recognisable at the start of its own text.
    """
    parts = [tool_input.get(key) for key in ("content", "new_string", "new_source")]
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        parts += [item.get("new_string") for item in edits if isinstance(item, dict)]
    return [part for part in parts if isinstance(part, str)]


def durable_note_shape(text: str) -> bool:
    match = DURABLE_FRONTMATTER.match(text or "")
    if not match:
        return False
    keys = {
        line.split(":", 1)[0].strip().casefold()
        for line in match.group(1).splitlines()
        if ":" in line and not line[:1].isspace()
    }
    return DURABLE_KEYS <= keys


def skill_call(event: dict) -> tuple[str, str] | None:
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if event.get("tool_name") == "Skill":
        name = skill_name(event)
        if name in CONSULTATION_SKILLS:
            query = str(tool_input.get("args") or tool_input.get("argument") or tool_input.get("query") or "")
            return name, " ".join(query.casefold().split())
    if event.get("tool_name") == "Bash":
        command = str(tool_input.get("command") or "")
        if REPORT_SEARCH in command:
            return "hackerone-intelligence", " ".join(command.casefold().split())
    return None


def successful(event: dict) -> bool:
    response = event.get("tool_response")
    if not isinstance(response, dict):
        return True
    return not (
        response.get("is_error")
        or response.get("isError")
        or response.get("success") is False
        or response.get("exit_code", response.get("exitCode", 0)) not in (0, None)
    )


def current_turn_id(session_id: str) -> str:
    """Return this turn's id, materializing one when no state exists yet.

    A constant fallback made every turn share one deduplication bucket, which
    could deny a legitimate later consultation for the rest of the session.
    """
    state = active_state(session_id)
    if state.get("session_id") == session_id and state.get("turn_id"):
        return str(state["turn_id"])
    turn_id = secrets.token_hex(12)
    if session_id:
        carried = dict(state) if state.get("session_id") == session_id else {}
        carried.update({
            "session_id": session_id,
            "active": bool(carried.get("active")),
            "session_active": bool(carried.get("session_active")),
            "turn_id": turn_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        atomic_json(ACTIVE / f"{digest(session_id)}.json", carried)
    return turn_id


def consultation_path(session_id: str, turn_id: str) -> Path:
    return CONSULTATIONS / f"{digest(session_id + ':' + turn_id)}.json"


def approved_writer_command(command: str) -> bool:
    if any(marker in command for marker in (";", "&", "|", "<", ">", "\n", "$", "`")):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    # The guarantee comes from tokens[1] (the writer's absolute path) and
    # tokens[2] (an allowed action), never from how argv[0] happens to be spelled:
    # "python3 <writer> search" and "/usr/bin/python3 <writer> search" are the same
    # binary with the same security profile.
    if len(tokens) < 3 or tokens[1] != WRITER or tokens[2] not in WRITER_ACTIONS:
        return False
    return _is_python(Path(tokens[0]).name)


def proposed_path(event: dict) -> Path | None:
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    raw = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
    )
    return normalized_path(raw, event.get("cwd")) if raw else None


def handle(event: dict) -> dict | None:
    event_name = str(event.get("hook_event_name") or "")
    tool_name = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}

    if event_name == "PreToolUse" and tool_name == "Skill" and is_security_skill(skill_name(event)):
        mark_security_active(str(event.get("session_id") or ""))

    if event_name == "PreToolUse" and tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        path = proposed_path(event)
        if path and in_vault(path):
            return deny("Direct Obsidian writes are denied. Use the SKMR writer preview/commit pathway; .obsidian is always off limits.")
        if path and NATIVE_MEMORY.search(str(path)) and any(map(durable_note_shape, written_texts(tool_input))):
            return deny(
                "This is a durable Obsidian note being written into native auto-memory. "
                "Native memory holds working continuity only; route permanent knowledge "
                "through the skmr skill's canonical writer instead."
            )

    if event_name == "PreToolUse" and tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        scoped = mentions_vault(command) or in_vault(normalized_path(event.get('cwd') or '/'))
        # Arbitrary shell/interpreter programs cannot be proven read-only by regex,
        # so vault context still fails closed -- but a chain of plain reads is a
        # read, and is no longer denied just for being chained.
        if scoped and not approved_writer_command(command) and not read_only_shell(command):
            return deny('Vault shell command is not a supported read-only operation. Use Read or the canonical writer.')
        if mentions_vault(command) and MUTATING_SHELL.search(command) and not approved_writer_command(command):
            return deny("Shell mutation of the Obsidian vault is denied. Use obsidian_memory.py preview then commit.")
        # Naming the writer is not running it. Separate the three real concerns
        # instead of denying every command the path appears in.
        if WRITER_NAME in command and not approved_writer_command(command):
            if invokes_writer(command):
                return deny(
                    "Only a single, direct obsidian_memory.py command is allowed, using one of: "
                    + "/".join(sorted(WRITER_ACTIONS))
                    + ". No pipes, redirection, substitution or chaining."
                )
            if inline_program(command):
                return deny(
                    "An inline program naming the canonical writer cannot be proven read-only. "
                    "Use Read or a plain read tool."
                )
            if WRITER_MUTATION.search(command):
                return deny("Mutating the canonical writer is denied.")

    call = skill_call(event)
    if not call:
        return None
    session_id = str(event.get("session_id") or "")
    turn_id = current_turn_id(session_id)
    path = consultation_path(session_id, turn_id)
    key = digest(json.dumps(call, ensure_ascii=False, sort_keys=True), 32)
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        payload = load_json(path)
        completed = payload.get("completed") if isinstance(payload.get("completed"), list) else []
        if event_name == "PreToolUse" and key in completed:
            return deny(f"Equivalent {call[0]} consultation already succeeded in this turn; reuse its result.")
        if event_name == "PostToolUse" and successful(event) and key not in completed:
            completed.append(key)
            atomic_json(path, {"session_id_hash": digest(session_id), "turn_id": turn_id, "completed": completed})
    return None


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    result = handle(event)
    if result:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
