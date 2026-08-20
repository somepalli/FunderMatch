"""Transactional asyncpg implementation of workflow and append-only audit persistence."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg

from fundermatch.workflow.errors import WorkflowConflictError, WorkflowNotFoundError
from fundermatch.workflow.repository import Transition
from fundermatch.workflow.schema import AuditEvent, TransitionResult, WorkflowRecord


class PostgresWorkflowRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(cls, dsn: str) -> AsyncIterator["PostgresWorkflowRepository"]:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        try:
            yield cls(pool)
        finally:
            await pool.close()

    async def create(self, workflow: WorkflowRecord, event: AuditEvent) -> TransitionResult:
        result = TransitionResult(workflow=workflow, audit_event=event)
        async with self._pool.acquire() as connection, connection.transaction():
            prior = await self._command_result(
                connection, workflow.application_id, str(event.command_id)
            )
            if prior is not None:
                return prior
            try:
                await connection.execute(
                    """
                    INSERT INTO workflow_cases
                        (application_id, state, version, suggestion, decision,
                         created_at, updated_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7)
                    """,
                    workflow.application_id,
                    workflow.state.value,
                    workflow.version,
                    self._json(workflow.suggestion),
                    self._json(workflow.decision),
                    workflow.created_at,
                    workflow.updated_at,
                )
            except asyncpg.UniqueViolationError as exc:
                raise WorkflowConflictError(
                    f"workflow {workflow.application_id!r} already exists"
                ) from exc
            await self._insert_audit(connection, event)
            await self._insert_command(connection, result)
        return result

    async def get(self, application_id: str) -> WorkflowRecord:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM workflow_cases WHERE application_id = $1", application_id
            )
        if row is None:
            raise WorkflowNotFoundError(f"workflow {application_id!r} not found")
        return self._workflow(row)

    async def audit(self, application_id: str) -> tuple[AuditEvent, ...]:
        async with self._pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT 1 FROM workflow_cases WHERE application_id = $1", application_id
            )
            if exists is None:
                raise WorkflowNotFoundError(f"workflow {application_id!r} not found")
            rows = await connection.fetch(
                """
                SELECT * FROM workflow_audit
                WHERE application_id = $1 ORDER BY sequence
                """,
                application_id,
            )
        return tuple(self._audit_event(row) for row in rows)

    async def transition(
        self,
        application_id: str,
        command_id: str,
        transition: Transition,
    ) -> TransitionResult:
        async with self._pool.acquire() as connection, connection.transaction():
            prior = await self._command_result(connection, application_id, command_id)
            if prior is not None:
                return prior
            row = await connection.fetchrow(
                "SELECT * FROM workflow_cases WHERE application_id = $1 FOR UPDATE",
                application_id,
            )
            if row is None:
                raise WorkflowNotFoundError(f"workflow {application_id!r} not found")
            sequence = await connection.fetchval(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM workflow_audit WHERE application_id = $1
                """,
                application_id,
            )
            workflow, event = transition(self._workflow(row), sequence)
            result = TransitionResult(workflow=workflow, audit_event=event)
            await connection.execute(
                """
                UPDATE workflow_cases
                SET state = $2, version = $3, suggestion = $4::jsonb,
                    decision = $5::jsonb, updated_at = $6
                WHERE application_id = $1
                """,
                workflow.application_id,
                workflow.state.value,
                workflow.version,
                self._json(workflow.suggestion),
                self._json(workflow.decision),
                workflow.updated_at,
            )
            await self._insert_audit(connection, event)
            await self._insert_command(connection, result)
            return result

    @staticmethod
    async def migrate(dsn: str, migration_path: Path) -> None:
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(migration_path.read_text(encoding="utf-8"))
        finally:
            await connection.close()

    @staticmethod
    async def _command_result(
        connection: asyncpg.Connection, application_id: str, command_id: str
    ) -> TransitionResult | None:
        payload = await connection.fetchval(
            """
            SELECT result FROM workflow_commands
            WHERE application_id = $1 AND command_id = $2::uuid
            """,
            application_id,
            command_id,
        )
        if payload is None:
            return None
        return TransitionResult.model_validate(PostgresWorkflowRepository._decode_json(payload))

    @classmethod
    async def _insert_command(
        cls, connection: asyncpg.Connection, result: TransitionResult
    ) -> None:
        await connection.execute(
            """
            INSERT INTO workflow_commands (application_id, command_id, result)
            VALUES ($1, $2, $3::jsonb)
            """,
            result.workflow.application_id,
            result.audit_event.command_id,
            result.model_dump_json(),
        )

    @classmethod
    async def _insert_audit(cls, connection: asyncpg.Connection, event: AuditEvent) -> None:
        await connection.execute(
            """
            INSERT INTO workflow_audit
                (audit_id, command_id, application_id, sequence, actor_id,
                 actor_display_name, actor_roles, from_state, to_state, action,
                 reason, changes, occurred_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13)
            """,
            event.audit_id,
            event.command_id,
            event.application_id,
            event.sequence,
            event.actor_id,
            event.actor_display_name,
            [role.value for role in event.actor_roles],
            event.from_state.value if event.from_state else None,
            event.to_state.value,
            event.action,
            event.reason,
            json.dumps(event.changes),
            event.occurred_at,
        )

    @staticmethod
    def _json(value: Any) -> str | None:
        if value is None:
            return None
        if hasattr(value, "model_dump_json"):
            return value.model_dump_json()
        return json.dumps(value)

    @staticmethod
    def _workflow(row: asyncpg.Record) -> WorkflowRecord:
        payload = dict(row)
        payload["suggestion"] = PostgresWorkflowRepository._decode_json(payload["suggestion"])
        payload["decision"] = PostgresWorkflowRepository._decode_json(payload["decision"])
        return WorkflowRecord.model_validate(payload)

    @staticmethod
    def _audit_event(row: asyncpg.Record) -> AuditEvent:
        payload = dict(row)
        payload["changes"] = PostgresWorkflowRepository._decode_json(payload["changes"])
        return AuditEvent.model_validate(payload)

    @staticmethod
    def _decode_json(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value
