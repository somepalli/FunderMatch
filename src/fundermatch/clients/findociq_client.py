"""Async HTTP-only client for FinDocIQ's public extraction boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from fundermatch.clients.findociq_contract import (
    ExtractRequest,
    ExtractResponse,
    IngestBatchRequest,
    IngestBatchResponse,
    IngestDocumentRequest,
    IngestDocumentResponse,
    IngestionActivityResponse,
    ProductionExtractRequest,
    RetentionDeleteRequest,
    RetentionDeleteResponse,
)


class FinDocIQClientConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: HttpUrl
    timeout_seconds: float = Field(default=120.0, gt=0, le=7200)
    ingest_token: str | None = Field(default=None, min_length=16)
    production_guardrails_enabled: bool = False
    service_jwt_secret: str | None = Field(default=None, min_length=32)
    service_jwt_issuer: str = "fundermatch"
    service_jwt_audience: str = "findociq-api"
    guardrail_policy_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FinDocIQUnavailable(RuntimeError):
    """Raised when FinDocIQ cannot service an extraction request."""

    def __init__(self, message: str, *, code: str = "findociq_unavailable") -> None:
        super().__init__(message)
        self.code = code


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

    @property
    def production_enabled(self) -> bool:
        return self._config.production_guardrails_enabled

    @property
    def policy_hash(self) -> str | None:
        return self._config.guardrail_policy_hash

    async def extract(
        self, request: ExtractRequest | ProductionExtractRequest
    ) -> ExtractResponse:
        headers = self._service_headers(request.application_id) if isinstance(
            request, ProductionExtractRequest
        ) else None
        try:
            response = await self._client.post(
                "/extract",
                json=request.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
                headers=headers,
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

    async def health(self) -> bool:
        try:
            response = await self._client.get("/healthz", timeout=5.0)
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError):
            return False
        if self.production_enabled:
            try:
                if response.json().get("policy_hash") != self.policy_hash:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    async def ingest(self, request: IngestDocumentRequest) -> IngestDocumentResponse:
        if not self.production_enabled and self._config.ingest_token is None:
            raise FinDocIQUnavailable("FinDocIQ ingestion token is not configured")
        headers = (
            self._service_headers(request.application_id)
            if self.production_enabled and request.application_id
            else {"X-FinDocIQ-Ingest-Token": self._config.ingest_token or ""}
        )
        try:
            response = await self._client.post(
                "/v1/documents",
                json=request.model_dump(mode="json"),
                headers=headers,
            )
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            raise FinDocIQUnavailable(
                "FinDocIQ document ingestion failed", code=_safe_http_code(error)
            ) from error
        try:
            return IngestDocumentResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise FinDocIQContractError(
                "FinDocIQ returned an invalid ingestion contract"
            ) from error

    async def ingest_batch(self, request: IngestBatchRequest) -> IngestBatchResponse:
        if not self.production_enabled and self._config.ingest_token is None:
            raise FinDocIQUnavailable("FinDocIQ ingestion token is not configured")
        application_id = request.documents[0].application_id
        headers = (
            self._service_headers(application_id)
            if self.production_enabled and application_id
            else {"X-FinDocIQ-Ingest-Token": self._config.ingest_token or ""}
        )
        try:
            response = await self._client.post(
                "/v1/document-batches",
                json=request.model_dump(mode="json"),
                headers=headers,
            )
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            raise FinDocIQUnavailable(
                "FinDocIQ document ingestion failed", code=_safe_http_code(error)
            ) from error
        try:
            return IngestBatchResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise FinDocIQContractError(
                "FinDocIQ returned an invalid ingestion contract"
            ) from error

    async def ingestion_activity(
        self, batch_id: str, *, after: int = 0, application_id: str | None = None
    ) -> IngestionActivityResponse:
        if not self.production_enabled and self._config.ingest_token is None:
            raise FinDocIQUnavailable("FinDocIQ ingestion token is not configured")
        headers = (
            self._service_headers(application_id)
            if self.production_enabled and application_id
            else {"X-FinDocIQ-Ingest-Token": self._config.ingest_token or ""}
        )
        try:
            response = await self._client.get(
                f"/v1/document-batches/{batch_id}/activity",
                params={"after": after},
                headers=headers,
            )
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            raise FinDocIQUnavailable("FinDocIQ activity request failed") from error
        try:
            return IngestionActivityResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise FinDocIQContractError("FinDocIQ returned an invalid activity contract") from error

    async def retention_delete(
        self, application_id: str, command_id: str
    ) -> RetentionDeleteResponse:
        request = RetentionDeleteRequest(command_id=command_id)
        try:
            response = await self._client.post(
                f"/v1/applications/{application_id}/retention-delete",
                json=request.model_dump(mode="json"),
                headers=self._service_headers(application_id),
            )
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            raise FinDocIQUnavailable("FinDocIQ retention deletion failed") from error
        try:
            return RetentionDeleteResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise FinDocIQContractError(
                "FinDocIQ returned an invalid retention receipt"
            ) from error

    def _service_headers(self, application_id: str | None) -> dict[str, str]:
        if not application_id:
            raise FinDocIQUnavailable("application-scoped FinDocIQ request required")
        secret = self._config.service_jwt_secret
        if not secret:
            raise FinDocIQUnavailable("FinDocIQ service JWT secret is not configured")
        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "sub": "fundermatch",
                "iss": self._config.service_jwt_issuer,
                "aud": self._config.service_jwt_audience,
                "roles": [
                    "document_ingest",
                    "document_extract",
                    "activity_read",
                    "document_delete",
                ],
                "application_id": application_id,
                "jti": uuid4().hex,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
            },
            secret,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}", "X-Correlation-ID": uuid4().hex}


def _safe_http_code(error: Exception) -> str:
    if not isinstance(error, httpx.HTTPStatusError):
        return "findociq_unavailable"
    try:
        code = error.response.json().get("detail")
    except (TypeError, ValueError):
        return "findociq_unavailable"
    allowed = {
        "active_pdf_content",
        "document_prompt_injection_risk",
        "embedded_file",
        "encrypted_pdf",
        "malformed_pdf",
        "malware_detected",
        "malware_scan_inconclusive",
        "malware_scanner_unavailable",
        "malware_scanner_version_unavailable",
    }
    return code if code in allowed else "findociq_unavailable"
