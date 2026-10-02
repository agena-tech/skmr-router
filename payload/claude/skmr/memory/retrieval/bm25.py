"""BM25 lexical retrieval over the SQLite postings table.

BM25 needs no embeddings and is the half of hybrid search that actually finds
exact technical strings: CVE ids, command names, model names, paths, error codes.
It is implemented directly (Okapi BM25) rather than pulled from a dependency, so
retrieval keeps working on a bare stdlib Python.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from . import tokenizer

K1 = 1.5
B = 0.75


@dataclass
class Hit:
    chunk_id: str
    relative: str
    heading: str
    content: str
    score: float
    matched: tuple[str, ...] = ()


def search(conn: sqlite3.Connection, query: str, top_k: int = 20) -> list[Hit]:
    terms = tokenizer.tokenize(query)
    if not terms:
        return []

    totals = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(AVG(length), 0) AS avgdl FROM chunks"
    ).fetchone()
    total_docs = int(totals["n"] or 0)
    if not total_docs:
        return []
    avgdl = float(totals["avgdl"] or 1.0) or 1.0

    scores: dict[str, float] = {}
    matched: dict[str, set[str]] = {}
    unique_terms = list(dict.fromkeys(terms))

    for term in unique_terms:
        rows = conn.execute(
            "SELECT p.chunk_id AS chunk_id, p.tf AS tf, c.length AS length"
            " FROM postings p JOIN chunks c ON c.chunk_id = p.chunk_id"
            " WHERE p.term = ?",
            (term,),
        ).fetchall()
        df = len(rows)
        if not df:
            continue
        idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
        for row in rows:
            tf = float(row["tf"])
            length = float(row["length"] or 1.0)
            denominator = tf + K1 * (1 - B + B * length / avgdl)
            scores[row["chunk_id"]] = scores.get(row["chunk_id"], 0.0) + idf * (tf * (K1 + 1)) / denominator
            matched.setdefault(row["chunk_id"], set()).add(term)

    if not scores:
        return []

    ranked = sorted(scores.items(), key=lambda item: -item[1])[:top_k]
    placeholders = ",".join("?" for _ in ranked)
    rows = {
        row["chunk_id"]: row
        for row in conn.execute(
            f"SELECT chunk_id, relative, heading, content FROM chunks WHERE chunk_id IN ({placeholders})",
            [chunk_id for chunk_id, _ in ranked],
        )
    }
    hits: list[Hit] = []
    for chunk_id, score in ranked:
        row = rows.get(chunk_id)
        if row is None:
            continue
        hits.append(
            Hit(
                chunk_id=chunk_id,
                relative=row["relative"],
                heading=row["heading"],
                content=row["content"],
                score=round(score, 4),
                matched=tuple(sorted(matched.get(chunk_id, ()))),
            )
        )
    return hits
