"""Dependency-free BM25 ranking over skill metadata."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
from typing import Iterable

from .catalog import Skill


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", re.IGNORECASE)


@dataclass(frozen=True)
class SearchResult:
    skill: Skill
    score: float


def _tokens(text: str) -> list[str]:
    values: list[str] = []
    for match in _TOKEN_RE.findall(text.casefold()):
        values.append(match)
        values.extend(part for part in re.split(r"[-_]", match) if part != match)
    return values


def _document_tokens(skill: Skill) -> list[str]:
    """Weight names and path labels above descriptions."""

    name_tokens = _tokens(skill.name) * 3
    path_tokens = _tokens(str(skill.path.parent))
    return name_tokens + path_tokens + _tokens(skill.description)


def search_skills(
    skills: Iterable[Skill],
    query: str,
    *,
    limit: int = 3,
    min_score: float = 0.0,
) -> list[SearchResult]:
    """Return best-first metadata matches without reading extra files."""

    if limit < 1:
        raise ValueError("limit must be positive")
    query_terms = _tokens(query)
    if not query_terms:
        return []

    documents = [(skill, _document_tokens(skill)) for skill in skills]
    if not documents:
        return []

    document_frequency = Counter(
        term for _, terms in documents for term in set(terms)
    )
    average_length = sum(len(terms) for _, terms in documents) / len(documents)
    ranked: list[SearchResult] = []
    for skill, terms in documents:
        counts = Counter(terms)
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_frequency = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
            length_factor = 1 - 0.75 + 0.75 * len(terms) / max(average_length, 1)
            score += inverse_frequency * (frequency * (1.2 + 1) / (frequency + 1.2 * length_factor))
        if score >= min_score and score > 0:
            ranked.append(SearchResult(skill=skill, score=score))

    ranked.sort(key=lambda result: (-result.score, result.skill.skill_id.casefold()))
    return ranked[:limit]

