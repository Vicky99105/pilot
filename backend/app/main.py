"""FastAPI app. The Temporal worker runs in the lifespan as a background task."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.api.routes import health
from app.api.routes import monitoring as monitoring_routes
from app.api.routes import submissions as submission_routes
from app.api.routes import tasks as task_routes
from app.api.routes import users as user_routes
from app.config import Settings
from app.integrations.core.contract import PermissionService
from app.integrations.core.http import (
    HttpMoneyService,
    HttpPermissionService,
    HttpPolicyService,
)
from app.mock_core.router import router as mock_core_router
from app.services.adjudication.service import DefaultAdjudicationService
from app.services.bill_review.service import DefaultBillReviewService
from app.services.claims.contract import ClaimQueryService
from app.services.claims.service import DefaultClaimQueryService
from app.services.classification.service import DefaultClassificationService
from app.services.communication.service import DefaultCommunicationService
from app.services.decision.service import DefaultDecisionService
from app.services.digitisation.service import DefaultDigitisationService
from app.services.file_store.contract import FileStore
from app.services.file_store.service import build_file_store
from app.services.health.contract import HealthService
from app.services.health.service import DefaultHealthService
from app.services.intake.contract import IntakeService
from app.services.intake.service import DefaultIntakeService, DefaultWorkflowStarter
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.service import DefaultMedicalReviewService
from app.services.model_gateway.contract import ModelGateway
from app.services.model_gateway.service import build_model_gateway
from app.services.monitoring.contract import Monitoring
from app.services.monitoring.service import DefaultMonitoring
from app.services.notification.contract import NotificationService
from app.services.notification.service import build_notification_service
from app.services.page_prep.service import DefaultPagePreparer
from app.services.query.contract import QueryService
from app.services.query.service import DefaultQueryService
from app.services.registration.service import DefaultRegistrationService
from app.services.risk.service import DefaultRiskService
from app.services.rules.service import DefaultRulesEngine
from app.services.settlement.service import DefaultSettlementService
from app.services.tasks.contract import TaskService
from app.services.tasks.service import DefaultTaskService, DefaultWorkflowSignaller
from app.services.users.contract import UserService
from app.services.users.service import DefaultUserService
from app.workflows.claim_activities import ClaimActivities
from app.workflows.worker import run_worker_forever

logging.basicConfig(level=logging.INFO)


def create_app(
    settings: Settings | None = None,
    health_service: HealthService | None = None,
    task_service: TaskService | None = None,
    monitoring: Monitoring | None = None,
    user_service: UserService | None = None,
    intake_service: IntakeService | None = None,
    claim_queries: ClaimQueryService | None = None,
    file_store: FileStore | None = None,
    permission_service: PermissionService | None = None,
    model_gateway: ModelGateway | None = None,
    query_service: QueryService | None = None,
    notification_service: NotificationService | None = None,
) -> FastAPI:
    """Services passed in (tests) are used as given; the rest are built in the lifespan."""
    settings = settings or Settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Engines connect lazily, so the app starts even when Postgres is down.
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        mock_core_engine = create_async_engine(settings.mock_core_database_url, pool_pre_ping=True)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        app.state.mock_core_sessions = async_sessionmaker(mock_core_engine, expire_on_commit=False)
        if app.state.health_service is None:
            app.state.health_service = DefaultHealthService(settings, engine)
        if app.state.task_service is None:
            signaller = DefaultWorkflowSignaller(settings)
            app.state.task_service = DefaultTaskService(sessions, signaller)
        if app.state.monitoring is None:
            app.state.monitoring = DefaultMonitoring(sessions)
        if app.state.user_service is None:
            app.state.user_service = DefaultUserService(sessions)
        if app.state.file_store is None:
            app.state.file_store = build_file_store(settings)
        files: FileStore = app.state.file_store
        core_http = httpx.AsyncClient(timeout=10.0)
        if app.state.permission_service is None:
            app.state.permission_service = HttpPermissionService(
                settings.mock_core_base_url, core_http
            )
        if app.state.intake_service is None:
            starter = DefaultWorkflowStarter(settings)
            app.state.intake_service = DefaultIntakeService(
                sessions, files, starter, signaller=starter
            )
        if app.state.claim_queries is None:
            app.state.claim_queries = DefaultClaimQueryService(sessions)
        gateway = model_gateway or build_model_gateway(
            config_path=settings.config_dir / "models.yaml", file_store=files, sessions=sessions
        )
        rules = DefaultRulesEngine(settings.config_dir / "products")
        kb = DefaultKnowledgeBase(sessions)
        policies = HttpPolicyService(settings.mock_core_base_url, core_http)
        money = HttpMoneyService(settings.mock_core_base_url, core_http)
        notifications = notification_service or build_notification_service(settings)
        communication = DefaultCommunicationService(sessions, files, gateway, notifications)
        if app.state.query_service is None:
            app.state.query_service = DefaultQueryService(
                sessions,
                communication,
                reply_days=settings.query_reply_days,
                reminder_days=settings.query_reminder_days,
            )
        activities = ClaimActivities(
            app.state.monitoring,
            app.state.task_service,
            intake=app.state.intake_service,
            classification=DefaultClassificationService(
                sessions, files, gateway, DefaultPagePreparer(sessions, files)
            ),
            digitisation=DefaultDigitisationService(sessions, files, gateway),
            registration=DefaultRegistrationService(sessions, policies, money, rules),
            bill_review=DefaultBillReviewService(sessions, rules, gateway, kb),
            medical_review=DefaultMedicalReviewService(
                sessions, gateway, kb, rules, policies, files
            ),
            risk=DefaultRiskService(sessions, gateway, policies),
            adjudication=DefaultAdjudicationService(sessions, gateway, kb),
            decision=DefaultDecisionService(
                sessions, settings.config_dir / "authority_matrix.yaml"
            ),
            communication=communication,
            query=app.state.query_service,
            settlement=DefaultSettlementService(sessions, money),
        )
        worker_task = asyncio.create_task(
            run_worker_forever(settings, activities), name="temporal-worker"
        )
        try:
            yield
        finally:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            await core_http.aclose()
            await _dispose(engine, mock_core_engine)

    app = FastAPI(title="ConsintIQ Claims Pilot", lifespan=lifespan)
    app.state.health_service = health_service
    app.state.task_service = task_service
    app.state.monitoring = monitoring
    app.state.user_service = user_service
    app.state.intake_service = intake_service
    app.state.claim_queries = claim_queries
    app.state.file_store = file_store
    app.state.permission_service = permission_service
    app.state.query_service = query_service
    app.state.mock_core_admin_enabled = settings.mock_core_admin_enabled
    app.include_router(health.router)
    app.include_router(task_routes.router)
    app.include_router(monitoring_routes.router)
    app.include_router(submission_routes.router)
    app.include_router(user_routes.router)
    app.include_router(mock_core_router, prefix="/mock-core")

    # Mounted last so API routes take precedence over static files.
    if settings.ui_dist_dir is not None and settings.ui_dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.ui_dist_dir, html=True), name="ui")
    return app


async def _dispose(*engines: AsyncEngine) -> None:
    for engine in engines:
        await engine.dispose()


app = create_app()
