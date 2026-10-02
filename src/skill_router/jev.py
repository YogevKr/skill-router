"""Optional Jev ranking after local skill search."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from time import monotonic
from typing import Literal, Protocol

from .catalog import Skill
from .search import SearchResult, search_skills


RecommendationStatus = Literal["route", "no_tool", "fallback"]


@dataclass(frozen=True)
class JevMetrics:
    """Metadata from one Jev request."""

    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    model: str | None


@dataclass(frozen=True)
class Recommendation:
    """A routing result that does not grant permission or load a skill."""

    status: RecommendationStatus
    provider: str
    skill: Skill | None
    confidence: float | None
    candidates: tuple[SearchResult, ...] = ()
    error: str | None = None
    metrics: JevMetrics | None = None


class SystemOneClient(Protocol):
    """The small client surface used by :class:`JevProvider`."""

    def system_one(self, state: object, questions: object, **kwargs: object) -> object:
        ...


def recommend_local(
    skills: list[Skill],
    query: str,
    *,
    limit: int = 3,
) -> Recommendation:
    """Return the top local match, or an explicit no-match result."""

    results = tuple(result for result in search_skills(skills, query, limit=limit))
    if not results:
        return Recommendation(
            status="no_tool",
            provider="local",
            skill=None,
            confidence=None,
            candidates=results,
        )
    return Recommendation(
        status="route",
        provider="local",
        skill=results[0].skill,
        confidence=None,
        candidates=results,
    )


class JevProvider:
    """Use Jev to choose from a bounded local shortlist.

    The SDK stays optional. The provider falls back to local search when Jev is
    unavailable, times out, or returns an invalid result.
    """

    def __init__(
        self,
        *,
        model: str = "jev-latest",
        timeout: float = 5.0,
        shortlist_limit: int = 8,
        confidence_threshold: float = 0.30,
        fit_threshold: float = 0.30,
        client: SystemOneClient | None = None,
    ) -> None:
        if shortlist_limit < 1 or shortlist_limit > 254:
            raise ValueError("shortlist_limit must be between 1 and 254")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        for name, value in (
            ("confidence_threshold", confidence_threshold),
            ("fit_threshold", fit_threshold),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        self.model = model
        self.timeout = timeout
        self.shortlist_limit = shortlist_limit
        self.confidence_threshold = confidence_threshold
        self.fit_threshold = fit_threshold
        self._client = client

    def recommend(
        self,
        skills: list[Skill],
        query: str,
        *,
        limit: int = 3,
    ) -> Recommendation:
        """Return a Jev recommendation with a safe local fallback."""

        local = recommend_local(skills, query, limit=limit)
        shortlist = tuple(
            search_skills(skills, query, limit=self.shortlist_limit)
        )
        if not shortlist:
            return local

        try:
            client = self._client_or_raise()
            started = monotonic()
            response = client.system_one(
                state={
                    "request": query,
                    "candidates": [
                        {
                            "id": result.skill.skill_id,
                            "name": result.skill.name,
                            "description": result.skill.description,
                        }
                        for result in shortlist
                    ],
                },
                questions=self._questions(shortlist),
                model=self.model,
            )
            metrics = self._metrics(response, started)
            return self._interpret(response, shortlist, local, metrics)
        except Exception as error:  # Jev is an optional accelerator.
            return replace(
                local,
                status="fallback",
                provider="local",
                error=self._error_text(error),
            )

    def _client_or_raise(self) -> SystemOneClient:
        if self._client is not None:
            return self._client
        if not os.environ.get("TYPESAFE_API_KEY", "").strip():
            raise RuntimeError("TYPESAFE_API_KEY is not configured")
        try:
            from typesafe_sdk import RetryPolicy, TypeSafeClient
        except ImportError as error:
            raise RuntimeError(
                "install the Jev extra with: pip install 'skill-router[jev]'"
            ) from error
        self._client = TypeSafeClient(
            model=self.model,
            retry=RetryPolicy(max_retries=0),
            timeout=self.timeout,
        )
        return self._client

    @staticmethod
    def _questions(shortlist: tuple[SearchResult, ...]) -> dict[str, object]:
        try:
            from typesafe_sdk import Choice, Noul
        except ImportError:
            return JevProvider._question_dicts(shortlist)

        criteria = {
            result.skill.skill_id: result.skill.description for result in shortlist
        }
        criteria["none"] = "No listed skill directly supports the request."
        questions: dict[str, object] = {
            "which": Choice(
                instructions=(
                    "Which skill, if any, should be loaded for the request? "
                    "Choose none when no candidate directly applies."
                ),
                criteria=criteria,
            )
        }
        for result in shortlist:
            skill_id = result.skill.skill_id
            questions[f"fits::{skill_id}"] = Noul(
                instructions=(
                    f"Does candidate skill '{skill_id}' directly support the user's "
                    "request? Return yes only when its documented purpose fits."
                )
            )
        return questions

    @staticmethod
    def _question_dicts(shortlist: tuple[SearchResult, ...]) -> dict[str, object]:
        """Build the same wire questions for injected clients without the SDK."""

        criteria = {
            result.skill.skill_id: result.skill.description for result in shortlist
        }
        criteria["none"] = "No listed skill directly supports the request."
        questions: dict[str, object] = {
            "which": {
                "type": "choice",
                "instructions": (
                    "Which skill, if any, should be loaded for the request? "
                    "Choose none when no candidate directly applies."
                ),
                "criteria": criteria,
            }
        }
        for result in shortlist:
            questions[f"fits::{result.skill.skill_id}"] = {
                "type": "noul",
                "instructions": (
                    f"Does candidate skill '{result.skill.skill_id}' directly support "
                    "the user's request? Return yes only when its documented purpose fits."
                ),
            }
        return questions

    def _interpret(
        self,
        response: object,
        shortlist: tuple[SearchResult, ...],
        local: Recommendation,
        metrics: JevMetrics,
    ) -> Recommendation:
        choice_answer = self._answer(response, "which", "choices")
        selected_id = self._value(choice_answer, "choice")
        confidence = self._number(choice_answer, "confidence")
        by_id = {result.skill.skill_id: result.skill for result in shortlist}
        if not selected_id or selected_id == "none":
            return Recommendation(
                status="no_tool",
                provider="jev",
                skill=None,
                confidence=confidence,
                candidates=shortlist,
                metrics=metrics,
            )
        if selected_id not in by_id or confidence is None:
            raise ValueError("Jev returned an unknown or incomplete choice")
        if confidence < self.confidence_threshold:
            return replace(
                local,
                status="fallback",
                provider="local",
                error="Jev choice confidence was below the threshold",
                metrics=metrics,
            )

        fit_answer = self._answer(response, f"fits::{selected_id}", "nouls")
        fit = self._number(fit_answer, "noul")
        if fit is None:
            raise ValueError("Jev returned no fit score for its choice")
        if fit < self.fit_threshold:
            return Recommendation(
                status="no_tool",
                provider="jev",
                skill=None,
                confidence=fit,
                candidates=shortlist,
                metrics=metrics,
            )
        return Recommendation(
            status="route",
            provider="jev",
            skill=by_id[selected_id],
            confidence=confidence,
            candidates=shortlist,
            metrics=metrics,
        )

    @staticmethod
    def _answer(response: object, key: str, group: str) -> object:
        answers = getattr(response, group, None)
        if isinstance(answers, dict) and key in answers:
            return answers[key]
        answers = getattr(response, "answers", None)
        if isinstance(answers, dict) and key in answers:
            answer = answers[key]
            return getattr(answer, "root", answer)
        raise ValueError(f"Jev response omitted answer: {key}")

    @staticmethod
    def _value(answer: object, name: str) -> str | None:
        value = getattr(answer, name, None)
        return value if isinstance(value, str) else None

    @staticmethod
    def _number(answer: object, name: str) -> float | None:
        value = getattr(answer, name, None)
        return float(value) if isinstance(value, (int, float)) else None

    def _metrics(self, response: object, started: float) -> JevMetrics:
        usage = getattr(response, "usage", None)
        return JevMetrics(
            latency_ms=round((monotonic() - started) * 1000),
            input_tokens=self._optional_int(usage, "input_tokens"),
            output_tokens=self._optional_int(usage, "output_tokens"),
            model=self._value(response, "model"),
        )

    @staticmethod
    def _optional_int(value: object, name: str) -> int | None:
        result = getattr(value, name, None)
        return result if isinstance(result, int) else None

    @staticmethod
    def _error_text(error: Exception) -> str:
        message = str(error).strip()
        return f"{type(error).__name__}: {message}" if message else type(error).__name__
