from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fundermatch.clients.findociq_client import (
    FinDocIQClient,
    FinDocIQClientConfig,
    FinDocIQContractError,
    FinDocIQUnavailable,
)
from fundermatch.clients.findociq_contract import ExtractRequest

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
        transport = httpx.MockTransport(
            lambda _: httpx.Response(503, text="private model failure")
        )
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


def test_source_tree_has_no_findociq_package_imports() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src").rglob("*.py")
    )
    assert "from findociq" not in source
    assert "import findociq" not in source
