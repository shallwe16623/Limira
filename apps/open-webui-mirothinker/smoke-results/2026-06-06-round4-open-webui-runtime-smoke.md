# Round 4 Open WebUI Runtime Smoke Result

Status: passed
Date: 2026-06-06

## Runtime

- Open WebUI version: 0.9.6
- Open WebUI URL: `http://127.0.0.1:18080`
- Runner API URL: `http://127.0.0.1:18081`
- Pending-archive controlled Runner URL: `http://127.0.0.1:18082`
- Function ID: `mirothinker_deep_research`
- Pipe model ID: `mirothinker_deep_research.mirothinker-deep-research`
- Runner base commit before this smoke result commit: `813fad7`
- Function artifact commit: recorded by the commit that adds this smoke result

## Runtime Configuration

- Open WebUI was started from the official `open-webui@latest` package through `uvx --python 3.11 open-webui@latest serve`.
- `DATA_DIR` used an isolated temporary directory: `/tmp/mirothinker-open-webui-smoke`.
- `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=false` was required because the `uvx` Open WebUI environment did not include `pip`; the Function's declared dependencies, `httpx` and `pydantic`, were already present in the Open WebUI runtime.
- `BYPASS_MODEL_ACCESS_CONTROL=true` was used for this local smoke so ordinary test users could see the global Pipe model without configuring workspace ACLs.
- The Function was installed through the Open WebUI admin Functions API, activated, and marked global for the smoke users.

## Users

- Admin role: `admin`
- User A role: `user`
- User B role: `user`

## Checklist Results

| Check | Result | Evidence |
|-------|--------|----------|
| User A completed research | passed | Authenticated User A called `/api/chat/completions`; output included progress, `Research status: completed`, and `Download Trace ZIP`. |
| Archive zip contents | passed | Completed task archive contained only `trace.json`, `report.md`, `metadata.json`, and `report.html`. |
| User B non-owner denial | passed | Runner status and archive requests for User A task using User B identity both returned `404`. |
| Pending archive disabled | passed | Open WebUI Pipe output for the controlled pending task included `Archive download disabled: pending`. |
| Failed diagnostic archive | passed | User A failed task output included `Research status: failed` and `Download Diagnostic ZIP`. |
| Cancelled diagnostic archive | passed | User A task was cancelled through the trusted Runner cancel path; output included `Research status: cancelled` and `Download Diagnostic ZIP`. |

## Task Evidence

- Completed task: `cf99a9ce-c8cb-49af-835f-e93c3fddddcc`
- Failed task: `4394f89e-77c6-439d-b242-c553ccbd8aa0`
- Cancelled task: `1d44e371-83a1-4b57-acaf-b7610cc36bf4`
- Pending controlled task: `pending-smoke-task`

## Notes

- Completed, failed, and cancelled branches used the current MiroThinker Runner API app (`runner_api.create_app`) with a controlled fake stream to avoid real LLM and external tool calls during smoke.
- The pending-archive branch used a minimal controlled Runner because the current Runner API finalizes archive state after event stream completion and cannot naturally finish an Open WebUI Pipe call while leaving `archive_status` as `pending`.
- The cancellation path exposed a real Pipe edge case: queued cancellation can make `/events` return `409 task_already_finished` before SSE attaches. The Pipe now treats `409` as terminal and polls final task status instead of failing the Open WebUI response.
