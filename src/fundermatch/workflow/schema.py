"""Typed boundaries for the Phase 4 human-review workflow."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fundermatch.precedent.schema import DecisionOverride


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


class RestartStage(StrEnum):
    SUPERVISOR = "supervisor"
    DOCUMENT_PROCESSING = "document_processing"
    FINANCIAL_ANALYSIS = "financial_analysis"
    ELIGIBILITY = "eligibility"
    PRECEDENT_RETRIEVAL = "precedent_retrieval"
    SUGGESTION = "suggestion"
    GUARDRAILS = "guardrails"


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
    funder_id: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    conditions: tuple[str, ...] = ()
    overrides: tuple[DecisionOverride, ...] = ()
    restart_stage: RestartStage | None = None

    @model_validator(mode="after")
    def validate_conditions(self) -> "HumanDecisionCommand":
        if self.action == HumanAction.APPROVE_WITH_CONDITIONS and not self.conditions:
            raise ValueError("approve_with_conditions requires at least one condition")
        if self.action != HumanAction.APPROVE_WITH_CONDITIONS and self.conditions:
            raise ValueError("conditions are only valid for approve_with_conditions")
        if self.action == HumanAction.SEND_BACK and self.funder_id is not None:
            raise ValueError("send_back does not select a funder")
        if self.action != HumanAction.SEND_BACK and self.funder_id is None:
            raise ValueError("a decided outcome requires funder_id")
        if self.action != HumanAction.SEND_BACK and self.restart_stage is not None:
            raise ValueError("restart_stage is only valid for send_back")
        return self


class PipelineReopenCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID = Field(default_factory=uuid4)
    expected_version: int = Field(ge=0)
    restart_stage: RestartStage
    reason: str = Field(min_length=1, max_length=2000)


class HumanDecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: HumanAction
    funder_id: str | None = None
    reason: str
    conditions: tuple[str, ...] = ()
    overrides: tuple[DecisionOverride, ...] = ()
    actor_id: str
    actor_display_name: str
    decided_at: datetime


class PrecedentWriteReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    point_id: UUID
    collection: str
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    written_at: datetime = Field(default_factory=utc_now)


class WorkflowRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_id: str = Field(min_length=1, max_length=200)
    state: WorkflowState = WorkflowState.INTAKE
    version: int = Field(default=0, ge=0)
    suggestion: dict[str, Any] | None = None
    decision: HumanDecisionRecord | None = None
    precedent_receipt: PrecedentWriteReceipt | None = None
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


class PrecedentWriteCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID = Field(default_factory=uuid4)
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)
