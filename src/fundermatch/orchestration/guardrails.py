"""Deterministic fail-closed validation before human-review handoff."""

from __future__ import annotations

import asyncio
from typing import Protocol

from qdrant_client import QdrantClient

from fundermatch.matching.schema import RuleGatedRetrievalResult
from fundermatch.orchestration.graph import WorkerContext, WorkerFailure
from fundermatch.orchestration.schema import (
    ApplicationMemoryState,
    GuardrailResult,
    WorkerName,
    WorkerResult,
    result_hash,
)
from fundermatch.orchestration.workspace import ApplicationWorkspace
from fundermatch.rules.schema import BorrowerApplication
from fundermatch.security.policy import WorkerExecutionPolicy
from fundermatch.security.receipts import ReceiptSigner
from fundermatch.suggest.schema import SuggestionBundle


class PrecedentResolver(Protocol):
    def resolve(self, case_ids: tuple[str, ...]) -> bool: ...


class QdrantPrecedentResolver:
    def __init__(self, client: QdrantClient, collection: str) -> None:
        self.client = client
        self.collection = collection

    def resolve(self, case_ids: tuple[str, ...]) -> bool:
        from fundermatch.precedent.store import QdrantPrecedentStore

        point_ids = [QdrantPrecedentStore.point_id(item) for item in case_ids]
        records = self.client.retrieve(
            collection_name=self.collection,
            ids=point_ids,
            with_payload=False,
            with_vectors=False,
        )
        return len(records) == len(set(case_ids))


class GuardrailWorker:
    name = WorkerName.GUARDRAILS

    def __init__(
        self,
        workspace: ApplicationWorkspace,
        precedent_resolver: PrecedentResolver | None = None,
        policy_hash: str | None = None,
        receipt_signer: ReceiptSigner | None = None,
        execution_policies: dict[str, WorkerExecutionPolicy] | None = None,
    ) -> None:
        self.workspace = workspace
        self.precedent_resolver = precedent_resolver
        self.policy_hash = policy_hash
        self.receipt_signer = receipt_signer
        self.execution_policies = execution_policies or {}

    async def run(self, state: ApplicationMemoryState, context: WorkerContext) -> WorkerResult:
        if self.policy_hash is not None:
            if self.receipt_signer is None:
                self._terminal("receipt_verifier_missing", "Worker receipt verifier is missing")
            for receipt in state.worker_receipts:
                if receipt.policy_hash != self.policy_hash or not receipt.signature:
                    self._terminal(
                        "worker_receipt_invalid", "Worker receipt policy identity is invalid"
                    )
                if not self.receipt_signer.verify(receipt.signed_payload(), receipt.signature):
                    self._terminal("worker_receipt_invalid", "Worker receipt signature is invalid")
                execution = self.execution_policies.get(receipt.worker.value)
                if execution is None:
                    self._terminal(
                        "worker_policy_missing", "Worker receipt has no production execution policy"
                    )
                if (
                    receipt.attempt > execution.max_attempts
                    or len(receipt.tool_calls) > execution.max_calls
                    or any(tool not in execution.permitted_tools for tool in receipt.tool_calls)
                    or receipt.latency_ms > execution.worker_deadline_seconds * 1000
                ):
                    self._terminal(
                        "worker_policy_violation",
                        "Worker receipt violates tool, retry, or deadline policy",
                    )
        application = self.workspace.load(
            state.application_id, WorkerName.FINANCIAL_ANALYSIS.value, BorrowerApplication
        )
        retrieval = self.workspace.load(
            state.application_id,
            WorkerName.PRECEDENT_RETRIEVAL.value,
            RuleGatedRetrievalResult,
        )
        suggestion = self.workspace.load(
            state.application_id, WorkerName.SUGGESTION.value, SuggestionBundle
        )
        if application.application_id != state.application_id:
            self._terminal("cross_application_artifact", "Application artifact ownership mismatch")
        if retrieval.application_id != state.application_id:
            self._terminal("cross_application_retrieval", "Retrieval artifact ownership mismatch")
        if suggestion.application.application_id != state.application_id:
            self._terminal("cross_application_suggestion", "Suggestion artifact ownership mismatch")

        document_ids = {item.document_id for item in state.documents}
        if not state.evidence:
            self._attention("missing_evidence", "Cited financial evidence is required")
        if any(item.document_id not in document_ids for item in state.evidence):
            self._terminal(
                "cross_application_evidence", "Evidence does not belong to this application"
            )
        if any(
            item.page_number < 1
            or item.bbox.x1 <= item.bbox.x0
            or item.bbox.y1 <= item.bbox.y0
            for item in state.evidence
        ):
            self._attention(
                "invalid_citation_geometry",
                "Cited evidence requires a valid page and non-empty bounding box",
            )
        expected_values = {
            "annual_revenue_crore": str(application.profile.annual_revenue_crore),
            "ebitda_margin_pct": str(application.profile.ebitda_margin_pct),
            "dscr": str(application.profile.dscr),
        }
        for evidence in state.evidence:
            if (
                evidence.metric in expected_values
                and evidence.value != expected_values[evidence.metric]
            ):
                self._attention(
                    "unsupported_numeric_claim",
                    "A numeric claim does not resolve to cited evidence",
                )

        eligible = {item.funder_id for item in state.eligibility if item.eligible}
        retrieved = {item.precedent.decision.funder_id for item in retrieval.matches}
        suggested = {item.funder_id for item in suggestion.candidates}
        if not retrieved <= eligible or not suggested <= eligible:
            self._terminal(
                "ineligible_funder_leakage", "An ineligible funder entered retrieval or suggestions"
            )
        precedent_ids = tuple(item.precedent.case_id for item in retrieval.matches)
        if precedent_ids and self.precedent_resolver is None:
            self._attention(
                "precedent_receipt_unverified",
                "Retrieved precedents could not be verified against stored data",
            )
        if precedent_ids:
            try:
                resolved = await context.execute(
                    "qdrant_verify",
                    asyncio.to_thread(
                        self.precedent_resolver.resolve,
                        precedent_ids,  # type: ignore[union-attr]
                    ),
                )
            except Exception as error:
                raise WorkerFailure(
                    "qdrant_verification_unavailable",
                    "Precedent receipt verification is temporarily unavailable",
                ) from error
            if not resolved:
                self._attention(
                    "precedent_receipt_unverified",
                    "A retrieved precedent no longer resolves to stored data",
                )
        if suggestion.authority != "advisory_only" or not suggestion.requires_human_decision:
            self._terminal(
                "automatic_lending_decision", "Suggestion attempted to assume human authority"
            )
        if not suggestion.advisory_notice:
            self._attention(
                "missing_advisory_notice", "Suggestion is missing the human-authority notice"
            )
        evidence_citations = {
            (
                item.document_id,
                item.page_number,
                item.bbox.x0,
                item.bbox.y0,
                item.bbox.x1,
                item.bbox.y1,
            )
            for item in state.evidence
        }
        precedent_set = set(precedent_ids)
        if self.policy_hash is not None and not suggestion.claims:
            self._attention("missing_claim_ledger", "Suggestion claim ledger is required")
        for claim in suggestion.claims:
            if claim.application_id != state.application_id:
                self._terminal("cross_application_claim", "Claim belongs to another application")
            if self.policy_hash is not None and claim.policy_hash != self.policy_hash:
                self._terminal("claim_policy_mismatch", "Claim policy identity is invalid")
            if claim.citation is not None and (
                claim.citation.document_id,
                claim.citation.page_number,
                claim.citation.bbox.x0,
                claim.citation.bbox.y0,
                claim.citation.bbox.x1,
                claim.citation.bbox.y1,
            ) not in evidence_citations:
                self._attention("unsupported_output_claim", "Output claim lacks owned evidence")
            if claim.precedent_id is not None and claim.precedent_id not in precedent_set:
                self._attention("unsupported_output_claim", "Output claim lacks precedent evidence")

        checkpoint_size = len(state.model_dump_json().encode("utf-8"))
        if checkpoint_size > 256 * 1024:
            self._terminal(
                "checkpoint_size_violation", "Checkpoint exceeds the production size policy"
            )
        results = (
            GuardrailResult(guardrail="citation_presence", passed=True),
            GuardrailResult(guardrail="evidence_ownership", passed=True),
            GuardrailResult(guardrail="numeric_grounding", passed=True),
            GuardrailResult(guardrail="eligible_only", passed=True),
            GuardrailResult(guardrail="human_authority", passed=True),
            GuardrailResult(guardrail="claim_grounding", passed=True),
            GuardrailResult(
                guardrail="checkpoint_schema",
                passed=True,
            ),
            GuardrailResult(
                guardrail="tool_timeout_retry_loop_limits",
                passed=(
                    self.policy_hash is None
                    or all(
                        item.policy_hash == self.policy_hash and bool(item.signature)
                        for item in state.worker_receipts
                    )
                ),
            ),
        )
        return WorkerResult(
            worker=self.name,
            output_sha256=result_hash(
                self.name, state.application_id, *(item.guardrail for item in results)
            ),
            guardrails=results,
        )

    @staticmethod
    def _attention(code: str, message: str) -> None:
        raise WorkerFailure(code, message, needs_attention=True)

    @staticmethod
    def _terminal(code: str, message: str) -> None:
        raise WorkerFailure(code, message, retryable=False)
