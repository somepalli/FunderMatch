FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/
ENV PATH="/app/.venv/bin:$PATH" UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra retrieval --no-install-project
COPY src ./src
COPY configs ./configs
COPY migrations ./migrations
COPY prompts ./prompts
RUN uv sync --frozen --no-dev --extra retrieval

CMD ["uvicorn", "fundermatch.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8977"]
