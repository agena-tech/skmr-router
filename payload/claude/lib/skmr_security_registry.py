#!/usr/bin/env python3
"""Canonical SKMR security-name registry shared by the UserPromptSubmit and
PreToolUse hooks.

One source of truth prevents drift between slash-command classification
(security-workflow-reminder.py) and Skill-tool classification
(security-consultation-guard.py). Every name is stored casefolded; use
`normalize()` before membership tests.

Groups are enumerated explicitly rather than derived at runtime so
classification stays deterministic and reviewable.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

# SKMR installs security skills at session start and records them here, so the
# installed set changes without anyone editing this file. Reading that inventory
# keeps a freshly synced skill classified as security from its first use instead
# of waiting for someone to notice the drift.
MANAGED_SKILLS_FILE = (
    Path(os.environ.get("SKMR_STATE_DIR", "/root/.claude/state"))
    / "security-managed-skills.json"
)

# Locally installed SKMR security and memory skills.
_LOCAL_SKILLS = frozenset({
    "403bypass", "apexdiscovery", "asnrecon", "bac-analyzer", "bugbountyskills",
    "bugbountyworkflow", "cachedeception", "crawl", "hackerone-intelligence", "jsa",
    "jsanalyzer", "osint-enrich", "otp-bruteforce-testing", "pulse-template", "skmr",
    "stack-bounds-format-auditing", "subdomainenum", "tabletopexercise",
    "url-parser-confusion-testing",
})

# claude-bughunter plugin commands. The harness exposes each as /<name> and as
# Skill(<name>), so both hooks must classify them identically.
_BUGHUNTER_COMMANDS = frozenset({
    "autopilot", "chain", "hunt", "intel", "memory-gc", "pickup", "recon", "remember",
    "report", "scope", "surface", "token-scan", "triage", "validate", "web3-audit",
})

# claude-bughunter plugin skills. The plugin is exclusively offensive-security
# tooling, so every skill it ships is security work by definition.
_BUGHUNTER_SKILLS = frozenset({
    "apk-redteam-pipeline", "bb-local-toolkit", "bb-methodology", "bug-bounty",
    "bugcrowd-reporting", "cloud-iam-deep", "enterprise-vpn-attack", "evidence-hygiene",
    "hunt-api-misconfig", "hunt-aspnet", "hunt-ato", "hunt-auth-bypass", "hunt-brute-force",
    "hunt-business-logic", "hunt-cache-poison", "hunt-captcha-bypass", "hunt-cicd",
    "hunt-clickjacking", "hunt-cloud-misconfig", "hunt-cors", "hunt-csrf",
    "hunt-deserialization", "hunt-dispatch", "hunt-dom", "hunt-exceptional-conditions",
    "hunt-file-upload", "hunt-fintech-graphql", "hunt-forgot-password", "hunt-graphql",
    "hunt-grpc", "hunt-host-header", "hunt-html-injection", "hunt-http-smuggling",
    "hunt-idor", "hunt-jwt-crypto", "hunt-k8s", "hunt-laravel", "hunt-ldap", "hunt-lfi",
    "hunt-llm-ai", "hunt-mfa-bypass", "hunt-misc", "hunt-nextjs", "hunt-nodejs",
    "hunt-nosqli", "hunt-ntlm-info", "hunt-oauth", "hunt-open-redirect",
    "hunt-race-condition", "hunt-rag-vector", "hunt-rce", "hunt-saml", "hunt-session",
    "hunt-shadow-api", "hunt-sharepoint", "hunt-source-leak", "hunt-spa-api",
    "hunt-springboot", "hunt-sqli", "hunt-ssrf", "hunt-ssti", "hunt-subdomain",
    "hunt-tls-network", "hunt-websocket", "hunt-xss", "hunt-xxe", "ios-redteam-pipeline",
    "m365-entra-attack", "meme-coin-audit", "mid-engagement-ir-detection",
    "offensive-osint", "okta-attack", "osint-methodology", "recon-scope-triage",
    "redteam-mindset", "redteam-report-template", "report-writing", "security-arsenal",
    "supply-chain-attack-recon", "triage-validation", "vmware-vcenter-attack", "web2-recon",
    "web3-audit",
})

# ECC skills whose stated purpose is finding or assessing vulnerabilities.
# Compliance, privacy, harness-safety and general code-quality skills are
# deliberately excluded so ordinary development does not activate SKMR.
_ECC_SECURITY_SKILLS = frozenset({
    "defi-amm-security", "django-security", "laravel-security",
    "llm-trading-agent-security", "perl-security", "quarkus-security",
    "security-bounty-hunter", "security-review", "security-scan", "springboot-security",
})

SECURITY_SKILL_NAMES = (
    _LOCAL_SKILLS | _BUGHUNTER_COMMANDS | _BUGHUNTER_SKILLS | _ECC_SECURITY_SKILLS
)

# Slash-command aliases with no Skill counterpart installed. Kept separate so
# they never imply a Skill classification.
SECURITY_COMMAND_ONLY_NAMES = frozenset({
    "caido", "xss-test",
})

# Skill names that must activate SKMR but are not reachable as slash commands.
# Currently none: the harness exposes every installed skill as /<name> too.
# Declared explicitly so a future exception is stated once rather than
# duplicating the registry.
SECURITY_SKILL_ONLY_NAMES = frozenset()

# Slash-command classification derived from the same source.
SECURITY_COMMANDS = (
    SECURITY_SKILL_NAMES - SECURITY_SKILL_ONLY_NAMES
) | SECURITY_COMMAND_ONLY_NAMES


def normalize(name: object) -> str:
    """Casefold a skill/command name and drop any plugin prefix."""
    return str(name or "").split(":")[-1].strip().casefold()


@lru_cache(maxsize=1)
def managed_skill_names() -> frozenset[str]:
    """Skills SKMR itself installed, read from its own inventory.

    Never raises: a missing or damaged inventory simply falls back to the
    enumerated sets above, so classification degrades to the static floor
    rather than failing open.

    Cached for the life of the process: this sits on the guard's hot path and
    was re-reading and re-parsing the inventory on every PreToolUse event. Hooks
    are short-lived separate processes, so each session still sees a fresh
    inventory and the cache cannot serve a stale answer across sessions.
    """
    try:
        payload = json.loads(MANAGED_SKILLS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
        return frozenset()
    return frozenset(
        normalize(entry) for entry in payload["skills"] if isinstance(entry, str) and entry.strip()
    )


def is_security_skill(name: object) -> bool:
    normalized = normalize(name)
    return normalized in SECURITY_SKILL_NAMES or normalized in managed_skill_names()


def is_security_command(name: object) -> bool:
    normalized = normalize(name)
    return normalized in SECURITY_COMMANDS or normalized in managed_skill_names()


__all__ = [
    "MANAGED_SKILLS_FILE",
    "SECURITY_SKILL_NAMES",
    "SECURITY_COMMAND_ONLY_NAMES",
    "SECURITY_SKILL_ONLY_NAMES",
    "SECURITY_COMMANDS",
    "managed_skill_names",
    "normalize",
    "is_security_skill",
    "is_security_command",
]
