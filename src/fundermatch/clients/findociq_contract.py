"""Local copy of FinDocIQ's public ``/extract`` contract, version 1.0.

This module intentionally has no dependency on the FinDocIQ Python package.
Changes to this file are API-contract changes and require contract tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def validate_coordinates(self) -> BoundingBox:
        if self.x0 > self.x1 or self.y0 > self.y1:
            raise ValueError("bbox coordinates must satisfy x0 <= x1 and y0 <= y1")
        return self


class SourceCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    bbox: BoundingBox


class ExtractedFigure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    value: str = Field(min_length=1)
    unit: str | None = None
    period: str | None = None
    citation: SourceCitation


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=4000)
    question_id: str | None = Field(default=None, min_length=1, max_length=200)
    document_ids: tuple[str, ...] = Field(default=(), max_length=20)


class IngestDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = "1.0"
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_base64: str


class IngestDocumentResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["1.0"]
    document_id: str
    filename: str
    sha256: str
    page_count: int
    chunk_count: int
    chunk_ids: tuple[str, ...]
    config_hash: str


class IngestBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = "1.0"
    batch_id: str | None = Field(
        default=None, min_length=3, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$"
    )
    documents: tuple[IngestDocumentRequest, ...] = Field(min_length=1)


class IngestBatchResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["1.0"]
    documents: tuple[IngestDocumentResponse, ...] = Field(min_length=1)


class IngestionActivityEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=1)
    batch_id: str
    stage: str
    message: str
    occurred_at: datetime
    document_name: str | None = None
    document_index: int | None = None
    document_count: int | None = None
    completed: int | None = None
    total: int | None = None


class IngestionActivityResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    status: Literal["pending", "running", "completed", "failed"]
    events: tuple[IngestionActivityEvent, ...] = ()
    last_sequence: int = Field(ge=0)


class ExtractResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["1.0"]
    question: str = Field(min_length=1)
    figures: tuple[ExtractedFigure, ...] = Field(min_length=1)
    notes: tuple[str, ...] = ()
