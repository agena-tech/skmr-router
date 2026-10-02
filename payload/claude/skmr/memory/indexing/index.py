"""Persistent incremental index over the Obsidian vault.

Re-embedding the whole vault on every query is forbidden, so chunks, BM25
postings and embeddings live in one SQLite file and only changed files are
reprocessed. A file is "changed" when its content hash differs -- mtime alone
lies after a copy or a restore.

Version fields matter as much as the content: when the embedding model, the
embedding version or the chunking version changes, the affected vectors are
invalidated rather than being silently mixed with vectors from another model.

This module only ever READS the vault. Permanent writes belong to the canonical
writer (`obsidian_memory.py`).
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...core import config
from ..retrieval import tokenizer
from ..retrieval.chunking import chunk_markdown

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    relative   TEXT PRIMARY KEY,
    file_hash  TEXT NOT NULL,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    relative     TEXT NOT NULL,
    heading      TEXT NOT NULL DEFAULT '',
    position     INTEGER NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    length       INTEGER NOT NULL,
    embedding    BLOB,
    embed_sig    TEXT
);
CREATE INDEX IF NOT EXISTS chunks_relative ON chunks(relative);
CREATE TABLE IF NOT EXISTS postings (
    term     TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    tf       INTEGER NOT NULL,
    PRIMARY KEY (term, chunk_id)
);
CREATE INDEX IF NOT EXISTS postings_term ON postings(term);
"""

SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", "__pycache__"}


@dataclass
class SyncReport:
    scanned: int = 0
    added: int = 0
    modified: int = 0
    deleted: int = 0
    chunks: int = 0
    embedded: int = 0
    invalidated: bool = False
    embedding_skipped: str = ""
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_lines(self) -> list[str]:
        lines = [
            f"files scanned : {self.scanned}",
            f"added         : {self.added}",
            f"modified      : {self.modified}",
            f"deleted       : {self.deleted}",
            f"chunks        : {self.chunks}",
            f"embedded      : {self.embedded}",
            f"elapsed       : {self.seconds:.2f}s",
        ]
        if self.invalidated:
            lines.append("note          : embedding/chunking version changed, vectors rebuilt")
        if self.embedding_skipped:
            lines.append(f"embedding     : skipped -- {self.embedding_skipped}")
        for err in self.errors[:5]:
            lines.append(f"error         : {err}")
        return lines


def pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def connect() -> sqlite3.Connection:
    path = config.path("index_path")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def vault_root() -> Path:
    return config.path("vault_path")


def vault_available() -> bool:
    root = vault_root()
    try:
        return root.is_dir() and any(root.iterdir())
    except OSError:
        return False


def _notes(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*.md"):
        parts = set(path.relative_to(root).parts[:-1])
        if parts & SKIP_DIRS or path.name.startswith("."):
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        found.append(path)
    return sorted(found)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _replace_chunks(conn: sqlite3.Connection, relative: str, text: str) -> int:
    conn.execute(
        "DELETE FROM postings WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE relative=?)",
        (relative,),
    )
    conn.execute("DELETE FROM chunks WHERE relative=?", (relative,))
    chunks = chunk_markdown(relative, text)
    for chunk in chunks:
        counts = tokenizer.term_frequencies(chunk.content)
        conn.execute(
            "INSERT INTO chunks(chunk_id, relative, heading, position, content, content_hash, length)"
            " VALUES(?,?,?,?,?,?,?)",
            (chunk.chunk_id, relative, chunk.heading, chunk.position,
             chunk.content, chunk.content_hash, sum(counts.values())),
        )
        if counts:
            conn.executemany(
                "INSERT INTO postings(term, chunk_id, tf) VALUES(?,?,?)",
                [(term, chunk.chunk_id, tf) for term, tf in counts.items()],
            )
    return len(chunks)


def _signature() -> str:
    from ..retrieval.embeddings import provider
    try:
        return provider().signature()
    except Exception:
        return "unavailable"


def sync(embed: bool = True, force: bool = False) -> SyncReport:
    """Bring the index up to date with the vault. Only changed files are touched."""
    started = time.time()
    report = SyncReport()
    root = vault_root()
    if not vault_available():
        report.errors.append(f"vault unavailable at {root}")
        report.seconds = time.time() - started
        return report

    conn = connect()
    try:
        signature = _signature()
        stored_sig = _meta_get(conn, "embedding_signature")
        stored_chunking = _meta_get(conn, "chunking_version")
        chunking_version = str(config.get("chunking_version", 1))
        index_version = str(config.get("index_version", 1))
        chunking_signature = f"{chunking_version}:{config.get('chunk_max_chars')}:{config.get('chunk_min_chars')}"
        stored_chunking_signature = _meta_get(conn, "chunking_signature")

        # Version invalidation: vectors from another model or another chunking
        # strategy are not comparable, so they are dropped rather than reused.
        chunking_changed = stored_chunking is not None and stored_chunking != chunking_version
        index_changed = _meta_get(conn, "index_version") not in (None, index_version)
        chunking_changed = chunking_changed or stored_chunking_signature not in (None, chunking_signature)
        if force or chunking_changed or index_changed:
            conn.execute("DELETE FROM postings")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM files")
            report.invalidated = True
        elif stored_sig is not None and signature != "unavailable" and stored_sig != signature:
            conn.execute("UPDATE chunks SET embedding=NULL, embed_sig=NULL")
            report.invalidated = True

        known = {row["relative"]: row for row in conn.execute("SELECT * FROM files")}
        seen: set[str] = set()

        for path in _notes(root):
            relative = str(path.relative_to(root))
            seen.add(relative)
            report.scanned += 1
            try:
                stat = path.stat()
                digest = _file_hash(path)
            except OSError as exc:
                report.errors.append(f"{relative}: {exc}")
                continue
            row = known.get(relative)
            if row is not None and row["file_hash"] == digest:
                continue  # unchanged: never re-chunked, never re-embedded
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                report.errors.append(f"{relative}: {exc}")
                continue
            report.chunks += _replace_chunks(conn, relative, text)
            conn.execute(
                "INSERT INTO files(relative, file_hash, mtime, size, indexed_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(relative) DO UPDATE SET file_hash=excluded.file_hash,"
                " mtime=excluded.mtime, size=excluded.size, indexed_at=excluded.indexed_at",
                (relative, digest, stat.st_mtime, stat.st_size,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
            if row is None:
                report.added += 1
            else:
                report.modified += 1

        for relative in set(known) - seen:
            conn.execute(
                "DELETE FROM postings WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE relative=?)",
                (relative,),
            )
            conn.execute("DELETE FROM chunks WHERE relative=?", (relative,))
            conn.execute("DELETE FROM files WHERE relative=?", (relative,))
            report.deleted += 1

        _meta_set(conn, "chunking_version", chunking_version)
        _meta_set(conn, "chunking_signature", chunking_signature)
        _meta_set(conn, "index_version", index_version)
        _meta_set(conn, "synced_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        conn.commit()

        if embed:
            report.embedded, report.embedding_skipped = _embed_pending(conn, signature)
            conn.commit()
    finally:
        conn.close()

    report.seconds = time.time() - started
    return report


def _embed_pending(conn: sqlite3.Connection, signature: str) -> tuple[int, str]:
    """Embed chunks that have no current vector. Returns (count, skip reason)."""
    from ..retrieval.embeddings import provider

    rows = conn.execute(
        "SELECT chunk_id, content FROM chunks WHERE embedding IS NULL OR embed_sig IS NOT ?",
        (signature,),
    ).fetchall()
    if not rows:
        _meta_set(conn, "embedding_signature", signature)
        return 0, ""

    try:
        engine = provider()
    except Exception as exc:
        return 0, str(exc)
    if not engine.available():
        return 0, engine.missing_reason()

    batch_size = max(1, int(config.get("embedding_batch", 16)))
    done = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start: start + batch_size]
        try:
            vectors = engine.embed([row["content"] for row in batch])
            import math
            if len(vectors) != len(batch) or any(not v or any(not math.isfinite(x) for x in v) for v in vectors):
                raise RuntimeError("embedding provider returned missing or invalid vectors")
        except Exception as exc:
            return done, f"embedding failed after {done} chunks: {exc}"
        for row, vector in zip(batch, vectors):
            conn.execute(
                "UPDATE chunks SET embedding=?, embed_sig=? WHERE chunk_id=?",
                (pack(vector), signature, row["chunk_id"]),
            )
        done += len(batch)
        conn.commit()
    _meta_set(conn, "embedding_signature", signature)
    return done, ""


def stats() -> dict[str, object]:
    conn = connect()
    try:
        files = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        vectors = conn.execute("SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NOT NULL").fetchone()["n"]
        terms = conn.execute("SELECT COUNT(DISTINCT term) AS n FROM postings").fetchone()["n"]
        return {
            "files": files,
            "chunks": chunks,
            "vectors": vectors,
            "terms": terms,
            "embedding_signature": _meta_get(conn, "embedding_signature") or "-",
            "chunking_version": _meta_get(conn, "chunking_version") or "-",
            "index_version": _meta_get(conn, "index_version") or "-",
            "synced_at": _meta_get(conn, "synced_at") or "never",
            "index_path": str(config.path("index_path")),
        }
    finally:
        conn.close()
