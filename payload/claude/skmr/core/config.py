"""Single source of truth for SKMR configuration.

Every tunable the rest of the package reads lives here, backed by one JSON file.
Missing keys fall back to DEFAULTS, so a partial config file is valid and a
corrupt one degrades to defaults rather than taking the system down.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SKMR_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("SKMR_CONFIG", str(SKMR_ROOT / "config" / "skmr.json")))

DEFAULTS: dict[str, Any] = {
    # Canonical stores. The vault is read-only from this package; all permanent
    # mutations still go through obsidian_memory.py preview -> commit.
    "vault_path": "/mnt/skmr-vault",
    "writer": "/root/.claude/skills/skmr/scripts/obsidian_memory.py",
    "python": "/usr/bin/python3",
    "memory_path": "/root/.claude/projects/-root/memory/MEMORY.md",
    "state_dir": "/root/.claude/skmr/state",
    "agents_path": "/root/.claude/skmr/state/agents.json",
    "index_path": "/root/.claude/skmr/state/index.sqlite3",
    "claude_md": "/root/.claude/CLAUDE.md",

    # Retrieval
    "search_mode": "hybrid",          # bm25 | vector | hybrid
    "hybrid_strategy": "rrf",         # rrf | weighted
    "bm25_weight": 0.40,
    "vector_weight": 0.60,
    "rrf_k": 60,
    "bm25_top_k": 20,
    "vector_top_k": 20,
    "rerank_k": 10,
    # Cosine floor for a vector hit to count as a match at all. Without it the
    # nearest-neighbour scan always returns top_k rows, so "no match" becomes
    # unreachable and unrelated notes are presented as durable knowledge.
    "vector_min_score": 0.50,
    "final_k": 6,

    # Chunking
    "chunk_max_chars": 1200,
    "chunk_min_chars": 120,
    "chunking_version": 1,

    # Embedding
    "embedding_model": "bge-m3",
    "embedding_provider": "ollama",
    "ollama_host": "http://127.0.0.1:11434",
    "embedding_version": 1,
    "embedding_timeout": 120,
    "embedding_batch": 16,

    # Index bookkeeping
    "index_version": 2,

    # Planning / orchestration
    "max_subagents": 10,
    "planning_enabled": True,

    # Output
    "color": "auto",                  # auto | always | never
}

_cache: dict[str, Any] | None = None


def load(refresh: bool = False) -> dict[str, Any]:
    """Return the merged configuration (defaults overlaid with the JSON file)."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    merged = dict(DEFAULTS)
    # The physical mount belongs to machine configuration, not an agent name.
    try:
        peer_conf = json.loads(Path(os.environ.get('AGENTCOMM_CONF', '/root/agentcomm/agent.conf')).read_text(encoding='utf-8'))
        if isinstance(peer_conf.get('vault_local'), str) and peer_conf['vault_local'].strip():
            merged['vault_path'] = peer_conf['vault_local']
    except (OSError, ValueError, AttributeError):
        pass
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key, value in raw.items():
                default = DEFAULTS.get(key)
                if default is None:
                    continue
                if isinstance(default, bool):
                    valid = isinstance(value, bool)
                elif isinstance(default, int):
                    valid = isinstance(value, int) and not isinstance(value, bool) and value >= (0 if key == "max_subagents" else 1)
                elif isinstance(default, float):
                    import math
                    valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
                else:
                    valid = isinstance(value, str) and bool(value.strip())
                choices = {"search_mode": {"bm25", "vector", "hybrid"}, "hybrid_strategy": {"rrf", "weighted"}, "color": {"auto", "always", "never"}}
                if key in choices:
                    valid = valid and value in choices[key]
                if valid:
                    merged[key] = value
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        # A broken config must not take the whole system down; defaults stand.
        pass
    # Environment overrides stay narrow and explicit.
    for key, env in (("vault_path", "SKMR_VAULT"), ("search_mode", "SKMR_SEARCH_MODE"),
                     ("embedding_model", "SKMR_EMBED_MODEL"), ("ollama_host", "OLLAMA_HOST"),
                     ("state_dir", "SKMR_RUNTIME_STATE"), ("memory_path", "SKMR_MEMORY_PATH"),
                     ("agents_path", "SKMR_AGENTS_PATH"), ("claude_md", "SKMR_CLAUDE_MD")) :
        value = os.environ.get(env)
        if value:
            merged[key] = value
    _cache = merged
    return merged


def get(key: str, default: Any = None) -> Any:
    return load().get(key, DEFAULTS.get(key, default))


def path(key: str) -> Path:
    return Path(str(get(key)))


def write(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist updates to the config file and refresh the cache."""
    current: dict[str, Any] = {}
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            current = raw
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        current = {}
    current.update(updates)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    return load(refresh=True)


def state_dir() -> Path:
    directory = path("state_dir")
    directory.mkdir(parents=True, exist_ok=True)
    return directory
