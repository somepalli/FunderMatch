"""Eligibility-first retrieval across Qdrant's two named vectors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from qdrant_client import QdrantClient, models

from fundermatch.matching.schema import PrecedentMatch, RuleGatedRetrievalResult
from fundermatch.precedent.schema import DecidedLoanCase
from fundermatch.precedent.store import COMMENTS_VECTOR, PROFILE_VECTOR
from fundermatch.rules.engine import EligibilityEngine
from fundermatch.rules.schema import BorrowerApplication, FunderPolicy


class TextEmbedder(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collection: str = "fundermatch_precedents"
    top_k: int = Field(default=5, ge=1, le=50)
    candidate_limit: int = Field(default=25, ge=1, le=200)
    profile_weight: float = Field(default=0.75, ge=0, le=1)
    comments_weight: float = Field(default=0.25, ge=0, le=1)
    min_score: float = Field(default=0.45, ge=-1, le=1)

    @model_validator(mode="after")
    def validate_weights(self) -> RetrievalConfig:
        if abs((self.profile_weight + self.comments_weight) - 1.0) > 1e-9:
            raise ValueError("profile and comments weights must sum to 1")
        if self.candidate_limit < self.top_k:
            raise ValueError("candidate_limit must be at least top_k")
        return self


@dataclass(frozen=True, slots=True)
class _Scores:
    payload: dict[str, object]
    profile: float | None = None
    comments: float | None = None


class RuleGatedPrecedentRetriever:
    def __init__(
        self,
        *,
        client: QdrantClient,
        embedder: TextEmbedder,
        engine: EligibilityEngine | None = None,
        config: RetrievalConfig | None = None,
    ) -> None:
        self.client = client
        self.embedder = embedder
        self.engine = engine or EligibilityEngine()
        self.config = config or RetrievalConfig()

    def retrieve(
        self,
        application: BorrowerApplication,
        policies: tuple[FunderPolicy, ...],
    ) -> RuleGatedRetrievalResult:
        eligibility = self.engine.evaluate_all(application, policies)
        eligible_funders = tuple(
            result.funder_id for result in eligibility if result.eligible
        )
        if not eligible_funders:
            return RuleGatedRetrievalResult(
                application_id=application.application_id,
                eligibility=eligibility,
                matches=(),
            )

        profile_vector, comments_vector = self.embedder.embed_texts(
            [application.profile_text(), application.context_text()]
        )
        funder_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="decision.funder_id",
                    match=models.MatchAny(any=list(eligible_funders)),
                )
            ]
        )
        profile_hits = self.client.query_points(
            collection_name=self.config.collection,
            query=profile_vector,
            using=PROFILE_VECTOR,
            query_filter=funder_filter,
            limit=self.config.candidate_limit,
            with_payload=True,
        ).points
        comment_hits = self.client.query_points(
            collection_name=self.config.collection,
            query=comments_vector,
            using=COMMENTS_VECTOR,
            query_filter=funder_filter,
            limit=self.config.candidate_limit,
            with_payload=True,
        ).points
        scores: dict[str, _Scores] = {}
        for hit in profile_hits:
            if hit.payload is not None:
                scores[str(hit.id)] = _Scores(
                    payload=dict(hit.payload), profile=float(hit.score)
                )
        for hit in comment_hits:
            if hit.payload is None:
                continue
            current = scores.get(str(hit.id))
            scores[str(hit.id)] = _Scores(
                payload=dict(hit.payload),
                profile=current.profile if current else None,
                comments=float(hit.score),
            )

        matches = []
        for item in scores.values():
            combined = (
                self.config.profile_weight * (item.profile or 0.0)
                + self.config.comments_weight * (item.comments or 0.0)
            )
            precedent = DecidedLoanCase.model_validate(item.payload)
            if precedent.decision.funder_id not in eligible_funders:
                raise RuntimeError("Qdrant returned an ineligible funder precedent")
            if combined >= self.config.min_score:
                matches.append(
                    PrecedentMatch(
                        precedent=precedent,
                        score=combined,
                        profile_score=item.profile,
                        comments_score=item.comments,
                    )
                )
        ranked = tuple(
            sorted(matches, key=lambda match: (-match.score, match.precedent.case_id))[
                : self.config.top_k
            ]
        )
        return RuleGatedRetrievalResult(
            application_id=application.application_id,
            eligibility=eligibility,
            matches=ranked,
        )
