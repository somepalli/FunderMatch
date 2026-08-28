from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fundermatch.clients.findociq_client import (
    FinDocIQClient,
    FinDocIQClientConfig,
    FinDocIQContractError,
    FinDocIQUnavailable,
)
from fundermatch.clients.findociq_contract import (
    ExtractRequest,
    IngestBatchRequest,
    IngestDocumentRequest,
)

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/findociq_extract_v1.json"


def run(coroutine):  # type: ignore[no-untyped-def]
    return asyncio.run(coroutine)


def test_client_posts_only_the_public_contract_and_validates_response() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/extract"
            assert request.content == (
                b'{"question":"Extract FY2025 revenue","question_id":"case-1"}'
            )
            return httpx.Response(200, content=FIXTURE.read_bytes())

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://findociq:8989"
        ) as http_client:
            client = FinDocIQClient(
                FinDocIQClientConfig(base_url="http://findociq:8989"),
                http_client=http_client,
            )
            result = await client.extract(
                ExtractRequest(question="Extract FY2025 revenue", question_id="case-1")
            )

        assert result.contract_version == "1.0"
        assert result.figures[0].value == "10"
        assert result.figures[0].citation.page_number == 2
        assert result.figures[0].citation.bbox.x0 == 43.2

    run(scenario())


def test_client_rejects_a_response_without_bbox_provenance() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "contract_version": "1.0",
                    "question": "Extract revenue",
                    "figures": [
                        {
                            "label": "Revenue",
                            "value": "10",
                            "citation": {"document_id": "doc", "page_number": 1},
                        }
                    ],
                },
            )
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://findociq:8989"
        ) as http_client:
            client = FinDocIQClient(
                FinDocIQClientConfig(base_url="http://findociq:8989"),
                http_client=http_client,
            )
            with pytest.raises(FinDocIQContractError):
                await client.extract(ExtractRequest(question="Extract revenue"))

    run(scenario())


def test_client_hides_upstream_error_details() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(503, text="private model failure"))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://findociq:8989"
        ) as http_client:
            client = FinDocIQClient(
                FinDocIQClientConfig(base_url="http://findociq:8989"),
                http_client=http_client,
            )
            with pytest.raises(FinDocIQUnavailable, match="extraction request failed") as caught:
                await client.extract(ExtractRequest(question="Extract revenue"))
            assert "private model failure" not in str(caught.value)

    run(scenario())


def test_client_health_uses_findociq_healthz_contract() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/healthz"
            return httpx.Response(200, json={"status": "ok"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://findociq:8989"
        ) as http_client:
            client = FinDocIQClient(
                FinDocIQClientConfig(base_url="http://findociq:8989"),
                http_client=http_client,
            )
            assert await client.health() is True

    run(scenario())


def test_client_posts_all_documents_as_one_ingestion_batch() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/v1/document-batches"
            payload = json.loads(request.content)
            assert payload["batch_id"] == "intake-job-1"
            assert [item["filename"] for item in payload["documents"]] == [
                "one.pdf",
                "two.pdf",
            ]
            return httpx.Response(
                201,
                json={
                    "contract_version": "1.0",
                    "documents": [
                        {
                            "contract_version": "1.0",
                            "document_id": item["sha256"],
                            "filename": item["filename"],
                            "sha256": item["sha256"],
                            "page_count": 1,
                            "chunk_count": 2,
                            "chunk_ids": [f"chunk-{index}"],
                            "config_hash": "c" * 64,
                        }
                        for index, item in enumerate(payload["documents"], start=1)
                    ],
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://findociq:8989"
        ) as http_client:
            client = FinDocIQClient(
                FinDocIQClientConfig(
                    base_url="http://findociq:8989",
                    ingest_token="local-ingestion-token-123456789",
                ),
                http_client=http_client,
            )
            result = await client.ingest_batch(
                IngestBatchRequest(
                    batch_id="intake-job-1",
                    documents=(
                        IngestDocumentRequest(
                            filename="one.pdf", sha256="a" * 64, content_base64="JVBERi0="
                        ),
                        IngestDocumentRequest(
                            filename="two.pdf", sha256="b" * 64, content_base64="JVBERi0="
                        ),
                    )
                )
            )

        assert len(result.documents) == 2

    run(scenario())


def test_client_reads_sanitized_batch_activity() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/document-batches/intake-job-1/activity"
            assert request.url.params["after"] == "2"
            return httpx.Response(
                200,
                json={
                    "batch_id": "intake-job-1",
                    "status": "running",
                    "last_sequence": 3,
                    "events": [
                        {
                            "sequence": 3,
                            "batch_id": "intake-job-1",
                            "stage": "parsing_document",
                            "message": "Parsing borrower.pdf",
                            "occurred_at": "2026-08-27T00:00:00Z",
                            "document_name": "borrower.pdf",
                            "document_index": 1,
                            "document_count": 2,
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://findociq:8989"
        ) as http_client:
            client = FinDocIQClient(
                FinDocIQClientConfig(
                    base_url="http://findociq:8989",
                    ingest_token="local-ingestion-token-123456789",
                ),
                http_client=http_client,
            )
            activity = await client.ingestion_activity("intake-job-1", after=2)
        assert activity.events[0].document_name == "borrower.pdf"

    run(scenario())


def test_source_tree_has_no_findociq_package_imports() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src").rglob("*.py"))
    assert "from findociq" not in source
    assert "import findociq" not in source
