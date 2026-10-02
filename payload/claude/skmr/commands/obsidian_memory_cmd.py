"""`/skmr:obsidian-memory` -- permanent knowledge: hybrid search, index, delegation.

Division of responsibility, which this command must not blur:

  * READS (search, status, index) are served by this package's retrieval engine,
  * WRITES (route, preview, commit, validate, lint, init, log) are delegated
    verbatim to the canonical writer `obsidian_memory.py`.

The writer stays the sole routing and mutation authority; nothing here ever
writes a note or picks a destination path.
"""
from __future__ import annotations

import subprocess
import importlib.util
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get('AGENTCOMM_LIB', str(Path(__file__).resolve().parents[2] / 'lib')))
from skmr_agent_policy import VAULT, vault_mount_usable

from ..core import config, output
from ..hooks import routing
from ..memory.indexing import index
from ..memory.retrieval import hybrid
from ..planning.planner import Plan

# Actions that belong to the canonical writer, passed through untouched.
DELEGATED = ("route", "preview", "commit", "validate", "lint", "init", "log")


def _delegate(args: list[str]) -> int:
    writer = str(config.get("writer"))
    python = str(config.get("python"))
    output.info(f"Delegating to the canonical writer: {args[0]}")
    try:
        completed = subprocess.run([python, writer, *args], check=False)
        return completed.returncode
    except OSError as exc:
        output.error(f"cannot run the canonical writer: {exc}")
        return 1


def _search(args: list[str]) -> int:
    if "--profile" in args or "--profile-section" in args:
        return _delegate(["search", *args])
    mode = None
    limit = None
    terms: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in {"--mode", "--limit", "-n", "--profile", "--profile-section"} and i + 1 >= len(args):
            output.error(f"{token} requires a value")
            return 2
        if token == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
            continue
        if token in {"--limit", "-n"} and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                output.error(f"--limit expects a number, got {args[i + 1]!r}")
                return 2
            if limit <= 0:
                output.error("--limit must be positive")
                return 2
            i += 2
            continue
        if token == "--profile" and i + 1 < len(args):
            # Profile-scoped recall is the writer's own semantics; hand it over
            # rather than reimplementing profile resolution here.
            return _delegate(["search", " ".join(terms + args[i:])])
        terms.append(token)
        i += 1

    query = " ".join(terms).strip().strip('"')
    if not query:
        output.error('usage: /skmr:obsidian-memory search "query" [--mode hybrid|bm25|vector] [--limit N]')
        return 2

    if mode and mode.casefold() not in {"hybrid", "bm25", "vector"}:
        output.error(f"unknown mode '{mode}'; use hybrid, bm25 or vector")
        return 2

    routing.to("obsidian-memory")
    canonical = Path(str(config.get('vault_path'))).resolve(strict=False) == VAULT.resolve(strict=False)
    if canonical and not vault_mount_usable():
        outcome = _remote_search(query, mode, limit)
    else:
        outcome = hybrid.search(query, mode=mode, limit=limit)
    hybrid.report(outcome)
    return 0 if outcome.ok else 1


def _remote_search(query, mode, limit):
    selected = (mode or str(config.get('search_mode', 'hybrid'))).casefold()
    outcome = hybrid.SearchOutcome(query, selected, str(config.get('hybrid_strategy', 'rrf')))
    try:
        send = Path(os.environ.get('AGENTCOMM_SEND', '/root/agentcomm/send.py'))
        if str(send.parent) not in sys.path:sys.path.insert(0,str(send.parent))
        spec = importlib.util.spec_from_file_location('skmr_search_send', send)
        client = importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
        data = client._request('POST','/api/vault/action',{'action':'search','query':query,'mode':selected,
            'limit':limit if limit is not None else int(config.get('final_k',6))})
        if not isinstance(data,dict):raise ValueError('canonical search returned invalid JSON')
        if not data.get('ok',True):raise RuntimeError(str(data.get('error') or 'canonical search failed'))
        outcome = hybrid.SearchOutcome(query=str(data.get('query',query)), mode=str(data.get('mode',selected)),
            strategy=str(data.get('strategy','rrf')), results=[hybrid.Result(**entry) for entry in data.get('results',[])],
            warnings=data.get('warnings',[]), error=str(data.get('error') or ''),
            bm25_count=int(data.get('bm25_count',0)), vector_count=int(data.get('vector_count',0)))
    except (OSError,ValueError,TypeError,RuntimeError,ImportError) as exc:
        outcome.error='canonical search unavailable: '+str(exc)
    return outcome


def _status() -> int:
    from ..memory.retrieval import vector

    routing.to("obsidian-memory")
    print()
    print(f"vault            : {index.vault_root()} "
          f"({'available' if index.vault_available() else 'UNAVAILABLE'})")
    stats = index.stats()
    for key in ("files", "chunks", "vectors", "terms", "synced_at",
                "embedding_signature", "chunking_version", "index_version", "index_path"):
        print(f"{key:<17}: {stats[key]}")
    state = vector.status()
    print(f"vector backend   : {'available' if state.available else 'unavailable — ' + state.reason}")
    print(f"search mode      : {config.get('search_mode')} (strategy {config.get('hybrid_strategy')})")
    print()
    if not index.vault_available():
        output.error("vault mount is missing; this is an error, not an empty vault")
        return 1
    return 0


def _index(args: list[str]) -> int:
    force = "--force" in args
    skip_embed = "--no-embed" in args
    routing.to("obsidian-memory")
    output.info("Indexing changed notes only (unchanged files are never re-embedded)…")
    report = index.sync(embed=not skip_embed, force=force)
    print()
    for line in report.as_lines():
        print(f"  {line}")
    print()
    if report.errors and not report.scanned:
        output.error("; ".join(report.errors[:3]))
        return 1
    if report.embedding_skipped:
        output.warning(f"vector index incomplete: {report.embedding_skipped}")
    return 0


def run(args: list[str], plan: Plan) -> int:
    action = (args[0].casefold() if args else "status")
    rest = args[1:]

    if action in {"search", "recall", "find"}:
        return _search(rest)
    if action == "status":
        return _status()
    if action in {"index", "reindex", "sync"}:
        return _index(rest)
    if action in {"remember", "save"}:
        # Saving is a guarded preview -> commit flow with a real candidate file;
        # a one-shot "remember this string" would bypass the acceptance gate.
        output.error(
            "permanent saves go through the canonical writer's preview -> commit flow.\n"
            "  1. build a candidate JSON (semantic_class, title, body, provenance …)\n"
            f"  2. {config.get('python')} {config.get('writer')} preview <candidate.json>\n"
            "  3. /skmr:obsidian-memory commit <token returned by preview>\n"
            "Use the `skmr` skill for the acceptance gate and schema."
        )
        return 2
    if action in DELEGATED:
        return _delegate(args)

    output.error(
        f'unknown action "{action}". Use: search | status | index | '
        + " | ".join(DELEGATED)
    )
    return 2
