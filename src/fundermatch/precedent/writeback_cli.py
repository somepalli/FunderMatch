"""Live Phase 5 smoke: decide case one, then retrieve it for case two."""

import argparse
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from fundermatch.matching.retriever import RetrievalConfig, RuleGatedPrecedentRetriever
from fundermatch.matching.schema import RuleGatedRetrievalResult
from fundermatch.precedent.corpus import load_cases
from fundermatch.precedent.embedder import BgeM3Config, BgeM3Embedder
from fundermatch.precedent.store import QdrantPrecedentConfig, QdrantPrecedentStore
from fundermatch.precedent.writeback import PrecedentWritebackService
from fundermatch.rules.config import load_policies
from fundermatch.rules.engine import EligibilityEngine
from fundermatch.rules.schema import BorrowerApplication
from fundermatch.suggest.assembler import SuggestionAssembler
from fundermatch.workflow.postgres import PostgresWorkflowRepository
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


async def run(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[3]
    collection = args.collection or f"fundermatch_phase5_smoke_{uuid4().hex[:8]}"
    for migration in sorted((root / "migrations").glob("*.sql")):
        await PostgresWorkflowRepository.migrate(args.database_url, migration)
    source = load_cases(root / "data" / "synthetic_decided_loans.jsonl")[0]
    application_id = f"APP-PHASE5-{uuid4().hex[:12].upper()}"
    application = BorrowerApplication(
        application_id=application_id,
        borrower_name=f"Phase 5 applicant based on {source.case_id}",
        industry=source.industry,
        region=source.region,
        profile=source.profile,
        evidence=source.evidence,
        finance_context="Synthetic finance review supports a monitored facility.",
        operations_context="Synthetic operations review confirms stable execution.",
    )
    policies = load_policies(root / "configs" / "funder_policies.yaml")
    eligibility = EligibilityEngine().evaluate_all(application, policies)
    suggestion = SuggestionAssembler().assemble(
        application,
        policies,
        RuleGatedRetrievalResult(
            application_id=application_id,
            eligibility=eligibility,
            matches=(),
        ),
    )
    if not suggestion.candidates:
        raise RuntimeError("synthetic smoke application has no eligible funder")
    selected_funder = suggestion.candidates[0].funder_id
    pipeline = ActorClaims(
        actor_id="phase5-smoke-pipeline",
        display_name="Phase 5 Smoke Pipeline",
        roles={ActorRole.PIPELINE},
    )
    reviewer = ActorClaims(
        actor_id="phase5-smoke-reviewer",
        display_name="Phase 5 Smoke Reviewer",
        roles={ActorRole.HUMAN_REVIEWER},
    )
    embedder = BgeM3Embedder(
        BgeM3Config(snapshot_dir=Path(args.model_dir) if args.model_dir else None)
    )
    store = QdrantPrecedentStore(
        QdrantPrecedentConfig(url=args.qdrant_url, collection=collection)
    )
    async with PostgresWorkflowRepository.connect(args.database_url) as repository:
        workflow = WorkflowService(repository)
        await workflow.create(application_id, pipeline)
        for version, target in enumerate(
            (
                WorkflowState.EXTRACTED,
                WorkflowState.RULE_GATED,
                WorkflowState.AI_SUGGESTED,
                WorkflowState.AWAITING_HUMAN,
            )
        ):
            await workflow.advance_pipeline(
                application_id,
                PipelineAdvanceCommand(
                    expected_version=version,
                    target_state=target,
                    reason=f"Phase 5 smoke entered {target.value}",
                    suggestion=suggestion.model_dump(mode="json")
                    if target == WorkflowState.AI_SUGGESTED
                    else None,
                ),
                pipeline,
            )
        await workflow.decide(
            application_id,
            HumanDecisionCommand(
                expected_version=4,
                action=HumanAction.APPROVE_WITH_CONDITIONS,
                funder_id=selected_funder,
                reason="Human reviewed the synthetic evidence",
                conditions=("Quarterly monitoring",),
            ),
            reviewer,
        )
        write_result = await PrecedentWritebackService(
            workflow, store, embedder
        ).write(
            application_id,
            PrecedentWriteCommand(
                expected_version=5,
                reason="Confirmed Qdrant precedent payload",
            ),
            pipeline,
        )

    case_two = application.model_copy(
        update={
            "application_id": f"APP-PHASE5-NEXT-{uuid4().hex[:8].upper()}",
            "borrower_name": "Phase 5 next synthetic applicant",
        }
    )
    retrieval = await asyncio.to_thread(
        RuleGatedPrecedentRetriever(
            client=store.client,
            embedder=embedder,
            config=RetrievalConfig(collection=collection, min_score=0.60),
        ).retrieve,
        case_two,
        policies,
    )
    if not retrieval.matches or retrieval.matches[0].precedent.case_id != application_id:
        raise RuntimeError("case two did not retrieve the newly decided case one")
    print(
        f"case_one={application_id} state="
        f"{write_result.transition.workflow.state.value} "
        f"case_two={case_two.application_id} retrieved={retrieval.matches[0].precedent.case_id} "
        f"score={retrieval.matches[0].score:.4f} collection={collection}"
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--database-url",
        default=os.getenv("FUNDERMATCH_DATABASE_URL"),
        required=os.getenv("FUNDERMATCH_DATABASE_URL") is None,
    )
    value.add_argument("--qdrant-url", default="http://127.0.0.1:6999")
    value.add_argument(
        "--collection",
        help="Qdrant collection; omitted uses an isolated phase-5 smoke collection",
    )
    value.add_argument("--model-dir")
    return value


def main() -> None:
    asyncio.run(run(parser().parse_args()))
