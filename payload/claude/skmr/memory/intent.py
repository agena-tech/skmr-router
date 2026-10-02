"""Intent detection for state and identity questions.

Three layers, cheapest first, because one giant regex over every language is not
maintainable and not what correctness needs here:

  1. lexical cues -- curated multilingual keyword sets, scored by overlap
     (not a single monolithic pattern; each intent owns its cues),
  2. structural cues -- question shape and pronoun/possessive markers,
  3. semantic fallback -- cosine similarity against short exemplar phrases using
     the configured embedding provider, used only when layers 1-2 are
     inconclusive and the provider happens to be available.

If every layer is inconclusive the answer is `None`, which means "no routing" --
the caller then behaves normally instead of guessing.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Intent names the router understands.
PREVIOUS_STATE = "previous_state"
CURRENT_STATE = "current_state"
LAST_ACTION = "last_action"
CONTINUE_TASK = "continue_task"
AGENT_PROGRESS = "agent_progress"
NEXT_STEP = "next_step"
IDENTITY_SELF = "identity_self"
IDENTITY_USER = "identity_user"
IDENTITY_PEER = "identity_peer"

STATE_INTENTS = frozenset({PREVIOUS_STATE, CURRENT_STATE, LAST_ACTION, CONTINUE_TASK, AGENT_PROGRESS, NEXT_STEP})
IDENTITY_INTENTS = frozenset({IDENTITY_SELF, IDENTITY_USER, IDENTITY_PEER})

# Curated cues per intent. Turkish and English are first-class because that is
# what this operator actually uses; other languages fall through to the semantic
# layer rather than being half-covered by guesswork here.
CUES: dict[str, tuple[tuple[str, ...], ...]] = {
    PREVIOUS_STATE: (
        ("nerede", "kalmis", "kalmistik", "kaldik"),
        ("where", "did", "we", "stop", "leave", "off", "were"),
        ("son", "durum", "neredeydik"),
    ),
    CURRENT_STATE: (
        ("hangi", "asama", "asamada", "durum", "neredeyiz"),
        ("what", "phase", "stage", "status", "current", "state", "doing"),
        ("ne", "yapiyorduk", "yapiyoruz"),
    ),
    LAST_ACTION: (
        ("en", "son", "ne", "yaptik", "yaptin", "yapildi"),
        ("last", "what", "did", "we", "do", "done", "recently"),
    ),
    CONTINUE_TASK: (
        ("devam", "edelim", "et", "kaldigimiz", "yerden"),
        ("continue", "resume", "carry", "on", "pick", "up", "where", "left"),
    ),
    AGENT_PROGRESS: (
        ("subagent", "subagentler", "ajan", "ajanlar", "agent", "agentler", "durumda", "ne", "yapti"),
        ("agents", "subagents", "what", "are", "doing", "status", "progress"),
    ),
    NEXT_STEP: (
        # "adım" is shared with "my name"; the leading ordinal is what separates
        # "sıradaki adım" (next step) from "benim adım" (my name).
        ("siradaki", "sonraki", "sirada", "adim", "nedir", "atmamiz", "yapilacak"),
        ("next", "step", "action", "todo", "remaining"),
    ),
    IDENTITY_SELF: (
        ("adin", "ne", "sen", "kimsin", "rolun", "kimim", "gorevin"),
        ("your", "name", "who", "are", "you", "role"),
    ),
    IDENTITY_USER: (
        ("ben", "benim", "adim", "ismim", "ne", "kimim", "beni", "taniyor", "musun", "tanidin"),
        ("who", "am", "i", "my", "name", "do", "you", "know", "me"),
    ),
    IDENTITY_PEER: (
        ("diger", "agent", "kim", "komutan", "uzak", "peer"),
        ("other", "agent", "who", "commander", "remote", "peer", "lieutenant"),
    ),
}

# Cues that identify an intent almost on their own. Without this weighting a
# distinctive word like "continue" scores the same as filler like "we" or "off",
# so "continue from where we left off" loses to previous_state on token count.
STRONG: dict[str, frozenset[str]] = {
    CONTINUE_TASK: frozenset({"continue", "resume", "devam", "kaldigimiz"}),
    AGENT_PROGRESS: frozenset({"subagent", "subagentler", "agents", "ajanlar", "agentler"}),
    IDENTITY_USER: frozenset({"kimim", "beni", "benim", "ismim"}),
    NEXT_STEP: frozenset({"siradaki", "sonraki"}),
    IDENTITY_PEER: frozenset({"komutan", "lieutenant", "peer"}),
    LAST_ACTION: frozenset({"yaptik", "yaptin"}),
}
STRONG_BONUS = 0.45

EXEMPLARS: dict[str, tuple[str, ...]] = {
    PREVIOUS_STATE: ("where did we stop last time", "nerede kalmıştık"),
    CURRENT_STATE: ("which phase are we in right now", "hangi aşamadayız"),
    LAST_ACTION: ("what did we do most recently", "en son ne yaptık"),
    CONTINUE_TASK: ("continue from where we left off", "kaldığımız yerden devam edelim"),
    AGENT_PROGRESS: ("what are the subagents doing", "ajanlar ne durumda"),
    NEXT_STEP: ("what is the next step", "sıradaki adım ne"),
    IDENTITY_SELF: ("what is your name and role", "senin adın ve rolün ne"),
    IDENTITY_USER: ("who am i, do you know me", "ben kimim, beni tanıyor musun"),
    IDENTITY_PEER: ("who is the other agent", "diğer agent kim"),
}

_QUESTION = re.compile(r"[?？]|\b(mi|mı|mu|mü|musun|misin)\b", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")

# How much lexical evidence is needed before a routing decision is made.
LEXICAL_MIN = 2
SEMANTIC_MIN = 0.62


@dataclass
class Detection:
    intent: str
    confidence: float
    layer: str          # "lexical" | "semantic"

    @property
    def is_state(self) -> bool:
        return self.intent in STATE_INTENTS

    @property
    def is_identity(self) -> bool:
        return self.intent in IDENTITY_INTENTS

    @property
    def route(self) -> str:
        return "native-memory" if self.is_state or self.is_identity else ""


def fold(text: str) -> str:
    """Casefold and strip diacritics so `kalmıştık` and `kalmistik` match."""
    lowered = (text or "").casefold()
    decomposed = unicodedata.normalize("NFKD", lowered)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.replace("ı", "i").replace("ğ", "g").replace("ş", "s").replace("ç", "c")


def tokens(text: str) -> set[str]:
    return set(_WORD.findall(fold(text)))


def _lexical(text: str) -> tuple[str | None, float]:
    words = tokens(text)
    folded = fold(text).strip().rstrip("?!. ")
    if re.search(r"\b(change|rename|edit|set|configure|degistir|duzenle)\b", folded):
        return None, 0.0
    if folded in {"continue", "resume", "devam"}:
        return CONTINUE_TASK, 1.0
    if "asamadayiz" in words or "neredeyiz" in words:
        return CURRENT_STATE, 1.0
    if {"were", "doing"} <= words:
        return CURRENT_STATE, 0.9
    if words & {"rolu", "role"}:
        from ..agents import topology
        for agent in topology.load().remote:
            if tokens(agent.name) & words:
                return IDENTITY_PEER, 1.0
        # No name literal here on purpose. A peer is whoever the persisted
        # topology says it is; a name absent from it is just a word, and
        # guessing from a historically installed name would answer confidently
        # on a machine whose agents are called something else entirely.
    if not words:
        return None, 0.0
    best, best_score = None, 0.0
    for intent, groups in CUES.items():
        hits = max(len(words & set(group)) for group in groups)
        if hits < LEXICAL_MIN:
            continue
        # Normalise by the strongest group so a long sentence does not win by length.
        size = max(len(group) for group in groups)
        score = hits / max(size, 1) + 0.1 * hits
        if words & STRONG.get(intent, frozenset()):
            score += STRONG_BONUS
        if score > best_score:
            best, best_score = intent, score
    if best and _QUESTION.search(text or ""):
        best_score += 0.1
    return best, round(min(best_score, 1.0), 3)


def _semantic(text: str) -> tuple[str | None, float]:
    """Compare against exemplars with the embedding provider, when available."""
    try:
        from .retrieval.embeddings import provider  # local import: optional path
    except Exception:
        return None, 0.0
    try:
        engine = provider()
        if not engine.available():
            return None, 0.0
    except Exception:
        return None, 0.0
    phrases = [(intent, phrase) for intent, group in EXEMPLARS.items() for phrase in group]
    try:
        vectors = engine.embed([text] + [phrase for _, phrase in phrases])
    except Exception:
        return None, 0.0
    if not vectors or len(vectors) != len(phrases) + 1:
        return None, 0.0
    query, rest = vectors[0], vectors[1:]
    best, best_score = None, 0.0
    for (intent, _), vector in zip(phrases, rest):
        score = _cosine(query, vector)
        if score > best_score:
            best, best_score = intent, score
    if best_score < SEMANTIC_MIN:
        return None, round(best_score, 3)
    return best, round(best_score, 3)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def detect(text: str, allow_semantic: bool = True) -> Detection | None:
    intent, score = _lexical(text)
    if intent:
        return Detection(intent, score, "lexical")
    if allow_semantic:
        intent, score = _semantic(text)
        if intent:
            return Detection(intent, score, "semantic")
    return None
