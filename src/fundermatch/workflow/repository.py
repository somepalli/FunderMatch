"""Workflow persistence port and deterministic in-memory implementation."""

import asyncio
from collections.abc import Callable
from typing import Protocol

from fundermatch.workflow.errors import WorkflowConflictError, WorkflowNotFoundError
from fundermatch.workflow.schema import AuditEvent, TransitionResult, WorkflowRecord

Transition = Callable[[WorkflowRecord, int], tuple[WorkflowRecord, AuditEvent]]


class WorkflowRepository(Protocol):
    async def create(self, workflow: WorkflowRecord, event: AuditEvent) -> TransitionResult: ...

    async def get(self, application_id: str) -> WorkflowRecord: ...

    async def audit(self, application_id: str) -> tuple[AuditEvent, ...]: ...

    async def transition(
        self,
        application_id: str,
        command_id: str,
        transition: Transition,
    ) -> TransitionResult: ...


class InMemoryWorkflowRepository:
    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowRecord] = {}
        self._events: dict[str, list[AuditEvent]] = {}
        self._commands: dict[tuple[str, str], TransitionResult] = {}
        self._lock = asyncio.Lock()

    async def create(self, workflow: WorkflowRecord, event: AuditEvent) -> TransitionResult:
        async with self._lock:
            if workflow.application_id in self._workflows:
                raise WorkflowConflictError(f"workflow {workflow.application_id!r} already exists")
            result = TransitionResult(workflow=workflow, audit_event=event)
            self._workflows[workflow.application_id] = workflow
            self._events[workflow.application_id] = [event]
            self._commands[(workflow.application_id, str(event.command_id))] = result
            return result

    async def get(self, application_id: str) -> WorkflowRecord:
        try:
            return self._workflows[application_id]
        except KeyError as exc:
            raise WorkflowNotFoundError(f"workflow {application_id!r} not found") from exc

    async def audit(self, application_id: str) -> tuple[AuditEvent, ...]:
        if application_id not in self._events:
            raise WorkflowNotFoundError(f"workflow {application_id!r} not found")
        return tuple(self._events[application_id])

    async def transition(
        self,
        application_id: str,
        command_id: str,
        transition: Transition,
    ) -> TransitionResult:
        async with self._lock:
            prior = self._commands.get((application_id, command_id))
            if prior is not None:
                return prior
            current = await self.get(application_id)
            sequence = len(self._events[application_id]) + 1
            workflow, event = transition(current, sequence)
            result = TransitionResult(workflow=workflow, audit_event=event)
            self._workflows[application_id] = workflow
            self._events[application_id].append(event)
            self._commands[(application_id, command_id)] = result
            return result
