"""Thin async HTTP endpoints and the same-origin Phase 6 review console."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import asyncpg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from fundermatch.api.auth import JwtAuthenticator, TokenAuthenticator
from fundermatch.clients.findociq_client import FinDocIQClient, FinDocIQClientConfig
from fundermatch.intake import (
    MAX_BATCH_BYTES,
    MAX_PDF_BYTES,
    BorrowerIntakeService,
    IntakeMetadata,
    IntakeResult,
)
from fundermatch.matching.retriever import RetrievalConfig, RuleGatedPrecedentRetriever
from fundermatch.precedent.embedder import BgeM3Config, BgeM3Embedder
from fundermatch.precedent.store import QdrantPrecedentConfig, QdrantPrecedentStore
from fundermatch.precedent.writeback import PrecedentWritebackService, WritebackResult
from fundermatch.rules.config import load_policies
from fundermatch.workflow.errors import (
    InvalidTransitionError,
    WorkflowAuthorizationError,
    WorkflowConflictError,
    WorkflowNotFoundError,
)
from fundermatch.workflow.postgres import PostgresWorkflowRepository
from fundermatch.workflow.repository import WorkflowRepository
from fundermatch.workflow.schema import (
    ActorClaims,
    ActorRole,
    AuditEvent,
    HumanDecisionCommand,
    PipelineAdvanceCommand,
    PrecedentWriteCommand,
    TransitionResult,
    WorkflowRecord,
)
from fundermatch.workflow.service import WorkflowService

bearer = HTTPBearer(auto_error=False)


class CreateWorkflowRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_id: str = Field(min_length=1, max_length=200)
    command_id: UUID = Field(default_factory=uuid4)
    reason: str = Field(default="Application entered intake", min_length=1, max_length=2000)


class AuditResponse(BaseModel):
    events: tuple[AuditEvent, ...]


def create_app(
    repository: WorkflowRepository | None = None,
    authenticator: TokenAuthenticator | None = None,
    writeback_service: PrecedentWritebackService | None = None,
    intake_service: BorrowerIntakeService | None = None,
) -> FastAPI:
    injected = repository is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if injected:
            yield
            return
        dsn = os.environ["FUNDERMATCH_DATABASE_URL"]
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        workflow_service = WorkflowService(PostgresWorkflowRepository(pool))
        app.state.workflow_service = workflow_service
        snapshot = os.getenv("FUNDERMATCH_BGE_SNAPSHOT_DIR")
        app.state.writeback_service = PrecedentWritebackService(
            workflow=workflow_service,
            store=QdrantPrecedentStore(
                QdrantPrecedentConfig(
                    url=os.getenv("FUNDERMATCH_QDRANT_URL", "http://127.0.0.1:6999"),
                    collection=os.getenv(
                        "FUNDERMATCH_QDRANT_COLLECTION", "fundermatch_precedents"
                    ),
                )
            ),
            embedder=BgeM3Embedder(
                BgeM3Config(snapshot_dir=Path(snapshot) if snapshot else None)
            ),
        )
        intake_root = os.getenv("FUNDERMATCH_INTAKE_DIR")
        ingest_token = os.getenv("FINDOCIQ_INGEST_TOKEN")
        if intake_root and ingest_token:
            findociq = FinDocIQClient(
                FinDocIQClientConfig(
                    base_url=os.getenv("FINDOCIQ_BASE_URL", "http://127.0.0.1:8989"),
                    timeout_seconds=600,
                    ingest_token=ingest_token,
                )
            )
            app.state.findociq_client = findociq
            app.state.intake_service = BorrowerIntakeService(
                storage_root=Path(intake_root),
                findociq=findociq,
                workflow=workflow_service,
                retriever=RuleGatedPrecedentRetriever(
                    client=app.state.writeback_service.store.client,
                    embedder=app.state.writeback_service.embedder,
                    config=RetrievalConfig(
                        collection=app.state.writeback_service.store.config.collection
                    ),
                ),
                policies=load_policies(
                    os.getenv("FUNDERMATCH_POLICY_PATH", "configs/funder_policies.yaml")
                ),
            )
        try:
            yield
        finally:
            if hasattr(app.state, "findociq_client"):
                await app.state.findociq_client.aclose()
            app.state.writeback_service.store.client.close()
            await pool.close()

    app = FastAPI(title="FunderMatch HITL API", version="0.7.1", lifespan=lifespan)
    static_dir = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static_dir), name="review-assets")
    if repository is not None:
        app.state.workflow_service = WorkflowService(repository)
    if writeback_service is not None:
        app.state.writeback_service = writeback_service
    if intake_service is not None:
        app.state.intake_service = intake_service
    app.state.authenticator = authenticator or JwtAuthenticator(
        secret=os.environ["FUNDERMATCH_JWT_SECRET"],
        issuer=os.getenv("FUNDERMATCH_JWT_ISSUER", "fundermatch"),
        audience=os.getenv("FUNDERMATCH_JWT_AUDIENCE", "fundermatch-api"),
    )

    def actor_from_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        request: Request,
    ) -> ActorClaims:
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="bearer token required"
            )
        try:
            return request.app.state.authenticator.authenticate(credentials.credentials)
        except WorkflowAuthorizationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    def service(request: Request) -> WorkflowService:
        return request.app.state.workflow_service

    def writeback(request: Request) -> PrecedentWritebackService:
        value = getattr(request.app.state, "writeback_service", None)
        if value is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="precedent write-back is not configured",
            )
        return value

    def intake(request: Request) -> BorrowerIntakeService:
        value = getattr(request.app.state, "intake_service", None)
        if value is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="borrower intake is not configured",
            )
        return value

    @app.exception_handler(WorkflowNotFoundError)
    async def not_found(_: Request, exc: WorkflowNotFoundError) -> HTTPException:
        return _http_exception_response(status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(WorkflowConflictError)
    async def conflict(_: Request, exc: WorkflowConflictError) -> HTTPException:
        return _http_exception_response(status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(InvalidTransitionError)
    async def invalid_transition(_: Request, exc: InvalidTransitionError) -> HTTPException:
        return _http_exception_response(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    @app.exception_handler(WorkflowAuthorizationError)
    async def forbidden(_: Request, exc: WorkflowAuthorizationError) -> HTTPException:
        return _http_exception_response(status.HTTP_403_FORBIDDEN, str(exc))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def review_console() -> FileResponse:
        return FileResponse(
            static_dir / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/v1/workflows", response_model=TransitionResult, status_code=201)
    async def create_workflow(
        payload: CreateWorkflowRequest,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        workflow_service: Annotated[WorkflowService, Depends(service)],
    ) -> TransitionResult:
        return await workflow_service.create(
            payload.application_id,
            actor,
            command_id=payload.command_id,
            reason=payload.reason,
        )

    @app.post("/v1/intake", response_model=IntakeResult, status_code=201)
    async def borrower_intake(
        metadata: Annotated[str, Form()],
        files: Annotated[list[UploadFile], File()],
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        intake_pipeline: Annotated[BorrowerIntakeService, Depends(intake)],
    ) -> IntakeResult:
        if ActorRole.PIPELINE not in actor.roles:
            raise WorkflowAuthorizationError("pipeline role required")
        try:
            parsed = IntakeMetadata.model_validate_json(metadata)
            payloads = []
            total_bytes = 0
            for item in files:
                content = await item.read(MAX_PDF_BYTES + 1)
                if len(content) > MAX_PDF_BYTES:
                    raise ValueError(f"{item.filename or 'PDF'} exceeds the 25 MB limit")
                total_bytes += len(content)
                if total_bytes > MAX_BATCH_BYTES:
                    raise ValueError("PDF batch exceeds the 512 MB aggregate limit")
                payloads.append((item.filename or "", content))
            return await intake_pipeline.process(parsed, tuple(payloads), actor)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/v1/workflows/{application_id}/pipeline", response_model=TransitionResult)
    async def advance_pipeline(
        application_id: str,
        payload: PipelineAdvanceCommand,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        workflow_service: Annotated[WorkflowService, Depends(service)],
    ) -> TransitionResult:
        return await workflow_service.advance_pipeline(application_id, payload, actor)

    @app.post("/v1/workflows/{application_id}/decision", response_model=TransitionResult)
    async def human_decision(
        application_id: str,
        payload: HumanDecisionCommand,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        workflow_service: Annotated[WorkflowService, Depends(service)],
    ) -> TransitionResult:
        return await workflow_service.decide(application_id, payload, actor)

    @app.post("/v1/workflows/{application_id}/precedent", response_model=WritebackResult)
    async def write_precedent(
        application_id: str,
        payload: PrecedentWriteCommand,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        precedent_service: Annotated[PrecedentWritebackService, Depends(writeback)],
    ) -> WritebackResult:
        return await precedent_service.write(application_id, payload, actor)

    @app.get("/v1/workflows/{application_id}", response_model=WorkflowRecord)
    async def get_workflow(
        application_id: str,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        workflow_service: Annotated[WorkflowService, Depends(service)],
    ) -> WorkflowRecord:
        _require_reader(actor)
        return await workflow_service.get(application_id)

    @app.get("/v1/workflows/{application_id}/audit", response_model=AuditResponse)
    async def get_audit(
        application_id: str,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        workflow_service: Annotated[WorkflowService, Depends(service)],
    ) -> AuditResponse:
        _require_reader(actor)
        return AuditResponse(events=await workflow_service.audit(application_id))

    return app


def _require_reader(actor: ActorClaims) -> None:
    if not actor.roles.intersection({ActorRole.PIPELINE, ActorRole.HUMAN_REVIEWER}):
        raise WorkflowAuthorizationError("workflow reader role required")


def _http_exception_response(status_code: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail})
