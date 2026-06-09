FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY libs/miroflow-tools /app/libs/miroflow-tools
COPY apps/miroflow-agent /app/apps/miroflow-agent
COPY apps/limira-runner /app/apps/limira-runner

WORKDIR /app/apps/limira-runner

RUN uv sync --frozen --no-dev || uv sync --no-dev

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/apps/limira-runner
ENV MIROTHINKER_RUNNER_PORT=8091

CMD ["uv", "run", "runner_api.py"]
