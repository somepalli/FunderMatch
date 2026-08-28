from __future__ import annotations

import asyncio
import json
import time

from fastapi.testclient import TestClient

from fundermatch.api.app import create_app
from fundermatch.intake_jobs import InMemoryIntakeJobStore
from fundermatch.workflow.repository import InMemoryWorkflowRepository
from fundermatch.workflow.schema import ActorClaims, ActorRole


class PipelineAuthenticator:
    def authenticate(self, _: str) -> ActorClaims:
        return ActorClaims(
            actor_id="pipeline-test",
            display_name="Pipeline Test",
            roles={ActorRole.PIPELINE},
        )


class LiveFakeIntake:
    async def process(self, metadata, files, actor, *, job_id=None, progress=None):  # type: ignore[no-untyped-def]
        del actor, job_id
        assert files[0][1].startswith(b"%PDF-")
        await progress("parsing_document", "Parsing borrower.pdf", document_name="borrower.pdf")
        await asyncio.sleep(0.01)
        await progress("awaiting_human", "Ready for human review")
        return metadata


def metadata() -> dict[str, object]:
    return {
        "application_id": "APP-LIVE-001",
        "borrower_name": "Synthetic Borrower",
        "industry": "Manufacturing",
        "region": "South",
        "requested_amount_crore": "12",
        "debt_to_ebitda": "1.2",
        "collateral_cover": "2.0",
        "years_operating": 10,
        "employee_count": 200,
        "finance_context": "Invented finance context",
        "operations_context": "Invented operations context",
    }


def test_in_memory_job_store_is_append_only_and_terminal() -> None:
    async def scenario() -> None:
        store = InMemoryIntakeJobStore()
        await store.create("job-1", "APP-1")
        first = await store.append("job-1", "queued", "Queued")
        second = await store.append("job-1", "running", "Running")
        assert (first.sequence, second.sequence) == (1, 2)
        assert [event.stage for event in await store.events_after("job-1", 1)] == ["running"]
        completed = await store.finish("job-1", status="completed")
        assert completed.status == "completed"

    asyncio.run(scenario())


def test_intake_job_returns_immediately_and_exposes_live_timeline() -> None:
    store = InMemoryIntakeJobStore()
    app = create_app(
        InMemoryWorkflowRepository(),
        PipelineAuthenticator(),
        intake_service=LiveFakeIntake(),  # type: ignore[arg-type]
        intake_job_store=store,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/intake-jobs",
            data={"metadata": json.dumps(metadata())},
            files={"files": ("borrower.pdf", b"%PDF-1.7 synthetic", "application/pdf")},
            headers={"Authorization": "Bearer pipeline-token"},
        )
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert accepted["status"] == "queued"

        snapshot = None
        for _ in range(30):
            snapshot = client.get(
                accepted["events_url"],
                headers={"Authorization": "Bearer pipeline-token"},
            ).json()
            if snapshot["job"]["status"] == "completed":
                break
            time.sleep(0.01)

        assert snapshot is not None
        assert snapshot["job"]["status"] == "completed"
        stages = [event["stage"] for event in snapshot["events"]]
        assert stages == [
            "upload_received",
            "parsing_document",
            "awaiting_human",
            "completed",
        ]
        assert "synthetic" not in json.dumps(snapshot).casefold()
