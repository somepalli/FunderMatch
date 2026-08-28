"""Durable borrower-intake jobs and sanitized live activity events."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

IntakeJobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class IntakeActivityEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    application_id: str
    sequence: int = Field(ge=1)
    stage: str
    message: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    document_name: str | None = None
    document_index: int | None = Field(default=None, ge=1)
    document_count: int | None = Field(default=None, ge=1)
    completed: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=1)
    metric: str | None = None
    worker: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    checkpoint_id: str | None = None
    guardrail_code: str | None = None
    resume_duration_ms: float | None = Field(default=None, ge=0)
    error_code: str | None = None
    retryable: bool | None = None


class IntakeJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    application_id: str
    status: IntakeJobStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    last_sequence: int = Field(ge=0)
    error_code: str | None = None
    retryable: bool | None = None


class IntakeJobSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    job: IntakeJob
    events: tuple[IntakeActivityEvent, ...] = ()


class IntakeJobAccepted(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    application_id: str
    status: Literal["queued"] = "queued"
    status_url: str
    events_url: str


class IntakeJobStore(Protocol):
    async def create(self, job_id: str, application_id: str) -> IntakeJob: ...

    async def append(
        self,
        job_id: str,
        stage: str,
        message: str,
        **details: object,
    ) -> IntakeActivityEvent: ...

    async def get(self, job_id: str) -> IntakeJob: ...

    async def events_after(
        self, job_id: str, after: int = 0
    ) -> tuple[IntakeActivityEvent, ...]: ...

    async def finish(
        self,
        job_id: str,
        *,
        status: Literal["completed", "failed", "cancelled"],
        error_code: str | None = None,
        retryable: bool | None = None,
    ) -> IntakeJob: ...

    async def fail_interrupted(self) -> int: ...


class InMemoryIntakeJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, IntakeJob] = {}
        self._events: dict[str, list[IntakeActivityEvent]] = {}
        self._lock = asyncio.Lock()

    async def create(self, job_id: str, application_id: str) -> IntakeJob:
        now = datetime.now(UTC)
        job = IntakeJob(
            job_id=job_id,
            application_id=application_id,
            status="queued",
            created_at=now,
            updated_at=now,
            last_sequence=0,
        )
        async with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"intake job {job_id!r} already exists")
            self._jobs[job_id] = job
            self._events[job_id] = []
        return job

    async def append(
        self, job_id: str, stage: str, message: str, **details: object
    ) -> IntakeActivityEvent:
        async with self._lock:
            job = self._require(job_id)
            sequence = job.last_sequence + 1
            event = IntakeActivityEvent(
                job_id=job_id,
                application_id=job.application_id,
                sequence=sequence,
                stage=stage,
                message=message,
                **details,
            )
            self._events[job_id].append(event)
            self._jobs[job_id] = job.model_copy(
                update={
                    "status": "running" if job.status == "queued" else job.status,
                    "updated_at": event.occurred_at,
                    "last_sequence": sequence,
                }
            )
            return event

    async def get(self, job_id: str) -> IntakeJob:
        async with self._lock:
            return self._require(job_id)

    async def events_after(self, job_id: str, after: int = 0) -> tuple[IntakeActivityEvent, ...]:
        async with self._lock:
            self._require(job_id)
            return tuple(event for event in self._events[job_id] if event.sequence > after)

    async def finish(
        self,
        job_id: str,
        *,
        status: Literal["completed", "failed", "cancelled"],
        error_code: str | None = None,
        retryable: bool | None = None,
    ) -> IntakeJob:
        async with self._lock:
            job = self._require(job_id)
            now = datetime.now(UTC)
            updated = job.model_copy(
                update={
                    "status": status,
                    "updated_at": now,
                    "completed_at": now,
                    "error_code": error_code,
                    "retryable": retryable,
                }
            )
            self._jobs[job_id] = updated
            return updated

    async def fail_interrupted(self) -> int:
        count = 0
        async with self._lock:
            for job_id, job in tuple(self._jobs.items()):
                if job.status in {"queued", "running"}:
                    now = datetime.now(UTC)
                    self._jobs[job_id] = job.model_copy(
                        update={
                            "status": "failed",
                            "updated_at": now,
                            "completed_at": now,
                            "error_code": "service_restarted",
                            "retryable": True,
                        }
                    )
                    count += 1
        return count

    def _require(self, job_id: str) -> IntakeJob:
        try:
            return self._jobs[job_id]
        except KeyError as error:
            raise KeyError(f"intake job {job_id!r} not found") from error


class PostgresIntakeJobStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, job_id: str, application_id: str) -> IntakeJob:
        row = await self._pool.fetchrow(
            """
            INSERT INTO intake_jobs (job_id, application_id, status)
            VALUES ($1, $2, 'queued') RETURNING *
            """,
            job_id,
            application_id,
        )
        return self._job(row)

    async def append(
        self, job_id: str, stage: str, message: str, **details: object
    ) -> IntakeActivityEvent:
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM intake_jobs WHERE job_id = $1 FOR UPDATE", job_id
            )
            if row is None:
                raise KeyError(f"intake job {job_id!r} not found")
            sequence = int(row["last_sequence"]) + 1
            event = IntakeActivityEvent(
                job_id=job_id,
                application_id=row["application_id"],
                sequence=sequence,
                stage=stage,
                message=message,
                **details,
            )
            await connection.execute(
                """
                INSERT INTO intake_job_events
                    (job_id, sequence, stage, message, details, occurred_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                """,
                job_id,
                sequence,
                stage,
                message,
                json.dumps(self._event_details(event)),
                event.occurred_at,
            )
            await connection.execute(
                """
                UPDATE intake_jobs
                SET status = CASE WHEN status = 'queued' THEN 'running' ELSE status END,
                    last_sequence = $2, updated_at = $3
                WHERE job_id = $1
                """,
                job_id,
                sequence,
                event.occurred_at,
            )
        return event

    async def get(self, job_id: str) -> IntakeJob:
        row = await self._pool.fetchrow("SELECT * FROM intake_jobs WHERE job_id = $1", job_id)
        if row is None:
            raise KeyError(f"intake job {job_id!r} not found")
        return self._job(row)

    async def events_after(self, job_id: str, after: int = 0) -> tuple[IntakeActivityEvent, ...]:
        rows = await self._pool.fetch(
            """
            SELECT e.*, j.application_id
            FROM intake_job_events e JOIN intake_jobs j USING (job_id)
            WHERE e.job_id = $1 AND e.sequence > $2 ORDER BY e.sequence
            """,
            job_id,
            after,
        )
        if not rows and not await self._pool.fetchval(
            "SELECT 1 FROM intake_jobs WHERE job_id = $1", job_id
        ):
            raise KeyError(f"intake job {job_id!r} not found")
        return tuple(self._event(row) for row in rows)

    async def finish(
        self,
        job_id: str,
        *,
        status: Literal["completed", "failed", "cancelled"],
        error_code: str | None = None,
        retryable: bool | None = None,
    ) -> IntakeJob:
        row = await self._pool.fetchrow(
            """
            UPDATE intake_jobs SET status = $2, error_code = $3, retryable = $4,
                updated_at = now(), completed_at = now()
            WHERE job_id = $1 RETURNING *
            """,
            job_id,
            status,
            error_code,
            retryable,
        )
        if row is None:
            raise KeyError(f"intake job {job_id!r} not found")
        return self._job(row)

    async def fail_interrupted(self) -> int:
        result = await self._pool.execute(
            """
            UPDATE intake_jobs SET status = 'failed', error_code = 'service_restarted',
                retryable = true, updated_at = now(), completed_at = now()
            WHERE status IN ('queued', 'running')
            """
        )
        return int(result.rsplit(" ", 1)[-1])

    @staticmethod
    def _event_details(event: IntakeActivityEvent) -> dict[str, Any]:
        return event.model_dump(
            mode="json",
            exclude={"job_id", "application_id", "sequence", "stage", "message", "occurred_at"},
            exclude_none=True,
        )

    @staticmethod
    def _job(row: asyncpg.Record) -> IntakeJob:
        return IntakeJob.model_validate(dict(row))

    @staticmethod
    def _event(row: asyncpg.Record) -> IntakeActivityEvent:
        details = row["details"]
        if isinstance(details, str):
            details = json.loads(details)
        return IntakeActivityEvent(
            job_id=row["job_id"],
            application_id=row["application_id"],
            sequence=row["sequence"],
            stage=row["stage"],
            message=row["message"],
            occurred_at=row["occurred_at"],
            **details,
        )
