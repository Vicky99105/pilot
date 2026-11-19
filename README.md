# ConsintIQ Claims Pilot

Agentic health-claims processing pilot. Temporal runs the claim lifecycle; one
FastAPI process serves the API and the React UI and runs the Temporal worker.

Current state: Phase 1 (tracer bullet), slices 1 to 7. One reimbursement claim
runs through all 13 stages with real services: intake, classification, digitisation,
registration, bill review, medical review, risk, adjudication, decision (approver bands
and CRC), communication (letter PDFs), the query loop, settlement and monitoring. People
decide at checkpoints in the Review area task queues. Core systems (policy, money,
permissions) are mocked behind contracts, and letters are recorded, not emailed (shadow
mode). The Phase 1 UI (slice 6) has the Submit and Review areas. Slice 7 adds the seeded
acceptance claim and a live end-to-end run against OpenRouter.
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
deploy/             Dockerfile, docker-compose.yml (+ .local.yml), Caddyfile, Postgres init
tools/              Mock claim generator and the live acceptance runner
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

Load the ICD-10-CM and ICD-10-PCS FY2027 release (the four CMS archives, kept in the
gitignored `.local/kb_sources/icd-2027/`). The archives are mounted read-only into a one-off
app container; they are never part of the image. Each load replaces that code set in one
transaction and prints file hashes, row counts, checks and a 20-row spot check:

```bash
for system in icd10cm icd10pcs; do
  docker compose -f deploy/docker-compose.yml run --rm --no-deps \
    -v "$PWD/.local/kb_sources/icd-2027:/kb_sources/icd-2027:ro" \
    app python -m app.knowledge.load "$system" --source-dir /kb_sources/icd-2027
done
```

Both seeds and both loads are idempotent. Phase 1 has no real auth: the `X-User-Id` header picks a
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

### Local access on port 8000

`deploy/docker-compose.local.yml` is a local-only override that also publishes the app on
`127.0.0.1:8000`, so host tools reach it over plain HTTP without Caddy's self-signed
certificate. Production compose is unchanged; never use the override on the server.

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up -d --build
curl http://127.0.0.1:8000/health
```

For UI development, start the stack with the override and run `npm --prefix ui run dev`.
The Vite dev server proxies `/health` to port 8000.

## Tracer acceptance claim

`tools/mockgen/tracer_claim.py` writes the acceptance bundle: a reimbursement claim for the
seeded member Asha Verma at Sunrise Multispeciality Hospital (acute appendicitis, 4 days,
a deluxe room above the room cap, one non-payable item), as one PDF per document, one
merged PDF and `ground_truth.json`. The payment receipt is left out of the bundle and
written separately as the query reply. Expected deductions come from the real rules engine.

```bash
uv run --project backend python tools/mockgen/tracer_claim.py --out .local/tracer
```

`tools/acceptance/run_tracer.py` runs the claim live through the stack on port 8000 (start it
with the override above, with the key for the provider in `config/models.yaml` set, currently `GEMINI_API_KEY`, then migrate and seed). It submits
the merged PDF as the member, completes every checkpoint as ops, doctor and finance through
the API, answers the query with the receipt, and scores the run against the ground truth.
Each run picks an admission date that overlaps no earlier claim, so it can be re-run.

```bash
uv run --project backend python tools/acceptance/run_tracer.py --out .local/acceptance
```

The report (`report.md` and `report.json` in the run folder) covers page labels, field
extraction, bill lines, rules and deductions, ICD codes, the drafts, letters, events, model
calls, tokens, cost and the time each stage took.
