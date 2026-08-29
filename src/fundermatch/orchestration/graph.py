"""Checkpointed supervisor for bounded deterministic or ReAct-style workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Protocol, TypedDict
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from fundermatch.orchestration.lifecycle import MemoryLifecycleStore
from fundermatch.orchestration.observability import (
    AgentSpan,
    AgentSpanRecorder,
    NoopAgentSpanRecorder,
)
from fundermatch.orchestration.schema import (
    ApplicationMemoryState,
    GraphStatus,
    InputReference,
    WorkerError,
    WorkerName,
    WorkerReceipt,
    WorkerResult,
    WriteReceipt,
)
from fundermatch.security.policy import WorkerExecutionPolicy
from fundermatch.security.receipts import ReceiptSigner


class _GraphState(TypedDict, total=False):
    application_id: str
    job_id: str | None
    status: str
    current_node: str
    completed_workers: list[str]
    pending_workers: list[str]
    attempts: dict[str, int]
    input_references: list[dict[str, object]]
    documents: list[dict[str, object]]
    evidence: list[dict[str, object]]
    eligibility: list[dict[str, object]]
    guardrails: list[dict[str, object]]
    human_review: dict[str, object] | None
    workflow_state: str | None
    workflow_version: int | None
    config_hash: str | None
    model_revision: str | None
    commands: list[dict[str, object]]
    worker_receipts: list[dict[str, object]]
    write_receipts: list[dict[str, object]]
    last_error: dict[str, object] | None


class WorkerFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        needs_attention: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.needs_attention = needs_attention


@dataclass(slots=True)
class WorkerContext:
    """Ephemeral worker budget; it is never included in checkpoint state."""

    application_id: str
    command_id: UUID
    max_tool_calls: int = 8
    permitted_tools: frozenset[str] | None = None
    call_timeout_seconds: float | None = None
    _tool_calls: int = field(default=0, init=False, repr=False)
    _tools: list[str] = field(default_factory=list, init=False, repr=False)

    @property
    def tool_calls_used(self) -> int:
        return self._tool_calls

    @property
    def tools_used(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def consume_tool_call(self, tool_name: str = "legacy_tool") -> None:
        if self.permitted_tools is not None and tool_name not in self.permitted_tools:
            raise WorkerFailure(
                "tool_not_permitted",
                "worker attempted a tool outside its production policy",
                retryable=False,
            )
        self._tool_calls += 1
        self._tools.append(tool_name)
        if self._tool_calls > self.max_tool_calls:
            raise WorkerFailure(
                "tool_budget_exceeded",
                "worker exceeded its bounded tool-call budget",
                retryable=False,
            )

    async def execute(self, tool_name: str, operation):  # type: ignore[no-untyped-def]
        self.consume_tool_call(tool_name)
        if self.call_timeout_seconds is None:
            return await operation
        try:
            return await asyncio.wait_for(operation, timeout=self.call_timeout_seconds)
        except TimeoutError as error:
            raise WorkerFailure(
                "tool_timeout",
                "worker tool exceeded its production timeout",
                retryable=True,
            ) from error


class ApplicationWorker(Protocol):
    name: WorkerName

    async def run(
        self,
        state: ApplicationMemoryState,
        context: WorkerContext,
    ) -> WorkerResult: ...


class ApplicationMemoryGraph:
    """Runs one bounded worker per durable super-step and resumes by application ID."""

    def __init__(
        self,
        *,
        workers: tuple[ApplicationWorker, ...],
        checkpointer: BaseCheckpointSaver,
        lifecycle: MemoryLifecycleStore,
        max_tool_calls: int = 8,
        recorder: AgentSpanRecorder | None = None,
        activity: Callable[..., Awaitable[None]] | None = None,
        execution_policies: dict[str, WorkerExecutionPolicy] | None = None,
        policy_hash: str | None = None,
        receipt_signer: ReceiptSigner | None = None,
        retention_cleanup: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        if not workers:
            raise ValueError("at least one application worker is required")
        order = tuple(worker.name for worker in workers)
        if len(set(order)) != len(order):
            raise ValueError("application worker names must be unique")
        self.worker_order = order
        self.workers = {worker.name: worker for worker in workers}
        self.checkpointer = checkpointer
        self.lifecycle = lifecycle
        self.max_tool_calls = max_tool_calls
        self.recorder = recorder or NoopAgentSpanRecorder()
        self.activity = activity
        self.execution_policies = execution_policies or {}
        self.policy_hash = policy_hash
        self.receipt_signer = receipt_signer
        self.retention_cleanup = retention_cleanup

        builder = StateGraph(_GraphState)
        builder.add_node("run_worker", self._run_worker)
        builder.add_edge(START, "run_worker")
        builder.add_conditional_edges(
            "run_worker",
            self._route,
            {"continue": "run_worker", "stop": END},
        )
        self.graph = builder.compile(checkpointer=checkpointer)

    async def start(
        self,
        application_id: str,
        *,
        job_id: str | None = None,
        input_references: tuple[InputReference, ...] = (),
    ) -> ApplicationMemoryState:
        existing = await self._state_or_none(application_id)
        if existing is not None:
            return existing
        initial = ApplicationMemoryState.initial(
            application_id,
            worker_order=self.worker_order,
            job_id=job_id,
            input_references=input_references,
        )
        await self.lifecycle.create(application_id, initial.current_node)
        return await self._invoke(initial.model_dump(mode="json"))

    async def resume(self, application_id: str) -> ApplicationMemoryState:
        current = await self.state(application_id)
        if current.status not in {
            GraphStatus.FAILED_RETRYABLE,
            GraphStatus.NEEDS_ATTENTION,
        }:
            raise ValueError(f"memory thread cannot resume from {current.status.value}")
        policy = self.execution_policies.get(current.current_node.value)
        if policy is not None and current.status == GraphStatus.FAILED_RETRYABLE:
            completed_attempts = current.attempts.get(current.current_node, 0)
            if completed_attempts >= policy.max_attempts:
                return await self.mark_terminal(application_id, GraphStatus.FAILED_TERMINAL)
            await asyncio.sleep(policy.backoff_seconds * (2 ** max(0, completed_attempts - 1)))
        return await self._invoke(
            {
                "application_id": application_id,
                "status": GraphStatus.RUNNING.value,
                "last_error": None,
            }
        )

    async def state(self, application_id: str) -> ApplicationMemoryState:
        current = await self._state_or_none(application_id)
        if current is None:
            raise KeyError(f"memory thread {application_id!r} not found")
        if current.application_id != application_id:
            raise RuntimeError("checkpoint application isolation violation")
        return current

    async def mark_terminal(
        self,
        application_id: str,
        status: GraphStatus,
    ) -> ApplicationMemoryState:
        if not status.terminal:
            raise ValueError("mark_terminal requires a terminal graph status")
        current = await self.state(application_id)
        config = self._config(application_id)
        await self.graph.aupdate_state(
            config,
            {"status": status.value, "last_error": None},
        )
        updated = current.model_copy(update={"status": status, "last_error": None})
        await self.lifecycle.set_status(
            application_id,
            status,
            current_node=updated.current_node,
        )
        return updated

    async def complete_after_writeback(
        self, application_id: str, receipt: WriteReceipt
    ) -> ApplicationMemoryState:
        current = await self.state(application_id)
        if current.status == GraphStatus.COMPLETED:
            return current
        await self.graph.aupdate_state(
            self._config(application_id),
            {
                "status": GraphStatus.COMPLETED.value,
                "write_receipts": [
                    *(item.model_dump(mode="json") for item in current.write_receipts),
                    receipt.model_dump(mode="json"),
                ],
                "last_error": None,
            },
        )
        updated = current.model_copy(
            update={
                "status": GraphStatus.COMPLETED,
                "write_receipts": (*current.write_receipts, receipt),
                "last_error": None,
            }
        )
        await self.lifecycle.set_status(
            application_id,
            GraphStatus.COMPLETED,
            current_node=updated.current_node,
        )
        return updated

    async def mark_needs_attention(
        self, application_id: str, *, code: str
    ) -> ApplicationMemoryState:
        current = await self.state(application_id)
        error = WorkerError(
            worker=current.current_node,
            code=code,
            message="Supervisor routing requires an explicit reviewer-selected stage",
            retryable=False,
        )
        await self.graph.aupdate_state(
            self._config(application_id),
            {
                "status": GraphStatus.NEEDS_ATTENTION.value,
                "last_error": error.model_dump(mode="json"),
            },
        )
        updated = current.model_copy(
            update={"status": GraphStatus.NEEDS_ATTENTION, "last_error": error}
        )
        await self.lifecycle.set_status(
            application_id,
            GraphStatus.NEEDS_ATTENTION,
            current_node=updated.current_node,
            last_error=error,
        )
        return updated

    async def mark_retryable(
        self, application_id: str, *, code: str, message: str
    ) -> ApplicationMemoryState:
        current = await self.state(application_id)
        error = WorkerError(
            worker=current.current_node,
            code=code,
            message=message,
            retryable=True,
        )
        await self.graph.aupdate_state(
            self._config(application_id),
            {
                "status": GraphStatus.FAILED_RETRYABLE.value,
                "last_error": error.model_dump(mode="json"),
            },
        )
        updated = current.model_copy(
            update={"status": GraphStatus.FAILED_RETRYABLE, "last_error": error}
        )
        await self.lifecycle.set_status(
            application_id,
            GraphStatus.FAILED_RETRYABLE,
            current_node=updated.current_node,
            last_error=error,
        )
        return updated

    async def rewind(self, application_id: str, worker: WorkerName) -> ApplicationMemoryState:
        if worker == WorkerName.HUMAN_REVIEW:
            raise ValueError("send-back cannot skip guardrail validation")
        if worker not in self.worker_order:
            raise ValueError("restart worker is not part of this graph")
        current = await self.state(application_id)
        if current.status not in {
            GraphStatus.WAITING_FOR_REVIEW,
            GraphStatus.NEEDS_ATTENTION,
        }:
            raise ValueError(f"cannot send back from {current.status.value}")
        index = self.worker_order.index(worker)
        completed = self.worker_order[:index]
        pending = self.worker_order[index:]
        changes: dict[str, object] = {
            "status": GraphStatus.RUNNING.value,
            "current_node": worker.value,
            "completed_workers": [item.value for item in completed],
            "pending_workers": [item.value for item in pending],
            "worker_receipts": [
                item.model_dump(mode="json")
                for item in current.worker_receipts
                if item.worker in completed
            ],
            "human_review": None,
            "last_error": None,
        }
        if index <= self.worker_order.index(WorkerName.DOCUMENT_PROCESSING):
            changes["documents"] = []
        if index <= self.worker_order.index(WorkerName.FINANCIAL_ANALYSIS):
            changes["evidence"] = []
        if index <= self.worker_order.index(WorkerName.ELIGIBILITY):
            changes["eligibility"] = []
        if index <= self.worker_order.index(WorkerName.GUARDRAILS):
            changes["guardrails"] = []
        await self.graph.aupdate_state(self._config(application_id), changes)
        await self.lifecycle.set_status(application_id, GraphStatus.RUNNING, current_node=worker)
        return await self._invoke(
            {
                "application_id": application_id,
                "status": GraphStatus.RUNNING.value,
                "current_node": worker.value,
            }
        )

    async def flag_stale(self, before: datetime) -> tuple[str, ...]:
        return await self.lifecycle.flag_stale(before)

    async def cleanup_expired(self, now: datetime | None = None) -> tuple[str, ...]:
        expired = await self.lifecycle.expired(now)
        for application_id in expired:
            if self.retention_cleanup is not None:
                await self.retention_cleanup(application_id)
            await self.checkpointer.adelete_thread(application_id)
            await self.lifecycle.delete(application_id)
        return expired

    async def _invoke(self, values: dict[str, object]) -> ApplicationMemoryState:
        application_id = str(values["application_id"])
        started = perf_counter()
        run_id = uuid4().hex
        result = await self.graph.ainvoke(values, self._config(application_id))
        state = ApplicationMemoryState.model_validate(result)
        checkpoint_id = await self.checkpoint_id(application_id)
        if state.application_id != application_id:
            raise RuntimeError("worker attempted to cross an application boundary")
        await self.lifecycle.set_status(
            application_id,
            state.status,
            current_node=state.current_node,
            last_error=state.last_error,
        )
        await self.recorder.record(
            AgentSpan(
                application_id=state.application_id,
                job_id=state.job_id,
                run_id=run_id,
                thread_id=state.application_id,
                checkpoint_id=checkpoint_id,
                attempt=sum(state.attempts.values()),
                latency_ms=(perf_counter() - started) * 1000,
                status=state.status,
                model_revision=state.model_revision,
                config_hash=state.config_hash,
                document_count=len(state.documents),
                evidence_count=len(state.evidence),
                candidate_count=sum(item.eligible for item in state.eligibility),
                guardrail_code=next(
                    (item.code for item in state.guardrails if not item.passed), None
                ),
                error_code=state.last_error.code if state.last_error else None,
                event="supervisor_run",
            )
        )
        return state

    async def _run_worker(self, raw_state: _GraphState) -> _GraphState:
        state = ApplicationMemoryState.model_validate(raw_state)
        started = perf_counter()
        worker_name = state.current_node
        attempts = dict(state.attempts)
        attempts[worker_name] = attempts.get(worker_name, 0) + 1
        policy = self.execution_policies.get(worker_name.value)
        context = WorkerContext(
            application_id=state.application_id,
            command_id=state.command_id_for(worker_name),
            max_tool_calls=policy.max_calls if policy else self.max_tool_calls,
            permitted_tools=frozenset(policy.permitted_tools) if policy else None,
            call_timeout_seconds=policy.call_timeout_seconds if policy else None,
        )
        await self._notify_activity(
            state.application_id,
            worker_name.value,
            worker=worker_name.value,
            attempt=attempts[worker_name],
        )
        try:
            if policy is not None and attempts[worker_name] > policy.max_attempts:
                raise WorkerFailure(
                    "worker_attempt_limit_exceeded",
                    "worker exceeded its production attempt limit",
                    retryable=False,
                )
            operation = self.workers[worker_name].run(state, context)
            result = (
                await asyncio.wait_for(operation, timeout=policy.worker_deadline_seconds)
                if policy
                else await operation
            )
            if result.worker != worker_name:
                raise WorkerFailure(
                    "worker_identity_mismatch",
                    "worker returned a result for a different stage",
                    retryable=False,
                )
        except WorkerFailure as error:
            status = (
                GraphStatus.NEEDS_ATTENTION
                if error.needs_attention
                else GraphStatus.FAILED_RETRYABLE
                if error.retryable
                else GraphStatus.FAILED_TERMINAL
            )
            update = {
                **state.model_dump(mode="json"),
                "attempts": {key.value: value for key, value in attempts.items()},
                "status": status.value,
                "last_error": WorkerError(
                    worker=worker_name,
                    code=error.code,
                    message=error.safe_message,
                    retryable=error.retryable,
                ).model_dump(mode="json"),
            }
            await self._record_worker_span(
                state, worker_name, attempts[worker_name], status, started, error.code
            )
            await self._notify_activity(
                state.application_id,
                "worker_failed",
                worker=worker_name.value,
                attempt=attempts[worker_name],
                error_code=error.code,
                retryable=error.retryable,
                guardrail_code=error.code if worker_name == WorkerName.GUARDRAILS else None,
            )
            return update
        except TimeoutError:
            update = {
                **state.model_dump(mode="json"),
                "attempts": {key.value: value for key, value in attempts.items()},
                "status": GraphStatus.FAILED_RETRYABLE.value,
                "last_error": WorkerError(
                    worker=worker_name,
                    code="worker_deadline_exceeded",
                    message="worker exceeded its production deadline",
                    retryable=True,
                ).model_dump(mode="json"),
            }
            return update
        except Exception:
            update = {
                **state.model_dump(mode="json"),
                "attempts": {key.value: value for key, value in attempts.items()},
                "status": GraphStatus.FAILED_RETRYABLE.value,
                "last_error": WorkerError(
                    worker=worker_name,
                    code="worker_failed",
                    message="worker failed without exposing private error details",
                    retryable=True,
                ).model_dump(mode="json"),
            }
            await self._record_worker_span(
                state,
                worker_name,
                attempts[worker_name],
                GraphStatus.FAILED_RETRYABLE,
                started,
                "worker_failed",
            )
            await self._notify_activity(
                state.application_id,
                "worker_failed",
                worker=worker_name.value,
                attempt=attempts[worker_name],
                error_code="worker_failed",
                retryable=True,
            )
            return update

        update = self._success_update(
            state, result, attempts, context, (perf_counter() - started) * 1000
        )
        updated_state = ApplicationMemoryState.model_validate(update)
        await self._record_worker_span(
            updated_state,
            worker_name,
            attempts[worker_name],
            updated_state.status,
            started,
            None,
        )
        await self._notify_activity(
            state.application_id,
            "worker_checkpoint",
            worker=worker_name.value,
            attempt=attempts[worker_name],
        )
        return updated_state.model_dump(mode="json")

    async def _notify_activity(self, application_id: str, stage: str, **details: object) -> None:
        if self.activity is None:
            return
        try:
            await self.activity(application_id, stage, **details)
        except Exception:
            return

    async def _record_worker_span(
        self,
        state: ApplicationMemoryState,
        worker: WorkerName,
        attempt: int,
        status: GraphStatus,
        started: float,
        error_code: str | None,
        *,
        checkpoint_id: str | None = None,
    ) -> None:
        await self.recorder.record(
            AgentSpan(
                application_id=state.application_id,
                job_id=state.job_id,
                run_id=str(state.command_id_for(worker)),
                thread_id=state.application_id,
                checkpoint_id=checkpoint_id,
                worker=worker,
                attempt=attempt,
                latency_ms=(perf_counter() - started) * 1000,
                status=status,
                model_revision=state.model_revision,
                config_hash=state.config_hash,
                document_count=len(state.documents),
                evidence_count=len(state.evidence),
                candidate_count=sum(item.eligible for item in state.eligibility),
                guardrail_code=(error_code if worker == WorkerName.GUARDRAILS else None),
                error_code=error_code,
                event="worker_attempt",
            )
        )

    def _success_update(
        self,
        state: ApplicationMemoryState,
        result: WorkerResult,
        attempts: dict[WorkerName, int],
        context: WorkerContext,
        latency_ms: float,
    ) -> dict[str, object]:
        completed = (*state.completed_workers, result.worker)
        pending = tuple(worker for worker in self.worker_order if worker not in completed)
        current = pending[0] if pending else result.worker
        status = GraphStatus.RUNNING if pending else GraphStatus.WAITING_FOR_REVIEW
        values = state.model_dump(mode="json")
        receipt = WorkerReceipt(
            worker=result.worker,
            command_id=state.command_id_for(result.worker),
            output_sha256=result.output_sha256,
            attempt=attempts[result.worker],
            tool_calls=context.tools_used,
            latency_ms=latency_ms,
            policy_hash=self.policy_hash,
        )
        if self.receipt_signer is not None:
            receipt = receipt.model_copy(
                update={"signature": self.receipt_signer.sign(receipt.signed_payload())}
            )
        values.update(
            {
                "status": status.value,
                "current_node": current.value,
                "completed_workers": [worker.value for worker in completed],
                "pending_workers": [worker.value for worker in pending],
                "attempts": {key.value: value for key, value in attempts.items()},
                "worker_receipts": [
                    *values["worker_receipts"],
                    receipt.model_dump(mode="json"),
                ],
                "write_receipts": [
                    *values["write_receipts"],
                    *(item.model_dump(mode="json") for item in result.write_receipts),
                ],
                "last_error": None,
            }
        )
        for field_name in (
            "documents",
            "evidence",
            "eligibility",
            "guardrails",
            "human_review",
            "workflow_state",
            "workflow_version",
            "config_hash",
            "model_revision",
        ):
            value = getattr(result, field_name)
            if value is not None:
                values[field_name] = (
                    [item.model_dump(mode="json") for item in value]
                    if isinstance(value, tuple)
                    else value.model_dump(mode="json")
                    if hasattr(value, "model_dump")
                    else value
                )
        return values

    @staticmethod
    def _route(state: _GraphState) -> str:
        return "continue" if state.get("status") == GraphStatus.RUNNING.value else "stop"

    async def _state_or_none(self, application_id: str) -> ApplicationMemoryState | None:
        snapshot = await self.graph.aget_state(self._config(application_id))
        if not snapshot.values:
            return None
        return ApplicationMemoryState.model_validate(snapshot.values)

    async def checkpoint_id(self, application_id: str) -> str | None:
        snapshot = await self.graph.aget_state(self._config(application_id))
        configurable = snapshot.config.get("configurable", {})
        value = configurable.get("checkpoint_id")
        return str(value) if value is not None else None

    @staticmethod
    def _config(application_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": application_id}}
