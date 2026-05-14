FROM python:3.11-slim AS base

WORKDIR /app

RUN groupadd -r appuser && useradd -r -g appuser appuser

# uv produces deterministic installs from uv.lock. We pin uv via pip
# (no curl-to-shell pattern in the image) then `uv sync --frozen` so the
# resolved package set in the production image matches what CI tested.
RUN pip install --no-cache-dir uv==0.5.18

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
