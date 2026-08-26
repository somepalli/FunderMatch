"""Real borrower PDF intake, extraction, rule gating, and review orchestration."""

from __future__ import annotations

import asyncio
import json
import re
from base64 import b64encode
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from fundermatch.clients.findociq_client import FinDocIQClient
from fundermatch.clients.findociq_contract import (
    ExtractedFigure,
    ExtractRequest,
    IngestDocumentRequest,
)
from fundermatch.matching.retriever import RuleGatedPrecedentRetriever
from fundermatch.precedent.schema import EvidenceMetric, FinancialProfile
from fundermatch.rules.schema import BorrowerApplication, FunderPolicy
from fundermatch.suggest.assembler import SuggestionAssembler
from fundermatch.workflow.schema import (
    ActorClaims,
    PipelineAdvanceCommand,
    WorkflowRecord,
    WorkflowState,
)
from fundermatch.workflow.service import WorkflowService

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_BATCH_BYTES = 512 * 1024 * 1024


class IntakeMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    application_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,199}$")
    borrower_name: str = Field(min_length=1, max_length=200)
    industry: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=100)
    requested_amount_crore: Decimal = Field(gt=0)
    debt_to_ebitda: Decimal = Field(ge=0)
    collateral_cover: Decimal = Field(ge=0)
    years_operating: int = Field(ge=0)
    employee_count: int = Field(gt=0)
    finance_context: str = Field(min_length=1, max_length=2000)
    operations_context: str = Field(min_length=1, max_length=2000)


class IntakeDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    sha256: str
    document_id: str
    page_count: int
    chunk_count: int


class IntakeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    workflow: WorkflowRecord
    documents: tuple[IntakeDocument, ...]


class BorrowerIntakeService:
    def __init__(
        self,
        *,
        storage_root: Path,
        findociq: FinDocIQClient,
        workflow: WorkflowService,
        retriever: RuleGatedPrecedentRetriever,
        policies: tuple[FunderPolicy, ...],
        max_file_bytes: int = MAX_PDF_BYTES,
        max_batch_bytes: int = MAX_BATCH_BYTES,
    ) -> None:
        self.storage_root = storage_root.resolve()
        self.findociq = findociq
        self.workflow = workflow
        self.retriever = retriever
        self.policies = policies
        self.max_file_bytes = max_file_bytes
        self.max_batch_bytes = max_batch_bytes

    async def process(
        self,
        metadata: IntakeMetadata,
        files: tuple[tuple[str, bytes], ...],
        actor: ActorClaims,
    ) -> IntakeResult:
        if not files:
            raise ValueError("at least one borrower PDF is required")
        if sum(len(content) for _, content in files) > self.max_batch_bytes:
            raise ValueError("PDF batch exceeds the 512 MB aggregate limit")
        folder = self.storage_root / metadata.application_id
        documents_folder = folder / "documents"
        if (folder / "manifest.json").exists():
            raise ValueError(f"application {metadata.application_id} is already processed")
        documents_folder.mkdir(parents=True, exist_ok=True)
        documents: list[IntakeDocument] = []
        try:
            for filename, content in files:
                safe_name = Path(filename).name
                if safe_name != filename or not safe_name.lower().endswith(".pdf"):
                    raise ValueError("all uploaded files must be safely named PDFs")
                if not content.startswith(b"%PDF-"):
                    raise ValueError(f"{safe_name} is not a PDF")
                if len(content) > self.max_file_bytes:
                    raise ValueError(f"{safe_name} exceeds the 25 MB limit")
                digest = sha256(content).hexdigest()
                (documents_folder / safe_name).write_bytes(content)
                ingested = await self.findociq.ingest(
                    IngestDocumentRequest(
                        filename=safe_name,
                        sha256=digest,
                        content_base64=b64encode(content).decode("ascii"),
                    )
                )
                documents.append(
                    IntakeDocument(
                        filename=safe_name,
                        sha256=digest,
                        document_id=ingested.document_id,
                        page_count=ingested.page_count,
                        chunk_count=ingested.chunk_count,
                    )
                )
            application = await self._extract_application(metadata, tuple(documents))
            retrieval = await asyncio.to_thread(
                self.retriever.retrieve, application, self.policies
            )
            suggestion = SuggestionAssembler().assemble(
                application, self.policies, retrieval
            )
            result = await self.workflow.create(
                metadata.application_id, actor, reason="Borrower PDFs accepted into intake"
            )
            for target, reason, bundle in (
                (WorkflowState.EXTRACTED, "FinDocIQ extracted cited borrower metrics", None),
                (WorkflowState.RULE_GATED, "Deterministic funder rules evaluated", None),
                (
                    WorkflowState.AI_SUGGESTED,
                    "Eligible precedents assembled as advisory evidence",
                    suggestion.model_dump(mode="json"),
                ),
                (WorkflowState.AWAITING_HUMAN, "Case queued for authoritative human review", None),
            ):
                result = await self.workflow.advance_pipeline(
                    metadata.application_id,
                    PipelineAdvanceCommand(
                        expected_version=result.workflow.version,
                        target_state=target,
                        reason=reason,
                        suggestion=bundle,
                    ),
                    actor,
                )
            manifest = {
                "metadata": metadata.model_dump(mode="json"),
                "documents": [item.model_dump(mode="json") for item in documents],
                "workflow_state": result.workflow.state.value,
            }
            (folder / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            (folder / "FAILED").unlink(missing_ok=True)
            return IntakeResult(workflow=result.workflow, documents=tuple(documents))
        except Exception:
            (folder / "FAILED").write_text(
                "Intake failed; inspect server logs.\n", encoding="utf-8"
            )
            raise

    async def _extract_application(
        self, metadata: IntakeMetadata, documents: tuple[IntakeDocument, ...]
    ) -> BorrowerApplication:
        document_ids = tuple(item.document_id for item in documents)
        specs = (
            ("annual_revenue_crore", "Extract the latest annual revenue in INR crore."),
            ("ebitda_margin_pct", "Extract the latest EBITDA margin percentage."),
            ("dscr", "Extract the latest debt service coverage ratio (DSCR)."),
        )
        evidence = []
        for name, question in specs:
            response = await self.findociq.extract(
                ExtractRequest(
                    question=question,
                    question_id=f"{metadata.application_id}:{name}",
                    document_ids=document_ids,
                )
            )
            figure = _select_figure(response.figures, name)
            evidence.append(
                EvidenceMetric(
                    name=name,
                    value=_decimal(figure.value),
                    unit=figure.unit or _default_unit(name),
                    period=figure.period or "latest reported period",
                    citation=figure.citation,
                )
            )
        values = {item.name: item.value for item in evidence}
        return BorrowerApplication(
            application_id=metadata.application_id,
            borrower_name=metadata.borrower_name,
            industry=metadata.industry,
            region=metadata.region,
            profile=FinancialProfile(
                annual_revenue_crore=values["annual_revenue_crore"],
                requested_amount_crore=metadata.requested_amount_crore,
                ebitda_margin_pct=values["ebitda_margin_pct"],
                dscr=values["dscr"],
                debt_to_ebitda=metadata.debt_to_ebitda,
                collateral_cover=metadata.collateral_cover,
                years_operating=metadata.years_operating,
                employee_count=metadata.employee_count,
            ),
            evidence=tuple(evidence),
            finance_context=metadata.finance_context,
            operations_context=metadata.operations_context,
        )


def _select_figure(figures: tuple[ExtractedFigure, ...], metric: str) -> ExtractedFigure:
    tokens = {
        "annual_revenue_crore": ("revenue", "income"),
        "ebitda_margin_pct": ("ebitda", "margin"),
        "dscr": ("dscr", "debt service"),
    }[metric]
    for figure in figures:
        label = figure.label.casefold()
        if any(token in label for token in tokens):
            return figure
    if len(figures) == 1:
        return figures[0]
    raise ValueError(f"FinDocIQ did not return an unambiguous {metric} figure")


def _decimal(raw: str) -> Decimal:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", raw)
    if not match:
        raise ValueError(f"extracted figure is not numeric: {raw!r}")
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation as error:
        raise ValueError(f"extracted figure is not numeric: {raw!r}") from error


def _default_unit(metric: str) -> str:
    return {"annual_revenue_crore": "INR crore", "ebitda_margin_pct": "%", "dscr": "x"}[
        metric
    ]
