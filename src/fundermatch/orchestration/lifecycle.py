"""Lifecycle metadata for checkpoint retention and stale-run operations."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Protocol

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from fundermatch.orchestration.schema import GraphStatus, WorkerError, WorkerName

TERMINAL_RETENTION = timedelta(days=30)
NON_TERMINAL_STATUSES = tuple(status for status in GraphStatus if not status.terminal)


class MemoryThreadRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: str = Field(min_length=1, max_length=200)
    application_id: str = Field(min_length=1, max_length=200)
    status: GraphStatus
    current_node: WorkerName | None = None
    last_error: WorkerError | None = None
    is_stale: bool = False
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None = None
    delete_after: datetime | None = None


class MemoryStatusCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    running: int = 0
    waiting: int = 0
    retryable: int = 0
    needs_attention: int = 0
    terminal: int = 0
    cancelled: int = 0
    stale: int = 0


class MemoryLifecycleStore(Protocol):
    async def create(self, application_id: str, current_node: WorkerName) -> MemoryThreadRecord: ...

    async def get(self, application_id: str) -> MemoryThreadRecord: ...

    async def set_status(
        self,
        application_id: str,
        status: GraphStatus,
        *,
        current_node: WorkerName | None,
        last_error: WorkerError | None = None,
        now: datetime | None = None,
    ) -> MemoryThreadRecord: ...

    async def flag_stale(self, before: datetime) -> tuple[str, ...]: ...

    async def expired(self, now: datetime | None = None) -> tuple[str, ...]: ...

    async def delete(self, application_id: str) -> None: ...

    async def counts(self) -> MemoryStatusCounts: ...


class InMemoryLifecycleStore:
    def __init__(self, retention: timedelta = TERMINAL_RETENTION) -> None:
        self.retention = retention
        self._records: dict[str, MemoryThreadRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, application_id: str, current_node: WorkerName) -> MemoryThreadRecord:
        async with self._lock:
            existing = self._records.get(application_id)
            if existing is not None:
                return existing
            now = datetime.now(UTC)
            record = MemoryThreadRecord(
                thread_id=application_id,
                application_id=application_id,
                status=GraphStatus.RUNNING,
                current_node=current_node,
                created_at=now,
                updated_at=now,
            )
            self._records[application_id] = record
            return record

    async def get(self, application_id: str) -> MemoryThreadRecord:
        async with self._lock:
            try:
                return self._records[application_id]
            except KeyError as error:
                raise KeyError(f"memory thread {application_id!r} not found") from error

    async def set_status(
        self,
        application_id: str,
        status: GraphStatus,
        *,
        current_node: WorkerName | None,
        last_error: WorkerError | None = None,
        now: datetime | None = None,
    ) -> MemoryThreadRecord:
        async with self._lock:
            try:
                current = self._records[application_id]
            except KeyError as error:
                raise KeyError(f"memory thread {application_id!r} not found") from error
            changed_at = now or datetime.now(UTC)
            terminal_at = changed_at if status.terminal else None
            updated = current.model_copy(
                update={
                    "status": status,
                    "current_node": current_node,
                    "last_error": last_error,
                    "is_stale": False,
                    "updated_at": changed_at,
                    "terminal_at": terminal_at,
                    "delete_after": terminal_at + self.retention if terminal_at else None,
                }
            )
            self._records[application_id] = updated
            return updated

    async def flag_stale(self, before: datetime) -> tuple[str, ...]:
        flagged = []
        async with self._lock:
            for application_id, current in tuple(self._records.items()):
                if not current.status.terminal and current.updated_at < before:
                    self._records[application_id] = current.model_copy(update={"is_stale": True})
                    flagged.append(application_id)
        return tuple(flagged)

    async def expired(self, now: datetime | None = None) -> tuple[str, ...]:
        threshold = now or datetime.now(UTC)
        async with self._lock:
            return tuple(
                application_id
                for application_id, record in self._records.items()
                if record.status.terminal
                and record.delete_after is not None
                and record.delete_after <= threshold
            )

    async def delete(self, application_id: str) -> None:
        async with self._lock:
            self._records.pop(application_id, None)

    async def counts(self) -> MemoryStatusCounts:
        async with self._lock:
            records = tuple(self._records.values())
        return _counts(records)


class PostgresLifecycleStore:
    def __init__(self, pool: asyncpg.Pool, retention: timedelta = TERMINAL_RETENTION) -> None:
        self._pool = pool
        self.retention = retention

    async def create(self, application_id: str, current_node: WorkerName) -> MemoryThreadRecord:
        row = await self._pool.fetchrow(
            """
            INSERT INTO langgraph.memory_threads
                (thread_id, application_id, status, current_node)
            VALUES ($1, $1, $2, $3)
            ON CONFLICT (thread_id) DO UPDATE
                SET thread_id = EXCLUDED.thread_id
            RETURNING *
            """,
            application_id,
            GraphStatus.RUNNING.value,
            current_node.value,
        )
        return self._record(row)

    async def get(self, application_id: str) -> MemoryThreadRecord:
        row = await self._pool.fetchrow(
            "SELECT * FROM langgraph.memory_threads WHERE thread_id = $1",
            application_id,
        )
        if row is None:
            raise KeyError(f"memory thread {application_id!r} not found")
        return self._record(row)

    async def set_status(
        self,
        application_id: str,
        status: GraphStatus,
        *,
        current_node: WorkerName | None,
        last_error: WorkerError | None = None,
        now: datetime | None = None,
    ) -> MemoryThreadRecord:
        changed_at = now or datetime.now(UTC)
        terminal_at = changed_at if status.terminal else None
        row = await self._pool.fetchrow(
            """
            UPDATE langgraph.memory_threads
            SET status = $2, current_node = $3, last_error = $4::jsonb,
                is_stale = false, updated_at = $5, terminal_at = $6,
                delete_after = $7
            WHERE thread_id = $1
            RETURNING *
            """,
            application_id,
            status.value,
            current_node.value if current_node else None,
            last_error.model_dump_json() if last_error else None,
            changed_at,
            terminal_at,
            terminal_at + self.retention if terminal_at else None,
        )
        if row is None:
            raise KeyError(f"memory thread {application_id!r} not found")
        return self._record(row)

    async def flag_stale(self, before: datetime) -> tuple[str, ...]:
        rows = await self._pool.fetch(
            """
            UPDATE langgraph.memory_threads SET is_stale = true
            WHERE status = ANY($1::text[]) AND updated_at < $2
            RETURNING thread_id
            """,
            [status.value for status in NON_TERMINAL_STATUSES],
            before,
        )
        return tuple(row["thread_id"] for row in rows)

    async def expired(self, now: datetime | None = None) -> tuple[str, ...]:
        rows = await self._pool.fetch(
            """
            SELECT thread_id FROM langgraph.memory_threads
            WHERE status = ANY($1::text[]) AND delete_after <= $2
            ORDER BY delete_after
            """,
            [status.value for status in GraphStatus if status.terminal],
            now or datetime.now(UTC),
        )
        return tuple(row["thread_id"] for row in rows)

    async def delete(self, application_id: str) -> None:
        await self._pool.execute(
            "DELETE FROM langgraph.memory_threads WHERE thread_id = $1",
            application_id,
        )

    async def counts(self) -> MemoryStatusCounts:
        rows = await self._pool.fetch(
            "SELECT status, is_stale, count(*) AS count "
            "FROM langgraph.memory_threads GROUP BY status, is_stale"
        )
        records = []
        for row in rows:
            records.extend([(GraphStatus(row["status"]), bool(row["is_stale"]))] * row["count"])
        return _status_counts(records)

    @staticmethod
    def _record(row: asyncpg.Record) -> MemoryThreadRecord:
        payload = dict(row)
        if isinstance(payload.get("last_error"), str):
            payload["last_error"] = json.loads(payload["last_error"])
        return MemoryThreadRecord.model_validate(payload)


def _counts(records: tuple[MemoryThreadRecord, ...]) -> MemoryStatusCounts:
    return _status_counts(tuple((item.status, item.is_stale) for item in records))


def _status_counts(
    values: tuple[tuple[GraphStatus, bool], ...] | list[tuple[GraphStatus, bool]],
) -> MemoryStatusCounts:
    return MemoryStatusCounts(
        running=sum(status == GraphStatus.RUNNING for status, _ in values),
        waiting=sum(status == GraphStatus.WAITING_FOR_REVIEW for status, _ in values),
        retryable=sum(status == GraphStatus.FAILED_RETRYABLE for status, _ in values),
        needs_attention=sum(status == GraphStatus.NEEDS_ATTENTION for status, _ in values),
        terminal=sum(status.terminal for status, _ in values),
        cancelled=sum(status == GraphStatus.CANCELLED for status, _ in values),
        stale=sum(is_stale for _, is_stale in values),
    )
