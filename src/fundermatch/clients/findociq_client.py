"""Async HTTP-only client for FinDocIQ's public extraction boundary."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from fundermatch.clients.findociq_contract import (
    ExtractRequest,
    ExtractResponse,
    IngestDocumentRequest,
    IngestDocumentResponse,
)


class FinDocIQClientConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: HttpUrl
    timeout_seconds: float = Field(default=120.0, gt=0, le=7200)
    ingest_token: str | None = Field(default=None, min_length=16)


class FinDocIQUnavailable(RuntimeError):
    """Raised when FinDocIQ cannot service an extraction request."""


class FinDocIQContractError(RuntimeError):
    """Raised when FinDocIQ returns a payload outside contract version 1.0."""


class FinDocIQClient:
    """Call FinDocIQ over HTTP without importing or depending on its internals."""

    def __init__(
        self,
        config: FinDocIQClientConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=str(config.base_url).rstrip("/"),
            timeout=config.timeout_seconds,
        )

    async def __aenter__(self) -> FinDocIQClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def extract(self, request: ExtractRequest) -> ExtractResponse:
        try:
            response = await self._client.post(
                "/extract",
                json=request.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
            )
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            raise FinDocIQUnavailable("FinDocIQ extraction request failed") from error

        try:
            return ExtractResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise FinDocIQContractError(
                "FinDocIQ returned an invalid extraction contract"
            ) from error

    async def ingest(self, request: IngestDocumentRequest) -> IngestDocumentResponse:
        if self._config.ingest_token is None:
            raise FinDocIQUnavailable("FinDocIQ ingestion token is not configured")
        try:
            response = await self._client.post(
                "/v1/documents",
                json=request.model_dump(mode="json"),
                headers={"X-FinDocIQ-Ingest-Token": self._config.ingest_token},
            )
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            raise FinDocIQUnavailable("FinDocIQ document ingestion failed") from error
        try:
            return IngestDocumentResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise FinDocIQContractError(
                "FinDocIQ returned an invalid ingestion contract"
            ) from error
