FROM docker.io/library/node:24-slim AS base

ARG ENV_FILE

RUN apt update && apt install jq unzip curl -y && apt clean && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://bun.sh/install | bash && \
    ln -s /root/.bun/bin/bun /usr/local/bin/bun

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Python.
COPY pyproject.toml uv.lock README.md /app/
COPY ${ENV_FILE} /app/.env
COPY src /app/src
RUN cd /app && uv sync --frozen --no-dev --no-install-project && uv build
ENV PATH="/app/.venv/bin:$PATH"

# Contracts. Use real node to run hardhat (bunx would route through Bun's runtime).
COPY solidity /app/solidity
RUN cd /app/solidity && ~/.bun/bin/bun install --frozen-lockfile && npx hardhat compile

# Helpers.
COPY docker/start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8000

WORKDIR app
CMD ["/start.sh"]
