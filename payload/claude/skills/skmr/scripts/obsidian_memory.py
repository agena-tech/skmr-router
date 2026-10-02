#!/usr/bin/env python3
"""Canonical guarded filesystem interface for SKMR permanent Obsidian memory."""

from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.environ.get('AGENTCOMM_LIB', str(Path(__file__).resolve().parents[3] / 'lib')))
from skmr_agent_policy import VAULT, ROLE, check_root, vault_mount_usable, vault_mount_writable
from skmr_permissions import can_write
STATE_DIR = Path(os.environ.get('SKMR_STATE_DIR', '/root/.claude/state'))
MAX_CONTENT_BYTES = 64 * 1024
PREVIEW_TTL = timedelta(hours=24)

ROUTES = {
    "system": ("00 System", "system", "system"),
    "system-skmr": ("00 System/SKMR", "system", "system"),
    "personal": ("02 Areas/Personal", "area", "personal"),
    "area-security": ("02 Areas/Security", "area", "security"),
    "area-ai": ("02 Areas/AI", "area", "ai"),
    "area-education": ("02 Areas/Education", "area", "education"),
    "area-technology": ("02 Areas/Technology", "area", "technology"),
    "bug-hunting-project": ("01 Projects/Bug Hunting", "project", "security"),
    "research-project": ("01 Projects/Research", "project", "research"),
    "other-project": ("01 Projects/Other", "project", "other"),
    "completed-project": ("04 Archives", "archive", "archive"),
    "vulnerability": ("03 Resources/Security/Vulnerabilities", "vulnerability", "security"),
    "methodology": ("03 Resources/Security/Methodologies", "methodology", "security"),
    "technique": ("03 Resources/Security/Techniques", "technique", "security"),
    "security-tool": ("03 Resources/Security/Tools", "tool", "security"),
    "security-protocol": ("03 Resources/Security/Protocols", "protocol", "security"),
    "security-report": ("03 Resources/Security/Reports", "report", "security"),
    "security-map": ("03 Resources/Security", "map", "security"),
    "ai-resource": ("03 Resources/AI", "resource", "ai"),
    "reference": ("03 Resources/Reference", "reference", "reference"),
}

STALE_AFTER_DAYS = 180
# Two vault layers that are browsable in Obsidian but are not canonical notes:
# the immutable raw-source collection the wiki is compiled from, and the
# chronological log. Neither is validated, indexed, or linkable as a note.
def _select_raw_sources_dir() -> str:
    """Choose the writer-owned raw-source layer without treating it as a note namespace.

    Legacy vaults may still contain ``04 Sources`` while newer layouts use
    ``05 Sources``. If only the legacy directory exists, keep using it rather
    than creating a second raw-source tree. Both names are always excluded from
    canonical note validation/indexing.
    """
    override = os.environ.get("SKMR_RAW_SOURCES_DIR", "").strip()
    if override:
        if override not in {"04 Sources", "05 Sources"}:
            raise RuntimeError("SKMR_RAW_SOURCES_DIR must be '04 Sources' or '05 Sources'")
        return override
    root = Path(VAULT)
    legacy = root / "04 Sources"
    current = root / "05 Sources"
    try:
        if legacy.is_dir() and not current.exists():
            return "04 Sources"
    except OSError:
        # Runs at import time, before argument parsing. An unreachable vault
        # answers ENODEV rather than ENOENT, which Path.is_dir()/exists() do not
        # swallow, so every command -- even --help -- became a bare traceback.
        # The layout cannot be probed: assume the current one and let the
        # commands that actually need the vault report its absence via
        # check_root(), which now enumerates instead of trusting the mountpoint.
        return "05 Sources"
    return "05 Sources"


RAW_SOURCES_DIR = _select_raw_sources_dir()
LOG_FILE = "log.md"
# Compatibility roots are non-note layers. Keeping both excluded makes schema
# migration safe even if an old vault temporarily contains both directories.
NON_NOTE_ROOTS = {".obsidian", "04 Sources", "05 Sources", RAW_SOURCES_DIR}
NON_NOTE_FILES = {LOG_FILE}
LOG_HEADER = (
    "# Vault log\n\n"
    "Append-only record of what this vault learned and when. The wiki owns it;\n"
    "every canonical write appends one line. Parseable with plain tools:\n"
    "`grep \"^## \\[\" log.md | tail -5`.\n"
)
LOG_MAX_DETAIL = 300
# A note's status says what kind of warrant it carries. "synthesis" is the vault
# reasoning over notes it already holds: its warrant is the notes it cites, not
# an outside source, which is exactly why it can never be promoted to verified.
STATUSES = {"verified", "source-observed", "user-provided", "synthesis"}
SYNTHESIS_MIN_LINKS = 2
# An index or map note makes no claim of its own; it points at the notes that
# do. Demanding external evidence for one only forces unrelated provenance onto
# the map.
INDEX_CLASSES = {"security-map"}

# --- User-profile domain ("06 Profiles/<username>/") -----------------------
# Profiles are a distinct canonical PARA domain, not "personal" area notes.
# Their physical filenames (01_Identity.md, ...) repeat across every user, so a
# profile note's canonical identity is its globally-unique frontmatter title
# ("Anezatra — Identity"), never its filename stem. See canonical_names().
# The root uses the space-separated, non-colliding PARA numbering ("06 Profiles")
# to match the sibling domains (00 System .. 05 Sources); the earlier
# "05_Profiles" clashed with the "05 Sources" number and broke the convention.
PROFILES_DIR = "06 Profiles"
PROFILE_INDEX_STEM = "00_Profile_Index"
# One centralized profile-section map: section key -> physical filename. A
# candidate names the section; the writer owns the filename. Unknown sections
# are rejected deterministically.
PROFILE_SECTIONS = {
    "index": "00_Profile_Index.md",
    "identity": "01_Identity.md",
    "preferences": "02_Preferences.md",
    "education-career": "03_Education_and_Career.md",
    "interests-projects": "04_Interests_and_Projects.md",
    "people-relationships": "05_People_and_Relationships.md",
    "life-events-timeline": "06_Life_Events_and_Timeline.md",
    "devices-technical": "07_Devices_and_Technical_Environment.md",
    "goals-plans": "08_Goals_and_Plans.md",
    "health-wellbeing": "09_Health_and_Wellbeing.md",
    "locations-travel": "10_Locations_and_Travel.md",
    "financial-logistical": "11_Financial_and_Logistical_Context.md",
    "communication": "12_Communication_and_Interaction_Preferences.md",
    "open-loops": "13_Open_Loops.md",
    "provenance-corrections": "14_Provenance_and_Corrections.md",
}
# SKMR_PROFILE_HARDENING_V1: section key -> canonical title suffix.
PROFILE_SECTION_TITLES = {
    "index": "Profile",
    "identity": "Identity",
    "preferences": "Preferences",
    "education-career": "Education and Career",
    "interests-projects": "Interests and Projects",
    "people-relationships": "People and Relationships",
    "life-events-timeline": "Life Events and Timeline",
    "devices-technical": "Devices and Technical Environment",
    "goals-plans": "Goals and Plans",
    "health-wellbeing": "Health and Wellbeing",
    "locations-travel": "Locations and Travel",
    "financial-logistical": "Financial and Logistical Context",
    "communication": "Communication and Interaction Preferences",
    "open-loops": "Open Loops",
    "provenance-corrections": "Provenance and Corrections",
}
PROFILE_SEMANTIC_CLASS = "user-profile"
# A finite provenance vocabulary. Profile facts are authoritative evidence of
# what the USER stated, which is not the same as externally verified technical
# truth; provenance records which of those a fact actually carries.
PROVENANCE = {"user-direct", "user-confirmed", "peer-relay", "sondra-relay", "external-observed", "imported-history"}
PROFILE_USER_PROVENANCE = {"user-direct", "user-confirmed"}
PROFILE_SOURCE_PROVENANCE = {"external-observed", "peer-relay", "sondra-relay", "imported-history"}
# Statuses a profile note may carry. "user-provided" is the normal case and does
# NOT require external technical evidence; "verified" keeps the vault's strong
# evidence semantics unchanged; "source-observed" records a non-user source.
PROFILE_STATUSES = {"user-provided", "source-observed", "verified"}
# A conservative canonical username: lowercased, alnum plus . _ - , 1-64 chars,
# no leading/trailing separator. Case is folded so Anezatra/anezatra/ANEZATRA
# resolve to one profile directory instead of three.
PROFILE_USERNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?\Z")
LINT_THIN_BODY_CHARS = 400
LINT_TAG_OVERLAP = 2
LINT_ADVICE = (
    "These are candidates for judgement, not defects. Structural validation cannot see "
    "meaning: two notes asserting opposite things pass it cleanly. Read the pairs in "
    "unlinked_topic_siblings and missing_cross_references for contradictions and for "
    "claims a newer source has superseded; dead_evidence and unwarranted_status are the "
    "only entries that fail the vault."
)
# A retrieval result must say what it means. "No hits" and "the vault was never
# searched properly" used to look identical to the caller.
SEARCH_ADVICE = {
    "high": "Every query term was found and a title or tag matched. Treat these notes as the prior knowledge.",
    "partial": "Every query term was found, but only inside note bodies. Read the hits and confirm relevance before relying on them.",
    "weak": "Some query terms are absent from the vault vocabulary (see unmatched_terms). Re-query using the suggestions before concluding nothing was saved.",
    "none": "No note contains any query term. This is a negative result about this wording, not proof the knowledge is absent; retry with the suggested terms if any.",
}
# Retrieval weights: a term in the title identifies a note, a term in the tags
# classifies it, a term in the body only mentions it.
WEIGHT_TITLE, WEIGHT_TAG, WEIGHT_BODY = 5, 3, 1
LINK_DECAY = 0.34
# Evidence must point at something a reader can go re-check. Unfalsifiable
# phrasing ("tested", "works as expected") is what made "verified" self-attested.
EVIDENCE_KINDS = {
    "advisory", "authorized-test", "commit", "command", "file", "hackerone-report",
    "measurement", "spec", "test", "tool-output", "url",
}
CHECKABLE_EVIDENCE_KINDS = {
    "authorized-test", "commit", "command", "file", "hackerone-report",
    "measurement", "test", "tool-output", "url",
}
PLACEHOLDER_LOCATORS = {
    "tested", "works", "it works", "verified", "confirmed", "yes", "done", "ok",
    "n/a", "na", "none", "manual", "manually", "checked", "true", "-", "self",
    "observed", "as expected", "works as expected", "by inspection",
}
# A locator earns trust by naming something addressable: a URL, a path, a test
# id, a quoted token, a number, or a file extension.
EVIDENCE_ANCHOR = re.compile(r"https?://|[/\\]|::|`[^`]+`|\d|\.[A-Za-z0-9]{1,6}\b")
TITLE_FORBIDDEN = re.compile(r"[\\/\x00-\x1f]|\.\.")
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SENSITIVE = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", re.I)),
    ("authorization header", re.compile(r"(?im)^\s*authorization\s*:\s*\S+")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("password", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*[^\s<>{}\[\]]{4,}")),
    ("API key", re.compile(r"(?i)\b(?:api[_ -]?key|secret[_ -]?key|client[_ -]?secret)\s*[:=]\s*[^\s<>{}\[\]]{6,}")),
    ("cookie/session value", re.compile(r"(?im)^\s*(?:cookie|set-cookie|session(?:id|_id)?)\s*:\s*\S+")),
    ("token assignment", re.compile(r"(?i)\b(?:access[_ -]?token|refresh[_ -]?token|session[_ -]?token)\s*[:=]\s*[^\s<>{}\[\]]{8,}")),
    # Vendor credential formats are self-identifying, so they leak even when
    # pasted bare -- with no "key:" or "password:" in front of them. Each prefix
    # is paired with its real length so ordinary prose cannot trip it.
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("provider API key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35,}")),
)


class PolicyDenied(ValueError):
    """A deterministic permanent-memory policy denial.

    Named away from the builtin it used to shadow. While it was called
    MemoryError, the name inside this module resolved to this ValueError
    subclass, so a genuine out-of-memory MemoryError -- which is not a subclass
    of it -- could never be caught by the handlers below and escaped as a bare
    traceback instead of the CLI's structured error.
    """


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: Any) -> None:
    atomic_write(path, json_dump(payload) + "\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def clean_title(value: Any) -> str:
    title = " ".join(str(value or "").split()).strip(" .")
    if not title or len(title) > 120 or TITLE_FORBIDDEN.search(title) or title.casefold() == ".obsidian":
        raise PolicyDenied("invalid canonical note title")
    return title


def canonical_username(value: Any) -> str:
    """Deterministic, path-safe canonical filesystem identity for a profile.

    Fails closed on anything that could escape the profile tree or spawn a
    duplicate profile: empty values, control/null characters, separators,
    traversal, absolute paths, and reserved names. Case is folded so a profile
    is never accidentally forked by capitalisation.
    """
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw:
        raise PolicyDenied("profile_username is empty")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in raw):
        raise PolicyDenied("profile_username contains control characters")
    lowered = raw.casefold()
    if "/" in lowered or "\\" in lowered or ".." in lowered:
        raise PolicyDenied("profile_username contains a path separator or traversal")
    if lowered in {".", "..", ".obsidian"} or lowered.startswith("."):
        raise PolicyDenied(f"unsafe profile_username: {value!r}")
    if not PROFILE_USERNAME_RE.match(lowered):
        raise PolicyDenied(
            "unsafe profile_username: use 1-64 chars of a-z, 0-9, dot, underscore or hyphen, "
            "not starting or ending with a separator"
        )
    return lowered


def profile_display_name(username: str) -> str:
    """Deterministic human-facing profile name derived from canonical username."""
    canonical = canonical_username(username)
    pieces = re.split(r"([._-])", canonical)
    return "".join(piece[:1].upper() + piece[1:] if piece not in {".", "_", "-"} else piece for piece in pieces)


def expected_profile_title(username: str, section: str) -> str:
    if section not in PROFILE_SECTION_TITLES:
        raise PolicyDenied(f"unsupported profile_section: {section!r}")
    return f"{profile_display_name(username)} — {PROFILE_SECTION_TITLES[section]}"


def is_profile_relative(relative: Path) -> bool:
    """True when a vault-relative path lives under the profile domain."""
    return bool(relative.parts) and relative.parts[0] == PROFILES_DIR


def canonical_names(relative: Path, metadata: dict[str, Any]) -> set[str]:
    """The canonical identities a note answers to, resolved in ONE place.

    Non-profile notes keep the historical behaviour (filename stem AND title are
    both aliases, since for them the stem equals the title). Profile notes repeat
    generic stems (01_Identity) across every user, so ONLY the unique frontmatter
    title is a canonical identity for them; the stem is never a global alias.
    """
    title = str(metadata.get("title") or "").strip()
    if is_profile_relative(relative):
        return {title} if title else {relative.stem}
    names = {relative.stem}
    if title:
        names.add(title)
    return names


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def find_sensitive(text: str) -> list[str]:
    return [label for label, pattern in SENSITIVE if pattern.search(text)]


def yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def note_frontmatter(text: str) -> dict[str, Any]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}
    result: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("\"'")
    return result


def frontmatter_list(text: str, key: str) -> list[str]:
    """Read a YAML block list (``tags:`` followed by ``  - value`` lines)."""
    match = FRONTMATTER.match(text)
    if not match:
        return []
    values: list[str] = []
    collecting = False
    for line in match.group(1).splitlines():
        if not line[:1].isspace():
            collecting = line.split(":", 1)[0].strip() == key if ":" in line else False
            continue
        if collecting and line.strip().startswith("- "):
            item = line.strip()[2:].strip().strip("\"'")
            if item:
                values.append(item)
    return values


def frontmatter_locators(text: str) -> list[str]:
    """Read the ``locator:`` values out of the frontmatter ``sources:`` block.

    Provenance decays silently: a note keeps asserting a source long after the
    file it points at has moved. Lint needs the locators to say so.
    """
    match = FRONTMATTER.match(text)
    if not match:
        return []
    found = re.findall(r'(?m)^\s+locator:\s*(.+?)\s*$', match.group(1))
    return [value.strip().strip("\"'") for value in found if value.strip()]


def note_age_days(updated: str) -> int | None:
    try:
        return (date.today() - date.fromisoformat(str(updated).strip())).days
    except ValueError:
        return None


def check_evidence_entry(item: Any) -> tuple[str, str, bool]:
    """Validate one evidence entry and report whether it is machine-checkable.

    A locator is accepted only when it names something addressable, so
    "verified" stops being a claim the writer makes about itself.
    """
    if not isinstance(item, dict):
        raise PolicyDenied("each evidence entry must be an object")
    kind = " ".join(str(item.get("kind") or "").split()).casefold()
    locator = " ".join(str(item.get("locator") or "").split())
    if not kind or not locator:
        raise PolicyDenied("evidence requires kind and locator")
    if kind not in EVIDENCE_KINDS:
        raise PolicyDenied(f"unknown evidence kind '{kind}'; use one of: " + ", ".join(sorted(EVIDENCE_KINDS)))
    if find_sensitive(locator):
        raise PolicyDenied("sensitive evidence locator denied")
    if locator.casefold().strip(" .") in PLACEHOLDER_LOCATORS:
        raise PolicyDenied(f"evidence locator '{locator}' asserts rather than points at anything re-checkable")
    checkable = kind in CHECKABLE_EVIDENCE_KINDS and len(locator) >= 8 and bool(EVIDENCE_ANCHOR.search(locator))
    if kind == "file":
        # Vault paths contain spaces ("03 Resources/..."), so splitting on
        # whitespace truncated every one of them to its first word and made the
        # whole vault unciteable. Prefer the locator as written, and fall back to
        # its first token so "path:line" or "path (note)" still resolves.
        candidates = [Path(locator), Path(locator.split()[0]), Path(locator.split(":")[0])]
        absolute = [item for item in candidates if item.is_absolute()]
        if absolute and not any(item.exists() for item in absolute):
            raise PolicyDenied(f"file evidence does not exist: {locator}")
    return kind, locator, checkable


def semantic_body(text: str) -> str:
    text = FRONTMATTER.sub("", text, count=1)
    text = re.sub(r"(?ms)^## Related\s*\n.*\Z", "", text)
    text = re.sub(r"(?m)^#\s+.*$", "", text, count=1)
    return text.strip().replace('\r\n', '\n')


def comparable_note(text: str) -> str:
    # Only the generated date is volatile. Operators, links and evidence matter.
    return re.sub(r'(?m)^updated:.*$', 'updated:', text).strip()


def check_sensitive_values(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            check_sensitive_values(key)
            check_sensitive_values(item)
    elif isinstance(value, list):
        for item in value:
            check_sensitive_values(item)
    elif isinstance(value, str) and find_sensitive(value):
        raise PolicyDenied('sensitive candidate field denied')


class VaultStore:
    def __init__(self, vault: Path = VAULT, state_dir: Path = STATE_DIR):
        self.vault = vault
        self.state_dir = state_dir
        self.lock_file = state_dir / "skmr-obsidian.lock"
        self.previews = state_dir / "skmr-obsidian-previews"
        self.transactions = state_dir / "skmr-obsidian-transactions"
        self.activity = state_dir / "skmr-activity.jsonl"

    def validate_root(self, *, writable: bool = False) -> Path:
        try:
            check_root(self.vault, writable)
        except PermissionError as error:
            raise PolicyDenied(str(error)) from error
        if not self.vault.is_dir() or self.vault.is_symlink():
            raise PolicyDenied(f"vault is missing or is a symlink: {self.vault}")
        root = self.vault.resolve(strict=True)
        if writable and not os.access(root, os.W_OK):
            raise PolicyDenied(f"vault is not writable: {root}")
        return root

    def all_notes(self) -> list[Path]:
        root = self.validate_root()
        result = []
        for path in root.rglob("*.md"):
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] in NON_NOTE_ROOTS:
                continue
            if len(relative.parts) == 1 and relative.name in NON_NOTE_FILES:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            result.append(path)
        return sorted(result)

    def canonical_index(self) -> dict[str, list[Path]]:
        root = self.validate_root()
        index: dict[str, list[Path]] = {}
        for path in self.all_notes():
            try:
                metadata = note_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            for name in canonical_names(path.relative_to(root), metadata):
                if not name:
                    continue
                bucket = index.setdefault(normalized(name), [])
                if path not in bucket:
                    bucket.append(path)
        return index

    def route(self, candidate: dict[str, Any]) -> tuple[Path, str, str]:
        semantic_class = str(candidate.get("semantic_class") or "")
        if semantic_class == PROFILE_SEMANTIC_CLASS:
            return self.route_profile(candidate)
        if semantic_class not in ROUTES:
            raise PolicyDenied(f"unsupported semantic_class: {semantic_class!r}")
        relative, note_type, area = ROUTES[semantic_class]
        if semantic_class in {"bug-hunting-project", "research-project", "other-project"}:
            project = clean_title(candidate.get("project"))
            relative = f"{relative}/{project}"
        title = clean_title(candidate.get("title"))
        return Path(relative) / f"{title}.md", note_type, area

    def route_profile(self, candidate: dict[str, Any]) -> tuple[Path, str, str]:
        """Route a user-profile candidate to "06 Profiles/<username>/<section>".

        The physical filename is owned by the writer (from the section map); the
        candidate's title is the note's canonical identity, not the filename.
        """
        username = canonical_username(candidate.get("profile_username"))
        section = str(candidate.get("profile_section") or "")
        if section not in PROFILE_SECTIONS:
            raise PolicyDenied(
                f"unsupported profile_section: {section!r}; use one of: "
                + ", ".join(sorted(PROFILE_SECTIONS))
            )
        # Keep username + title deterministic so repeating physical stems can
        # never become ambiguous canonical identities.
        candidate["profile_username"] = username
        title = clean_title(candidate.get("title"))
        expected = expected_profile_title(username, section)
        if title != expected:
            raise PolicyDenied(f"profile title must be exactly {expected!r}")
        candidate["title"] = expected
        relative = Path(PROFILES_DIR) / username / PROFILE_SECTIONS[section]
        return relative, PROFILE_SEMANTIC_CLASS, "profile"

    def safe_target(self, relative: Path) -> Path:
        root = self.validate_root(writable=True)
        if relative.is_absolute() or ".." in relative.parts or ".obsidian" in relative.parts:
            raise PolicyDenied("destination escapes the vault or targets .obsidian")
        # The canonical vault is served over SMB from a Windows host, where a
        # backslash separates path components; POSIX treats it as an ordinary
        # filename character. A component holding one would therefore name one
        # location here and another there. No canonical name may contain one
        # (TITLE_FORBIDDEN), so refusing it costs nothing and keeps both views
        # of the vault in agreement.
        if any("\\" in part for part in relative.parts):
            raise PolicyDenied("destination component contains a backslash path separator")
        target = root / relative
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise PolicyDenied(f"symlink path component denied: {current}")
        resolved_parent = target.parent.resolve(strict=False)
        if not is_within(resolved_parent, root):
            raise PolicyDenied("destination resolves outside the vault")
        if target.exists() and target.is_symlink():
            raise PolicyDenied("symlink note target denied")
        return target

    def existing_for_title(self, title: str) -> Path | None:
        matches = self.canonical_index().get(normalized(title), [])
        if len(matches) > 1:
            raise PolicyDenied(f"duplicate canonical notes already exist for {title!r}: {matches}")
        return matches[0] if matches else None

    def validate_candidate(self, candidate: dict[str, Any]) -> tuple[Path, str, str]:
        if not isinstance(candidate, dict):
            raise PolicyDenied("candidate must be a JSON object")
        check_sensitive_values(candidate)
        relative, note_type, area = self.route(candidate)
        title = clean_title(candidate.get("title"))
        content = str(candidate.get("content") or "").strip()
        if not content or len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise PolicyDenied("content is empty or exceeds 64 KiB")
        sensitive = find_sensitive(content)
        if sensitive:
            raise PolicyDenied("sensitive material denied: " + ", ".join(sensitive))
        if str(candidate.get("semantic_class") or "") == PROFILE_SEMANTIC_CLASS:
            self.validate_profile_candidate(candidate, title)
            return relative, note_type, area
        links = candidate.get("links")
        if not isinstance(links, list) or not links:
            raise PolicyDenied("at least one meaningful wikilink is required")
        clean_links = []
        for value in links:
            link = clean_title(value)
            if link.endswith(".md"):
                raise PolicyDenied("wikilink targets use canonical names, not .md filenames")
            if normalized(link) != normalized(title) and link not in clean_links:
                clean_links.append(link)
        if not clean_links:
            raise PolicyDenied("self-links do not satisfy the wikilink requirement")
        candidate["links"] = clean_links

        explicit = candidate.get("explicit_user_request") is True
        automatic = candidate.get("automatic") is True
        if explicit == automatic:
            raise PolicyDenied("exactly one of automatic or explicit_user_request must be true")
        evidence = candidate.get("evidence")
        status = str(candidate.get("status") or ("verified" if automatic else "user-provided"))
        if status not in STATUSES:
            raise PolicyDenied(f"unknown status {status!r}; use one of: " + ", ".join(sorted(STATUSES)))
        index_note = str(candidate.get("semantic_class") or "") in INDEX_CLASSES
        synthesis = status == "synthesis"
        if synthesis:
            # Synthesis earns its place by connecting notes, so it is held to the
            # citation it actually rests on rather than to outside evidence.
            if candidate.get("verified") is True:
                raise PolicyDenied("synthesis rests on cited vault notes, not outside evidence; it cannot claim verified")
            if len(clean_links) < SYNTHESIS_MIN_LINKS:
                raise PolicyDenied(f"synthesis must cite at least {SYNTHESIS_MIN_LINKS} existing canonical notes")
        if automatic:
            required = ("novel", "reusable") if synthesis else ("novel", "reusable", "verified")
            failed = [name for name in required if candidate.get(name) is not True]
            if failed:
                raise PolicyDenied("automatic save-worthiness gate failed: " + ", ".join(failed))
            if not (synthesis or index_note) and (not isinstance(evidence, list) or not evidence):
                raise PolicyDenied("automatic verified memory requires evidence")
        if status == "verified" and not (candidate.get("verified") is True and isinstance(evidence, list) and evidence):
            raise PolicyDenied("verified status requires verification and evidence")
        if isinstance(evidence, list) and evidence:
            checkable = [check_evidence_entry(item)[2] for item in evidence]
            # "verified" must rest on at least one re-checkable claim; narrative
            # evidence may accompany it but can never carry the status alone.
            if status == "verified" and not any(checkable):
                raise PolicyDenied(
                    "verified status requires at least one machine-checkable evidence entry "
                    "(kind in: " + ", ".join(sorted(CHECKABLE_EVIDENCE_KINDS)) +
                    ") whose locator names a path, URL, identifier or measurement"
                )
        candidate["status"] = status
        return relative, note_type, area

    def validate_profile_candidate(self, candidate: dict[str, Any], title: str) -> None:
        """Profile-specific gate. Distinct from the technical gate.

        A directly user-stated fact is authoritative evidence of what the USER
        said; it is not automatically externally-verified technical truth. So
        automatic profile saves require novel + reusable + valid provenance, and
        `status: verified` still demands the vault's full evidence semantics.
        """
        section = str(candidate.get("profile_section") or "")
        is_index = section == "index"
        username = canonical_username(candidate.get("profile_username"))
        expected_title = expected_profile_title(username, section)
        if title != expected_title:
            raise PolicyDenied(f"profile title must be exactly {expected_title!r}")

        # Links: section notes must link back to the profile index (a real
        # relationship). ONLY the initial Profile Index may bootstrap with no
        # outgoing wikilink, because no truthful canonical relation exists yet.
        links = candidate.get("links")
        clean_links: list[str] = []
        if isinstance(links, list):
            for value in links:
                link = clean_title(value)
                if link.endswith(".md"):
                    raise PolicyDenied("wikilink targets use canonical names, not .md filenames")
                if normalized(link) != normalized(title) and link not in clean_links:
                    clean_links.append(link)
        expected_index = expected_profile_title(username, "index")
        if not is_index:
            if normalized(expected_index) not in {normalized(link) for link in clean_links}:
                raise PolicyDenied(f"profile section must link to canonical Profile Index: [[{expected_index}]]")
        else:
            # Linkless index is a bootstrap exception only while no section note exists.
            profile_records = [
                record for record in self.note_records()
                if record.get("profile") == username and record.get("profile_section") != "index"
            ]
            if profile_records and not clean_links:
                raise PolicyDenied("Profile Index may be linkless only before the first profile section exists")
        candidate["links"] = clean_links

        provenance = str(candidate.get("provenance") or "")
        if provenance not in PROVENANCE:
            raise PolicyDenied(
                f"profile candidate requires a valid provenance; use one of: "
                + ", ".join(sorted(PROVENANCE))
            )

        status = str(candidate.get("status") or "user-provided")
        if status not in PROFILE_STATUSES:
            raise PolicyDenied(
                f"unknown profile status {status!r}; use one of: " + ", ".join(sorted(PROFILE_STATUSES))
            )
        if status == "user-provided" and provenance not in PROFILE_USER_PROVENANCE:
            raise PolicyDenied(
                "status 'user-provided' requires provenance 'user-direct' or 'user-confirmed'"
            )
        if status == "source-observed" and provenance not in PROFILE_SOURCE_PROVENANCE:
            raise PolicyDenied(
                "status 'source-observed' requires a non-user provenance "
                "(external-observed, peer-relay, sondra-relay, or imported-history)"
            )

        explicit = candidate.get("explicit_user_request") is True
        automatic = candidate.get("automatic") is True
        if explicit == automatic:
            raise PolicyDenied("exactly one of automatic or explicit_user_request must be true")

        evidence = candidate.get("evidence")
        if status == "verified":
            # The strong meaning of "verified" is preserved exactly: it never
            # rides on a user statement alone. Applies to explicit saves too.
            if not (candidate.get("verified") is True and isinstance(evidence, list) and evidence):
                raise PolicyDenied("verified status requires verification and evidence")
            if not any(check_evidence_entry(item)[2] for item in evidence):
                raise PolicyDenied(
                    "verified status requires at least one machine-checkable evidence entry "
                    "(kind in: " + ", ".join(sorted(CHECKABLE_EVIDENCE_KINDS)) +
                    ") whose locator names a path, URL, identifier or measurement"
                )
        elif isinstance(evidence, list) and evidence:
            # Optional supporting provenance for a user-provided/source-observed
            # note is allowed, but is validated and can never confer "verified".
            for item in evidence:
                check_evidence_entry(item)

        # Automatic (proactive) preservation still needs save-worthiness, but NOT
        # external technical verification. An explicit user request bypasses only
        # the proactive novel/reusable question, never the safety checks above.
        if automatic and status != "verified":
            failed = [name for name in ("novel", "reusable") if candidate.get(name) is not True]
            if failed:
                raise PolicyDenied("profile automatic save-worthiness gate failed: " + ", ".join(failed))

        candidate["status"] = status
        candidate["provenance"] = provenance

    def render(self, candidate: dict[str, Any], note_type: str, area: str) -> str:
        title = clean_title(candidate.get("title"))
        tags = candidate.get("tags") if isinstance(candidate.get("tags"), list) else []
        tags = list(dict.fromkeys(normalized(str(tag)).replace(" ", "-") for tag in tags if normalized(str(tag))))
        evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
        is_profile = note_type == PROFILE_SEMANTIC_CLASS
        lines = [
            "---",
            f"title: {yaml_scalar(title)}",
            f"type: {yaml_scalar(note_type)}",
            f"area: {yaml_scalar(area)}",
        ]
        if is_profile:
            lines.append(f"profile: {yaml_scalar(canonical_username(candidate.get('profile_username')))}")
            lines.append(f"profile_section: {yaml_scalar(str(candidate.get('profile_section')))}")
        lines.append(f"status: {yaml_scalar(str(candidate['status']))}")
        if is_profile:
            lines.append(f"provenance: {yaml_scalar(str(candidate.get('provenance')))}")
        lines.append(f"updated: {date.today().isoformat()}")
        if tags:
            lines.append("tags:")
            lines.extend(f"  - {yaml_scalar(tag)}" for tag in tags)
        if evidence:
            lines.append("sources:")
            for item in evidence:
                kind, locator, _ = check_evidence_entry(item)
                lines.append(f"  - kind: {yaml_scalar(kind)}")
                lines.append(f"    locator: {yaml_scalar(locator)}")
        lines.extend(["---", "", f"# {title}", "", str(candidate["content"]).strip(), "", "## Related", ""])
        lines.extend(f"- [[{link}]]" for link in candidate["links"])
        return "\n".join(lines).rstrip() + "\n"

    def prepare(self, candidate: dict[str, Any]) -> dict[str, Any]:
        relative, note_type, area = self.validate_candidate(candidate)
        preferred = self.safe_target(relative)
        existing = self.existing_for_title(clean_title(candidate.get("title")))
        profile_base = existing or (preferred if note_type == PROFILE_SEMANTIC_CLASS and preferred.exists() else None)
        if note_type == PROFILE_SEMANTIC_CLASS and profile_base:
            # An update candidate replaces the whole rendered note. Prove the
            # caller actually read the exact base revision before accepting it.
            # The physical target is also protected even if an old malformed
            # note has the wrong/missing canonical title and is absent from the index.
            if candidate.get("profile_full_merge") is not True:
                raise PolicyDenied("profile update requires profile_full_merge=true after read-merge of the complete section")
            base = str(candidate.get("base_sha256") or "").casefold()
            actual_base = sha256_file(profile_base)
            if not re.fullmatch(r"[0-9a-f]{64}", base) or base != actual_base:
                raise PolicyDenied("profile update base_sha256 does not match the current canonical section; re-read and merge")
        moving = bool(existing and existing != preferred)
        if moving and candidate.get('move_existing') is not True:
            raise PolicyDenied('PARA destination conflicts with existing canonical note; explicit migration is required: ' + str(existing))
        if moving and preferred.exists():
            raise PolicyDenied('migration destination already exists')
        target = preferred if moving else (existing or preferred)
        root = self.validate_root()
        if not is_within(target.resolve(strict=False), root):
            raise PolicyDenied("canonical note resolves outside the vault")
        rendered = self.render(candidate, note_type, area)
        if find_sensitive(rendered):
            raise PolicyDenied('sensitive rendered note denied')
        index = self.canonical_index()
        missing = [name for name in WIKILINK.findall(rendered)
                   if normalized(name) != normalized(candidate['title']) and not index.get(normalized(name))]
        if missing and index:
            raise PolicyDenied('wikilink targets do not exist: ' + ', '.join(sorted(set(missing))))
        if missing:
            # Bootstrap. A vault holding no canonical notes has nothing a link
            # can point at, so this rule made the FIRST note impossible and a
            # freshly installed vault could never receive anything at all. This
            # is the same narrow exemption the Profile Index already carries,
            # and it applies only while the vault is empty: the second note is
            # checked normally, so the links written here must be made real by
            # the notes that follow.
            self.log_activity('bootstrap-note',
                              'first note in an empty vault; unresolved links: '
                              + ', '.join(sorted(set(missing))))
        if not existing and str(candidate.get("semantic_class") or "") != PROFILE_SEMANTIC_CLASS:
            # Profile notes are namespaced by user and section and identified by a
            # unique title, so identical rendered bodies across two profiles (a
            # templated index, say) are not duplicates and must not be rejected.
            for note in self.all_notes():
                if semantic_body(note.read_text(encoding='utf-8')) == semantic_body(rendered):
                    raise PolicyDenied('duplicate content; update canonical note: ' + str(note))
        source = existing if moving else target
        old = source.read_text(encoding="utf-8") if source.exists() else ""
        operation = 'move' if moving else ("unchanged" if old and comparable_note(rendered) == comparable_note(old) else ("update" if old else "create"))
        return {
            "candidate": candidate,
            "target": str(target),
            "source": str(source),
            "relative": str(target.relative_to(root)),
            "operation": operation,
            "before_sha256": sha256_bytes(old.encode("utf-8")) if old else None,
            "after_sha256": sha256_bytes(rendered.encode("utf-8")),
            "rendered": rendered,
            "old": old,
        }

    def preview(self, candidate: dict[str, Any]) -> dict[str, Any]:
        prepared = self.prepare(candidate)
        keys = ("target", "relative", "operation", "before_sha256", "after_sha256")
        if prepared["operation"] == "unchanged":
            return {key: prepared[key] for key in keys}
        self.previews.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(12)
        payload = {
            "token": token,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "candidate": prepared["candidate"],
            "target": prepared["target"],
            "source": prepared['source'],
            "before_sha256": prepared["before_sha256"],
            "after_sha256": prepared["after_sha256"],
        }
        atomic_json(self.previews / f"{token}.json", payload)
        diff = "".join(
            difflib.unified_diff(
                prepared["old"].splitlines(keepends=True),
                prepared["rendered"].splitlines(keepends=True),
                fromfile=prepared["relative"] + ":before",
                tofile=prepared["relative"] + ":after",
            )
        )
        result = {key: prepared[key] for key in keys}
        result.update({"token": token, "diff": diff})
        return result

    def commit(self, token: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{24}", token):
            raise PolicyDenied("invalid preview token")
        preview_path = self.previews / f"{token}.json"
        if preview_path.is_symlink() or not preview_path.is_file():
            raise PolicyDenied("preview not found")
        payload = json.loads(preview_path.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - created > PREVIEW_TTL:
            raise PolicyDenied("preview expired")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock_file.touch(exist_ok=True)
        with self.lock_file.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            prepared = self.prepare(dict(payload["candidate"]))
            if prepared['source'] != payload.get('source', payload['target']) or prepared["target"] != payload["target"] or prepared["before_sha256"] != payload["before_sha256"]:
                raise PolicyDenied("concurrent change detected; create a new preview")
            if prepared["after_sha256"] != payload["after_sha256"]:
                raise PolicyDenied("preview content changed during validation")
            if prepared["operation"] == "unchanged":
                preview_path.unlink(missing_ok=True)
                return {"operation": "unchanged", "target": prepared["target"], "sha256": prepared["after_sha256"]}
            target = Path(prepared["target"])
            self.transactions.mkdir(parents=True, exist_ok=True)
            transaction = self.transactions / f"{token}.json"
            atomic_json(
                transaction,
                {
                    "token": token,
                    "target": str(target),
                    "source": prepared['source'],
                    "before_sha256": prepared["before_sha256"],
                    "after_sha256": prepared["after_sha256"],
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            atomic_write(target, prepared["rendered"], mode=0o644)
            actual = sha256_file(target)
            if actual != prepared["after_sha256"]:
                raise PolicyDenied("post-write hash mismatch; transaction retained")
            if prepared['operation'] == 'move':
                source = Path(prepared['source'])
                if sha256_file(source) != prepared['before_sha256']:
                    raise PolicyDenied('migration source changed; transaction retained')
                source.unlink()
            candidate = prepared["candidate"]
            logged = self.append_log(
                prepared["operation"],
                f"{clean_title(candidate.get('title'))} | {candidate['status']}",
            )
            self.log_activity("obsidian-write", prepared["relative"], token)
            transaction.unlink(missing_ok=True)
            preview_path.unlink(missing_ok=True)
            return {"operation": prepared["operation"], "target": str(target), "sha256": actual, "logged": logged}

    def append_log(self, action: str, detail: str) -> str:
        """Append one line to the vault's human-readable chronological log.

        ``log_activity`` already journals for the machine; this is the timeline a
        person reads to see how their own knowledge accumulated. Kept to one
        prefixed line per event so grep and tail remain sufficient.
        """
        root = self.validate_root(writable=True)
        path = root / LOG_FILE
        if path.is_symlink():
            raise PolicyDenied("vault log is a symlink")
        entry = " ".join(f"{action} | {detail}".split())[:LOG_MAX_DETAIL]
        existing = path.read_text(encoding="utf-8") if path.is_file() else LOG_HEADER
        line = f"## [{date.today().isoformat()}] {entry}"
        atomic_write(path, existing.rstrip("\n") + "\n\n" + line + "\n", mode=0o644)
        return line

    def init_layers(self) -> dict[str, Any]:
        """Create the raw-source directory and the log if they are absent."""
        root = self.validate_root(writable=True)
        raw = root / RAW_SOURCES_DIR
        created = []
        if not raw.exists():
            raw.mkdir(parents=True)
            created.append(RAW_SOURCES_DIR)
        elif raw.is_symlink() or not raw.is_dir():
            raise PolicyDenied(f"raw source path is not a directory: {raw}")
        readme = raw / "README.md"
        if not readme.exists():
            atomic_write(
                readme,
                "# Raw sources\n\nImmutable source documents the wiki is compiled from.\n"
                "The writer reads these and never edits them, and they are excluded from\n"
                "canonical note validation, indexing, and wikilink resolution.\n"
                "Cite one from a note as `kind: file` evidence.\n",
                mode=0o644,
            )
            created.append(f"{RAW_SOURCES_DIR}/README.md")
        log = root / LOG_FILE
        if not log.exists():
            atomic_write(log, LOG_HEADER, mode=0o644)
            created.append(LOG_FILE)
        return {"ok": True, "created": created, "raw_sources": str(raw), "log": str(log)}

    def log_activity(self, action: str, detail: str, session_id: str = "") -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "detail": detail[:500],
            "session": hashlib.sha256(session_id.encode()).hexdigest()[:16] if session_id else None,
        }
        with self.activity.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def note_records(self) -> list[dict[str, Any]]:
        """Read every note once and derive everything retrieval needs from it."""
        root = self.validate_root()
        records = []
        for path in self.all_notes():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            metadata = note_frontmatter(text)
            relative_path = path.relative_to(root)
            profile = is_profile_relative(relative_path)
            title = str(metadata.get("title") or path.stem)
            tags = frontmatter_list(text, "tags")
            body = semantic_body(text)
            updated = str(metadata.get("updated") or "")
            age = note_age_days(updated)
            # Profile notes are identified by their unique title, never by their
            # repeating physical stem (01_Identity), so the stem is excluded from
            # their canonical keys and search identity.
            keys = {normalized(name) for name in canonical_names(relative_path, {"title": title})} - {""}
            id_text = title if profile else (path.stem + " " + title)
            records.append({
                "path": path,
                "relative": str(relative_path),
                "profile": relative_path.parts[1] if profile and len(relative_path.parts) > 1 else "",
                "profile_section": str(metadata.get("profile_section") or "") if profile else "",
                "title": title,
                "type": str(metadata.get("type") or ""),
                "area": str(metadata.get("area") or ""),
                "status": str(metadata.get("status") or ""),
                "updated": updated,
                "age_days": age,
                "stale": age is not None and age > STALE_AFTER_DAYS,
                "tags": tags,
                "locators": frontmatter_locators(text),
                "links": sorted({normalized(name) for name in WIKILINK.findall(text)}),
                "keys": keys,
                "title_key": normalized(id_text),
                "tag_key": normalized(" ".join(tags)),
                "haystack": normalized(id_text + " " + body[:16000]),
                "body": body,
            })
        return records

    def snippet(self, record: dict[str, Any], terms: set[str]) -> str:
        for line in record["body"].splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            haystack = normalized(stripped)
            if any(term in haystack for term in terms):
                return stripped[:220]
        return ""

    def vocabulary(self, records: list[dict[str, Any]]) -> set[str]:
        words: set[str] = set()
        for record in records:
            words.update(record["title_key"].split())
            words.update(record["tag_key"].split())
        return {word for word in words if len(word) > 2}

    def search(self, query: str, limit: int = 10, profile: str | None = None, profile_section: str | None = None) -> dict[str, Any]:
        """Rank notes and report how much of the query was actually understood.

        The result always states which terms matched and which did not, so an
        empty or thin result is a reportable outcome rather than an ambiguous
        silence that reads the same as "nothing was ever saved".

        When ``profile`` is given the same ranking runs against only that
        profile's notes; unscoped search keeps its whole-vault behaviour.
        """
        terms = set(normalized(query).split())
        if not terms:
            raise PolicyDenied("search query is empty")
        limit = max(1, min(limit, 25))
        records = self.note_records()
        scope = None
        if profile is not None:
            scope = canonical_username(profile)
            prefix = f"{PROFILES_DIR}/{scope}/"
            records = [r for r in records if r["relative"].replace(os.sep, "/").startswith(prefix)]
        if profile_section is not None:
            if scope is None:
                raise PolicyDenied("--profile-section requires --profile")
            if profile_section not in PROFILE_SECTIONS:
                raise PolicyDenied("unsupported profile_section: " + repr(profile_section))
            records = [r for r in records if r.get("profile_section") == profile_section]

        direct: dict[str, dict[str, Any]] = {}
        for record in records:
            hits: dict[str, int] = {}
            for term in terms:
                if term in record["title_key"]:
                    hits[term] = WEIGHT_TITLE
                elif term in record["tag_key"]:
                    hits[term] = WEIGHT_TAG
                elif term in record["haystack"]:
                    hits[term] = WEIGHT_BODY
            if hits:
                direct[record["relative"]] = {
                    "record": record, "hits": hits, "score": float(sum(hits.values())),
                    # The strongest field any single term reached: a title or tag
                    # hit identifies a note, several body hits only suggest it.
                    "best_field": max(hits.values()),
                }

        # Wikilinks are the vault's own structure: a note one hop from a strong
        # hit is often the answer when the query used different vocabulary.
        matched_terms = sorted({term for entry in direct.values() for term in entry["hits"]})
        coverage = len(matched_terms) / len(terms)
        by_key = {key: record for record in records for key in record["keys"]}
        expanded: dict[str, dict[str, Any]] = {}
        # Only a title- or tag-strength hit is a trustworthy anchor; expanding
        # from an incidental body mention drags in unrelated neighbours.
        strong = [
            entry for entry in direct.values()
            if entry["best_field"] >= WEIGHT_TAG or len(entry["hits"]) >= 2
        ]
        anchors = sorted(strong, key=lambda entry: -entry["score"])[:3]
        for anchor in anchors:
            neighbours = [by_key[key] for key in anchor["record"]["links"] if key in by_key]
            neighbours += [r for r in records if anchor["record"]["keys"] & set(r["links"])]
            for neighbour in neighbours:
                relative = neighbour["relative"]
                if relative in direct or relative == anchor["record"]["relative"]:
                    continue
                score = round(anchor["score"] * LINK_DECAY, 2)
                if score > expanded.get(relative, {}).get("score", 0):
                    expanded[relative] = {
                        "record": neighbour, "hits": {}, "score": score,
                        "via": anchor["record"]["title"],
                    }

        ordered = sorted(
            list(direct.values()) + list(expanded.values()),
            key=lambda entry: (-entry["score"], entry["record"]["relative"].casefold()),
        )
        matches = []
        for entry in ordered[:limit]:
            record = entry["record"]
            matches.append({
                "score": entry["score"],
                "title": record["title"],
                "path": record["relative"],
                "type": record["type"],
                "area": record["area"],
                "status": record["status"],
                "updated": record["updated"],
                "age_days": record["age_days"],
                "stale": record["stale"],
                "matched_terms": sorted(entry["hits"]),
                "via": entry.get("via", "direct"),
                "snippet": self.snippet(record, set(entry["hits"]) or terms),
            })

        unmatched = sorted(terms - set(matched_terms))
        suggestions: dict[str, list[str]] = {}
        if unmatched:
            words = self.vocabulary(records)
            for term in unmatched:
                near = difflib.get_close_matches(term, words, n=3, cutoff=0.72)
                if near:
                    suggestions[term] = near
        # Two independent axes: was the whole query understood, and did anything
        # match strongly enough to identify a note rather than merely mention it.
        if not matches:
            confidence = "none"
        elif unmatched:
            confidence = "weak"
        elif any(entry["best_field"] >= WEIGHT_TAG for entry in direct.values()):
            confidence = "high"
        else:
            confidence = "partial"

        self.log_activity(
            "obsidian-search",
            f"{query}{(' [profile:' + scope + ']') if scope else ''} -> {confidence} ({len(matches)} hits)",
        )
        return {
            "query": query,
            "profile": scope,
            "profile_section": profile_section,
            "terms": sorted(terms),
            "matched_terms": matched_terms,
            "unmatched_terms": unmatched,
            "coverage": round(coverage, 3),
            "confidence": confidence,
            "notes_scanned": len(records),
            "matches": matches,
            "suggestions": suggestions,
            "advice": SEARCH_ADVICE[confidence],
        }

    def validate_note(self, path: Path) -> list[str]:
        root = self.validate_root()
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            return [str(error)]
        if path.is_symlink() or not is_within(resolved, root) or ".obsidian" in resolved.relative_to(root).parts:
            return ["note is outside the vault, inside .obsidian, or a symlink"]
        relative = resolved.relative_to(root)
        text = path.read_text(encoding="utf-8")
        metadata = note_frontmatter(text)
        errors = [f"missing frontmatter field: {key}" for key in ("title", "type", "area", "status", "updated") if not metadata.get(key)]
        if find_sensitive(text):
            errors.append("sensitive material detected")

        profile = is_profile_relative(relative)
        index_bootstrap = False
        if profile:
            if len(relative.parts) != 3:
                errors.append(f"profile note path must be {PROFILES_DIR}/<username>/<section-file>")
            else:
                try:
                    username = canonical_username(relative.parts[1])
                except PolicyDenied as error:
                    errors.append(str(error))
                    username = relative.parts[1]
                section = str(metadata.get("profile_section") or "")
                if metadata.get("type") != PROFILE_SEMANTIC_CLASS:
                    errors.append("profile note type must be user-profile")
                if metadata.get("area") != "profile":
                    errors.append("profile note area must be profile")
                if str(metadata.get("profile") or "") != username:
                    errors.append(f"profile frontmatter must equal canonical username {username!r}")
                if section not in PROFILE_SECTIONS:
                    errors.append("profile_section is missing or unsupported")
                else:
                    if relative.name != PROFILE_SECTIONS[section]:
                        errors.append(f"profile_section {section!r} must use filename {PROFILE_SECTIONS[section]!r}")
                    expected_title = expected_profile_title(username, section)
                    if str(metadata.get("title") or "") != expected_title:
                        errors.append(f"profile title must be exactly {expected_title!r}")
                    provenance = str(metadata.get("provenance") or "")
                    status = str(metadata.get("status") or "")
                    if provenance not in PROVENANCE:
                        errors.append("profile provenance is missing or unsupported")
                    if status not in PROFILE_STATUSES:
                        errors.append("profile status is unsupported")
                    if status == "user-provided" and provenance not in PROFILE_USER_PROVENANCE:
                        errors.append("user-provided profile status requires user-direct or user-confirmed provenance")
                    if status == "source-observed" and provenance not in PROFILE_SOURCE_PROVENANCE:
                        errors.append("source-observed profile status requires external-observed, peer-relay, sondra-relay, or imported-history provenance")
                    if status == "verified" and not frontmatter_locators(text):
                        errors.append("verified profile status requires evidence locators")

                    links = {normalized(name) for name in WIKILINK.findall(text)}
                    if section == "index":
                        other_sections = [
                            item for item in self.all_notes()
                            if item != path
                            and is_profile_relative(item.relative_to(root))
                            and len(item.relative_to(root).parts) == 3
                            and item.relative_to(root).parts[1] == username
                            and item.stem != PROFILE_INDEX_STEM
                        ]
                        index_bootstrap = not other_sections
                        if other_sections and not links:
                            errors.append("Profile Index may be linkless only before the first profile section exists")
                    else:
                        expected_index = expected_profile_title(username, "index")
                        if normalized(expected_index) not in links:
                            errors.append(f"profile section must link to canonical Profile Index: [[{expected_index}]]")

        if not WIKILINK.search(text) and not index_bootstrap:
            errors.append("no wikilink")
        return list(dict.fromkeys(errors))

    def validate_vault(self) -> dict[str, Any]:
        root = self.validate_root()
        notes = self.all_notes()
        index = self.canonical_index()
        duplicates = {key: [str(p.relative_to(root)) for p in paths] for key, paths in index.items() if len(paths) > 1}
        errors = {}
        for path in notes:
            note_errors = self.validate_note(path)
            if note_errors and path.name != "Hoş geldiniz.md":
                errors[str(path.relative_to(root))] = note_errors
        # Resolve links against canonical identities (title for profile notes,
        # stem-or-title for the rest), not bare filename stems — otherwise every
        # profile link like [[Anezatra — Identity]] reads as broken because the
        # file is 01_Identity.md, and two users' 01_Identity.md do not collide.
        resolvable = set(index)
        broken = {}
        for path in notes:
            if path.name == "Hoş geldiniz.md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            missing = sorted({name for name in WIKILINK.findall(text) if normalized(name) not in resolvable})
            if missing:
                broken[str(path.relative_to(root))] = missing
        # Staleness is review pressure, not corruption: it is reported, never
        # auto-deleted, and it does not fail validation on its own.
        stale = []
        for record in self.note_records():
            if record["stale"]:
                stale.append({
                    "path": record["relative"], "updated": record["updated"],
                    "age_days": record["age_days"], "status": record["status"],
                })
        stale.sort(key=lambda item: -(item["age_days"] or 0))
        return {
            "ok": not duplicates and not errors and not broken,
            "notes": len(notes),
            "duplicates": duplicates,
            "errors": errors,
            "broken_links": broken,
            "stale_after_days": STALE_AFTER_DAYS,
            "stale": stale,
        }


    def lint_vault(self) -> dict[str, Any]:
        """Report semantic health, which structural validation cannot see.

        ``validate`` answers "is this vault well-formed". Lint asks the question
        a maintainer actually cares about: what has nothing pointing at it, what
        quietly lost the source it cites, what shares a subject with a note it
        never links, and what is too thin to earn its page.
        """
        records = self.note_records()
        by_key = {key: record for record in records for key in record["keys"]}
        inbound: dict[str, list[str]] = {record["relative"]: [] for record in records}
        for record in records:
            for link in record["links"]:
                target = by_key.get(link)
                if target and target["relative"] != record["relative"]:
                    inbound[target["relative"]].append(record["title"])

        orphans, thin, dead_evidence, unwarranted, missing_xref = [], [], [], [], []
        for record in records:
            # A map exists to point outward; nothing pointing at it is normal.
            if record["type"] != "map" and not inbound[record["relative"]]:
                orphans.append({"path": record["relative"], "title": record["title"]})
            # Maps and system pages are meant to be short signposts; only a
            # knowledge-bearing note is suspicious for being thin.
            if (len(record["body"]) < LINT_THIN_BODY_CHARS
                    and record["type"] != "map" and record["area"] != "system"):
                thin.append({"path": record["relative"], "chars": len(record["body"])})
            missing = [
                locator for locator in record["locators"]
                if Path(locator).is_absolute() and not Path(locator).exists()
            ]
            if missing:
                dead_evidence.append({"path": record["relative"], "locators": missing})
            if record["status"] == "verified" and not record["locators"]:
                unwarranted.append({"path": record["relative"], "status": record["status"]})
            # Naming another note in prose without linking it is a connection the
            # graph is missing, and the cheapest place to spot a contradiction.
            named = sorted({
                other["title"] for other in records
                if other["relative"] != record["relative"]
                and normalized(other["title"]) not in record["links"]
                and len(normalized(other["title"]).split()) > 1
                and normalized(other["title"]) in record["haystack"]
            })
            if named:
                missing_xref.append({"path": record["relative"], "mentions": named})

        siblings = []
        for index, record in enumerate(records):
            for other in records[index + 1:]:
                shared = sorted((set(record["tags"]) & set(other["tags"])) - {"security"})
                if len(shared) < LINT_TAG_OVERLAP:
                    continue
                if (record["keys"] & set(other["links"])) or (other["keys"] & set(record["links"])):
                    continue
                siblings.append({
                    "pair": [record["relative"], other["relative"]],
                    "shared_tags": shared,
                })

        stale = [
            {"path": record["relative"], "updated": record["updated"], "age_days": record["age_days"]}
            for record in records if record["stale"]
        ]
        findings = {
            "orphans": orphans,
            "dead_evidence": dead_evidence,
            "unwarranted_status": unwarranted,
            "missing_cross_references": missing_xref,
            "unlinked_topic_siblings": siblings,
            "thin_notes": thin,
            "stale": stale,
        }
        total = sum(len(value) for value in findings.values())
        self.log_activity("obsidian-lint", f"{len(records)} notes -> {total} findings")
        return {"ok": not dead_evidence and not unwarranted, "notes": len(records),
                "findings": total, "advice": LINT_ADVICE, **findings}


def load_candidate(path: str) -> dict[str, Any]:
    candidate_path = Path(path)
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise PolicyDenied("candidate JSON is missing or a symlink")
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PolicyDenied("candidate JSON must contain an object")
    return payload


def _remote_action(args):
    """Use the authenticated communication client; canonical host owns writes."""
    send_path = Path(os.environ.get('AGENTCOMM_SEND', '/root/agentcomm/send.py'))
    directory = str(send_path.parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location('skmr_vault_send', send_path)
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    payload = {key: value for key, value in vars(args).items() if value is not None}
    if args.action == 'preview':
        payload['candidate'] = load_candidate(args.candidate)
    result = client._request('POST', '/api/vault/action', payload)
    if not isinstance(result, dict):
        raise PolicyDenied('canonical vault API returned invalid JSON')
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    route = sub.add_parser("route")
    route.add_argument("semantic_class", choices=sorted(ROUTES) + [PROFILE_SEMANTIC_CLASS])
    route.add_argument("--title", required=True)
    route.add_argument("--project")
    route.add_argument("--profile-username", dest="profile_username")
    route.add_argument("--profile-section", dest="profile_section")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--profile")
    search.add_argument("--profile-section")
    preview = sub.add_parser("preview")
    preview.add_argument("candidate")
    commit = sub.add_parser("commit")
    commit.add_argument("token")
    sub.add_parser("validate")
    sub.add_parser("lint")
    sub.add_parser("init")
    log = sub.add_parser("log")
    # Not "action": the subparser dest already owns that name, and reusing it
    # would overwrite which subcommand was chosen.
    log.add_argument("entry_action")
    log.add_argument("detail")
    args = parser.parse_args()
    store = VaultStore()
    try:
        writing = args.action in {'preview', 'commit', 'init', 'log'}
        if writing and not can_write(ROLE):
            raise PolicyDenied(
                "Assigned READ vault access: writer action "
                + repr(args.action)
                + " is denied. Delegate candidates to the configured WRITE peer via /skmr:send."
            )
        if not (vault_mount_writable() if writing else vault_mount_usable()):
            output = _remote_action(args)
            print(json_dump(output))
            return 0 if output.get('ok', True) else 1
        if args.action == "route":
            relative, note_type, area = store.route(
                {
                    "semantic_class": args.semantic_class,
                    "title": args.title,
                    "project": args.project,
                    "profile_username": args.profile_username,
                    "profile_section": args.profile_section,
                }
            )
            output = {"relative": str(relative), "type": note_type, "area": area}
        elif args.action == "search":
            output = store.search(args.query, args.limit, profile=args.profile, profile_section=args.profile_section)
        elif args.action == "preview":
            output = store.preview(load_candidate(args.candidate))
        elif args.action == "commit":
            output = store.commit(args.token)
        elif args.action == "lint":
            output = store.lint_vault()
        elif args.action == "init":
            output = store.init_layers()
        elif args.action == "log":
            output = {"ok": True, "logged": store.append_log(args.entry_action, args.detail)}
        else:
            output = store.validate_vault()
        print(json_dump(output))
        return 0 if output.get("ok", True) else 1
    except (MemoryError, OSError, ValueError, RuntimeError, ImportError) as error:
        print(json_dump({"ok": False, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
