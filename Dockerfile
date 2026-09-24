FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so code changes don't invalidate this layer.
# --frozen installs exactly what uv.lock pins without re-resolving (the lock's
# exclude-newer option comes from host uv config the image doesn't have).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY *.py dashboard.html ./

EXPOSE 8000

# RELAY_DATABASE_URL must point at PostgreSQL; compose.yaml sets it.

# Bind to 0.0.0.0 so the published port is reachable from the host.
CMD ["/app/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
