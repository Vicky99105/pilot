# ConsintIQ Claims Pilot

Agentic health-claims processing pilot. Temporal runs the claim lifecycle; one
FastAPI process serves the API and the React UI and runs the Temporal worker.

Current state: Phase 1 (tracer bullet), backend slices 1 to 5. One reimbursement claim
runs through all 13 stages with real services: intake, classification, digitisation,
registration, bill review, medical review, risk, adjudication, decision (approver bands
and CRC), communication (letter PDFs), the query loop, settlement and monitoring. People
decide at checkpoints in the Review area task queues. Core systems (policy, money,
permissions) are mocked behind contracts, and letters are recorded, not emailed (shadow
mode). The Phase 1 UI (slice 6) and the seeded acceptance claim (slice 7) come next.
Contracts and slices: `docs/plans/2026-11-19-phase-1-tracer.md`.

## Layout

```
backend/            Python (uv). FastAPI app, Temporal worker, Alembic.
  app/main.py       FastAPI app; Temporal worker starts in the lifespan
  app/config.py     Settings from environment variables
  app/api/routes/   Thin HTTP routes that call service contracts
  app/services/     One folder per service: contract.py + service.py
  app/workflows/    Temporal workflows, activities and the worker
  app/database/     SQLAlchemy models, Alembic migrations and the seeds
config/             Model roles, product rules config, authority matrix
ui/                 React + Vite + TypeScript + Tailwind
tests/              All backend tests (run against contracts, with fakes)
deploy/             Dockerfile, docker-compose.yml, Caddyfile, Postgres init
.github/workflows/  CI: lint, types, tests, UI build, full-stack health check
```

## Tests and checks

From the repo root:

```bash
uv sync --project backend
uv run --project backend pytest tests
uv run --project backend ruff check backend tests
uv run --project backend mypy backend/app

npm ci --prefix ui
npm --prefix ui run typecheck
npm --prefix ui run build
```

## Run the stack locally

Requires Docker. From the repo root:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
curl -k https://localhost/health
docker compose -f deploy/docker-compose.yml exec app alembic upgrade head
docker compose -f deploy/docker-compose.yml exec app python -m app.database.seed
docker compose -f deploy/docker-compose.yml exec app python -m app.mock_core.seed
curl -k -H 'X-User-Id: 00000000-0000-4000-8000-000000000003' https://localhost/api/tasks/queue
```

The compose file sets the project name `claims-pilot`. Do not pass `-p`: another
project name starts a second stack with its own containers and volumes.

Both seeds are idempotent. Phase 1 has no real auth: the `X-User-Id` header picks a
seeded user (IDs in `backend/app/shared/seed_ids.py`): `...0001` member, `...0002`
hospital, `...0003` ops, `...0004` doctor (approver up to Rs 1 lakh, CRC), `...0005`
finance, `...0006` senior approver (no limit).

Letters are never emailed by default. Microsoft Graph sending is off unless
`NOTIFICATIONS_GRAPH_ENABLED=true`, and then only to addresses in `NOTIFICATION_ALLOWLIST`.

- The UI is at https://localhost (Caddy uses a self-signed certificate).
- The Temporal UI is at http://127.0.0.1:8080. On the server, reach it through an
  SSH tunnel: `ssh -L 8080:127.0.0.1:8080 <server>`.
- On the server, pass real values: copy `.env.example` to `.env`, fill it in, and
  add `--env-file .env` to each `docker compose` command.

For UI development, run the backend on port 8000 and `npm --prefix ui run dev`.
The Vite dev server proxies `/health` to it.
