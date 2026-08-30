"""Fixed, typed workers for the FunderMatch application graph."""

from __future__ import annotations

import asyncio
from base64 import b64encode
from hashlib import sha256
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from fundermatch.clients.findociq_client import FinDocIQClient, FinDocIQUnavailable
from fundermatch.clients.findociq_contract import (
    ExtractedFigure,
    ExtractRequest,
    IngestBatchRequest,
    IngestDocumentRequest,
    ProductionExtractRequest,
)
from fundermatch.intake import (
    EXTRACTED_FACTS,
    IntakeDocument,
    _default_unit,
    _fact_value,
    _integer,
    _number,
    _select_figure,
    _text,
)
from fundermatch.matching.retriever import RuleGatedPrecedentRetriever
from fundermatch.matching.schema import RuleGatedRetrievalResult
from fundermatch.orchestration.graph import WorkerContext, WorkerFailure
from fundermatch.orchestration.schema import (
    ApplicationMemoryState,
    BoundingBox,
    DocumentReference,
    EligibilityReference,
    EvidenceReference,
    HumanReviewCheckpoint,
    WorkerName,
    WorkerResult,
    result_hash,
)
from fundermatch.orchestration.workspace import ApplicationWorkspace
from fundermatch.precedent.schema import EvidenceMetric, FinancialProfile
from fundermatch.prompts import load_prompt
from fundermatch.rules.engine import EligibilityEngine
from fundermatch.rules.schema import BorrowerApplication, FunderEligibility, FunderPolicy
from fundermatch.security.pii import redact_sensitive_text
from fundermatch.suggest.assembler import SuggestionAssembler
from fundermatch.workflow.errors import WorkflowNotFoundError
from fundermatch.workflow.schema import (
    ActorClaims,
    PipelineAdvanceCommand,
    WorkflowRecord,
    WorkflowState,
)
from fundermatch.workflow.service import WorkflowService


class ActivityReporter(Protocol):
    async def __call__(self, application_id: str, stage: str, **details: object) -> None: ...


class DocumentArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    documents: tuple[IntakeDocument, ...] = Field(min_length=1)
    chunk_ids_by_document: dict[str, tuple[str, ...]]
    config_hashes: dict[str, str]


class MetricArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    figure: ExtractedFigure


class EligibilityArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    application_id: str
    results: tuple[FunderEligibility, ...]


class WorkerDependencies:
    def __init__(
        self,
        *,
        workspace: ApplicationWorkspace,
        findociq: FinDocIQClient,
        workflow: WorkflowService,
        retriever: RuleGatedPrecedentRetriever,
        policies: tuple[FunderPolicy, ...],
        actor: ActorClaims,
        activity: ActivityReporter | None = None,
    ) -> None:
        self.workspace = workspace
        self.findociq = findociq
        self.workflow = workflow
        self.retriever = retriever
        self.policies = policies
        self.actor = actor
        self.activity = activity

    async def report(self, application_id: str, stage: str, **details: object) -> None:
        if self.activity is not None:
            await self.activity(application_id, stage, **details)


class _Worker:
    name: WorkerName

    def __init__(self, dependencies: WorkerDependencies) -> None:
        self.d = dependencies


class DocumentProcessingWorker(_Worker):
    name = WorkerName.DOCUMENT_PROCESSING

    async def run(self, state: ApplicationMemoryState, context: WorkerContext) -> WorkerResult:
        artifact_name = self.name.value
        if self.d.workspace.exists(state.application_id, artifact_name):
            artifact = self.d.workspace.load(state.application_id, artifact_name, DocumentArtifact)
        else:
            request = self.d.workspace.request(state.application_id)
            ingest_documents = []
            production = bool(getattr(self.d.findociq, "production_enabled", False))
            policy_hash = getattr(self.d.findociq, "policy_hash", None)
            if production and not policy_hash:
                raise WorkerFailure(
                    "guardrail_policy_unavailable",
                    "Production guardrail policy identity is not configured",
                    retryable=False,
                )
            for staged in request.files:
                content = self.d.workspace.read_pdf(
                    state.application_id, staged.filename, staged.sha256
                )
                ingest_documents.append(
                    IngestDocumentRequest(
                        contract_version="2.0" if production else "1.0",
                        filename=staged.filename,
                        sha256=staged.sha256,
                        content_base64=b64encode(content).decode("ascii"),
                        application_id=state.application_id if production else None,
                        command_id=str(context.command_id) if production else None,
                        policy_hash=policy_hash if production else None,
                    )
                )
            await self.d.report(
                state.application_id,
                "document_processing",
                attempt=state.attempts.get(self.name, 0) + 1,
            )
            try:
                response = await context.execute(
                    "findociq_ingest",
                    self.d.findociq.ingest_batch(IngestBatchRequest(
                        contract_version="2.0" if production else "1.0",
                        batch_id=state.job_id or str(context.command_id),
                        documents=tuple(ingest_documents),
                    )),
                )
            except FinDocIQUnavailable as error:
                if error.code == "document_prompt_injection_risk":
                    raise WorkerFailure(
                        error.code,
                        "Document contains instruction-like evidence requiring human attention",
                        needs_attention=True,
                    ) from error
                if error.code in {
                    "active_pdf_content",
                    "embedded_file",
                    "encrypted_pdf",
                    "malformed_pdf",
                    "malware_detected",
                }:
                    raise WorkerFailure(
                        error.code,
                        "Document was blocked by the production input policy",
                        retryable=False,
                    ) from error
                raise WorkerFailure(
                    "findociq_ingest_unavailable", "FinDocIQ ingestion is temporarily unavailable"
                ) from error
            except Exception as error:
                raise WorkerFailure(
                    "findociq_ingest_unavailable", "FinDocIQ ingestion is temporarily unavailable"
                ) from error
            expected = {item.filename: item.sha256 for item in request.files}
            if {item.filename: item.sha256 for item in response.documents} != expected:
                raise WorkerFailure(
                    "ingest_receipt_mismatch",
                    "FinDocIQ ingestion receipt did not match staged documents",
                    retryable=False,
                )
            if production and any(
                item.application_id != state.application_id
                or item.policy_hash != policy_hash
                or item.scan_status != "clean"
                or not item.scan_receipt_sha256
                or not item.dlp_receipt_sha256
                or not item.storage_receipt_sha256
                or not item.ownership_receipt_sha256
                for item in response.documents
            ):
                raise WorkerFailure(
                    "production_ingest_receipt_invalid",
                    "FinDocIQ did not return verified production security receipts",
                    retryable=False,
                )
            artifact = DocumentArtifact(
                documents=tuple(
                    IntakeDocument(
                        filename=item.filename,
                        sha256=item.sha256,
                        document_id=item.document_id,
                        page_count=item.page_count,
                        chunk_count=item.chunk_count,
                    )
                    for item in response.documents
                ),
                chunk_ids_by_document={
                    item.document_id: item.chunk_ids for item in response.documents
                },
                config_hashes={item.document_id: item.config_hash for item in response.documents},
            )
            self.d.workspace.save(state.application_id, artifact_name, artifact)
        workflow = await _ensure_workflow(self.d, state.application_id, context)
        references = tuple(
            DocumentReference(
                document_id=item.document_id,
                sha256=item.sha256,
                page_count=item.page_count,
                chunk_count=item.chunk_count,
                config_hash=artifact.config_hashes[item.document_id],
            )
            for item in artifact.documents
        )
        return WorkerResult(
            worker=self.name,
            output_sha256=result_hash(self.name, *(item.document_id for item in references)),
            documents=references,
            workflow_state=workflow.state.value,
            workflow_version=workflow.version,
            config_hash=_combined_hash(tuple(artifact.config_hashes.values())),
        )


class FinancialMetricExtractionWorker(_Worker):
    name = WorkerName.FINANCIAL_ANALYSIS
    metrics = EXTRACTED_FACTS

    async def run(self, state: ApplicationMemoryState, context: WorkerContext) -> WorkerResult:
        request = self.d.workspace.request(state.application_id)
        documents = self.d.workspace.load(
            state.application_id, WorkerName.DOCUMENT_PROCESSING.value, DocumentArtifact
        )
        document_ids = tuple(item.document_id for item in documents.documents)
        evidence: list[EvidenceMetric] = []
        for metric in self.metrics:
            artifact_name = f"financial/{metric}"
            if self.d.workspace.exists(state.application_id, artifact_name):
                metric_artifact = self.d.workspace.load(
                    state.application_id, artifact_name, MetricArtifact
                )
            else:
                await self.d.report(state.application_id, "extracting_metric", metric=metric)
                try:
                    extraction_request = (
                        ProductionExtractRequest(
                            application_id=state.application_id,
                            document_ids=document_ids,
                            metric_ids=(metric,),
                            command_id=f"{context.command_id}:{metric}",
                        )
                        if bool(getattr(self.d.findociq, "production_enabled", False))
                        else ExtractRequest(
                            question=load_prompt(f"extract_{metric}"),
                            question_id=f"{state.application_id}:{metric}",
                            document_ids=document_ids,
                        )
                    )
                    response = await context.execute(
                        "findociq_extract", self.d.findociq.extract(extraction_request)
                    )
                except Exception as error:
                    raise WorkerFailure(
                        "findociq_extract_unavailable",
                        f"FinDocIQ extraction is temporarily unavailable for {metric}",
                    ) from error
                try:
                    selected = _select_figure(response.figures, metric)
                except ValueError as error:
                    raise WorkerFailure(
                        "required_document_fact_missing",
                        f"Required document fact is missing or ambiguous: {metric}",
                        retryable=False,
                        needs_attention=True,
                    ) from error
                metric_artifact = MetricArtifact(metric=metric, figure=selected)
                if bool(getattr(self.d.findociq, "production_enabled", False)) and (
                    response.application_id != state.application_id
                    or response.command_id != f"{context.command_id}:{metric}"
                    or response.policy_hash != getattr(self.d.findociq, "policy_hash", None)
                ):
                    raise WorkerFailure(
                        "production_extract_receipt_invalid",
                        "FinDocIQ extraction receipt did not match the application",
                        retryable=False,
                    )
                self.d.workspace.save(state.application_id, artifact_name, metric_artifact)
            figure = metric_artifact.figure
            if figure.citation.document_id not in document_ids:
                raise WorkerFailure(
                    "cross_application_evidence",
                    "FinDocIQ returned evidence outside the current application",
                    retryable=False,
                )
            try:
                evidence.append(
                    EvidenceMetric(
                        name=metric,
                        value=_fact_value(metric, figure.value),
                        unit=figure.unit or _default_unit(metric),
                        period=figure.period or "latest reported period",
                        citation=figure.citation,
                    )
                )
            except ValueError as error:
                raise WorkerFailure(
                    "required_document_fact_invalid",
                    f"Required document fact is invalid: {metric}",
                    retryable=False,
                    needs_attention=True,
                ) from error
        values = {item.name: item.value for item in evidence}
        metadata = request.metadata
        try:
            application = BorrowerApplication(
                application_id=state.application_id,
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
        except ValueError as error:
            raise WorkerFailure(
                "required_document_profile_invalid",
                "Required document facts could not form a valid borrower profile",
                retryable=False,
                needs_attention=True,
            ) from error
        digest = self.d.workspace.save(state.application_id, self.name.value, application)
        workflow = await _advance(self.d, state.application_id, WorkflowState.EXTRACTED, context)
        refs = tuple(_evidence_reference(item) for item in evidence)
        return WorkerResult(
            worker=self.name,
            output_sha256=digest,
            evidence=refs,
            workflow_state=workflow.state.value,
            workflow_version=workflow.version,
        )


class DeterministicEligibilityWorker(_Worker):
    name = WorkerName.ELIGIBILITY

    async def run(self, state: ApplicationMemoryState, context: WorkerContext) -> WorkerResult:
        application = self.d.workspace.load(
            state.application_id, WorkerName.FINANCIAL_ANALYSIS.value, BorrowerApplication
        )
        results = EligibilityEngine().evaluate_all(application, self.d.policies)
        artifact = EligibilityArtifact(application_id=state.application_id, results=results)
        digest = self.d.workspace.save(state.application_id, self.name.value, artifact)
        workflow = await _advance(self.d, state.application_id, WorkflowState.RULE_GATED, context)
        refs = tuple(
            EligibilityReference(
                funder_id=item.funder_id,
                eligible=item.eligible,
                failed_criteria=tuple(
                    check.criterion.value for check in item.checks if not check.passed
                ),
            )
            for item in results
        )
        return WorkerResult(
            worker=self.name,
            output_sha256=digest,
            eligibility=refs,
            workflow_state=workflow.state.value,
            workflow_version=workflow.version,
        )


class EligiblePrecedentRetrievalWorker(_Worker):
    name = WorkerName.PRECEDENT_RETRIEVAL

    async def run(self, state: ApplicationMemoryState, context: WorkerContext) -> WorkerResult:
        application = self.d.workspace.load(
            state.application_id, WorkerName.FINANCIAL_ANALYSIS.value, BorrowerApplication
        )
        expected = self.d.workspace.load(
            state.application_id, WorkerName.ELIGIBILITY.value, EligibilityArtifact
        )
        try:
            retrieval = await context.execute(
                "qdrant_search",
                asyncio.to_thread(self.d.retriever.retrieve, application, self.d.policies),
            )
        except RuntimeError as error:
            raise WorkerFailure(
                "ineligible_precedent",
                "Precedent retrieval returned an ineligible funder",
                retryable=False,
            ) from error
        except Exception as error:
            raise WorkerFailure(
                "qdrant_unavailable", "Precedent retrieval is temporarily unavailable"
            ) from error
        if retrieval.eligibility != expected.results:
            raise WorkerFailure(
                "eligibility_changed",
                "Eligibility result changed before precedent retrieval",
                needs_attention=True,
            )
        digest = self.d.workspace.save(state.application_id, self.name.value, retrieval)
        return WorkerResult(worker=self.name, output_sha256=digest)


class AdvisorySuggestionWorker(_Worker):
    name = WorkerName.SUGGESTION

    async def run(self, state: ApplicationMemoryState, context: WorkerContext) -> WorkerResult:
        application = self.d.workspace.load(
            state.application_id, WorkerName.FINANCIAL_ANALYSIS.value, BorrowerApplication
        )
        retrieval = self.d.workspace.load(
            state.application_id, WorkerName.PRECEDENT_RETRIEVAL.value, RuleGatedRetrievalResult
        )
        suggestion = SuggestionAssembler(
            policy_hash=getattr(self.d.findociq, "policy_hash", None)
        ).assemble(application, self.d.policies, retrieval)
        digest = self.d.workspace.save(state.application_id, self.name.value, suggestion)
        persisted_suggestion = suggestion
        if bool(getattr(self.d.findociq, "production_enabled", False)):
            safe_application = suggestion.application.model_copy(
                update={
                    "borrower_name": (
                        "Application "
                        f"{sha256(state.application_id.encode()).hexdigest()[:12]}"
                    ),
                    "finance_context": redact_sensitive_text(
                        suggestion.application.finance_context
                    ),
                    "operations_context": redact_sensitive_text(
                        suggestion.application.operations_context
                    ),
                }
            )
            persisted_suggestion = suggestion.model_copy(
                update={"application": safe_application}
            )
        workflow = await _advance(
            self.d,
            state.application_id,
            WorkflowState.AI_SUGGESTED,
            context,
            suggestion=persisted_suggestion.model_dump(mode="json"),
        )
        return WorkerResult(
            worker=self.name,
            output_sha256=digest,
            workflow_state=workflow.state.value,
            workflow_version=workflow.version,
        )


class HumanReviewHandoffWorker(_Worker):
    name = WorkerName.HUMAN_REVIEW

    async def run(self, state: ApplicationMemoryState, context: WorkerContext) -> WorkerResult:
        workflow = await _advance(
            self.d, state.application_id, WorkflowState.AWAITING_HUMAN, context
        )
        checkpoint = HumanReviewCheckpoint(
            expected_workflow_version=workflow.version,
            allowed_actions=("approve", "reject", "approve_with_conditions", "send_back"),
        )
        return WorkerResult(
            worker=self.name,
            output_sha256=result_hash(self.name, state.application_id, str(workflow.version)),
            human_review=checkpoint,
            workflow_state=workflow.state.value,
            workflow_version=workflow.version,
        )


def fixed_workers(dependencies: WorkerDependencies, guardrail_worker: object) -> tuple[object, ...]:
    """The fixed order is code-owned and cannot be model- or request-selected."""

    return (
        DocumentProcessingWorker(dependencies),
        FinancialMetricExtractionWorker(dependencies),
        DeterministicEligibilityWorker(dependencies),
        EligiblePrecedentRetrievalWorker(dependencies),
        AdvisorySuggestionWorker(dependencies),
        guardrail_worker,
        HumanReviewHandoffWorker(dependencies),
    )


async def _ensure_workflow(
    dependencies: WorkerDependencies, application_id: str, context: WorkerContext
) -> WorkflowRecord:
    try:
        return await dependencies.workflow.get(application_id)
    except WorkflowNotFoundError:
        result = await dependencies.workflow.create(
            application_id,
            dependencies.actor,
            command_id=context.command_id,
            reason="Agent-supervised borrower PDFs accepted into intake",
        )
        return result.workflow


async def _advance(
    dependencies: WorkerDependencies,
    application_id: str,
    target: WorkflowState,
    context: WorkerContext,
    *,
    suggestion: dict[str, object] | None = None,
) -> WorkflowRecord:
    current = await _ensure_workflow(dependencies, application_id, context)
    order = tuple(WorkflowState)
    if order.index(current.state) >= order.index(target):
        return current
    command_id = uuid5(NAMESPACE_URL, f"fundermatch:{application_id}:workflow:{target.value}")
    result = await dependencies.workflow.advance_pipeline(
        application_id,
        PipelineAdvanceCommand(
            command_id=command_id,
            expected_version=current.version,
            target_state=target,
            reason=f"Agent worker completed {target.value.lower()}",
            suggestion=suggestion,
        ),
        dependencies.actor,
    )
    return result.workflow


def _evidence_reference(item: EvidenceMetric) -> EvidenceReference:
    citation = item.citation
    canonical = (
        f"{citation.document_id}|{citation.page_number}|"
        f"{citation.bbox.model_dump_json()}|{item.name}"
    )
    return EvidenceReference(
        evidence_id=sha256(canonical.encode("utf-8")).hexdigest(),
        metric=item.name,
        value=str(item.value),
        unit=item.unit,
        period=item.period,
        document_id=citation.document_id,
        page_number=citation.page_number,
        bbox=BoundingBox.model_validate(citation.bbox.model_dump()),
    )


def _combined_hash(values: tuple[str, ...]) -> str:
    return sha256("|".join(sorted(values)).encode("utf-8")).hexdigest()
