"""Typed responses for eligible-only precedent retrieval."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from fundermatch.precedent.schema import DecidedLoanCase
from fundermatch.rules.schema import FunderEligibility


class PrecedentMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    precedent: DecidedLoanCase
    score: float = Field(ge=-1, le=1)
    profile_score: float | None = Field(default=None, ge=-1, le=1)
    comments_score: float | None = Field(default=None, ge=-1, le=1)


class RuleGatedRetrievalResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    application_id: str
    eligibility: tuple[FunderEligibility, ...]
    matches: tuple[PrecedentMatch, ...]
