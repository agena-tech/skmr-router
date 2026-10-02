"""Heading-aware markdown chunking.

A whole note is the wrong embedding unit -- one vector cannot represent six
unrelated headings. Splitting on heading boundaries keeps each chunk
semantically coherent and lets a hit point at the section that actually matched.

Oversized sections are split further on paragraph boundaries; undersized ones are
merged forward, so the index holds neither 8000-character blobs nor one-line
fragments.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ...core import config

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


@dataclass
class Chunk:
    chunk_id: str
    relative: str
    heading: str
    position: int
    content: str
    content_hash: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_long(text: str, limit: int) -> list[str]:
    """Split on blank lines, then hard-wrap anything still over the limit."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buffer = ""
    for paragraph in re.split(r"\n\s*\n", text):
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            out.append(buffer)
        while len(paragraph) > limit:
            out.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        buffer = paragraph
    if buffer:
        out.append(buffer)
    return [part for part in out if part.strip()]


def chunk_markdown(relative: str, text: str) -> list[Chunk]:
    limit = int(config.get("chunk_max_chars", 1200))
    floor = int(config.get("chunk_min_chars", 120))

    body = _FRONTMATTER.sub("", text or "")
    matches = list(_HEADING.finditer(body))

    raw: list[tuple[str, str]] = []
    if not matches:
        raw.append(("", body))
    else:
        lead = body[: matches[0].start()].strip()
        if lead:
            raw.append(("", lead))
        for i, match in enumerate(matches):
            heading = match.group(2).strip()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            raw.append((heading, body[match.end(): end]))

    # Merge sections that are too thin to stand alone with the next one, so a
    # bare `## Summary` heading does not become its own chunk.
    merged: list[tuple[str, str]] = []
    for heading, content in raw:
        content = content.strip()
        if not content and not heading:
            continue
        if merged and len(content) < floor and len(merged[-1][1]) + len(content) <= limit:
            prev_heading, prev_content = merged[-1]
            joined = f"{prev_content}\n\n## {heading}\n{content}".strip() if heading else f"{prev_content}\n\n{content}".strip()
            merged[-1] = (prev_heading, joined)
            continue
        merged.append((heading, content))

    chunks: list[Chunk] = []
    position = 0
    for heading, content in merged:
        for piece in _split_long(content, limit):
            piece = piece.strip()
            if not piece:
                continue
            # The heading is prepended to the embedded text: retrieval should be
            # able to match on the section title, not only its body.
            embedded = f"{heading}\n\n{piece}" if heading else piece
            chunks.append(
                Chunk(
                    chunk_id=f"{relative}#{position}",
                    relative=relative,
                    heading=heading,
                    position=position,
                    content=embedded,
                    content_hash=_hash(embedded),
                )
            )
            position += 1
    return chunks
