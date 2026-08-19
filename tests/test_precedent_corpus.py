from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient

from fundermatch.precedent.schema import DecidedLoanCase, DecisionOutcome
from fundermatch.precedent.store import (
    COMMENTS_VECTOR,
    PROFILE_VECTOR,
    QdrantPrecedentConfig,
    QdrantPrecedentStore,
)


class StubEmbedder:
    vector_size = 4

    def embed_profiles(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]:
        return [[1.0, float(index), 0.0, 0.5] for index, _ in enumerate(cases)]

    def embed_comments(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]:
        return [[0.0, 0.5, float(index), 1.0] for index, _ in enumerate(cases)]


def test_corpus_contains_twenty_unique_invented_cases(
    synthetic_cases: tuple[DecidedLoanCase, ...],
) -> None:
    assert len(synthetic_cases) == 20
    assert len({case.case_id for case in synthetic_cases}) == 20
    assert all(case.case_id.startswith("SYN-") for case in synthetic_cases)
    assert all(
        metric.citation.document_id.startswith("synthetic-")
        for case in synthetic_cases
        for metric in case.evidence
    )
    assert {case.decision.outcome for case in synthetic_cases} == set(DecisionOutcome)


def test_phase_two_scenarios_are_explicit_fixtures(
    aligned_precedent: DecidedLoanCase,
    similar_but_hard_rule_ineligible: DecidedLoanCase,
    no_close_precedent: DecidedLoanCase,
) -> None:
    assert aligned_precedent.industry == similar_but_hard_rule_ineligible.industry
    assert (
        similar_but_hard_rule_ineligible.profile.requested_amount_crore
        > similar_but_hard_rule_ineligible.profile.annual_revenue_crore
    )
    assert no_close_precedent.industry != aligned_precedent.industry


def test_qdrant_uses_profile_and_comments_named_vectors(
    synthetic_cases: tuple[DecidedLoanCase, ...],
) -> None:
    client = QdrantClient(location=":memory:")
    config = QdrantPrecedentConfig(collection="phase1_test")
    stored = QdrantPrecedentStore(config, client=client).seed(
        synthetic_cases, StubEmbedder(), recreate=True
    )

    assert stored == 20
    collection = client.get_collection(config.collection)
    vectors: Any = collection.config.params.vectors
    assert set(vectors) == {PROFILE_VECTOR, COMMENTS_VECTOR}
    assert vectors[PROFILE_VECTOR].size == 4
    assert vectors[COMMENTS_VECTOR].size == 4
    assert client.count(config.collection, exact=True).count == 20

    records, _ = client.scroll(config.collection, limit=20, with_payload=True)
    assert {record.payload["case_id"] for record in records if record.payload} == {
        case.case_id for case in synthetic_cases
    }
