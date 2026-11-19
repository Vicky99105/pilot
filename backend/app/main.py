"""FastAPI app. The Temporal worker runs in the lifespan as a background task."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.api.routes import health
from app.api.routes import monitoring as monitoring_routes
from app.api.routes import tasks as task_routes
from app.config import Settings
from app.mock_core.router import router as mock_core_router
from app.services.health.contract import HealthService
from app.services.health.service import DefaultHealthService
from app.services.monitoring.contract import Monitoring
from app.services.monitoring.service import DefaultMonitoring
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
        activities = ClaimActivities(app.state.monitoring, app.state.task_service)
        worker_task = asyncio.create_task(
            run_worker_forever(settings, activities), name="temporal-worker"
        )
        try:
            yield
        finally:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            await _dispose(engine, mock_core_engine)

    app = FastAPI(title="ConsintIQ Claims Pilot", lifespan=lifespan)
    app.state.health_service = health_service
    app.state.task_service = task_service
    app.state.monitoring = monitoring
    app.state.user_service = user_service
    app.include_router(health.router)
    app.include_router(task_routes.router)
    app.include_router(monitoring_routes.router)
    app.include_router(mock_core_router, prefix="/mock-core")

    # Mounted last so API routes take precedence over static files.
    if settings.ui_dist_dir is not None and settings.ui_dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.ui_dist_dir, html=True), name="ui")
    return app


async def _dispose(*engines: AsyncEngine) -> None:
    for engine in engines:
        await engine.dispose()


app = create_app()
