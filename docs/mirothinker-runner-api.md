# MiroThinker Runner API

Phase 1 exposes a backend-only Runner API for curl and test-client validation before Open WebUI frontend work starts.

## Start

From `apps/gradio-demo`:

```bash
export MIROTHINKER_SERVICE_TOKEN="dev-shared-secret"
export MIROTHINKER_RUNNER_PORT=8081
uv run runner_api.py
```

The dev identity bridge requires both headers:

- `X-MiroThinker-Service-Token`: must match `MIROTHINKER_SERVICE_TOKEN`
- `X-OpenWebUI-User-Id`: trusted user ID injected by the backend/proxy

Do not send `user_id` in the JSON body. Production identity must come from Open WebUI backend/JWT/API-key verification or a trusted proxy.

## Curl Smoke

```bash
curl -X POST http://localhost:8081/mirothinker/research \
  -H "Content-Type: application/json" \
  -H "X-MiroThinker-Service-Token: dev-shared-secret" \
  -H "X-OpenWebUI-User-Id: user-a" \
  -d '{"query":"测试研究问题","client_options":{"stream":true}}'
```

The response contains `task_id`, `stream_url`, and `task_url`.

```bash
curl -N http://localhost:8081/mirothinker/tasks/<task_id>/events \
  -H "X-MiroThinker-Service-Token: dev-shared-secret" \
  -H "X-OpenWebUI-User-Id: user-a"
```

```bash
curl http://localhost:8081/mirothinker/tasks/<task_id> \
  -H "X-MiroThinker-Service-Token: dev-shared-secret" \
  -H "X-OpenWebUI-User-Id: user-a"
```

To request cancellation for a queued or running task:

```bash
curl -X POST http://localhost:8081/mirothinker/tasks/<task_id>/cancel \
  -H "X-MiroThinker-Service-Token: dev-shared-secret" \
  -H "X-OpenWebUI-User-Id: user-a"
```

Only the task owner or an explicit admin identity may cancel a task. The browser must never submit `user_id`; it must be injected by the trusted Open WebUI backend/proxy path.

```bash
curl -OJ http://localhost:8081/mirothinker/tasks/<task_id>/archive.zip \
  -H "X-MiroThinker-Service-Token: dev-shared-secret" \
  -H "X-OpenWebUI-User-Id: user-a"
```

## Archive Contract

Completed, failed, and cancelled tasks attempt to create:

- `trace.json`
- `report.md`
- `metadata.json`
- `report.html`
- `archive.zip`

`archive.zip` contains only the first four files with relative paths. If zip creation fails, research `status` remains unchanged and `archive_status` becomes `failed`.

## Trace Compatibility

`archives/<timestamp>_<task_id>/trace.json` is a new archive contract for downloadable research diagnostics. It is separate from the legacy MiroThinker trace viewer input.

The legacy pipeline still writes `logs/api-server/task_<task_id>_<timestamp>.json` through `TaskLog.save()`. Runner archives must not replace, move, or rewrite those legacy task JSON files. Existing trace tooling should continue reading `logs/api-server/task_*.json` unless a viewer update explicitly adds support for the new archive `trace.json` format and is regression-tested.
