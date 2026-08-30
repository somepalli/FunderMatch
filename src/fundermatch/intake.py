"""Real borrower PDF intake, extraction, rule gating, and review orchestration."""

from __future__ import annotations

import asyncio
import json
import re
from base64 import b64encode
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from fundermatch.clients.findociq_client import FinDocIQClient
from fundermatch.clients.findociq_contract import (
    ExtractedFigure,
    ExtractRequest,
    IngestBatchRequest,
    IngestDocumentRequest,
)
from fundermatch.matching.retriever import RuleGatedPrecedentRetriever
from fundermatch.precedent.schema import EvidenceMetric, FinancialProfile
from fundermatch.prompts import load_prompt
from fundermatch.rules.schema import BorrowerApplication, FunderPolicy, LoanType
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

ProgressReporter = Callable[..., Awaitable[None]]

EXTRACTED_FACTS = (
    "annual_revenue_crore",
    "ebitda_margin_pct",
    "dscr",
    "pat_crore",
    "debt_to_equity",
    "debt_to_ebitda",
    "collateral_cover",
    "years_operating",
    "employee_count",
    "borrower_name",
    "industry",
    "sub_industry",
    "region",
)
TEXT_FACTS = frozenset({"borrower_name", "industry", "sub_industry", "region"})


class IntakeSubmission(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    requested_amount_crore: Decimal = Field(gt=0)
    loan_type: LoanType


class IntakeMetadata(IntakeSubmission):
    application_id: str = Field(
        default_factory=lambda: f"APP-{uuid4().hex[:12].upper()}",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,199}$",
    )


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
        *,
        job_id: str | None = None,
        progress: ProgressReporter | None = None,
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
            requests: list[IngestDocumentRequest] = []
            await _report(
                progress,
                "validating_upload",
                f"Validating {len(files)} uploaded PDF(s)",
                total=len(files),
            )
            for index, (filename, content) in enumerate(files, start=1):
                safe_name = Path(filename).name
                await _report(
                    progress,
                    "validating_document",
                    f"Validating {safe_name}",
                    document_name=safe_name,
                    document_index=index,
                    document_count=len(files),
                )
                if safe_name != filename or not safe_name.lower().endswith(".pdf"):
                    raise ValueError("all uploaded files must be safely named PDFs")
                if not content.startswith(b"%PDF-"):
                    raise ValueError(f"{safe_name} is not a PDF")
                if len(content) > self.max_file_bytes:
                    raise ValueError(f"{safe_name} exceeds the 25 MB limit")
                digest = sha256(content).hexdigest()
                (documents_folder / safe_name).write_bytes(content)
                await _report(
                    progress,
                    "staged_document",
                    f"Stored {safe_name} for processing",
                    document_name=safe_name,
                    document_index=index,
                    document_count=len(files),
                    completed=index,
                    total=len(files),
                )
                requests.append(
                    IngestDocumentRequest(
                        filename=safe_name,
                        sha256=digest,
                        content_base64=b64encode(content).decode("ascii"),
                    )
                )
            await _report(
                progress,
                "submitting_batch",
                "Submitting one GPU-optimized document batch to FinDocIQ",
                total=len(requests),
            )
            ingested_batch = await self._ingest_with_activity(
                IngestBatchRequest(batch_id=job_id, documents=tuple(requests)),
                progress,
            )
            for ingested in ingested_batch.documents:
                documents.append(
                    IntakeDocument(
                        filename=ingested.filename,
                        sha256=ingested.sha256,
                        document_id=ingested.document_id,
                        page_count=ingested.page_count,
                        chunk_count=ingested.chunk_count,
                    )
                )
            application = await self._extract_application(
                metadata, tuple(documents), progress=progress
            )
            await _report(progress, "rule_gating", "Evaluating deterministic funder rules")
            retrieval = await asyncio.to_thread(self.retriever.retrieve, application, self.policies)
            await _report(
                progress,
                "assembling_suggestions",
                "Assembling eligible funders and precedent evidence",
            )
            suggestion = SuggestionAssembler().assemble(application, self.policies, retrieval)
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
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            (folder / "FAILED").unlink(missing_ok=True)
            await _report(
                progress,
                "awaiting_human",
                "Case is ready for authoritative human review",
            )
            return IntakeResult(workflow=result.workflow, documents=tuple(documents))
        except Exception:
            (folder / "FAILED").write_text(
                "Intake failed; inspect server logs.\n", encoding="utf-8"
            )
            raise

    async def _extract_application(
        self,
        metadata: IntakeMetadata,
        documents: tuple[IntakeDocument, ...],
        *,
        progress: ProgressReporter | None = None,
    ) -> BorrowerApplication:
        document_ids = tuple(item.document_id for item in documents)
        metrics = EXTRACTED_FACTS
        evidence = []
        for index, name in enumerate(metrics, start=1):
            await _report(
                progress,
                "extracting_metric",
                f"Extracting cited {name.replace('_', ' ')}",
                metric=name,
                completed=index - 1,
                total=len(metrics),
            )
            response = await self.findociq.extract(
                ExtractRequest(
                    question=load_prompt(f"extract_{name}"),
                    question_id=f"{metadata.application_id}:{name}",
                    document_ids=document_ids,
                )
            )
            figure = _select_figure(response.figures, name)
            evidence.append(
                EvidenceMetric(
                    name=name,
                    value=_fact_value(name, figure.value),
                    unit=figure.unit or _default_unit(name),
                    period=figure.period or "latest reported period",
                    citation=figure.citation,
                )
            )
            await _report(
                progress,
                "metric_completed",
                f"Extracted cited {name.replace('_', ' ')}",
                metric=name,
                completed=index,
                total=len(metrics),
            )
        values = {item.name: item.value for item in evidence}
        return BorrowerApplication(
            application_id=metadata.application_id,
            borrower_name=_text(values, "borrower_name"),
            industry=_text(values, "industry"),
            sub_industry=_text(values, "sub_industry"),
            region=_text(values, "region"),
            loan_type=metadata.loan_type,
            profile=FinancialProfile(
                annual_revenue_crore=_number(values, "annual_revenue_crore"),
                requested_amount_crore=metadata.requested_amount_crore,
                ebitda_margin_pct=_number(values, "ebitda_margin_pct"),
                pat_crore=_number(values, "pat_crore"),
                dscr=_number(values, "dscr"),
                debt_to_equity=_number(values, "debt_to_equity"),
                debt_to_ebitda=_number(values, "debt_to_ebitda"),
                collateral_cover=_number(values, "collateral_cover"),
                years_operating=_integer(values, "years_operating"),
                employee_count=_integer(values, "employee_count"),
            ),
            evidence=tuple(evidence),
            finance_context="Document-extracted financial facts with cited provenance.",
            operations_context="Document-extracted operating facts with cited provenance.",
        )

    async def _ingest_with_activity(
        self,
        request: IngestBatchRequest,
        progress: ProgressReporter | None,
    ):
        task = asyncio.create_task(self.findociq.ingest_batch(request))
        last_sequence = 0
        activity_method = getattr(self.findociq, "ingestion_activity", None)
        while not task.done():
            await asyncio.wait({task}, timeout=0.75)
            if task.done() or activity_method is None or request.batch_id is None:
                continue
            try:
                activity = await activity_method(request.batch_id, after=last_sequence)
            except Exception:  # Activity is advisory; the batch response remains authoritative.
                continue
            for event in activity.events:
                last_sequence = max(last_sequence, event.sequence)
                await _report(
                    progress,
                    event.stage,
                    event.message,
                    document_name=event.document_name,
                    document_index=event.document_index,
                    document_count=event.document_count,
                    completed=event.completed,
                    total=event.total,
                )
        result = await task
        if activity_method is not None and request.batch_id is not None:
            try:
                activity = await activity_method(request.batch_id, after=last_sequence)
            except Exception:  # Activity is advisory; the receipt remains authoritative.
                pass
            else:
                for event in activity.events:
                    await _report(
                        progress,
                        event.stage,
                        event.message,
                        document_name=event.document_name,
                        document_index=event.document_index,
                        document_count=event.document_count,
                        completed=event.completed,
                        total=event.total,
                    )
        return result


def _select_figure(figures: tuple[ExtractedFigure, ...], metric: str) -> ExtractedFigure:
    tokens = {
        "annual_revenue_crore": ("revenue", "income"),
        "ebitda_margin_pct": ("ebitda", "margin"),
        "dscr": ("dscr", "debt service"),
        "pat_crore": ("pat", "profit after tax"),
        "debt_to_equity": ("debt to equity", "debt/equity", "d/e"),
        "debt_to_ebitda": ("debt to ebitda", "debt/ebitda"),
        "collateral_cover": ("collateral", "security cover"),
        "years_operating": ("years operating", "operating history", "incorporated"),
        "employee_count": ("employee", "workforce", "headcount"),
        "borrower_name": ("borrower", "company", "legal entity"),
        "industry": ("industry", "sector"),
        "sub_industry": ("sub-industry", "sub industry", "business activity"),
        "region": ("region", "registered office", "location"),
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


def _fact_value(metric: str, raw: str) -> Decimal | str:
    value = raw.strip()
    if not value:
        raise ValueError(f"FinDocIQ returned an empty {metric} value")
    return value if metric in TEXT_FACTS else _decimal(value)


def _number(values: dict[str, Decimal | str], name: str) -> Decimal:
    value = values[name]
    if not isinstance(value, Decimal):
        raise ValueError(f"FinDocIQ returned non-numeric {name}")
    return value


def _integer(values: dict[str, Decimal | str], name: str) -> int:
    value = _number(values, name)
    if value != value.to_integral_value():
        raise ValueError(f"FinDocIQ returned non-integral {name}")
    return int(value)


def _text(values: dict[str, Decimal | str], name: str) -> str:
    value = values[name]
    if not isinstance(value, str):
        raise ValueError(f"FinDocIQ returned non-text {name}")
    return value


def _default_unit(metric: str) -> str:
    return {
        "annual_revenue_crore": "INR crore",
        "ebitda_margin_pct": "%",
        "dscr": "x",
        "pat_crore": "INR crore",
        "debt_to_equity": "x",
        "debt_to_ebitda": "x",
        "collateral_cover": "x",
        "years_operating": "years",
        "employee_count": "count",
        "borrower_name": "text",
        "industry": "text",
        "sub_industry": "text",
        "region": "text",
    }[metric]


async def _report(
    reporter: ProgressReporter | None,
    stage: str,
    message: str,
    **details: object,
) -> None:
    if reporter is not None:
        await reporter(
            stage,
            message,
            **{key: value for key, value in details.items() if value is not None},
        )
