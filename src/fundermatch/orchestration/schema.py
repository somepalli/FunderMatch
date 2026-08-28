"""Typed, compact state allowed inside LangGraph checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_CHECKPOINT_BYTES = 256 * 1024


class GraphStatus(StrEnum):
    RUNNING = "running"
    FAILED_RETRYABLE = "failed_retryable"
    NEEDS_ATTENTION = "needs_attention"
    WAITING_FOR_REVIEW = "waiting_for_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED_TERMINAL = "failed_terminal"

    @property
    def terminal(self) -> bool:
        return self in {
            GraphStatus.COMPLETED,
            GraphStatus.CANCELLED,
            GraphStatus.FAILED_TERMINAL,
        }


class WorkerName(StrEnum):
    DOCUMENT_PROCESSING = "document_processing"
    FINANCIAL_ANALYSIS = "financial_analysis"
    ELIGIBILITY = "eligibility"
    PRECEDENT_RETRIEVAL = "precedent_retrieval"
    SUGGESTION = "suggestion"
    GUARDRAILS = "guardrails"
    HUMAN_REVIEW = "human_review"


DEFAULT_WORKER_ORDER = tuple(WorkerName)


class InputReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_type: str = Field(min_length=1, max_length=80)
    reference_id: str = Field(min_length=1, max_length=500)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DocumentReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    chunk_count: int = Field(ge=0)
    config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bbox coordinates must be ordered")
        return self


class EvidenceReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=500)
    metric: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=500)
    unit: str | None = Field(default=None, max_length=80)
    period: str | None = Field(default=None, max_length=120)
    document_id: str = Field(min_length=1, max_length=500)
    page_number: int = Field(ge=1)
    bbox: BoundingBox


class EligibilityReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    funder_id: str = Field(min_length=1, max_length=200)
    eligible: bool
    failed_criteria: tuple[str, ...] = Field(default=(), max_length=50)


class GuardrailResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    guardrail: str = Field(min_length=1, max_length=120)
    passed: bool
    code: str | None = Field(default=None, max_length=120)


class HumanReviewCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_workflow_version: int = Field(ge=0)
    allowed_actions: tuple[str, ...] = Field(max_length=10)


class WorkerCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    worker: WorkerName
    command_id: UUID


class WriteReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    store: str = Field(min_length=1, max_length=80)
    record_id: str = Field(min_length=1, max_length=500)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkerReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    worker: WorkerName
    command_id: UUID
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkerError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    worker: WorkerName
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkerResult(BaseModel):
    """Compact worker output; scratch messages and source payloads are intentionally absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    worker: WorkerName
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    documents: tuple[DocumentReference, ...] | None = Field(default=None, max_length=100)
    evidence: tuple[EvidenceReference, ...] | None = Field(default=None, max_length=200)
    eligibility: tuple[EligibilityReference, ...] | None = Field(default=None, max_length=500)
    guardrails: tuple[GuardrailResult, ...] | None = Field(default=None, max_length=100)
    human_review: HumanReviewCheckpoint | None = None
    workflow_state: str | None = Field(default=None, max_length=80)
    workflow_version: int | None = Field(default=None, ge=0)
    config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_revision: str | None = Field(default=None, max_length=200)
    write_receipts: tuple[WriteReceipt, ...] = Field(default=(), max_length=50)


class ApplicationMemoryState(BaseModel):
    """The complete allow-list for durable operational graph memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    application_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,199}$")
    job_id: str | None = Field(default=None, max_length=200)
    status: GraphStatus = GraphStatus.RUNNING
    current_node: WorkerName
    completed_workers: tuple[WorkerName, ...] = Field(default=(), max_length=20)
    pending_workers: tuple[WorkerName, ...] = Field(max_length=20)
    attempts: dict[WorkerName, int] = Field(default_factory=dict)
    input_references: tuple[InputReference, ...] = Field(default=(), max_length=100)
    documents: tuple[DocumentReference, ...] = Field(default=(), max_length=100)
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=200)
    eligibility: tuple[EligibilityReference, ...] = Field(default=(), max_length=500)
    guardrails: tuple[GuardrailResult, ...] = Field(default=(), max_length=100)
    human_review: HumanReviewCheckpoint | None = None
    workflow_state: str | None = Field(default=None, max_length=80)
    workflow_version: int | None = Field(default=None, ge=0)
    config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_revision: str | None = Field(default=None, max_length=200)
    commands: tuple[WorkerCommand, ...] = Field(max_length=20)
    worker_receipts: tuple[WorkerReceipt, ...] = Field(default=(), max_length=20)
    write_receipts: tuple[WriteReceipt, ...] = Field(default=(), max_length=100)
    last_error: WorkerError | None = None

    @classmethod
    def initial(
        cls,
        application_id: str,
        *,
        worker_order: tuple[WorkerName, ...] = DEFAULT_WORKER_ORDER,
        job_id: str | None = None,
        input_references: tuple[InputReference, ...] = (),
    ) -> Self:
        if not worker_order:
            raise ValueError("at least one worker is required")
        commands = tuple(
            WorkerCommand(
                worker=worker,
                command_id=uuid5(
                    NAMESPACE_URL,
                    f"fundermatch:{application_id}:{worker.value}",
                ),
            )
            for worker in worker_order
        )
        return cls(
            application_id=application_id,
            job_id=job_id,
            current_node=worker_order[0],
            pending_workers=worker_order,
            input_references=input_references,
            commands=commands,
        )

    def command_id_for(self, worker: WorkerName) -> UUID:
        return next(command.command_id for command in self.commands if command.worker == worker)

    @model_validator(mode="after")
    def validate_compact_state(self) -> Self:
        if len(set(self.completed_workers)) != len(self.completed_workers):
            raise ValueError("completed_workers cannot contain duplicates")
        if len(set(self.pending_workers)) != len(self.pending_workers):
            raise ValueError("pending_workers cannot contain duplicates")
        if set(self.completed_workers).intersection(self.pending_workers):
            raise ValueError("completed and pending workers must be disjoint")
        command_workers = tuple(command.worker for command in self.commands)
        if len(set(command_workers)) != len(command_workers):
            raise ValueError("commands must contain one entry per worker")
        if self.current_node not in command_workers:
            raise ValueError("current_node must have a stable command id")
        if any(value < 0 for value in self.attempts.values()):
            raise ValueError("worker attempts cannot be negative")
        if len(self.model_dump_json().encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            raise ValueError("checkpoint state exceeds the 256 KiB safety limit")
        return self


def result_hash(worker: WorkerName, *references: str) -> str:
    canonical = "|".join((worker.value, *references))
    return sha256(canonical.encode("utf-8")).hexdigest()
