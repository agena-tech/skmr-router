#!/usr/bin/env python3
"""Emit a tiny SKMR reminder only for genuine security or memory work."""

import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from skmr_security_registry import is_security_command  # noqa: E402

STATE_DIR = Path(os.environ.get("SKMR_STATE_DIR", "/root/.claude/state")) / "skmr-active-sessions"
SECURITY = re.compile(
    r"(?i)\b(?:bug\s*bounty|pentest|security\s+(?:test|research|review|audit)|vulnerabilit(?:y|ies)|"
    r"exploit(?:ation)?|recon(?:naissance)?|authorized\s+target|burp(?:\s+suite)?|"
    r"(?:xss|ssrf|sqli|idor|bola|csrf|cors|oauth|oidc|saml|jwt|xxe|ssti|rce|lfi)|"
    r"request\s+smuggling|race\s+condition|account\s+takeover|authorization\s+bypass|"
    r"yetki\s+atlatma|açık\s+ara|zafiyet|güvenlik\s+(?:testi|araştırması|incelemesi))\b"
)
MEMORY_INTENT = re.compile(
    r"(?i)(?:\bobsidian\b|what did we (?:do|try|learn|find|decide|discuss|use)(?:\b.{0,48})?|"
    r"do you remember|\brecall\b|bunu\s+(?:kaydet|unutma)|remember this|save this|store this|"
    r"hat[ıi]rl[ıi]yor musun|\bhat[ıi]rla\b|en son ne yapm[ıi]şt[ıi]k|"
    r"(?:daha önce|önceden|geçen sefer).{0,48}(?:ne|biz|sen|yap|öğren|bul|dene|kullan|konuş|karar|kaydet|hat[ıi]r)|"
    r"(?:ne|biz|sen|yap|öğren|bul|dene|kullan|konuş|karar|kaydet|hat[ıi]r).{0,48}(?:daha önce|önceden|geçen sefer)|"
    r"(?:previously|last time).{0,48}(?:we|you|our|learned|used|found|decided|discussed|tried|saved)|"
    r"(?:we|you|our|learned|used|found|decided|discussed|tried|saved).{0,48}(?:previously|last time))"
)
# SKMR_PROFILE_HARDENING_V1: implicit questions about the user's durable
# devices/plans/preferences/etc. are memory-intent too. This only arms the lazy
# reminder; current-context-first policy still decides whether a vault read is needed.
PROFILE_CONTEXT_INTENT = re.compile(
    r"(?i)(?:\bmy\s+(?:laptop|computer|pc|phone|tablet|device|ram|university|school|major|department|plan|goal|preference|address|city|birthday|family|partner)\b|"
    r"\bbenim\s+(?:laptop(?:um|ta|ım)?|bilgisayar(?:ım|da)?|telefon(?:um|da)?|tablet(?:im|te)?|cihaz(?:ım|ımda)?|ram(?:im|ım)?|üniversite(?:m|de)?|okul(?:um|da)?|bölüm(?:üm|ümde)?|plan(?:ım|larım)?|hedef(?:im|lerim)?|tercih(?:im|lerim)?|adres(?:im)?|şehir(?:im)?|aile(?:m)?|kardeş(?:im)?|babam|annem|sevgilim)\b|"
    r"\b(?:hangi|ne)\s+(?:laptop|bilgisayar|telefon|tablet|cihaz|ram|üniversite|okul|bölüm|plan|tercih|hedef)\b.{0,40}\b(?:var|vardı|kullanıyorum|kullanıyordum|almıştım|seçmiştim|karar vermiştim)\b)"
)
STANDALONE_RECALL = {"recall", "previously", "last time", "hatırla", "hatirla", "daha önce", "önceden", "geçen sefer"}
COMMAND = re.compile(r"^\s*/(?:(?:[a-z0-9_.-]+):)?([a-z0-9_.-]+)(?:\s|$)", re.I)


def state_path(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return STATE_DIR / f"{digest}.json"


def session_was_active(session_id: str) -> bool:
    """Whether this session already entered SKMR security work.

    Classification is per prompt, but the engagement is not: the real work
    happens in the turns after the trigger. Without this, a neutral follow-up
    such as "devam et" silently disarmed the failure and subagent reminders.
    """
    try:
        state = json.loads(state_path(session_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(state, dict) or state.get("session_id") != session_id:
        return False
    return bool(state.get("session_active") or state.get("active"))


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


def memory_intent(prompt: str) -> bool:
    normalized = " ".join(prompt.casefold().strip().rstrip("?!.").split())
    return normalized in STANDALONE_RECALL or bool(MEMORY_INTENT.search(prompt) or PROFILE_CONTEXT_INTENT.search(prompt))


def security_command(prompt: str) -> bool:
    match = COMMAND.match(prompt)
    return bool(match and is_security_command(match.group(1)))


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        event = {}
    prompt = str(event.get("prompt") or "")
    session_id = str(event.get("session_id") or "")
    command_active = security_command(prompt)
    turn_active = bool(SECURITY.search(prompt) or memory_intent(prompt) or command_active)
    active = turn_active or (bool(session_id) and session_was_active(session_id))
    if session_id:
        atomic_json(
            state_path(session_id),
            {
                "session_id": session_id,
                # "active" is what downstream reminders read; it stays armed for
                # the whole engagement. "turn_active" keeps this prompt's own
                # classification for anything that needs the narrower signal.
                "active": active,
                "turn_active": turn_active,
                "session_active": active,
                "turn_id": secrets.token_hex(12),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    # Direct security commands already expand their own instructions. Keep this
    # path state-only so slash-command activation adds zero model-context bytes.
    # Emission follows this prompt's own signal, never the sticky session flag,
    # so a long engagement never pays the reminder cost more than once per turn
    # that genuinely asks for it.
    if not turn_active or command_active:
        return
    message = (
        "SKMR active. Use current context first; retrieve only for a concrete gap. "
        "Durable knowledge belongs in Obsidian via the on-demand skmr skill; native memory is working continuity only."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": message}}, ensure_ascii=False))


if __name__ == "__main__":
    from skmr_inbox import run_with_inbox
    run_with_inbox(main, "UserPromptSubmit")
