import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fundermatch.workflow.errors import (
    InvalidTransitionError,
    WorkflowAuthorizationError,
    WorkflowConflictError,
)
from fundermatch.workflow.repository import InMemoryWorkflowRepository
from fundermatch.workflow.schema import (
    ActorClaims,
    ActorRole,
    HumanAction,
    HumanDecisionCommand,
    PipelineAdvanceCommand,
    WorkflowState,
)
from fundermatch.workflow.service import WorkflowService

PIPELINE = ActorClaims(
    actor_id="pipeline-1", display_name="Matching Pipeline", roles={ActorRole.PIPELINE}
)
REVIEWER = ActorClaims(
    actor_id="reviewer-7", display_name="Riya Reviewer", roles={ActorRole.HUMAN_REVIEWER}
)


async def awaiting_human() -> tuple[WorkflowService, str]:
    service = WorkflowService(InMemoryWorkflowRepository())
    application_id = f"app-{uuid4()}"
    await service.create(application_id, PIPELINE)
    for version, state in enumerate(
        [
            WorkflowState.EXTRACTED,
            WorkflowState.RULE_GATED,
            WorkflowState.AI_SUGGESTED,
            WorkflowState.AWAITING_HUMAN,
        ]
    ):
        await service.advance_pipeline(
            application_id,
            PipelineAdvanceCommand(
                expected_version=version,
                target_state=state,
                reason=f"entered {state.value}",
                suggestion={"recommendation": "manual review"}
                if state == WorkflowState.AI_SUGGESTED
                else None,
            ),
            PIPELINE,
        )
    return service, application_id


@pytest.mark.parametrize(
    ("action", "conditions"),
    [
        (HumanAction.APPROVE, ()),
        (HumanAction.REJECT, ()),
        (HumanAction.APPROVE_WITH_CONDITIONS, ("Monthly reporting",)),
        (HumanAction.SEND_BACK, ()),
    ],
)
def test_each_human_action_is_audited(action, conditions):
    async def scenario():
        service, application_id = await awaiting_human()
        result = await service.decide(
            application_id,
            HumanDecisionCommand(
                expected_version=4,
                action=action,
                funder_id=None if action == HumanAction.SEND_BACK else "funder-alpha",
                reason="Reviewer assessed the evidence",
                conditions=conditions,
            ),
            REVIEWER,
        )
        assert result.workflow.state == WorkflowState.HUMAN_DECIDED
        assert result.workflow.decision.actor_id == REVIEWER.actor_id
        assert result.audit_event.from_state == WorkflowState.AWAITING_HUMAN
        assert result.audit_event.action == action.value
        sequences = [event.sequence for event in await service.audit(application_id)]
        assert sequences == list(range(1, 7))

    asyncio.run(scenario())


def test_only_human_can_leave_awaiting_human():
    async def scenario():
        service, application_id = await awaiting_human()
        command = HumanDecisionCommand(
            expected_version=4,
            action=HumanAction.APPROVE,
            funder_id="funder-alpha",
            reason="Not a human",
        )
        with pytest.raises(WorkflowAuthorizationError):
            await service.decide(application_id, command, PIPELINE)

    asyncio.run(scenario())


def test_human_cannot_decide_before_awaiting_state():
    async def scenario():
        service = WorkflowService(InMemoryWorkflowRepository())
        await service.create("early", PIPELINE)
        command = HumanDecisionCommand(
            expected_version=0,
            action=HumanAction.REJECT,
            funder_id="funder-alpha",
            reason="Too early",
        )
        with pytest.raises(InvalidTransitionError):
            await service.decide("early", command, REVIEWER)

    asyncio.run(scenario())


def test_stale_version_is_rejected_and_command_is_idempotent():
    async def scenario():
        service, application_id = await awaiting_human()
        command = HumanDecisionCommand(
            expected_version=4,
            action=HumanAction.APPROVE,
            funder_id="funder-alpha",
            reason="Evidence supports approval",
        )
        first = await service.decide(application_id, command, REVIEWER)
        assert await service.decide(application_id, command, REVIEWER) == first
        stale = command.model_copy(update={"command_id": uuid4()})
        with pytest.raises(WorkflowConflictError):
            await service.decide(application_id, stale, REVIEWER)

    asyncio.run(scenario())


def test_conditions_are_action_specific():
    with pytest.raises(ValidationError):
        HumanDecisionCommand(
            expected_version=4,
            action=HumanAction.APPROVE_WITH_CONDITIONS,
            funder_id="funder-alpha",
            reason="Needs controls",
        )
    with pytest.raises(ValidationError):
        HumanDecisionCommand(
            expected_version=4,
            action=HumanAction.REJECT,
            funder_id="funder-alpha",
            reason="Rejected",
            conditions=("Impossible",),
        )


def test_pipeline_cannot_skip_or_cross_human_boundary():
    async def scenario():
        service = WorkflowService(InMemoryWorkflowRepository())
        await service.create("skip", PIPELINE)
        with pytest.raises(InvalidTransitionError):
            await service.advance_pipeline(
                "skip",
                PipelineAdvanceCommand(
                    expected_version=0,
                    target_state=WorkflowState.AI_SUGGESTED,
                    reason="skip",
                    suggestion={"value": 1},
                ),
                PIPELINE,
            )

    asyncio.run(scenario())
