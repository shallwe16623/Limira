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

## Environment Files

`scripts/start-local.sh` loads environment files from the current worktree before starting the runner:

```text
<worktree>/.env
  Shared local service settings such as ports, RUNNER_SERVICE_TOKEN, and LIMIRA_AUTH_SECRET.

<worktree>/apps/limira-agent/.env
  Tool credentials such as SERPER_API_KEY, JINA_API_KEY, E2B_API_KEY, and SUMMARY_LLM_*.

<worktree>/apps/limira-runner/.env
  Runner model settings such as DEFAULT_LLM_PROVIDER, DEFAULT_MODEL_NAME, BASE_URL, and API_KEY.
```

Each worktree needs its own copy of these files. They are local secrets and should remain git-ignored.

## Notes

- `pipeline_helpers.py` contains the research pipeline helpers consumed by `runner_api.py`.
- `archive_writer.py`, `task_store.py`, and `auth_adapter.py` are still part of the active runner chain.
- Historical browser demo code has been removed from this directory.
