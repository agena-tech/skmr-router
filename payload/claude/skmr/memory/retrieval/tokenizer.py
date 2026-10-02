"""Tokenizer shared by BM25 indexing and querying.

Technical identifiers are the whole point of keeping a lexical index next to the
vector one, so `CVE-2026-1234`, `/skmr:send`, `bge-m3` and `VaultStore.search`
must survive tokenization. Each such token is emitted twice: once whole, and once
split into its parts. A query for the exact id then scores strongly, while a
query for just `cve` or `send` still finds it.
"""
from __future__ import annotations

import re
import unicodedata

# A token runs over letters/digits and the punctuation that appears *inside*
# identifiers; trailing separators are stripped afterwards.
_TOKEN = re.compile(r"[^\W_][\w\-./:]*", re.UNICODE)
_SPLIT = re.compile(r"[_\-./:]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were",
    "for", "on", "with", "that", "this", "it", "as", "at", "by", "be", "from",
    "ve", "ile", "bir", "bu", "su", "icin", "olarak", "da", "de", "mi", "ki",
})


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return (stripped.replace("ı", "i").replace("ğ", "g")
            .replace("ş", "s").replace("ç", "c").replace("ö", "o").replace("ü", "u"))


def tokenize(text: str, keep_stopwords: bool = False) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN.findall(text or ""):
        raw = raw.strip("./:-_")
        if not raw:
            continue
        whole = fold(_CAMEL.sub(" ", raw).replace(" ", ""))
        if whole and (keep_stopwords or whole not in STOPWORDS):
            out.append(whole)
        parts = [p for p in _SPLIT.split(fold(raw)) if p]
        camel_parts = [fold(p) for p in _CAMEL.split(raw) if p]
        for part in parts + camel_parts:
            if part and part != whole and (keep_stopwords or part not in STOPWORDS):
                out.append(part)
    return out


def term_frequencies(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in tokenize(text):
        counts[token] = counts.get(token, 0) + 1
    return counts
