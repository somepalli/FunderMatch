"""Convert a human-decided workflow into verified, searchable precedent memory."""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict

from fundermatch.precedent.schema import (
    DecidedLoanCase,
    DecisionOutcome,
    DecisionOverride,
    HumanDecision,
    ReviewerComment,
)
from fundermatch.precedent.store import PrecedentEmbedder, QdrantPrecedentStore
from fundermatch.security.pii import redact_sensitive_text
from fundermatch.suggest.schema import SuggestionBundle
from fundermatch.workflow.errors import (
    InvalidTransitionError,
    WorkflowAuthorizationError,
    WorkflowConflictError,
)
from fundermatch.workflow.schema import (
    ActorClaims,
    ActorRole,
    HumanAction,
    HumanDecisionRecord,
    PrecedentWriteCommand,
    PrecedentWriteReceipt,
    TransitionResult,
    WorkflowState,
)
from fundermatch.workflow.service import WorkflowService


class WritebackResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    transition: TransitionResult
    precedent: DecidedLoanCase


@dataclass(slots=True)
class PrecedentWritebackService:
    workflow: WorkflowService
    store: QdrantPrecedentStore
    embedder: PrecedentEmbedder
    policy_hash: str | None = None
    validity_days: int | None = None
    outbox_pool: asyncpg.Pool | None = None
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def write(
        self,
        application_id: str,
        command: PrecedentWriteCommand,
        actor: ActorClaims,
    ) -> WritebackResult:
        if ActorRole.PIPELINE not in actor.roles:
            raise WorkflowAuthorizationError("pipeline role required")
        current = await self.workflow.get(application_id)
        if current.state == WorkflowState.PRECEDENT_WRITTEN:
            if current.precedent_receipt is None:
                raise RuntimeError("written workflow is missing its precedent receipt")
            transition = await self.workflow.mark_precedent_written(
                application_id, command, current.precedent_receipt, actor
            )
            precedent = await asyncio.to_thread(
                self._read_confirmed, current.precedent_receipt
            )
            return WritebackResult(transition=transition, precedent=precedent)
        if current.state != WorkflowState.HUMAN_DECIDED or current.decision is None:
            raise InvalidTransitionError("workflow must contain a completed human decision")
        if current.version != command.expected_version:
            raise WorkflowConflictError(
                f"expected version {command.expected_version}, current version is {current.version}"
            )
        if current.decision.action == HumanAction.SEND_BACK:
            raise InvalidTransitionError("send_back is not written as lending precedent")
        if current.suggestion is None:
            raise InvalidTransitionError("workflow has no advisory bundle to preserve")
        suggestion = SuggestionBundle.model_validate(current.suggestion)
        if suggestion.application.application_id != application_id:
            raise InvalidTransitionError("advisory bundle application_id does not match workflow")
        self._validate_selected_funder(
            suggestion, current.decision.funder_id, current.decision.overrides
        )
        precedent = self._build_case(suggestion, current.decision)
        payload_sha256 = self._payload_hash(precedent)
        await self._outbox_begin(
            command.command_id, application_id, "precedent_writeback", payload_sha256
        )
        try:
            async with self._write_lock:
                confirmed = await asyncio.to_thread(
                    self.store.write_one, precedent, self.embedder
                )
        except Exception:
            await self._outbox_fail(command.command_id, "precedent_writeback_failed")
            raise
        receipt = PrecedentWriteReceipt(
            case_id=confirmed.case_id,
            point_id=UUID(self.store.point_id(confirmed.case_id)),
            collection=self.store.config.collection,
            payload_sha256=self._payload_hash(confirmed),
        )
        await self._outbox_complete(
            command.command_id, receipt.model_dump(mode="json")
        )
        transition = await self.workflow.mark_precedent_written(
            application_id, command, receipt, actor
        )
        return WritebackResult(transition=transition, precedent=confirmed)

    async def _outbox_begin(
        self,
        command_id: UUID,
        application_id: str,
        operation: str,
        payload_hash: str,
    ) -> None:
        if self.outbox_pool is None:
            return
        row = await self.outbox_pool.fetchrow(
            """
            INSERT INTO guardrail_outbox
                (command_id, application_id, operation, payload_hash, status)
            VALUES ($1, $2, $3, $4, 'pending')
            ON CONFLICT (command_id) DO UPDATE
                SET updated_at = guardrail_outbox.updated_at
            RETURNING application_id, operation, payload_hash
            """,
            command_id,
            application_id,
            operation,
            payload_hash,
        )
        if (
            row["application_id"] != application_id
            or row["operation"] != operation
            or row["payload_hash"] != payload_hash
        ):
            raise WorkflowConflictError("command_id was already used for another side effect")

    async def _outbox_complete(self, command_id: UUID, receipt: dict[str, object]) -> None:
        if self.outbox_pool is None:
            return
        await self.outbox_pool.execute(
            """
            UPDATE guardrail_outbox
            SET status = 'completed', attempts = attempts + 1,
                receipt = $2::jsonb, last_error_code = NULL, updated_at = now()
            WHERE command_id = $1
            """,
            command_id,
            json.dumps(receipt, sort_keys=True),
        )

    async def _outbox_fail(self, command_id: UUID, error_code: str) -> None:
        if self.outbox_pool is None:
            return
        await self.outbox_pool.execute(
            """
            UPDATE guardrail_outbox
            SET status = 'failed', attempts = attempts + 1,
                last_error_code = $2, updated_at = now()
            WHERE command_id = $1
            """,
            command_id,
            error_code,
        )

    def _read_confirmed(self, receipt: PrecedentWriteReceipt) -> DecidedLoanCase:
        records = self.store.client.retrieve(
            collection_name=receipt.collection,
            ids=[str(receipt.point_id)],
            with_payload=True,
            with_vectors=False,
        )
        if len(records) != 1 or records[0].payload is None:
            raise RuntimeError(f"precedent {receipt.case_id!r} is no longer available")
        precedent = DecidedLoanCase.model_validate(records[0].payload)
        if self._payload_hash(precedent) != receipt.payload_sha256:
            raise RuntimeError(f"precedent {receipt.case_id!r} no longer matches its receipt")
        return precedent

    def _build_case(
        self,
        suggestion: SuggestionBundle, decision: HumanDecisionRecord
    ) -> DecidedLoanCase:
        application = suggestion.application
        outcome = {
            HumanAction.APPROVE: DecisionOutcome.APPROVED,
            HumanAction.REJECT: DecisionOutcome.REJECTED,
            HumanAction.APPROVE_WITH_CONDITIONS: DecisionOutcome.APPROVED_WITH_CONDITIONS,
        }[decision.action]
        comments = (
            ReviewerComment(
                team="finance",
                author="Finance review context",
                text=redact_sensitive_text(application.finance_context),
                created_at=decision.decided_at,
            ),
            ReviewerComment(
                team="operations",
                author="Operations review context",
                text=redact_sensitive_text(application.operations_context),
                created_at=decision.decided_at,
            ),
        )
        return DecidedLoanCase(
            case_id=application.application_id,
            borrower_name=(
                "Application "
                f"{hashlib.sha256(application.application_id.encode()).hexdigest()[:12]}"
            ),
            industry=application.industry,
            region=application.region,
            profile=application.profile,
            evidence=application.evidence,
            comments=comments,
            decision=HumanDecision(
                outcome=outcome,
                funder_id=decision.funder_id,
                decided_by="Verified human reviewer",
                decided_at=decision.decided_at,
                rationale=redact_sensitive_text(decision.reason),
                conditions=tuple(redact_sensitive_text(item) for item in decision.conditions),
                overrides=tuple(
                    item.model_copy(
                        update={
                            "original_result": redact_sensitive_text(item.original_result),
                            "justification": redact_sensitive_text(item.justification),
                        }
                    )
                    for item in decision.overrides
                ),
            ),
            policy_hash=self.policy_hash,
            valid_until=(
                decision.decided_at + timedelta(days=self.validity_days)
                if self.validity_days is not None
                else None
            ),
        )

    @staticmethod
    def _validate_selected_funder(
        suggestion: SuggestionBundle,
        funder_id: str | None,
        overrides: tuple[DecisionOverride, ...],
    ) -> None:
        if funder_id is None:
            raise InvalidTransitionError("decided outcome is missing funder_id")
        candidates = {candidate.funder_id for candidate in suggestion.candidates}
        if funder_id in candidates:
            return
        excluded = {item.funder_id: item for item in suggestion.excluded_funders}
        selected = excluded.get(funder_id)
        if selected is None:
            raise InvalidTransitionError("selected funder is absent from the advisory bundle")
        failed = {check.criterion.value for check in selected.failed_checks}
        documented = {override.criterion for override in overrides}
        missing = failed - documented
        if missing:
            raise InvalidTransitionError(
                "excluded funder requires explicit human overrides for: "
                + ", ".join(sorted(missing))
            )

    @staticmethod
    def _payload_hash(precedent: DecidedLoanCase) -> str:
        canonical = json.dumps(
            precedent.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
