FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --locked --no-dev --no-editable \
    && groupadd --gid 1000 gmail-mcp \
    && useradd --uid 1000 --gid gmail-mcp --no-create-home gmail-mcp

USER gmail-mcp

ENTRYPOINT ["gmail"]
CMD ["serve", "--creds-file-path", "/run/credentials/client-secret.json", "--token-path", "/data/token.json"]
