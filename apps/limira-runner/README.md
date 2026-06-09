# Limira Runner

This directory contains the service-side research runner used by the Limira web app.

It is not a browser UI. The user-facing UI lives in `apps/limira-standalone`, and the public API lives in `apps/limira-web/backend`.

## Responsibilities

- Accept authenticated internal research requests from the Limira backend.
- Execute the Limira research pipeline.
- Stream normalized research events back to the backend.
- Persist task state through the configured task store.
- Build archive-compatible report artifacts for completed tasks.

## Local Run

Install dependencies from this directory:

```bash
uv sync
```

Run the runner API:

```bash
RUNNER_SERVICE_TOKEN=dev-token \
LIMIRA_RUNNER_INTERNAL_PORT=8091 \
uv run runner_api.py
```

The service exposes the internal runner API under `/limira-runner/*`. Browser traffic should not call these routes directly; the standalone frontend only proxies `/api/limira/*` to the Limira backend.

## Notes

- `pipeline_helpers.py` contains the research pipeline helpers consumed by `runner_api.py`.
- `archive_writer.py`, `task_store.py`, and `auth_adapter.py` are still part of the active runner chain.
- Historical browser demo code has been removed from this directory.
