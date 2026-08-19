"""Typed Phase 2 policy and rule-result contracts."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fundermatch.precedent.schema import FinancialProfile


class RuleCriterion(StrEnum):
    INDUSTRY = "industry"
    REGION = "region"
    REQUESTED_AMOUNT = "requested_amount_crore"
    DSCR = "dscr"
    DEBT_TO_EBITDA = "debt_to_ebitda"
    COLLATERAL_COVER = "collateral_cover"
    OPERATING_HISTORY = "years_operating"


class BorrowerApplication(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    application_id: str = Field(min_length=1)
    borrower_name: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    region: str = Field(min_length=1)
    profile: FinancialProfile
    finance_context: str = Field(min_length=1, max_length=2000)
    operations_context: str = Field(min_length=1, max_length=2000)

    def profile_text(self) -> str:
        profile = self.profile
        return (
            f"Industry: {self.industry}. Region: {self.region}. "
            f"Revenue INR {profile.annual_revenue_crore} crore. "
            f"Requested amount INR {profile.requested_amount_crore} crore. "
            f"EBITDA margin {profile.ebitda_margin_pct} percent. DSCR {profile.dscr}. "
            f"Debt to EBITDA {profile.debt_to_ebitda}. "
            f"Collateral cover {profile.collateral_cover}. "
            f"Operating history {profile.years_operating} years."
        )

    def context_text(self) -> str:
        return (
            f"Finance review context: {self.finance_context} "
            f"Operations review context: {self.operations_context}"
        )


class FunderPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    funder_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    allowed_industries: frozenset[str] = Field(min_length=1)
    allowed_regions: frozenset[str] = Field(min_length=1)
    min_requested_amount_crore: Decimal = Field(ge=0)
    max_requested_amount_crore: Decimal = Field(gt=0)
    min_dscr: Decimal = Field(gt=0)
    max_debt_to_ebitda: Decimal = Field(ge=0)
    min_collateral_cover: Decimal = Field(ge=0)
    min_years_operating: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_amount_range(self) -> FunderPolicy:
        if self.min_requested_amount_crore > self.max_requested_amount_crore:
            raise ValueError("minimum requested amount cannot exceed maximum")
        return self


class PolicySet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policies: tuple[FunderPolicy, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_funders(self) -> PolicySet:
        identifiers = [policy.funder_id for policy in self.policies]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("funder policies contain duplicate funder_id values")
        return self


class RuleCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: RuleCriterion
    passed: bool
    actual: str
    requirement: str


class FunderEligibility(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    funder_id: str
    eligible: bool
    checks: tuple[RuleCheck, ...] = Field(min_length=7, max_length=7)
