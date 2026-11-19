from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.services.health.service import DefaultHealthService

UNREACHABLE = Settings(
    database_url="postgresql+psycopg://user:pass@127.0.0.1:1/none",
    temporal_address="127.0.0.1:1",
)


def _service() -> DefaultHealthService:
    engine = create_async_engine(UNREACHABLE.database_url)
    return DefaultHealthService(UNREACHABLE, engine, timeout_seconds=2.0)


async def test_check_database_reports_down_when_unreachable() -> None:
    result = await _service().check_database()

    assert result.status == "down"
    assert result.detail


async def test_check_temporal_reports_down_when_unreachable() -> None:
    result = await _service().check_temporal()

    assert result.status == "down"
    assert result.detail
