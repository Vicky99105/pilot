"""The isolated eval environment (tools/eval/isolated.py): its own databases, Temporal
namespace and task queue. The commands must never write to the dev databases."""

from app.database.models import Base
from tools.eval import isolated


def test_every_claims_table_is_either_copied_or_left_empty() -> None:
    """A new table forces a decision: reference data copied from dev, or claim data that
    starts empty in the eval database."""
    tables = set(Base.metadata.tables)
    assert tables == isolated.COPY_DATA | isolated.START_EMPTY
    assert not isolated.COPY_DATA & isolated.START_EMPTY
    for claim_table in ("claims", "submissions", "stage_records", "model_calls", "events"):
        assert claim_table in isolated.START_EMPTY


def test_the_dump_reads_dev_and_the_restore_writes_only_the_eval_database() -> None:
    dump, restore = isolated.copy_commands()
    assert dump[-1].startswith("pg_dump") and "-d claims " in dump[-1] + " "
    assert all(f"-t {t} " in dump[-1] + " " for t in isolated.COPY_DATA)
    assert "-d claims_eval" in restore[-1]
    for command in isolated.write_commands():
        text = " ".join(command)
        assert "-d claims " not in text + " " and "-d mock_core " not in text + " ", text


def test_the_eval_app_has_its_own_namespace_queue_port_and_databases() -> None:
    env = isolated.EVAL_ENV
    assert env["TEMPORAL_NAMESPACE"] != "default"
    assert env["TEMPORAL_TASK_QUEUE"] != "claims"
    assert env["DATABASE_URL"].endswith("/claims_eval")
    assert env["MOCK_CORE_DATABASE_URL"].endswith("/mock_core_eval")
    assert isolated.BASE_URL == "http://127.0.0.1:8001"
