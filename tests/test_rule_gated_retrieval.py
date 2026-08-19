from __future__ import annotations

from qdrant_client import QdrantClient

from fundermatch.matching.retriever import RetrievalConfig, RuleGatedPrecedentRetriever
from fundermatch.precedent.schema import DecidedLoanCase
from fundermatch.precedent.store import QdrantPrecedentConfig, QdrantPrecedentStore
from fundermatch.rules.engine import EligibilityEngine
from fundermatch.rules.schema import BorrowerApplication, FunderPolicy, RuleCriterion


class ControlledEmbedder:
    vector_size = 4

    def embed_profiles(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]:
        return [self._case_vector(case) for case in cases]

    def embed_comments(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]:
        return [self._case_vector(case) for case in cases]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if any("NO_CLOSE_VECTOR" in text for text in texts):
            return [[0.0, 0.0, 0.0, 1.0] for _ in texts]
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    @staticmethod
    def _case_vector(case: DecidedLoanCase) -> list[float]:
        if case.case_id == "SYN-001":
            return [1.0, 0.0, 0.0, 0.0]
        if case.decision.funder_id == "harborline-credit":
            return [0.9, 0.1, 0.0, 0.0]
        return [0.0, 1.0, 0.2, 0.0]


def _retriever(
    cases: tuple[DecidedLoanCase, ...], *, min_score: float = 0.45
) -> RuleGatedPrecedentRetriever:
    client = QdrantClient(location=":memory:")
    collection = "phase2_test"
    embedder = ControlledEmbedder()
    QdrantPrecedentStore(
        QdrantPrecedentConfig(collection=collection), client=client
    ).seed(cases, embedder, recreate=True)
    return RuleGatedPrecedentRetriever(
        client=client,
        embedder=embedder,
        config=RetrievalConfig(
            collection=collection,
            top_k=5,
            candidate_limit=20,
            min_score=min_score,
        ),
    )


def test_rules_report_every_criterion_before_retrieval(
    aligned_application: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    northstar = next(
        policy for policy in funder_policies if policy.funder_id == "northstar-capital"
    )
    result = EligibilityEngine().evaluate(aligned_application, northstar)

    assert result.eligible
    assert len(result.checks) == 7
    assert {check.criterion for check in result.checks} == set(RuleCriterion)
    assert all(check.passed for check in result.checks)


def test_aligned_application_returns_eligible_precedent(
    synthetic_cases: tuple[DecidedLoanCase, ...],
    aligned_application: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    result = _retriever(synthetic_cases).retrieve(aligned_application, funder_policies)

    assert result.matches
    assert result.matches[0].precedent.case_id == "SYN-001"
    assert result.matches[0].precedent.decision.funder_id == "northstar-capital"


def test_hard_rule_ineligible_funder_is_absent_not_low_ranked(
    synthetic_cases: tuple[DecidedLoanCase, ...],
    similar_but_hard_rule_ineligible: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    result = _retriever(synthetic_cases).retrieve(
        similar_but_hard_rule_ineligible, funder_policies
    )
    northstar = next(
        item for item in result.eligibility if item.funder_id == "northstar-capital"
    )
    harborline = next(
        item for item in result.eligibility if item.funder_id == "harborline-credit"
    )

    assert not northstar.eligible
    assert not next(
        check for check in northstar.checks if check.criterion is RuleCriterion.REQUESTED_AMOUNT
    ).passed
    assert harborline.eligible
    assert result.matches
    assert all(
        match.precedent.decision.funder_id != "northstar-capital"
        for match in result.matches
    )


def test_no_close_precedent_returns_empty_matches(
    synthetic_cases: tuple[DecidedLoanCase, ...],
    no_close_precedent: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    result = _retriever(synthetic_cases, min_score=0.80).retrieve(
        no_close_precedent, funder_policies
    )

    assert any(item.eligible for item in result.eligibility)
    assert result.matches == ()
