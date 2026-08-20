import asyncio
from dataclasses import dataclass

import pytest
from qdrant_client import QdrantClient

from fundermatch.matching.retriever import RetrievalConfig, RuleGatedPrecedentRetriever
from fundermatch.matching.schema import RuleGatedRetrievalResult
from fundermatch.precedent.schema import DecidedLoanCase, DecisionOverride
from fundermatch.precedent.store import QdrantPrecedentConfig, QdrantPrecedentStore
from fundermatch.precedent.writeback import PrecedentWritebackService
from fundermatch.rules.engine import EligibilityEngine
from fundermatch.rules.schema import BorrowerApplication, FunderPolicy
from fundermatch.suggest.assembler import SuggestionAssembler
from fundermatch.workflow.errors import InvalidTransitionError
from fundermatch.workflow.repository import InMemoryWorkflowRepository
from fundermatch.workflow.schema import (
    ActorClaims,
    ActorRole,
    HumanAction,
    HumanDecisionCommand,
    PipelineAdvanceCommand,
    PrecedentWriteCommand,
    WorkflowState,
)
from fundermatch.workflow.service import WorkflowService

PIPELINE = ActorClaims(
    actor_id="writeback-pipeline",
    display_name="Write-back Pipeline",
    roles={ActorRole.PIPELINE},
)
REVIEWER = ActorClaims(
    actor_id="writeback-reviewer",
    display_name="Write-back Reviewer",
    roles={ActorRole.HUMAN_REVIEWER},
)


@dataclass
class SemanticStubEmbedder:
    vector_size: int = 4
    write_calls: int = 0

    def embed_profiles(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]:
        self.write_calls += 1
        return [[1.0, 0.0, 0.0, 0.0] for _ in cases]

    def embed_comments(self, cases: tuple[DecidedLoanCase, ...]) -> list[list[float]]:
        return [[0.0, 1.0, 0.0, 0.0] for _ in cases]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        assert len(texts) == 2
        return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]


async def decided_workflow(
    application: BorrowerApplication,
    policies: tuple[FunderPolicy, ...],
    *,
    action: HumanAction = HumanAction.APPROVE_WITH_CONDITIONS,
    funder_id: str | None = "northstar-capital",
    overrides: tuple[DecisionOverride, ...] = (),
) -> WorkflowService:
    eligibility = EligibilityEngine().evaluate_all(application, policies)
    suggestion = SuggestionAssembler().assemble(
        application,
        policies,
        RuleGatedRetrievalResult(
            application_id=application.application_id,
            eligibility=eligibility,
            matches=(),
        ),
    )
    workflow = WorkflowService(InMemoryWorkflowRepository())
    await workflow.create(application.application_id, PIPELINE)
    for version, target in enumerate(
        (
            WorkflowState.EXTRACTED,
            WorkflowState.RULE_GATED,
            WorkflowState.AI_SUGGESTED,
            WorkflowState.AWAITING_HUMAN,
        )
    ):
        await workflow.advance_pipeline(
            application.application_id,
            PipelineAdvanceCommand(
                expected_version=version,
                target_state=target,
                reason=f"entered {target.value}",
                suggestion=suggestion.model_dump(mode="json")
                if target == WorkflowState.AI_SUGGESTED
                else None,
            ),
            PIPELINE,
        )
    await workflow.decide(
        application.application_id,
        HumanDecisionCommand(
            expected_version=4,
            action=action,
            funder_id=funder_id,
            reason="Human approved after reviewing the evidence",
            conditions=("Quarterly monitoring",)
            if action == HumanAction.APPROVE_WITH_CONDITIONS
            else (),
            overrides=overrides,
        ),
        REVIEWER,
    )
    return workflow


def test_decided_case_is_written_once_and_retrieved_for_next_case(
    aligned_application: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    async def scenario() -> None:
        workflow = await decided_workflow(aligned_application, funder_policies)
        client = QdrantClient(location=":memory:")
        embedder = SemanticStubEmbedder()
        store = QdrantPrecedentStore(
            QdrantPrecedentConfig(collection="phase5_loop"), client=client
        )
        service = PrecedentWritebackService(workflow, store, embedder)
        command = PrecedentWriteCommand(
            expected_version=5, reason="Persist confirmed human decision"
        )
        first = await service.write(aligned_application.application_id, command, PIPELINE)
        replay = await service.write(aligned_application.application_id, command, PIPELINE)

        assert first == replay
        assert first.transition.workflow.state == WorkflowState.PRECEDENT_WRITTEN
        assert first.transition.workflow.precedent_receipt.payload_sha256
        assert first.precedent.decision.conditions == ("Quarterly monitoring",)
        assert client.count("phase5_loop", exact=True).count == 1
        assert embedder.write_calls == 1
        assert len(await workflow.audit(aligned_application.application_id)) == 7

        case_two = aligned_application.model_copy(
            update={"application_id": "APP-CASE-2", "borrower_name": "Synthetic Case Two"}
        )
        retrieval = RuleGatedPrecedentRetriever(
            client=client,
            embedder=embedder,
            config=RetrievalConfig(collection="phase5_loop", min_score=0.9),
        ).retrieve(case_two, funder_policies)
        assert retrieval.matches[0].precedent.case_id == aligned_application.application_id
        assert retrieval.matches[0].precedent.decision.funder_id == "northstar-capital"

    asyncio.run(scenario())


def test_send_back_is_not_written_to_precedent_memory(
    aligned_application: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    async def scenario() -> None:
        workflow = await decided_workflow(
            aligned_application,
            funder_policies,
            action=HumanAction.SEND_BACK,
            funder_id=None,
        )
        service = PrecedentWritebackService(
            workflow,
            QdrantPrecedentStore(
                QdrantPrecedentConfig(collection="send_back"),
                client=QdrantClient(location=":memory:"),
            ),
            SemanticStubEmbedder(),
        )
        with pytest.raises(InvalidTransitionError, match="send_back"):
            await service.write(
                aligned_application.application_id,
                PrecedentWriteCommand(expected_version=5, reason="should not write"),
                PIPELINE,
            )
        assert (await workflow.get(aligned_application.application_id)).state == (
            WorkflowState.HUMAN_DECIDED
        )

    asyncio.run(scenario())


def test_excluded_funder_requires_explicit_overrides(
    aligned_application: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    async def scenario() -> None:
        workflow = await decided_workflow(
            aligned_application,
            funder_policies,
            action=HumanAction.REJECT,
            funder_id="meridian-growth-finance",
        )
        service = PrecedentWritebackService(
            workflow,
            QdrantPrecedentStore(
                QdrantPrecedentConfig(collection="override_required"),
                client=QdrantClient(location=":memory:"),
            ),
            SemanticStubEmbedder(),
        )
        with pytest.raises(InvalidTransitionError, match="explicit human overrides"):
            await service.write(
                aligned_application.application_id,
                PrecedentWriteCommand(expected_version=5, reason="write rejected case"),
                PIPELINE,
            )

    asyncio.run(scenario())


def test_documented_rule_overrides_are_embedded_in_human_precedent(
    aligned_application: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    async def scenario() -> None:
        application = aligned_application.model_copy(
            update={"application_id": "APP-OVERRIDE-CASE"}
        )
        eligibility = EligibilityEngine().evaluate_all(application, funder_policies)
        meridian = next(
            item for item in eligibility if item.funder_id == "meridian-growth-finance"
        )
        overrides = tuple(
            DecisionOverride(
                criterion=check.criterion.value,
                original_result=f"{check.actual} versus {check.requirement}",
                justification="Reviewer documented a synthetic exception.",
            )
            for check in meridian.checks
            if not check.passed
        )
        workflow = await decided_workflow(
            application,
            funder_policies,
            action=HumanAction.REJECT,
            funder_id="meridian-growth-finance",
            overrides=overrides,
        )
        result = await PrecedentWritebackService(
            workflow,
            QdrantPrecedentStore(
                QdrantPrecedentConfig(collection="documented_override"),
                client=QdrantClient(location=":memory:"),
            ),
            SemanticStubEmbedder(),
        ).write(
            application.application_id,
            PrecedentWriteCommand(expected_version=5, reason="persist documented override"),
            PIPELINE,
        )
        assert result.precedent.decision.overrides == overrides
        assert "Human policy overrides" in result.precedent.comments_text()
        assert result.transition.workflow.state == WorkflowState.PRECEDENT_WRITTEN

    asyncio.run(scenario())


def test_qdrant_failure_does_not_mark_workflow_written(
    aligned_application: BorrowerApplication,
    funder_policies: tuple[FunderPolicy, ...],
) -> None:
    class FailingStore(QdrantPrecedentStore):
        def write_one(self, case, embedder):  # type: ignore[no-untyped-def]
            raise RuntimeError("qdrant unavailable")

    async def scenario() -> None:
        workflow = await decided_workflow(aligned_application, funder_policies)
        service = PrecedentWritebackService(
            workflow,
            FailingStore(
                QdrantPrecedentConfig(collection="failure"),
                client=QdrantClient(location=":memory:"),
            ),
            SemanticStubEmbedder(),
        )
        with pytest.raises(RuntimeError, match="qdrant unavailable"):
            await service.write(
                aligned_application.application_id,
                PrecedentWriteCommand(expected_version=5, reason="attempt write"),
                PIPELINE,
            )
        assert (await workflow.get(aligned_application.application_id)).state == (
            WorkflowState.HUMAN_DECIDED
        )

    asyncio.run(scenario())
