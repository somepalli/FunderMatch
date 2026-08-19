from __future__ import annotations

from pathlib import Path

import pytest

from fundermatch.precedent.corpus import load_cases
from fundermatch.precedent.schema import DecidedLoanCase

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def synthetic_cases() -> tuple[DecidedLoanCase, ...]:
    return load_cases(ROOT / "data/synthetic_decided_loans.jsonl")


@pytest.fixture
def aligned_precedent(synthetic_cases: tuple[DecidedLoanCase, ...]) -> DecidedLoanCase:
    return synthetic_cases[0]


@pytest.fixture
def similar_but_hard_rule_ineligible(
    aligned_precedent: DecidedLoanCase,
) -> DecidedLoanCase:
    profile = aligned_precedent.profile.model_copy(
        update={"requested_amount_crore": aligned_precedent.profile.annual_revenue_crore * 2}
    )
    return aligned_precedent.model_copy(update={"case_id": "SYN-901", "profile": profile})


@pytest.fixture
def no_close_precedent(synthetic_cases: tuple[DecidedLoanCase, ...]) -> DecidedLoanCase:
    return synthetic_cases[16]
