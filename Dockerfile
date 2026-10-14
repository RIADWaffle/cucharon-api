# syntax=docker/dockerfile:1

FROM python:3.13-slim AS builder
COPY --from=astral/uv:0.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project


FROM python:3.13-slim
RUN groupadd --system app && useradd --system --gid app --no-create-home app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000
USER app
EXPOSE 8000

# Render free no tiene pre-deploy command: las migraciones corren al arrancar.
# --forwarded-allow-ips='*': Render está detrás de su proxy. uvicorn toma entonces la
# entrada más a la izquierda de X-Forwarded-For, que es spoofeable (ver ADR 0003).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port \"$PORT\" --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
