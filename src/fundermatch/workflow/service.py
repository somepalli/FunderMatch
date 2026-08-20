"""Authorization and transition invariants for durable human review."""

from uuid import UUID, uuid4

from fundermatch.workflow.errors import (
    InvalidTransitionError,
    WorkflowAuthorizationError,
    WorkflowConflictError,
)
from fundermatch.workflow.repository import WorkflowRepository
from fundermatch.workflow.schema import (
    ActorClaims,
    ActorRole,
    AuditEvent,
    HumanDecisionCommand,
    HumanDecisionRecord,
    PipelineAdvanceCommand,
    TransitionResult,
    WorkflowRecord,
    WorkflowState,
    utc_now,
)

PIPELINE_TRANSITIONS = {
    WorkflowState.INTAKE: WorkflowState.EXTRACTED,
    WorkflowState.EXTRACTED: WorkflowState.RULE_GATED,
    WorkflowState.RULE_GATED: WorkflowState.AI_SUGGESTED,
    WorkflowState.AI_SUGGESTED: WorkflowState.AWAITING_HUMAN,
}


class WorkflowService:
    def __init__(self, repository: WorkflowRepository) -> None:
        self._repository = repository

    async def create(
        self,
        application_id: str,
        actor: ActorClaims,
        *,
        command_id: UUID | None = None,
        reason: str = "Application entered intake",
    ) -> TransitionResult:
        self._require_role(actor, ActorRole.PIPELINE)
        workflow = WorkflowRecord(application_id=application_id)
        event = self._event(
            command_id=command_id or uuid4(),
            workflow=workflow,
            sequence=1,
            actor=actor,
            from_state=None,
            to_state=WorkflowState.INTAKE,
            action="create",
            reason=reason,
            changes={"state": WorkflowState.INTAKE.value},
        )
        return await self._repository.create(workflow, event)

    async def advance_pipeline(
        self,
        application_id: str,
        command: PipelineAdvanceCommand,
        actor: ActorClaims,
    ) -> TransitionResult:
        self._require_role(actor, ActorRole.PIPELINE)

        def transition(current: WorkflowRecord, sequence: int) -> tuple[WorkflowRecord, AuditEvent]:
            self._require_version(current, command.expected_version)
            expected_target = PIPELINE_TRANSITIONS.get(current.state)
            if expected_target != command.target_state:
                raise InvalidTransitionError(
                    f"pipeline cannot transition {current.state.value} "
                    f"to {command.target_state.value}"
                )
            if command.target_state == WorkflowState.AI_SUGGESTED and command.suggestion is None:
                raise InvalidTransitionError("AI_SUGGESTED requires a suggestion payload")
            if (
                command.suggestion is not None
                and command.target_state != WorkflowState.AI_SUGGESTED
            ):
                raise InvalidTransitionError(
                    "suggestion is only accepted when entering AI_SUGGESTED"
                )
            now = utc_now()
            updated = current.model_copy(
                update={
                    "state": command.target_state,
                    "version": current.version + 1,
                    "suggestion": command.suggestion or current.suggestion,
                    "updated_at": now,
                }
            )
            event = self._event(
                command_id=command.command_id,
                workflow=updated,
                sequence=sequence,
                actor=actor,
                from_state=current.state,
                to_state=updated.state,
                action="pipeline_advance",
                reason=command.reason,
                changes={"state": updated.state.value, "version": updated.version},
            )
            return updated, event

        return await self._repository.transition(
            application_id, str(command.command_id), transition
        )

    async def decide(
        self,
        application_id: str,
        command: HumanDecisionCommand,
        actor: ActorClaims,
    ) -> TransitionResult:
        self._require_role(actor, ActorRole.HUMAN_REVIEWER)

        def transition(current: WorkflowRecord, sequence: int) -> tuple[WorkflowRecord, AuditEvent]:
            self._require_version(current, command.expected_version)
            if current.state != WorkflowState.AWAITING_HUMAN:
                raise InvalidTransitionError(
                    f"human decision requires AWAITING_HUMAN, found {current.state.value}"
                )
            now = utc_now()
            decision = HumanDecisionRecord(
                action=command.action,
                reason=command.reason,
                conditions=command.conditions,
                actor_id=actor.actor_id,
                actor_display_name=actor.display_name,
                decided_at=now,
            )
            updated = current.model_copy(
                update={
                    "state": WorkflowState.HUMAN_DECIDED,
                    "version": current.version + 1,
                    "decision": decision,
                    "updated_at": now,
                }
            )
            event = self._event(
                command_id=command.command_id,
                workflow=updated,
                sequence=sequence,
                actor=actor,
                from_state=current.state,
                to_state=updated.state,
                action=command.action.value,
                reason=command.reason,
                changes={
                    "state": updated.state.value,
                    "version": updated.version,
                    "decision": decision.model_dump(mode="json"),
                },
            )
            return updated, event

        return await self._repository.transition(
            application_id, str(command.command_id), transition
        )

    async def get(self, application_id: str) -> WorkflowRecord:
        return await self._repository.get(application_id)

    async def audit(self, application_id: str) -> tuple[AuditEvent, ...]:
        return await self._repository.audit(application_id)

    @staticmethod
    def _require_role(actor: ActorClaims, role: ActorRole) -> None:
        if role not in actor.roles:
            raise WorkflowAuthorizationError(f"{role.value} role required")

    @staticmethod
    def _require_version(current: WorkflowRecord, expected: int) -> None:
        if current.version != expected:
            raise WorkflowConflictError(
                f"expected version {expected}, current version is {current.version}"
            )

    @staticmethod
    def _event(
        *,
        command_id: UUID,
        workflow: WorkflowRecord,
        sequence: int,
        actor: ActorClaims,
        from_state: WorkflowState | None,
        to_state: WorkflowState,
        action: str,
        reason: str,
        changes: dict[str, object],
    ) -> AuditEvent:
        return AuditEvent(
            command_id=command_id,
            application_id=workflow.application_id,
            sequence=sequence,
            actor_id=actor.actor_id,
            actor_display_name=actor.display_name,
            actor_roles=tuple(sorted(actor.roles, key=str)),
            from_state=from_state,
            to_state=to_state,
            action=action,
            reason=reason,
            changes=changes,
        )
