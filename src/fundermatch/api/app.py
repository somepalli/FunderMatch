"""Thin async HTTP endpoints and the same-origin Phase 6 review console."""

import asyncio
import json
import os
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import asyncpg
import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fundermatch.api.auth import JwtAuthenticator, TokenAuthenticator
from fundermatch.clients.findociq_client import (
    FinDocIQClient,
    FinDocIQClientConfig,
    FinDocIQUnavailable,
)
from fundermatch.intake import (
    MAX_BATCH_BYTES,
    MAX_PDF_BYTES,
    BorrowerIntakeService,
    IntakeMetadata,
    IntakeResult,
)
from fundermatch.intake_jobs import (
    InMemoryIntakeJobStore,
    IntakeJobAccepted,
    IntakeJobSnapshot,
    IntakeJobStore,
    PostgresIntakeJobStore,
)
from fundermatch.matching.retriever import RetrievalConfig, RuleGatedPrecedentRetriever
from fundermatch.orchestration.graph import ApplicationMemoryGraph
from fundermatch.orchestration.guardrails import (
    GuardrailWorker,
    QdrantPrecedentResolver,
)
from fundermatch.orchestration.lifecycle import PostgresLifecycleStore
from fundermatch.orchestration.observability import (
    CompositeAgentSpanRecorder,
    JsonlAgentSpanRecorder,
    OtlpAgentSpanRecorder,
)
from fundermatch.orchestration.postgres import open_checkpointer
from fundermatch.orchestration.runtime import (
    AgentActivityBridge,
    AgentIntakeRuntime,
    MemoryStatus,
)
from fundermatch.orchestration.schema import GraphStatus, WorkerName, WriteReceipt
from fundermatch.orchestration.supervisor import (
    GemmaSendBackRouter,
    SupervisorRoutingConfig,
)
from fundermatch.orchestration.workers import WorkerDependencies, fixed_workers
from fundermatch.orchestration.workspace import ApplicationWorkspace
from fundermatch.precedent.embedder import BgeM3Config, BgeM3Embedder
from fundermatch.precedent.schema import PrecedentStatus
from fundermatch.precedent.store import QdrantPrecedentConfig, QdrantPrecedentStore
from fundermatch.precedent.writeback import PrecedentWritebackService, WritebackResult
from fundermatch.rules.config import load_policies
from fundermatch.security.policy import ProductionGuardrailPolicy
from fundermatch.security.rate_limit import PostgresRateLimiter
from fundermatch.security.receipts import ReceiptSigner
from fundermatch.security.secrets import read_secret
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
    HumanAction,
    HumanDecisionCommand,
    PipelineAdvanceCommand,
    PipelineReopenCommand,
    PrecedentWriteCommand,
    RestartStage,
    TransitionResult,
    WorkflowRecord,
    WorkflowState,
)
from fundermatch.workflow.service import WorkflowService

bearer = HTTPBearer(auto_error=False)


class CreateWorkflowRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_id: str = Field(min_length=1, max_length=200)
    command_id: UUID = Field(default_factory=uuid4)
    reason: str = Field(default="Application entered intake", min_length=1, max_length=2000)


class SensitiveRevealRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID = Field(default_factory=uuid4)
    expected_version: int = Field(ge=0)
    field_name: str = Field(pattern=r"^(borrower_name|finance_context|operations_context)$")
    reason: str = Field(min_length=10, max_length=1000)


class SensitiveRevealResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    application_id: str
    field_name: str
    value: str
    expires_in_seconds: int = 300


class PrecedentLifecycleRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID = Field(default_factory=uuid4)
    expected_status: Literal["active", "revoked", "superseded"] = "active"
    status: Literal["revoked", "superseded"]
    replacement_case_id: str | None = Field(default=None, min_length=3, max_length=200)
    reason: str = Field(min_length=10, max_length=1000)


class PrecedentLifecycleResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    status: PrecedentStatus
    command_id: UUID
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AuditResponse(BaseModel):
    events: tuple[AuditEvent, ...]


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool
    dependencies: dict[str, str]


def create_app(
    repository: WorkflowRepository | None = None,
    authenticator: TokenAuthenticator | None = None,
    writeback_service: PrecedentWritebackService | None = None,
    intake_service: BorrowerIntakeService | None = None,
    intake_job_store: IntakeJobStore | None = None,
    agent_runtime: AgentIntakeRuntime | None = None,
) -> FastAPI:
    injected = repository is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if injected:
            yield
            return
        dsn = read_secret("FUNDERMATCH_DATABASE_URL")
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        app.state.database_pool = pool
        app.state.intake_job_store = PostgresIntakeJobStore(pool)
        await app.state.intake_job_store.fail_interrupted()
        workflow_service = WorkflowService(PostgresWorkflowRepository(pool))
        app.state.workflow_service = workflow_service
        production_guardrails = _production_guardrails_enabled()
        if production_guardrails and not _agent_orchestration_enabled():
            raise RuntimeError(
                "production guardrails require the checkpointed agent orchestration path"
            )
        guardrail_policy = (
            ProductionGuardrailPolicy.from_yaml(
                os.getenv(
                    "FUNDERMATCH_GUARDRAIL_POLICY", "configs/guardrails/production.yaml"
                )
            )
            if production_guardrails
            else None
        )
        snapshot = os.getenv("FUNDERMATCH_BGE_SNAPSHOT_DIR")
        app.state.writeback_service = PrecedentWritebackService(
            workflow=workflow_service,
            store=QdrantPrecedentStore(
                QdrantPrecedentConfig(
                    url=os.getenv("FUNDERMATCH_QDRANT_URL", "http://127.0.0.1:6999"),
                    collection=os.getenv("FUNDERMATCH_QDRANT_COLLECTION", "fundermatch_precedents"),
                )
            ),
            embedder=BgeM3Embedder(BgeM3Config(snapshot_dir=Path(snapshot) if snapshot else None)),
            policy_hash=(guardrail_policy.policy_hash if guardrail_policy else None),
            validity_days=(
                guardrail_policy.precedent.default_validity_days
                if guardrail_policy
                else None
            ),
            outbox_pool=pool,
        )
        intake_root = os.getenv("FUNDERMATCH_INTAKE_DIR")
        ingest_token = os.getenv("FINDOCIQ_INGEST_TOKEN")
        if intake_root and (ingest_token or production_guardrails):
            receipt_signer = (
                ReceiptSigner(_read_secret("FUNDERMATCH_RECEIPT_SIGNING_SECRET"))
                if production_guardrails
                else None
            )
            findociq = FinDocIQClient(
                FinDocIQClientConfig(
                    base_url=os.getenv("FINDOCIQ_BASE_URL", "http://127.0.0.1:8989"),
                    timeout_seconds=7200,
                    ingest_token=ingest_token,
                    production_guardrails_enabled=production_guardrails,
                    service_jwt_secret=(
                        _read_secret("FINDOCIQ_SERVICE_JWT_SECRET")
                        if production_guardrails
                        else None
                    ),
                    service_jwt_issuer=(
                        guardrail_policy.service_auth.issuer
                        if guardrail_policy
                        else "fundermatch"
                    ),
                    service_jwt_audience=(
                        guardrail_policy.service_auth.audience
                        if guardrail_policy
                        else "findociq-api"
                    ),
                    guardrail_policy_hash=(
                        guardrail_policy.policy_hash if guardrail_policy else None
                    ),
                )
            )
            app.state.findociq_client = findociq
            policies = load_policies(
                os.getenv("FUNDERMATCH_POLICY_PATH", "configs/funder_policies.yaml")
            )
            retriever = RuleGatedPrecedentRetriever(
                client=app.state.writeback_service.store.client,
                embedder=app.state.writeback_service.embedder,
                config=RetrievalConfig(
                    collection=app.state.writeback_service.store.config.collection,
                    require_active_lifecycle=production_guardrails,
                ),
            )
            app.state.intake_service = BorrowerIntakeService(
                storage_root=Path(intake_root),
                findociq=findociq,
                workflow=workflow_service,
                retriever=retriever,
                policies=policies,
            )
            if _agent_orchestration_enabled():
                checkpoint_context = open_checkpointer(dsn)
                checkpointer = await checkpoint_context.__aenter__()
                app.state.agent_checkpoint_context = checkpoint_context
                workspace = ApplicationWorkspace(
                    Path(intake_root),
                    master_key=(
                        b64decode(
                            _read_secret("FUNDERMATCH_DOCUMENT_MASTER_KEY"), validate=True
                        )
                        if production_guardrails
                        else None
                    ),
                    key_version=os.getenv("FUNDERMATCH_DOCUMENT_KEY_VERSION", "v1"),
                )
                activity = AgentActivityBridge(app.state.intake_job_store)
                dependencies = WorkerDependencies(
                    workspace=workspace,
                    findociq=findociq,
                    workflow=workflow_service,
                    retriever=retriever,
                    policies=policies,
                    actor=ActorClaims(
                        actor_id="fundermatch-agent-pipeline",
                        display_name="FunderMatch Agent Pipeline",
                        roles=frozenset({ActorRole.PIPELINE}),
                    ),
                    activity=activity,
                )
                graph = ApplicationMemoryGraph(
                    workers=fixed_workers(
                        dependencies,
                        GuardrailWorker(
                            workspace,
                            QdrantPrecedentResolver(
                                app.state.writeback_service.store.client,
                                app.state.writeback_service.store.config.collection,
                            ),
                            policy_hash=(
                                guardrail_policy.policy_hash if guardrail_policy else None
                            ),
                            receipt_signer=receipt_signer,
                            execution_policies=(
                                guardrail_policy.workers if guardrail_policy else None
                            ),
                        ),
                    ),
                    checkpointer=checkpointer,
                    lifecycle=PostgresLifecycleStore(pool),
                    recorder=_agent_recorder(Path(intake_root)),
                    activity=activity,
                    execution_policies=(guardrail_policy.workers if guardrail_policy else None),
                    policy_hash=(guardrail_policy.policy_hash if guardrail_policy else None),
                    receipt_signer=receipt_signer,
                    retention_cleanup=lambda application_id: _retention_cleanup(
                        pool,
                        findociq,
                        workspace,
                        guardrail_policy.policy_hash if guardrail_policy else "",
                        application_id,
                    ),
                )
                app.state.agent_runtime = AgentIntakeRuntime(
                    graph=graph,
                    workspace=workspace,
                    jobs=app.state.intake_job_store,
                    activity=activity,
                    sendback_router=GemmaSendBackRouter(
                        SupervisorRoutingConfig(
                            base_url=os.getenv(
                                "FUNDERMATCH_VLLM_BASE_URL",
                                "http://127.0.0.1:8900/v1",
                            )
                        )
                    ),
                )
                maintenance_task = asyncio.create_task(
                    _agent_maintenance_loop(graph),
                    name="fundermatch-agent-maintenance",
                )
                app.state.intake_tasks.add(maintenance_task)
                maintenance_task.add_done_callback(app.state.intake_tasks.discard)
        try:
            yield
        finally:
            tasks = tuple(app.state.intake_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if hasattr(app.state, "findociq_client"):
                await app.state.findociq_client.aclose()
            if hasattr(app.state, "agent_checkpoint_context"):
                await app.state.agent_checkpoint_context.__aexit__(None, None, None)
            app.state.writeback_service.store.client.close()
            await pool.close()

    app = FastAPI(title="FunderMatch HITL API", version="0.8.0", lifespan=lifespan)
    if _production_guardrails_enabled():
        allowed_hosts = [
            item.strip()
            for item in os.getenv("FUNDERMATCH_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
            if item.strip()
        ]
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    static_dir = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static_dir), name="review-assets")
    if repository is not None:
        app.state.workflow_service = WorkflowService(repository)
    if writeback_service is not None:
        app.state.writeback_service = writeback_service
    if intake_service is not None:
        app.state.intake_service = intake_service
    if agent_runtime is not None:
        app.state.agent_runtime = agent_runtime
    app.state.intake_job_store = intake_job_store or InMemoryIntakeJobStore()
    app.state.intake_tasks = set()
    app.state.authenticator = authenticator or JwtAuthenticator(
        secret=_read_secret("FUNDERMATCH_JWT_SECRET"),
        issuer=os.getenv("FUNDERMATCH_JWT_ISSUER", "fundermatch"),
        audience=os.getenv("FUNDERMATCH_JWT_AUDIENCE", "fundermatch-api"),
    )

    @app.middleware("http")
    async def production_security(request: Request, call_next):  # type: ignore[no-untyped-def]
        correlation_id = request.headers.get("X-Correlation-ID") or uuid4().hex
        concurrency_lease_id = uuid4().hex
        concurrency_limiter: PostgresRateLimiter | None = None
        concurrency_acquired = False
        if _production_guardrails_enabled():
            pool = getattr(request.app.state, "database_pool", None)
            category = _rate_category(request.method, request.url.path)
            if pool is not None and category is not None:
                policy = ProductionGuardrailPolicy.from_yaml(
                    os.getenv(
                        "FUNDERMATCH_GUARDRAIL_POLICY",
                        "configs/guardrails/production.yaml",
                    )
                )
                authorization = request.headers.get("Authorization", "anonymous")
                subject = sha256(authorization.encode()).hexdigest()
                concurrency_limiter = PostgresRateLimiter(pool)
                if not await concurrency_limiter.allow(
                    subject, category, policy.api_limits[category]
                ):
                    return _http_exception_response(
                        status.HTTP_429_TOO_MANY_REQUESTS,
                        "request rate limit exceeded",
                        headers={"Retry-After": str(policy.api_limits[category].window_seconds)},
                    )
                concurrency_acquired = await concurrency_limiter.acquire_concurrency(
                    subject,
                    category,
                    concurrency_lease_id,
                    policy.api_limits[category],
                )
                if not concurrency_acquired:
                    return _http_exception_response(
                        status.HTTP_429_TOO_MANY_REQUESTS,
                        "request concurrency limit exceeded",
                        headers={"Retry-After": "5"},
                    )
        try:
            response = await call_next(request)
        finally:
            if concurrency_limiter is not None and concurrency_acquired:
                await concurrency_limiter.release_concurrency(concurrency_lease_id)
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        return response

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

    def intake_jobs(request: Request) -> IntakeJobStore:
        return request.app.state.intake_job_store

    def agents(request: Request) -> AgentIntakeRuntime:
        value = getattr(request.app.state, "agent_runtime", None)
        if value is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="agent orchestration is not configured",
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

    @app.get("/ready", response_model=ReadinessResponse)
    async def readiness(request: Request, response: Response) -> ReadinessResponse:
        dependencies: dict[str, str] = {}
        pool = getattr(request.app.state, "database_pool", None)
        dependencies["postgresql"] = await _postgres_health(pool)
        dependencies["qdrant"] = await _qdrant_health(
            getattr(request.app.state, "writeback_service", None)
        )
        findociq = getattr(request.app.state, "findociq_client", None)
        dependencies["findociq"] = (
            "healthy" if findociq is not None and await findociq.health() else "unavailable"
        )
        dependencies["vllm"] = await _http_dependency_health(
            os.getenv("FUNDERMATCH_VLLM_HEALTH_URL")
        )
        dependencies["langfuse"] = await _http_dependency_health(
            os.getenv("FUNDERMATCH_LANGFUSE_HEALTH_URL"), warning=True
        )
        required = ("postgresql", "qdrant", "findociq", "vllm")
        ready = all(dependencies[item] == "healthy" for item in required)
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(ready=ready, dependencies=dependencies)

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
        except FinDocIQUnavailable as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="FinDocIQ could not process the borrower documents; retry is safe",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/v1/intake-jobs", response_model=IntakeJobAccepted, status_code=202)
    async def create_intake_job(
        request: Request,
        metadata: Annotated[str, Form()],
        files: Annotated[list[UploadFile], File()],
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        intake_pipeline: Annotated[BorrowerIntakeService, Depends(intake)],
        job_store: Annotated[IntakeJobStore, Depends(intake_jobs)],
    ) -> IntakeJobAccepted:
        if ActorRole.PIPELINE not in actor.roles:
            raise WorkflowAuthorizationError("pipeline role required")
        try:
            parsed = IntakeMetadata.model_validate_json(metadata)
            payloads = await _read_uploaded_pdfs(files)
            job_id = f"intake-{uuid4().hex}"
            await job_store.create(job_id, parsed.application_id)
            await job_store.append(
                job_id,
                "upload_received",
                f"Received {len(payloads)} PDF(s) for processing",
                completed=len(payloads),
                total=len(payloads),
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except asyncpg.UniqueViolationError as error:
            raise HTTPException(
                status_code=409,
                detail=f"application {parsed.application_id} already has an active intake job",
            ) from error

        configured_agent = getattr(request.app.state, "agent_runtime", None)
        runner = (
            _run_agent_intake_job(job_store, job_id, parsed, payloads, configured_agent)
            if configured_agent is not None
            else _run_intake_job(
                job_store,
                job_id,
                parsed,
                payloads,
                actor,
                intake_pipeline,
            )
        )
        task = asyncio.create_task(
            runner,
            name=f"fundermatch-{job_id}",
        )
        request.app.state.intake_tasks.add(task)
        task.add_done_callback(request.app.state.intake_tasks.discard)
        return IntakeJobAccepted(
            job_id=job_id,
            application_id=parsed.application_id,
            status_url=f"/v1/intake-jobs/{job_id}",
            events_url=f"/v1/intake-jobs/{job_id}/events",
        )

    @app.get("/v1/intake-jobs/{job_id}", response_model=IntakeJobSnapshot)
    async def intake_job_status(
        job_id: str,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        job_store: Annotated[IntakeJobStore, Depends(intake_jobs)],
    ) -> IntakeJobSnapshot:
        _require_reader(actor)
        return await _job_snapshot(job_store, job_id)

    @app.get("/v1/intake-jobs/{job_id}/events", response_model=IntakeJobSnapshot)
    async def intake_job_events(
        job_id: str,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        job_store: Annotated[IntakeJobStore, Depends(intake_jobs)],
        after: int = 0,
    ) -> IntakeJobSnapshot:
        _require_reader(actor)
        return await _job_snapshot(job_store, job_id, after=max(0, after))

    @app.post("/v1/intake-jobs/{job_id}/resume", response_model=MemoryStatus)
    async def resume_intake_job(
        request: Request,
        job_id: str,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        runtime: Annotated[AgentIntakeRuntime, Depends(agents)],
    ) -> MemoryStatus:
        if ActorRole.PIPELINE not in actor.roles:
            raise WorkflowAuthorizationError("pipeline role required")
        try:
            job = await runtime.jobs.get(job_id)
            memory_state = await runtime.graph.state(job.application_id)
            if memory_state.current_node == WorkerName.HUMAN_REVIEW:
                workflow_record = await request.app.state.workflow_service.get(job.application_id)
                if (
                    memory_state.status == GraphStatus.FAILED_RETRYABLE
                    and workflow_record.state == WorkflowState.HUMAN_DECIDED
                    and workflow_record.decision is not None
                    and workflow_record.decision.action != HumanAction.SEND_BACK
                ):
                    await _finalize_human_decision(
                        request, job.application_id, workflow_record.version
                    )
                    return await runtime.memory(job.application_id)
            state = await runtime.resume(job_id)
            return await runtime.memory(state.application_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="intake job not found") from error

    @app.post("/v1/intake-jobs/{job_id}/cancel", response_model=MemoryStatus)
    async def cancel_intake_job(
        job_id: str,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        runtime: Annotated[AgentIntakeRuntime, Depends(agents)],
    ) -> MemoryStatus:
        if ActorRole.PIPELINE not in actor.roles:
            raise WorkflowAuthorizationError("pipeline role required")
        try:
            state = await runtime.cancel(job_id)
            return await runtime.memory(state.application_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="intake job not found") from error

    @app.get("/v1/applications/{application_id}/memory", response_model=MemoryStatus)
    async def application_memory(
        application_id: str,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        runtime: Annotated[AgentIntakeRuntime, Depends(agents)],
    ) -> MemoryStatus:
        _require_reader(actor)
        try:
            return await runtime.memory(application_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="application memory not found") from error

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
        request: Request,
        application_id: str,
        payload: HumanDecisionCommand,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        workflow_service: Annotated[WorkflowService, Depends(service)],
    ) -> TransitionResult:
        result = await workflow_service.decide(application_id, payload, actor)
        runtime = getattr(request.app.state, "agent_runtime", None)
        if payload.action == HumanAction.SEND_BACK and runtime is not None:
            restart = payload.restart_stage or RestartStage.SUPERVISOR
            if restart == RestartStage.SUPERVISOR:
                resolved = await runtime.resolve_supervisor(application_id, payload.reason)
                if resolved is None:
                    await runtime.supervisor_needs_attention(application_id)
                    return result
                restart = RestartStage(resolved.value)
            if restart != RestartStage.SUPERVISOR:
                await workflow_service.reopen_after_send_back(
                    application_id,
                    PipelineReopenCommand(
                        command_id=uuid5(
                            NAMESPACE_URL,
                            f"fundermatch:{application_id}:reopen:{payload.command_id}",
                        ),
                        expected_version=result.workflow.version,
                        restart_stage=restart,
                        reason=payload.reason,
                    ),
                    actor,
                )
                await runtime.send_back(application_id, WorkerName(restart.value))
        elif runtime is not None:
            await _finalize_human_decision(request, application_id, result.workflow.version)
        return result

    @app.post("/v1/workflows/{application_id}/precedent", response_model=WritebackResult)
    async def write_precedent(
        request: Request,
        application_id: str,
        payload: PrecedentWriteCommand,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        precedent_service: Annotated[PrecedentWritebackService, Depends(writeback)],
    ) -> WritebackResult:
        result = await precedent_service.write(application_id, payload, actor)
        runtime = getattr(request.app.state, "agent_runtime", None)
        if runtime is not None:
            receipt = result.transition.workflow.precedent_receipt
            if receipt is None:
                raise RuntimeError("verified precedent write-back returned no receipt")
            await runtime.graph.complete_after_writeback(
                application_id,
                WriteReceipt(
                    store=f"qdrant:{receipt.collection}",
                    record_id=receipt.case_id,
                    payload_sha256=receipt.payload_sha256,
                ),
            )
        return result

    @app.get("/v1/workflows/{application_id}", response_model=WorkflowRecord)
    async def get_workflow(
        application_id: str,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        workflow_service: Annotated[WorkflowService, Depends(service)],
    ) -> WorkflowRecord:
        _require_reader(actor)
        record = await workflow_service.get(application_id)
        return _mask_workflow(record) if _production_guardrails_enabled() else record

    @app.post(
        "/v1/applications/{application_id}/reveal",
        response_model=SensitiveRevealResponse,
    )
    async def reveal_sensitive_field(
        request: Request,
        application_id: str,
        payload: SensitiveRevealRequest,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        workflow_service: Annotated[WorkflowService, Depends(service)],
    ) -> SensitiveRevealResponse:
        if ActorRole.HUMAN_REVIEWER not in actor.roles:
            raise WorkflowAuthorizationError("human reviewer role required")
        record = await workflow_service.get(application_id)
        if record.version != payload.expected_version:
            raise WorkflowConflictError("reveal request used a stale workflow version")
        runtime = getattr(request.app.state, "agent_runtime", None)
        if runtime is None:
            raise HTTPException(503, "application workspace is not configured")
        intake_request = runtime.workspace.request(application_id)
        value = str(getattr(intake_request.metadata, payload.field_name))
        pool = getattr(request.app.state, "database_pool", None)
        if pool is None:
            raise HTTPException(503, "reveal audit storage is unavailable")
        await pool.execute(
            """
            INSERT INTO sensitive_reveal_audit
                (command_id, application_id, actor_id, field_name, reason)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (command_id) DO NOTHING
            """,
            payload.command_id,
            application_id,
            actor.actor_id,
            payload.field_name,
            payload.reason,
        )
        return SensitiveRevealResponse(
            application_id=application_id,
            field_name=payload.field_name,
            value=value,
        )

    @app.post(
        "/v1/precedents/{case_id}/lifecycle",
        response_model=PrecedentLifecycleResponse,
    )
    async def change_precedent_lifecycle(
        request: Request,
        case_id: str,
        payload: PrecedentLifecycleRequest,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        precedent_service: Annotated[PrecedentWritebackService, Depends(writeback)],
    ) -> PrecedentLifecycleResponse:
        if ActorRole.HUMAN_REVIEWER not in actor.roles:
            raise WorkflowAuthorizationError("human reviewer role required")
        if payload.status == "superseded" and not payload.replacement_case_id:
            raise InvalidTransitionError("superseded precedent requires replacement_case_id")
        pool = getattr(request.app.state, "database_pool", None)
        if pool is None:
            raise HTTPException(503, "precedent lifecycle audit storage is unavailable")
        existing = await pool.fetchrow(
            "SELECT case_id, status, policy_hash FROM precedent_lifecycle WHERE command_id = $1",
            payload.command_id,
        )
        if existing is not None:
            return PrecedentLifecycleResponse(
                case_id=existing["case_id"],
                status=PrecedentStatus(existing["status"]),
                command_id=payload.command_id,
                policy_hash=existing["policy_hash"],
            )
        policy = ProductionGuardrailPolicy.from_yaml(
            os.getenv("FUNDERMATCH_GUARDRAIL_POLICY", "configs/guardrails/production.yaml")
        )
        try:
            changed = await asyncio.to_thread(
                precedent_service.store.set_lifecycle,
                case_id,
                expected_status=PrecedentStatus(payload.expected_status),
                status=PrecedentStatus(payload.status),
            )
        except KeyError as error:
            raise HTTPException(404, "precedent not found") from error
        except ValueError as error:
            raise WorkflowConflictError(str(error)) from error
        await pool.execute(
            """
            INSERT INTO precedent_lifecycle
                (case_id, status, policy_hash, valid_until, supersedes_case_id,
                 command_id, reason, actor_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (command_id) DO NOTHING
            """,
            case_id,
            changed.lifecycle_status.value,
            policy.policy_hash,
            changed.valid_until,
            payload.replacement_case_id,
            payload.command_id,
            payload.reason,
            actor.actor_id,
        )
        return PrecedentLifecycleResponse(
            case_id=case_id,
            status=changed.lifecycle_status,
            command_id=payload.command_id,
            policy_hash=policy.policy_hash,
        )

    @app.get("/v1/workflows/{application_id}/audit", response_model=AuditResponse)
    async def get_audit(
        application_id: str,
        actor: Annotated[ActorClaims, Depends(actor_from_token)],
        workflow_service: Annotated[WorkflowService, Depends(service)],
    ) -> AuditResponse:
        _require_reader(actor)
        return AuditResponse(events=await workflow_service.audit(application_id))

    return app


async def _read_uploaded_pdfs(
    files: list[UploadFile],
) -> tuple[tuple[str, bytes], ...]:
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
    if not payloads:
        raise ValueError("at least one borrower PDF is required")
    return tuple(payloads)


async def _run_intake_job(
    store: IntakeJobStore,
    job_id: str,
    metadata: IntakeMetadata,
    payloads: tuple[tuple[str, bytes], ...],
    actor: ActorClaims,
    intake_pipeline: BorrowerIntakeService,
) -> None:
    async def report(stage: str, message: str, **details: object) -> None:
        await store.append(job_id, stage, message, **details)

    try:
        await intake_pipeline.process(
            metadata,
            payloads,
            actor,
            job_id=job_id,
            progress=report,
        )
    except asyncio.CancelledError:
        await report(
            "failed",
            "Processing stopped because the local service shut down",
            error_code="service_stopped",
            retryable=True,
        )
        await store.finish(job_id, status="failed", error_code="service_stopped", retryable=True)
        raise
    except FinDocIQUnavailable:
        await report(
            "failed",
            "FinDocIQ could not complete the current processing stage",
            error_code="findociq_unavailable",
            retryable=True,
        )
        await store.finish(
            job_id,
            status="failed",
            error_code="findociq_unavailable",
            retryable=True,
        )
    except ValueError:
        await report(
            "failed",
            "The intake data could not be validated or grounded",
            error_code="intake_validation_failed",
            retryable=False,
        )
        await store.finish(
            job_id,
            status="failed",
            error_code="intake_validation_failed",
            retryable=False,
        )
    except Exception:
        await report(
            "failed",
            "An unexpected local processing error occurred",
            error_code="internal_processing_error",
            retryable=True,
        )
        await store.finish(
            job_id,
            status="failed",
            error_code="internal_processing_error",
            retryable=True,
        )
    else:
        await report("completed", "Processing completed; opening human review")
        await store.finish(job_id, status="completed")


async def _run_agent_intake_job(
    store: IntakeJobStore,
    job_id: str,
    metadata: IntakeMetadata,
    payloads: tuple[tuple[str, bytes], ...],
    runtime: AgentIntakeRuntime,
) -> None:
    try:
        await runtime.start(metadata, payloads, job_id=job_id)
    except asyncio.CancelledError:
        await store.append(
            job_id,
            "interrupted",
            "Agent execution stopped; the last checkpoint remains resumable",
            error_code="service_stopped",
            retryable=True,
        )
        await store.finish(
            job_id,
            status="failed",
            error_code="service_stopped",
            retryable=True,
        )
        raise
    except Exception:
        await store.append(
            job_id,
            "failed",
            "Agent execution failed before a safe status could be returned",
            error_code="agent_runtime_error",
            retryable=True,
        )
        await store.finish(
            job_id,
            status="failed",
            error_code="agent_runtime_error",
            retryable=True,
        )


async def _job_snapshot(store: IntakeJobStore, job_id: str, *, after: int = 0) -> IntakeJobSnapshot:
    try:
        job = await store.get(job_id)
        events = await store.events_after(job_id, after)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="intake job not found") from error
    return IntakeJobSnapshot(job=job, events=events)


def _require_reader(actor: ActorClaims) -> None:
    if not actor.roles.intersection({ActorRole.PIPELINE, ActorRole.HUMAN_REVIEWER}):
        raise WorkflowAuthorizationError("workflow reader role required")


def _agent_orchestration_enabled() -> bool:
    return os.getenv("FUNDERMATCH_AGENT_ORCHESTRATION_ENABLED", "false").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _production_guardrails_enabled() -> bool:
    return os.getenv("FUNDERMATCH_PRODUCTION_GUARDRAILS_ENABLED", "false").casefold() in {
        "1",
        "true",
        "yes",
    }


def _read_secret(name: str) -> str:
    return read_secret(name)


def _agent_recorder(intake_root: Path) -> CompositeAgentSpanRecorder:
    recorders = [JsonlAgentSpanRecorder(intake_root / "operations" / "agent-spans.jsonl")]
    endpoint = os.getenv("FUNDERMATCH_LANGFUSE_OTLP_ENDPOINT")
    public_key = _optional_secret("LANGFUSE_PUBLIC_KEY")
    secret_key = _optional_secret("LANGFUSE_SECRET_KEY")
    if endpoint and public_key and secret_key:
        credentials = b64encode(f"{public_key}:{secret_key}".encode()).decode()
        with suppress(RuntimeError):
            recorders.append(
                OtlpAgentSpanRecorder(
                    endpoint=endpoint,
                    headers={"Authorization": f"Basic {credentials}"},
                )
            )
    return CompositeAgentSpanRecorder(*recorders)


def _optional_secret(name: str) -> str | None:
    try:
        return read_secret(name)
    except RuntimeError:
        return None


async def _agent_maintenance_loop(graph: ApplicationMemoryGraph) -> None:
    from fundermatch.orchestration.maintenance import AgentMaintenance

    interval = max(60, int(os.getenv("FUNDERMATCH_MAINTENANCE_INTERVAL_SECONDS", "3600")))
    maintenance = AgentMaintenance(graph)
    while True:
        await maintenance.run_once()
        await asyncio.sleep(interval)


async def _retention_cleanup(
    pool: asyncpg.Pool,
    findociq: FinDocIQClient,
    workspace: ApplicationWorkspace,
    policy_hash: str,
    application_id: str,
) -> None:
    """Run one idempotent application deletion through the transactional outbox."""

    command_id = uuid5(
        NAMESPACE_URL, f"fundermatch:{application_id}:retention-delete:{policy_hash}"
    )
    payload_hash = sha256(f"{application_id}|{policy_hash}".encode()).hexdigest()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            INSERT INTO guardrail_outbox
                (command_id, application_id, operation, payload_hash, status)
            VALUES ($1, $2, 'retention_delete', $3, 'pending')
            ON CONFLICT (command_id) DO UPDATE
                SET updated_at = guardrail_outbox.updated_at
            RETURNING status, receipt, application_id, operation, payload_hash
            """,
            command_id,
            application_id,
            payload_hash,
        )
    if (
        row["application_id"] != application_id
        or row["operation"] != "retention_delete"
        or row["payload_hash"] != payload_hash
    ):
        raise RuntimeError("retention command identity conflict")
    if row["status"] == "completed":
        await asyncio.to_thread(workspace.delete_application, application_id)
        return
    try:
        receipt = await findociq.retention_delete(application_id, str(command_id))
        await asyncio.to_thread(workspace.delete_application, application_id)
    except Exception:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE guardrail_outbox
                SET status = 'failed', attempts = attempts + 1,
                    last_error_code = 'retention_dependency_failed', updated_at = now()
                WHERE command_id = $1
                """,
                command_id,
            )
        raise
    receipt_payload = receipt.model_dump(mode="json")
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """
            UPDATE guardrail_outbox
            SET status = 'completed', attempts = attempts + 1,
                receipt = $2::jsonb, last_error_code = NULL, updated_at = now()
            WHERE command_id = $1
            """,
            command_id,
            json.dumps(receipt_payload, sort_keys=True),
        )
        await connection.execute(
            """
            INSERT INTO retention_tombstones
                (application_id, policy_hash, artifact_hashes)
            VALUES ($1, $2, $3::jsonb)
            ON CONFLICT (application_id) DO NOTHING
            """,
            application_id,
            policy_hash,
            json.dumps(
                {
                    "findociq_receipt": receipt.receipt_sha256,
                    "payload": payload_hash,
                },
                sort_keys=True,
            ),
        )


async def _finalize_human_decision(
    request: Request, application_id: str, expected_version: int
) -> bool:
    runtime = request.app.state.agent_runtime
    precedent_service = request.app.state.writeback_service
    actor = ActorClaims(
        actor_id="fundermatch-agent-pipeline",
        display_name="FunderMatch Agent Pipeline",
        roles=frozenset({ActorRole.PIPELINE}),
    )
    command = PrecedentWriteCommand(
        command_id=uuid5(
            NAMESPACE_URL,
            f"fundermatch:{application_id}:verified-precedent:{expected_version}",
        ),
        expected_version=expected_version,
        reason="Verified human decision written to precedent memory",
    )
    try:
        result = await precedent_service.write(application_id, command, actor)
        receipt = result.transition.workflow.precedent_receipt
        if receipt is None:
            raise RuntimeError("precedent receipt missing")
        await runtime.graph.complete_after_writeback(
            application_id,
            WriteReceipt(
                store=f"qdrant:{receipt.collection}",
                record_id=receipt.case_id,
                payload_sha256=receipt.payload_sha256,
            ),
        )
    except Exception:
        state = await runtime.graph.mark_retryable(
            application_id,
            code="precedent_writeback_failed",
            message="Human decision is saved; verified precedent write-back can be retried",
        )
        if state.job_id is not None:
            await runtime.jobs.append(
                state.job_id,
                "worker_failed",
                "Human decision saved; precedent write-back will resume safely",
                worker=WorkerName.HUMAN_REVIEW.value,
                error_code="precedent_writeback_failed",
                retryable=True,
            )
        return False
    return True


async def _postgres_health(pool: asyncpg.Pool | None) -> str:
    if pool is None:
        return "unavailable"
    try:
        await pool.fetchval("SELECT 1")
    except Exception:
        return "unavailable"
    return "healthy"


async def _qdrant_health(service: PrecedentWritebackService | None) -> str:
    if service is None:
        return "unavailable"
    try:
        await asyncio.to_thread(service.store.client.get_collections)
    except Exception:
        return "unavailable"
    return "healthy"


async def _http_dependency_health(url: str | None, *, warning: bool = False) -> str:
    if not url:
        return "warning_not_configured" if warning else "unavailable"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError):
        return "warning_unavailable" if warning else "unavailable"
    return "healthy"


def _rate_category(method: str, path: str) -> str | None:
    if path in {"/health", "/ready", "/"} or path.startswith("/assets/"):
        return None
    if path == "/v1/intake-jobs" and method == "POST":
        return "upload"
    if path.endswith("/resume"):
        return "resume"
    if path.endswith("/decision"):
        return "review"
    if "/reveal" in path:
        return "reveal"
    return "read"


def _mask_workflow(record: WorkflowRecord) -> WorkflowRecord:
    if record.suggestion is None:
        return record
    suggestion = dict(record.suggestion)
    application = dict(suggestion.get("application", {}))
    for field in ("borrower_name", "finance_context", "operations_context"):
        if application.get(field):
            application[field] = "[MASKED]"
    suggestion["application"] = application
    return record.model_copy(update={"suggestion": suggestion})


def _http_exception_response(
    status_code: int, detail: str, headers: dict[str, str] | None = None
):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail}, headers=headers)
