from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from fundermatch.matching.schema import PrecedentMatch, RuleGatedRetrievalResult
from fundermatch.precedent.schema import DecidedLoanCase
from fundermatch.rules.engine import EligibilityEngine
from fundermatch.rules.schema import BorrowerApplication, FunderPolicy
from fundermatch.suggest.assembler import SuggestionAssembler
from fundermatch.suggest.narrative import (
    GemmaNarrativeClient,
    GemmaNarrativeConfig,
    GeneratedNarrative,
)

ROOT = Path(__file__).parents[1]


def _retrieval(
    application: BorrowerApplication,
    policies: tuple[FunderPolicy, ...],
    cases: tuple[DecidedLoanCase, ...],
) -> RuleGatedRetrievalResult:
    by_funder = {case.decision.funder_id: case for case in cases}
    return RuleGatedRetrievalResult(
        application_id=application.application_id,
        eligibility=EligibilityEngine().evaluate_all(application, policies),
        matches=(
            PrecedentMatch(
                precedent=by_funder["northstar-capital"],
                score=0.90,
                profile_score=0.98,
                comments_score=0.66,
            ),
            PrecedentMatch(
                precedent=by_funder["harborline-credit"],
                score=0.82,
                profile_score=0.88,
                comments_score=0.64,
            ),
        ),
    )


def _bundle(
    application: BorrowerApplication,
    policies: tuple[FunderPolicy, ...],
    cases: tuple[DecidedLoanCase, ...],
):  # type: ignore[no-untyped-def]
    return SuggestionAssembler().assemble(
        application, policies, _retrieval(application, policies, cases)
    )


def test_bundle_is_advisory_only_and_preserves_click_to_source_evidence(
    aligned_application: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
    synthetic_cases: tuple[DecidedLoanCase, ...],
) -> None:
    bundle = _bundle(aligned_application, funder_policies, synthetic_cases)

    assert bundle.authority == "advisory_only"
    assert bundle.requires_human_decision is True
    assert "decision" not in bundle.model_dump(exclude={"application", "candidates"})
    assert len(bundle.application.evidence) == 3
    assert bundle.application.evidence[0].citation.page_number >= 1
    assert bundle.application.evidence[0].citation.bbox.x1 > 0

    assert {candidate.funder_id for candidate in bundle.candidates} == {
        "northstar-capital",
        "harborline-credit",
    }
    assert {item.funder_id for item in bundle.excluded_funders} == {
        "meridian-growth-finance",
        "cobalt-infrastructure-fund",
    }
    northstar = next(
        candidate for candidate in bundle.candidates if candidate.funder_id == "northstar-capital"
    )
    assert len(northstar.passed_checks) == 8
    assert northstar.precedents[0].match.precedent.comments
    assert northstar.precedents[0].match.precedent.evidence[0].citation.bbox.x0 >= 0
    assert len(northstar.precedents[0].factors) == 11
    assert {factor.metric for factor in northstar.precedents[0].factors} >= {
        "sub_industry",
        "loan_type",
        "pat_crore",
        "debt_to_equity",
    }


def test_no_close_precedent_is_visible_without_inventing_evidence(
    no_close_precedent: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    retrieval = RuleGatedRetrievalResult(
        application_id=no_close_precedent.application_id,
        eligibility=EligibilityEngine().evaluate_all(no_close_precedent, funder_policies),
        matches=(),
    )
    bundle = SuggestionAssembler().assemble(
        no_close_precedent, funder_policies, retrieval
    )

    assert bundle.candidates
    assert all(candidate.no_close_precedent for candidate in bundle.candidates)
    assert all(candidate.precedents == () for candidate in bundle.candidates)
    assert all("no precedent" in candidate.evidence_summary for candidate in bundle.candidates)


def test_assembler_rejects_an_ineligible_funder_precedent(
    similar_but_hard_rule_ineligible: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
    synthetic_cases: tuple[DecidedLoanCase, ...],
) -> None:
    northstar_case = next(
        case for case in synthetic_cases if case.decision.funder_id == "northstar-capital"
    )
    retrieval = RuleGatedRetrievalResult(
        application_id=similar_but_hard_rule_ineligible.application_id,
        eligibility=EligibilityEngine().evaluate_all(
            similar_but_hard_rule_ineligible, funder_policies
        ),
        matches=(PrecedentMatch(precedent=northstar_case, score=0.99),),
    )

    with pytest.raises(ValueError, match="ineligible funder"):
        SuggestionAssembler().assemble(
            similar_but_hard_rule_ineligible, funder_policies, retrieval
        )


def test_gemma_narrative_uses_pinned_zero_temperature_contract(
    aligned_application: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
    synthetic_cases: tuple[DecidedLoanCase, ...],
) -> None:
    candidate = _bundle(
        aligned_application, funder_policies, synthetic_cases
    ).candidates[0]

    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert request.url.path == "/v1/chat/completions"
            assert payload["model"] == "google/gemma-3-12b-it"
            assert payload["temperature"] == 0.0
            assert payload["seed"] == 17
            assert "Never recommend" in payload["messages"][0]["content"]
            assert payload["response_format"]["type"] == "json_schema"
            facts = json.loads(payload["messages"][1]["content"])
            assert facts["funder_id"] == candidate.funder_id
            assert len(facts["precedents"]) <= 2
            assert "citation" not in payload["messages"][1]["content"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "summary": "Two eligible synthetic precedents are shown.",
                                        "similarities": ["Revenue scale is comparable."],
                                        "differences": ["Reviewer context differs."],
                                        "caveat": "Advisory evidence; a human decides.",
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://vllm:8900/v1"
        ) as http_client:
            client = GemmaNarrativeClient(
                GemmaNarrativeConfig(
                    base_url="http://vllm:8900/v1",
                    prompt_path=ROOT / "prompts/suggestion_narrative_system.txt",
                ),
                http_client=http_client,
            )
            run = await client.explain(candidate)
        assert run.temperature == 0.0
        assert run.revision == "3b0c67b98eee8fb90633ef1bfbf3d39f43b9cf9d"
        assert "human decides" in run.output.caveat

    asyncio.run(scenario())


def test_generated_narrative_cannot_exercise_decision_authority() -> None:
    with pytest.raises(ValidationError, match="decision authority"):
        GeneratedNarrative(
            summary="The reviewer should approve this application.",
            similarities=("The profiles are similar.",),
            differences=("The regions differ.",),
            caveat="Human review is pending.",
        )
