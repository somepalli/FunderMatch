from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from langgraph.checkpoint.memory import InMemorySaver

from fundermatch.orchestration.graph import (
    ApplicationMemoryGraph,
    WorkerContext,
)
from fundermatch.orchestration.lifecycle import InMemoryLifecycleStore
from fundermatch.orchestration.observability import (
    AgentSpan,
    CompositeAgentSpanRecorder,
    JsonlAgentSpanRecorder,
    assert_trace_is_content_safe,
)
from fundermatch.orchestration.schema import (
    ApplicationMemoryState,
    GraphStatus,
    WorkerName,
    WorkerResult,
    result_hash,
)
from fundermatch.orchestration.supervisor import SupervisorRoutingConfig
from fundermatch.prompts import identify_prompt, prompt_config_hash


@dataclass
class CapturingRecorder:
    events: list[AgentSpan] = field(default_factory=list)

    async def record(self, event: AgentSpan) -> None:
        self.events.append(event)


class FailingRecorder:
    async def record(self, event: AgentSpan) -> None:
        del event
        raise RuntimeError("telemetry unavailable")


@dataclass
class SafeWorker:
    name: WorkerName

    async def run(self, state: ApplicationMemoryState, context: WorkerContext) -> WorkerResult:
        context.consume_tool_call()
        return WorkerResult(
            worker=self.name,
            output_sha256=result_hash(self.name, state.application_id),
        )


def test_every_worker_and_supervisor_emit_content_safe_spans(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        capture = CapturingRecorder()
        local = JsonlAgentSpanRecorder(tmp_path / "agent.jsonl")
        recorder = CompositeAgentSpanRecorder(FailingRecorder(), capture, local)
        graph = ApplicationMemoryGraph(
            workers=(
                SafeWorker(WorkerName.DOCUMENT_PROCESSING),
                SafeWorker(WorkerName.FINANCIAL_ANALYSIS),
            ),
            checkpointer=InMemorySaver(),
            lifecycle=InMemoryLifecycleStore(),
            recorder=recorder,
        )
        state = await graph.start("APP-TRACE-001", job_id="intake-trace-001")
        assert state.status == GraphStatus.WAITING_FOR_REVIEW
        assert [event.event for event in capture.events].count("worker_attempt") == 2
        assert [event.event for event in capture.events].count("supervisor_run") == 1
        lines = (tmp_path / "agent.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        for line in lines:
            payload = json.loads(line)
            assert_trace_is_content_safe(payload)
            serialized = line.casefold()
            for forbidden in (
                "borrower_name",
                "prompt_body",
                "messages",
                "checkpoint_state",
            ):
                assert forbidden not in serialized

    asyncio.run(scenario())


def test_prompt_identity_is_content_safe_and_config_bound(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prompt_path = tmp_path / "sendback_supervisor_system.txt"
    prompt_path.write_text("Return only the typed routing result.", encoding="utf-8")
    config = SupervisorRoutingConfig(prompt_path=prompt_path)
    identity = identify_prompt(prompt_path)
    event = AgentSpan(
        application_id="APP-PROMPT-001",
        run_id="run-1",
        thread_id="APP-PROMPT-001",
        attempt=1,
        latency_ms=1,
        status=GraphStatus.RUNNING,
        config_hash=prompt_config_hash(config, identity),
        prompt_template_id=identity.template_id,
        prompt_version=identity.version,
        prompt_sha256=identity.sha256,
        event="supervisor_route",
    )
    serialized = event.model_dump_json()
    assert identity.version == identity.sha256[:12]
    assert "Return only the typed routing result." not in serialized

    prompt_path.write_text("Return a revised typed routing result.", encoding="utf-8")
    revised = identify_prompt(prompt_path)
    assert revised.sha256 != identity.sha256
    assert prompt_config_hash(config, revised) != event.config_hash
