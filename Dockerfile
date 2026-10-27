# One image for the API and both MCP servers; the compose file picks the command.
FROM python:3.12-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --uid 10001 --no-create-home app
ENV PATH="/app/.venv/bin:$PATH"
USER app
EXPOSE 8000
CMD ["opscopilot", "serve", "--host", "0.0.0.0", "--port", "8000"]
