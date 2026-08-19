from __future__ import annotations

from pathlib import Path

import pytest

from fundermatch.precedent.corpus import load_cases
from fundermatch.precedent.schema import DecidedLoanCase
from fundermatch.rules.config import load_policies
from fundermatch.rules.schema import BorrowerApplication, FunderPolicy

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def synthetic_cases() -> tuple[DecidedLoanCase, ...]:
    return load_cases(ROOT / "data/synthetic_decided_loans.jsonl")


@pytest.fixture
def aligned_precedent(synthetic_cases: tuple[DecidedLoanCase, ...]) -> DecidedLoanCase:
    return synthetic_cases[0]


@pytest.fixture(scope="session")
def funder_policies() -> tuple[FunderPolicy, ...]:
    return load_policies(ROOT / "configs/funder_policies.yaml")


def _application(
    precedent: DecidedLoanCase,
    *,
    application_id: str,
    requested_amount: int | None = None,
    context: str = "Routine review context",
) -> BorrowerApplication:
    profile = precedent.profile
    if requested_amount is not None:
        profile = profile.model_copy(update={"requested_amount_crore": requested_amount})
    return BorrowerApplication(
        application_id=application_id,
        borrower_name=f"Applicant based on {precedent.case_id}",
        industry=precedent.industry,
        region=precedent.region,
        profile=profile,
        evidence=precedent.evidence,
        finance_context=context,
        operations_context=context,
    )


@pytest.fixture
def aligned_application(aligned_precedent: DecidedLoanCase) -> BorrowerApplication:
    return _application(aligned_precedent, application_id="APP-ALIGNED")


@pytest.fixture
def similar_but_hard_rule_ineligible(
    aligned_precedent: DecidedLoanCase,
) -> BorrowerApplication:
    return _application(
        aligned_precedent,
        application_id="APP-HARD-RULE",
        requested_amount=80,
    )


@pytest.fixture
def no_close_precedent(aligned_precedent: DecidedLoanCase) -> BorrowerApplication:
    return _application(
        aligned_precedent,
        application_id="APP-NO-CLOSE",
        context="NO_CLOSE_VECTOR deliberately outside the synthetic precedent cluster",
    )
