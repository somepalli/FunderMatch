"""Typed, advisory-only Phase 3 suggestion contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fundermatch.clients.findociq_contract import SourceCitation
from fundermatch.matching.schema import PrecedentMatch
from fundermatch.rules.schema import BorrowerApplication, RuleCheck


class SimilarityFactor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str = Field(min_length=1)
    application_value: str = Field(min_length=1)
    precedent_value: str = Field(min_length=1)
    observation: str = Field(min_length=1)


class ExplainedPrecedent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    match: PrecedentMatch
    factors: tuple[SimilarityFactor, ...] = Field(min_length=6)


class AdvisoryCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    funder_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    passed_checks: tuple[RuleCheck, ...] = Field(min_length=8, max_length=8)
    precedents: tuple[ExplainedPrecedent, ...]
    evidence_summary: str = Field(min_length=1, max_length=2000)
    no_close_precedent: bool

    @model_validator(mode="after")
    def validate_candidate(self) -> AdvisoryCandidate:
        if not all(check.passed for check in self.passed_checks):
            raise ValueError("advisory candidates may contain passed rule checks only")
        if self.no_close_precedent == bool(self.precedents):
            raise ValueError("no_close_precedent must be the inverse of precedent presence")
        if any(
            item.match.precedent.decision.funder_id != self.funder_id
            for item in self.precedents
        ):
            raise ValueError("candidate precedents must belong to the same eligible funder")
        return self


class ExcludedFunder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    funder_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    failed_checks: tuple[RuleCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_failed_checks(self) -> ExcludedFunder:
        if any(check.passed for check in self.failed_checks):
            raise ValueError("excluded funders may contain failed rule checks only")
        return self


class GroundedClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    application_id: str
    claim_type: Literal["evidence", "calculation", "precedent"]
    text: str = Field(min_length=1, max_length=1000)
    citation: SourceCitation | None = None
    calculation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    precedent_id: str | None = None
    policy_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_grounding(self) -> GroundedClaim:
        sources = (self.citation is not None, self.calculation_sha256 is not None,
                   self.precedent_id is not None)
        if sum(sources) != 1:
            raise ValueError("claim must resolve to exactly one grounding source")
        return self


class SuggestionBundle(BaseModel):
    """Evidence for a human reviewer; deliberately contains no decision field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authority: Literal["advisory_only"] = "advisory_only"
    requires_human_decision: Literal[True] = True
    advisory_notice: str = Field(min_length=1)
    application: BorrowerApplication
    candidates: tuple[AdvisoryCandidate, ...]
    excluded_funders: tuple[ExcludedFunder, ...]
    claims: tuple[GroundedClaim, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def validate_partition(self) -> SuggestionBundle:
        candidate_ids = [candidate.funder_id for candidate in self.candidates]
        excluded_ids = [funder.funder_id for funder in self.excluded_funders]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("suggestion contains duplicate candidate funders")
        if len(excluded_ids) != len(set(excluded_ids)):
            raise ValueError("suggestion contains duplicate excluded funders")
        if set(candidate_ids) & set(excluded_ids):
            raise ValueError("a funder cannot be both candidate and excluded")
        return self
