# Quorum Web in a container: the browser UI plus the claude and codex CLIs.
# Logins and history live in the "quorum-home" volume (see docker-compose.yml).
FROM node:22-bookworm-slim

# Versions verified with Quorum's agent invocations (docs/agent-participants.md).
ARG CLAUDE_CODE_VERSION=2.1.328
ARG CODEX_VERSION=0.170.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} @openai/codex@${CODEX_VERSION} \
 && npm cache clean --force

WORKDIR /app
COPY pyproject.toml README.md LICENSE .env.example ./
COPY src ./src
# The terminal UI is not part of this image; the wheel build expects its folder to exist.
RUN mkdir -p frontend/dist \
 && python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir . \
 && rm -rf frontend
COPY webui ./webui

# Run as the image's unprivileged "node" user; its home holds the CLI logins and ~/.quorum.
USER node
ENV HOME=/home/node PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
EXPOSE 8765
CMD ["python", "webui/server.py", "--host", "0.0.0.0", "--no-browser"]
