"""Live Postgres smoke for the complete Phase 4 transition and audit path."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import asyncpg

from fundermatch.workflow.postgres import PostgresWorkflowRepository
from fundermatch.workflow.schema import (
    ActorClaims,
    ActorRole,
    HumanAction,
    HumanDecisionCommand,
    PipelineAdvanceCommand,
    WorkflowState,
)
from fundermatch.workflow.service import WorkflowService


async def run() -> None:
    dsn = os.environ["FUNDERMATCH_DATABASE_URL"]
    migration = Path(__file__).resolve().parents[3] / "migrations" / "001_hitl_workflow.sql"
    await PostgresWorkflowRepository.migrate(dsn, migration)
    pipeline = ActorClaims(
        actor_id="phase4-smoke-pipeline",
        display_name="Phase 4 Smoke Pipeline",
        roles={ActorRole.PIPELINE},
    )
    reviewer = ActorClaims(
        actor_id="phase4-smoke-reviewer",
        display_name="Phase 4 Smoke Reviewer",
        roles={ActorRole.HUMAN_REVIEWER},
    )
    application_id = f"phase4-smoke-{uuid4()}"
    async with PostgresWorkflowRepository.connect(dsn) as repository:
        service = WorkflowService(repository)
        await service.create(application_id, pipeline)
        for version, target in enumerate(
            (
                WorkflowState.EXTRACTED,
                WorkflowState.RULE_GATED,
                WorkflowState.AI_SUGGESTED,
                WorkflowState.AWAITING_HUMAN,
            )
        ):
            await service.advance_pipeline(
                application_id,
                PipelineAdvanceCommand(
                    expected_version=version,
                    target_state=target,
                    reason=f"Smoke entered {target.value}",
                    suggestion={"authority": "advisory_only"}
                    if target == WorkflowState.AI_SUGGESTED
                    else None,
                ),
                pipeline,
            )
        result = await service.decide(
            application_id,
            HumanDecisionCommand(
                expected_version=4,
                action=HumanAction.APPROVE_WITH_CONDITIONS,
                reason="Human reviewed synthetic smoke evidence",
                conditions=("Quarterly monitoring",),
            ),
            reviewer,
        )
        events = await service.audit(application_id)
        if result.workflow.state != WorkflowState.HUMAN_DECIDED or len(events) != 6:
            raise RuntimeError("live workflow did not reach the expected durable state")

    connection = await asyncpg.connect(dsn)
    try:
        try:
            await connection.execute(
                "UPDATE workflow_audit SET reason = reason WHERE application_id = $1",
                application_id,
            )
        except asyncpg.RaiseError:
            pass
        else:
            raise RuntimeError("append-only audit trigger allowed an update")
    finally:
        await connection.close()
    print(
        f"application_id={application_id} state={result.workflow.state.value} "
        f"version={result.workflow.version} audit_events={len(events)} "
        "audit_append_only=true"
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
