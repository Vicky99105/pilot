"""FastAPI app. The Temporal worker runs in the lifespan as a background task."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.routes import health
from app.config import Settings
from app.services.health.contract import HealthService
from app.services.health.service import DefaultHealthService
from app.workflows.worker import run_worker_forever

logging.basicConfig(level=logging.INFO)


def create_app(
    settings: Settings | None = None, health_service: HealthService | None = None
) -> FastAPI:
    settings = settings or Settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        if health_service is None:
            engine = create_async_engine(settings.database_url, pool_pre_ping=True)
            app.state.health_service = DefaultHealthService(settings, engine)
        worker_task = asyncio.create_task(run_worker_forever(settings), name="temporal-worker")
        try:
            yield
        finally:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="ConsintIQ Claims Pilot", lifespan=lifespan)
    app.state.health_service = health_service
    app.include_router(health.router)

    # Mounted last so API routes take precedence over static files.
    if settings.ui_dist_dir is not None and settings.ui_dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.ui_dist_dir, html=True), name="ui")
    return app


app = create_app()
