"""Vector retrieval over the stored embeddings.

Answers the queries BM25 cannot: a question worded nothing like the note that
holds the answer. The vault is small enough that a full scan of stored vectors is
faster and far simpler than maintaining an ANN structure, so that is what this
does -- one query embedding, cosine against every indexed chunk.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ...core import config
from ..indexing.index import unpack
from .bm25 import Hit
from .embeddings import provider

try:  # numpy is present here and makes the scan ~20x faster; not required.
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None


@dataclass
class VectorStatus:
    available: bool
    reason: str = ""


def indexed(conn: sqlite3.Connection) -> int:
    """Stored vectors for the current signature.

    Lets a caller tell "nothing is indexed" apart from "nothing was similar
    enough", which the empty hit list alone cannot express.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NOT NULL AND embed_sig=?",
            (provider().signature(),),
        ).fetchone()
        return int(row["n"] or 0)
    except Exception:
        return 0


def status() -> VectorStatus:
    try:
        engine = provider()
    except Exception as exc:
        return VectorStatus(False, str(exc))
    if not engine.available():
        return VectorStatus(False, engine.missing_reason())
    return VectorStatus(True)


def _cosine_py(query: list[float], matrix: list[list[float]]) -> list[float]:
    norm_q = sum(x * x for x in query) ** 0.5 or 1.0
    out = []
    for vector in matrix:
        norm_v = sum(x * x for x in vector) ** 0.5 or 1.0
        out.append(sum(a * b for a, b in zip(query, vector)) / (norm_q * norm_v))
    return out


def search(conn: sqlite3.Connection, query: str, top_k: int = 20) -> list[Hit]:
    state = status()
    if not state.available:
        return []
    rows = conn.execute(
        "SELECT chunk_id, relative, heading, content, embedding FROM chunks WHERE embedding IS NOT NULL AND embed_sig=?",
        (provider().signature(),),
    ).fetchall()
    if not rows:
        return []

    try:
        vectors = provider().embed([query])
    except Exception as exc:
        raise RuntimeError(f"query embedding failed: {exc}") from exc
    if not vectors:
        return []
    query_vector = vectors[0]

    stored = [unpack(row["embedding"]) for row in rows]
    width = len(query_vector)
    # A chunk embedded by a different model would have a different width; skip it
    # rather than comparing incomparable vectors.
    usable = [(row, vector) for row, vector in zip(rows, stored) if len(vector) == width]
    if not usable:
        return []

    if _np is not None:
        matrix = _np.asarray([vector for _, vector in usable], dtype=_np.float32)
        q = _np.asarray(query_vector, dtype=_np.float32)
        norms = _np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        qn = float(_np.linalg.norm(q)) or 1.0
        scores = (matrix @ q) / (norms * qn)
        similarities = scores.tolist()
    else:  # pragma: no cover
        similarities = _cosine_py(query_vector, [vector for _, vector in usable])

    # A cosine scan always has a nearest neighbour, so without a floor an
    # unrelated query still returns top_k rows and "no match" can never occur.
    floor = float(config.get("vector_min_score", 0.50))
    ranked = sorted(zip(usable, similarities), key=lambda item: -item[1])
    scored = [item for item in ranked if item[1] >= floor][:top_k]
    return [
        Hit(
            chunk_id=row["chunk_id"],
            relative=row["relative"],
            heading=row["heading"],
            content=row["content"],
            score=round(float(similarity), 4),
        )
        for (row, _), similarity in scored
    ]
