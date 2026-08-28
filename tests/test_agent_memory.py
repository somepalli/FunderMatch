from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from fundermatch.orchestration.graph import (
    ApplicationMemoryGraph,
    WorkerContext,
    WorkerFailure,
)
from fundermatch.orchestration.lifecycle import InMemoryLifecycleStore
from fundermatch.orchestration.postgres import checkpoint_dsn
from fundermatch.orchestration.schema import (
    ApplicationMemoryState,
    GraphStatus,
    InputReference,
    WorkerName,
    WorkerResult,
    result_hash,
)


@dataclass
class FakeWorker:
    name: WorkerName
    fail_once: bool = False
    calls: list[str] = field(default_factory=list)
    command_ids: list[UUID] = field(default_factory=list)

    async def run(
        self,
        state: ApplicationMemoryState,
        context: WorkerContext,
    ) -> WorkerResult:
        self.calls.append(state.application_id)
        self.command_ids.append(context.command_id)
        context.consume_tool_call()
        if self.fail_once and len(self.calls) == 1:
            raise WorkerFailure("temporary_upstream", "upstream is temporarily unavailable")
        return WorkerResult(
            worker=self.name,
            output_sha256=result_hash(self.name, state.application_id),
        )


def run(coroutine):  # type: ignore[no-untyped-def]
    return asyncio.run(coroutine)


def graph_with(*workers: FakeWorker):
    checkpointer = InMemorySaver()
    lifecycle = InMemoryLifecycleStore()
    graph = ApplicationMemoryGraph(
        workers=workers,
        checkpointer=checkpointer,
        lifecycle=lifecycle,
    )
    return graph, checkpointer, lifecycle


def test_retry_resumes_at_failed_worker_without_rerunning_completed_work() -> None:
    async def scenario() -> None:
        document = FakeWorker(WorkerName.DOCUMENT_PROCESSING)
        finance = FakeWorker(WorkerName.FINANCIAL_ANALYSIS, fail_once=True)
        eligibility = FakeWorker(WorkerName.ELIGIBILITY)
        graph, _, _ = graph_with(document, finance, eligibility)

        failed = await graph.start(
            "APP-MEM-001",
            input_references=(
                InputReference(
                    reference_type="staged_pdf",
                    reference_id="borrower.pdf",
                    sha256="a" * 64,
                ),
            ),
        )
        assert failed.status == GraphStatus.FAILED_RETRYABLE
        assert failed.completed_workers == (WorkerName.DOCUMENT_PROCESSING,)
        assert failed.current_node == WorkerName.FINANCIAL_ANALYSIS

        resumed = await graph.resume("APP-MEM-001")
        assert resumed.status == GraphStatus.WAITING_FOR_REVIEW
        assert resumed.last_error is None
        assert document.calls == ["APP-MEM-001"]
        assert finance.calls == ["APP-MEM-001", "APP-MEM-001"]
        assert eligibility.calls == ["APP-MEM-001"]
        assert finance.command_ids[0] == finance.command_ids[1]

    run(scenario())


def test_repeated_start_is_idempotent_and_application_threads_are_isolated() -> None:
    async def scenario() -> None:
        worker = FakeWorker(WorkerName.DOCUMENT_PROCESSING)
        graph, _, _ = graph_with(worker)

        first = await graph.start("APP-MEM-101")
        repeated = await graph.start("APP-MEM-101")
        second = await graph.start("APP-MEM-202")

        assert repeated == first
        assert worker.calls == ["APP-MEM-101", "APP-MEM-202"]
        assert first.application_id == "APP-MEM-101"
        assert second.application_id == "APP-MEM-202"
        assert (await graph.state("APP-MEM-101")).application_id == "APP-MEM-101"

    run(scenario())


def test_checkpoint_schema_rejects_raw_payloads_and_message_history() -> None:
    payload = ApplicationMemoryState.initial("APP-MEM-SAFE").model_dump(mode="json")
    for forbidden, value in (
        ("pdf_base64", "JVBERi0xLjQ="),
        ("chunks", ["borrower private text"]),
        ("messages", [{"role": "assistant", "content": "private reasoning"}]),
    ):
        with pytest.raises(ValidationError):
            ApplicationMemoryState.model_validate({**payload, forbidden: value})


def test_worker_tool_scratchpad_is_bounded_and_not_part_of_state() -> None:
    state = ApplicationMemoryState.initial("APP-MEM-BUDGET")
    context = WorkerContext(
        application_id=state.application_id,
        command_id=state.command_id_for(state.current_node),
        max_tool_calls=2,
    )
    context.consume_tool_call()
    context.consume_tool_call()
    with pytest.raises(WorkerFailure, match="bounded tool-call budget"):
        context.consume_tool_call()
    assert "tool_calls" not in state.model_dump(mode="json")


def test_retention_deletes_only_terminal_threads_after_thirty_days() -> None:
    async def scenario() -> None:
        lifecycle = InMemoryLifecycleStore()
        worker = FakeWorker(WorkerName.DOCUMENT_PROCESSING)
        graph = ApplicationMemoryGraph(
            workers=(worker,),
            checkpointer=InMemorySaver(),
            lifecycle=lifecycle,
        )
        await graph.start("APP-MEM-DONE")
        await graph.start("APP-MEM-WAIT")
        terminal_at = datetime(2026, 1, 1, tzinfo=UTC)
        await lifecycle.set_status(
            "APP-MEM-DONE",
            GraphStatus.COMPLETED,
            current_node=WorkerName.DOCUMENT_PROCESSING,
            now=terminal_at,
        )
        await lifecycle.set_status(
            "APP-MEM-WAIT",
            GraphStatus.WAITING_FOR_REVIEW,
            current_node=WorkerName.DOCUMENT_PROCESSING,
            now=terminal_at,
        )

        before = terminal_at + timedelta(days=29)
        assert await graph.cleanup_expired(before) == ()
        expired = await graph.cleanup_expired(terminal_at + timedelta(days=30))
        assert expired == ("APP-MEM-DONE",)
        with pytest.raises(KeyError):
            await lifecycle.get("APP-MEM-DONE")
        assert (await lifecycle.get("APP-MEM-WAIT")).status == GraphStatus.WAITING_FOR_REVIEW

    run(scenario())


def test_stale_non_terminal_threads_are_flagged_not_deleted() -> None:
    async def scenario() -> None:
        lifecycle = InMemoryLifecycleStore()
        await lifecycle.create("APP-MEM-STALE", WorkerName.DOCUMENT_PROCESSING)
        old = datetime(2026, 1, 1, tzinfo=UTC)
        await lifecycle.set_status(
            "APP-MEM-STALE",
            GraphStatus.FAILED_RETRYABLE,
            current_node=WorkerName.DOCUMENT_PROCESSING,
            now=old,
        )
        flagged = await lifecycle.flag_stale(old + timedelta(days=7))
        assert flagged == ("APP-MEM-STALE",)
        record = await lifecycle.get("APP-MEM-STALE")
        assert record.is_stale is True
        assert await lifecycle.expired(old + timedelta(days=365)) == ()

    run(scenario())


def test_terminal_status_is_checkpointed_and_starts_retention_clock() -> None:
    async def scenario() -> None:
        graph, _, lifecycle = graph_with(FakeWorker(WorkerName.DOCUMENT_PROCESSING))
        await graph.start("APP-MEM-TERMINAL")
        terminal = await graph.mark_terminal("APP-MEM-TERMINAL", GraphStatus.COMPLETED)

        assert terminal.status == GraphStatus.COMPLETED
        assert (await graph.state("APP-MEM-TERMINAL")).status == GraphStatus.COMPLETED
        record = await lifecycle.get("APP-MEM-TERMINAL")
        assert record.terminal_at is not None
        assert record.delete_after == record.terminal_at + timedelta(days=30)

    run(scenario())


def test_postgres_checkpointer_is_forced_into_dedicated_schema() -> None:
    result = checkpoint_dsn("postgresql://user:pass@localhost:7444/fundermatch")
    assert "options=-csearch_path%3Dlanggraph%2Cpublic" in result
    assert result.startswith("postgresql://user:pass@localhost:7444/fundermatch?")
