# syntax=docker/dockerfile:1
# Build context: repository root (see deploy/docker-compose.yml).

# --- Stage 1: build the React UI ---
FROM node:24.22.2-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# --- Stage 2: uv binary ---
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

# --- Stage 3: runtime ---
FROM python:3.12.16-slim AS runtime
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1

WORKDIR /srv/backend
# Dependencies first, so code changes do not invalidate this layer.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY backend/ ./
COPY config/ /srv/config/
COPY --from=ui /ui/dist /srv/ui

ENV PATH=/opt/venv/bin:$PATH \
    UI_DIST_DIR=/srv/ui

RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data/files && chown app /data/files
# A new named volume mounted here copies this ownership, so the app user can write to it.
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
