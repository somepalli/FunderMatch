"""Typed boundaries for the Phase 4 human-review workflow."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class WorkflowState(StrEnum):
    INTAKE = "INTAKE"
    EXTRACTED = "EXTRACTED"
    RULE_GATED = "RULE_GATED"
    AI_SUGGESTED = "AI_SUGGESTED"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    HUMAN_DECIDED = "HUMAN_DECIDED"
    PRECEDENT_WRITTEN = "PRECEDENT_WRITTEN"


class ActorRole(StrEnum):
    PIPELINE = "pipeline"
    HUMAN_REVIEWER = "human_reviewer"


class HumanAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    APPROVE_WITH_CONDITIONS = "approve_with_conditions"
    SEND_BACK = "send_back"


class ActorClaims(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    roles: frozenset[ActorRole]


class PipelineAdvanceCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID = Field(default_factory=uuid4)
    expected_version: int = Field(ge=0)
    target_state: WorkflowState
    reason: str = Field(min_length=1, max_length=2000)
    suggestion: dict[str, Any] | None = None


class HumanDecisionCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID = Field(default_factory=uuid4)
    expected_version: int = Field(ge=0)
    action: HumanAction
    reason: str = Field(min_length=1, max_length=2000)
    conditions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_conditions(self) -> "HumanDecisionCommand":
        if self.action == HumanAction.APPROVE_WITH_CONDITIONS and not self.conditions:
            raise ValueError("approve_with_conditions requires at least one condition")
        if self.action != HumanAction.APPROVE_WITH_CONDITIONS and self.conditions:
            raise ValueError("conditions are only valid for approve_with_conditions")
        return self


class HumanDecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: HumanAction
    reason: str
    conditions: tuple[str, ...] = ()
    actor_id: str
    actor_display_name: str
    decided_at: datetime


class WorkflowRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_id: str = Field(min_length=1, max_length=200)
    state: WorkflowState = WorkflowState.INTAKE
    version: int = Field(default=0, ge=0)
    suggestion: dict[str, Any] | None = None
    decision: HumanDecisionRecord | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    audit_id: UUID = Field(default_factory=uuid4)
    command_id: UUID
    application_id: str
    sequence: int = Field(ge=1)
    actor_id: str
    actor_display_name: str
    actor_roles: tuple[ActorRole, ...]
    from_state: WorkflowState | None
    to_state: WorkflowState
    action: str
    reason: str
    changes: dict[str, Any]
    occurred_at: datetime = Field(default_factory=utc_now)


class TransitionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    workflow: WorkflowRecord
    audit_event: AuditEvent
