"""An isolated eval environment beside the dev stack, so earlier acceptance claims (which fire
risk flags RF5 and RF7 on repeat claims) never touch eval scores, and eval claims never
touch dev data. Nothing here deletes or resets dev data.

What it is:
- Databases claims_eval and mock_core_eval in the same Postgres container. claims_eval gets
  the dev schema, plus the data of the reference and seed tables (COPY_DATA) read with
  pg_dump from claims; claim tables (START_EMPTY) start empty. mock_core_eval is seeded fresh
  by the mock core seed, plus eval-only submit grants (the member user may submit for every
  mock policy, so a case can use any persona).
- Temporal namespace "eval" and task queue "claims-eval", so workflow IDs never meet the dev
  worker's.
- Service app-eval (deploy/docker-compose.eval.yml) on 127.0.0.1:8001, with its own file
  store volume.

    uv run --project backend python tools/eval/isolated.py setup   # idempotent
    uv run --project backend python tools/eval/isolated.py status

The app-eval container is started with the same key handling as the dev stack (the key is
read from .env by awk inside the command, never printed):

    GEMINI_API_KEY="$(awk -F= '/^GEMINI_API_KEY=/{sub(/^[^=]*=/,""); gsub(/["\\r ]/,""); \
print}' .env)" docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml \
-f deploy/docker-compose.eval.yml up -d --build app-eval

To compare models, add MODEL_ROLE_OVERRIDES="role=provider:model,..." to that command.
"""

import argparse
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    "deploy/docker-compose.yml",
    "-f",
    "deploy/docker-compose.local.yml",
    "-f",
    "deploy/docker-compose.eval.yml",
]
CLAIMS_DB, MOCK_DB = "claims_eval", "mock_core_eval"
NAMESPACE, TASK_QUEUE = "eval", "claims-eval"
BASE_URL = "http://127.0.0.1:8001"
_PG = "postgresql+psycopg://${POSTGRES_USER:-claims}:${POSTGRES_PASSWORD:-local-dev-only}@postgres:5432"
EVAL_ENV = {
    "DATABASE_URL": f"{_PG}/{CLAIMS_DB}",
    "MOCK_CORE_DATABASE_URL": f"{_PG}/{MOCK_DB}",
    "TEMPORAL_NAMESPACE": NAMESPACE,
    "TEMPORAL_TASK_QUEUE": TASK_QUEUE,
}

# Reference data and seeds: copied from dev, so the knowledge base need not be reloaded.
COPY_DATA = frozenset(
    {
        "users",
        "user_roles",
        "hospitals",
        "hospital_agreements",
        "package_crosswalk",
        "document_checklists",
        "policy_clauses",
        "non_payable_items",
        "exclusions",
        "icd_tabular",
        "icd_index",
        "pcs_tables",
        "pcs_rows",
        "pcs_definitions",
        "package_masters",
        "package_items",
        "rate_factors",
        "los_norms",
        "drug_prices",
        "nlem_medicines",
        "guideline_sections",
        "kb_source_loads",
    }
)
# Claim data: schema only, so the eval starts with no earlier claims.
START_EMPTY = frozenset(
    {
        "claims",
        "submissions",
        "files",
        "documents",
        "pages",
        "stage_records",
        "events",
        "tasks",
        "queries",
        "letters",
        "model_calls",
        "overrides",
        "reconsiderations",
    }
)

_PSQL = 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER"'


def _pg(shell: str) -> list[str]:
    return [*COMPOSE, "exec", "-T", "postgres", "sh", "-c", shell]


def create_commands() -> list[list[str]]:
    """Create each eval database when it is missing."""
    return [
        _pg(
            f"{_PSQL} -d postgres -tAc \"select 1 from pg_database where datname='{db}'\" "
            f"| grep -q 1 || {_PSQL} -d postgres -c 'create database {db}'"
        )
        for db in (CLAIMS_DB, MOCK_DB)
    ]


def copy_commands() -> tuple[list[str], list[str]]:
    """(dump, restore): the dev schema and COPY_DATA rows, read from claims, written to
    claims_eval. The restore runs only into an empty claims_eval (see setup)."""
    tables = " ".join(f"-t {t}" for t in sorted(COPY_DATA))
    dump = _pg(
        f'pg_dump -U "$POSTGRES_USER" -d claims --no-owner --schema-only > /tmp/eval_schema.sql'
        f' && pg_dump -U "$POSTGRES_USER" -d claims --no-owner --data-only {tables} '
        f"> /tmp/eval_data.sql"
    )
    restore = _pg(
        f"{_PSQL} -d {CLAIMS_DB} -f /tmp/eval_schema.sql && "
        f"{_PSQL} -d {CLAIMS_DB} -f /tmp/eval_data.sql && rm /tmp/eval_*.sql"
    )
    return dump, restore


GRANTS_SQL = (
    "insert into mc_submit_grants (id, user_id, policy_number, hospital_id) "
    "select 'eval-member-' || policy_number, '00000000-0000-4000-8000-000000000001', "
    "policy_number, null from mc_policies on conflict (id) do nothing;"
)


def write_commands() -> list[list[str]]:
    """Every command that writes: each names an eval database or the eval namespace."""
    _, restore = copy_commands()
    return [
        *create_commands(),
        restore,
        [*COMPOSE, "exec", "-T", "app-eval", "python", "-m", "app.mock_core.seed"],
        _pg(f'{_PSQL} -d {MOCK_DB} -c "{GRANTS_SQL}"'),
        [
            *COMPOSE,
            "exec",
            "-T",
            "temporal",
            "sh",
            "-c",
            f"temporal operator namespace describe --namespace {NAMESPACE} --address temporal:7233"
            f" >/dev/null 2>&1 || temporal operator namespace create --namespace {NAMESPACE} "
            f"--address temporal:7233",
        ],
    ]


def _run(command: list[str]) -> str:
    done = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"failed: {' '.join(command[-1:])}\n{done.stderr.strip()}")
    return done.stdout


def _claims_eval_is_empty() -> bool:
    out = _run(
        _pg(
            f'{_PSQL} -d {CLAIMS_DB} -tAc "select count(*) from pg_tables '
            "where schemaname='public'\""
        )
    )
    return out.strip() == "0"


def setup(*, app_running: bool) -> None:
    """Idempotent. Databases and namespace first; the mock core seed and grants need the
    app-eval container, so they run when it is up (run setup again after starting it)."""
    *create, restore, seed, grants, namespace = write_commands()
    for command in create:
        _run(command)
    if _claims_eval_is_empty():
        dump, _ = copy_commands()
        _run(dump)
        _run(restore)
        print(f"claims_eval: dev schema and {len(COPY_DATA)} reference tables copied")
    _run(namespace)
    if app_running:
        print(_run(seed).strip())
        _run(grants)
    print(json.dumps(status(), indent=2))


def status() -> dict[str, object]:
    counts = _run(
        _pg(
            f'{_PSQL} -d {CLAIMS_DB} -tAc "select (select count(*) from claims), '
            '(select count(*) from model_calls), (select count(*) from icd_tabular)"'
        )
    )
    claims, calls, icd = counts.strip().split("|")
    return {
        "database": CLAIMS_DB,
        "claims": int(claims),
        "model_calls": int(calls),
        "icd_tabular_rows": int(icd),
        "namespace": NAMESPACE,
        "task_queue": TASK_QUEUE,
        "base_url": BASE_URL,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("action", choices=["setup", "status"])
    parser.add_argument(
        "--app-running",
        action="store_true",
        help="also seed mock_core_eval through the app-eval container",
    )
    args = parser.parse_args()
    if args.action == "setup":
        setup(app_running=args.app_running)
    else:
        print(json.dumps(status(), indent=2))


if __name__ == "__main__":
    main()
