# Dev Risk Hardening And Completion Draft

## Source Request

This draft is the input for a Humanize generated plan and RLCR loop on the current `dev` branch. The user asked for a software-engineering risk review, blocker remediation, and completion pass over code that appears partially implemented but not fully closed. Every RLCR round must make real code changes, add or update tests, run relevant verification, commit, and write a round summary.

The original product direction is to keep Limira's existing product shell intact while improving the research brain from a single-agent MVP into a clearer workflow:

```text
scope -> plan -> parallel research units -> evidence compression -> verification -> final report -> artifact reconciliation
```

The original plan emphasizes:

- Keep the browser workbench, FastAPI backend, runner, agent runtime, MCP tools, structured artifacts, reports, archives, authentication, owner-scoped task history, SSE, uploads, PDF export, object storage, and enterprise usage boundaries.
- Do not rewrite the core agent/orchestrator/search/scrape/LLM loop unless there is a confirmed bug.
- Move from one main agent plus optional tools toward explicit `ResearchBrief`, `ResearchPlan`, `ResearchUnit`, `EvidenceItem`, `CompressedFinding`, and `VerifiedClaim` contracts.
- Make evidence creation automatic at the tool layer instead of relying only on model calls to `record_research_artifact`.
- Decouple task execution from SSE subscription in the longer term so task creation/enqueue and event observation are separate product semantics.
- Split the large web router gradually into security, storage, runner client, repositories, services, and route modules, starting with low-risk extraction only.
- Make uploaded documents first-class research inputs through chunking, lexical retrieval, vector retrieval, and rerank fallback.
- Split model configuration by planner, researcher, compressor, verifier, and writer roles.
- Improve frontend state boundaries with a typed API client, SSE client, reducer, and render modules without forcing an immediate framework migration.
- Preserve browser security: no runner URL exposure, no service token exposure, no object key trust from browser payloads, no raw hidden chain-of-thought as a product feature.
- Add internal OSINT/research-quality regression tests beyond route contract tests.

## Startup Checks

- Current directory: `/home/limira/MiroLimira/dev`.
- Current branch: `dev`.
- Working tree at startup: clean.
- Recent commits:
  - `9d38d8b (HEAD -> dev, origin/dev) 让执行器按研究图计划运行`
  - `ee5036a 自动记录工具证据事件`
  - `91f19ba 引入研究图工作流骨架`
  - `ba3b6a9 提交了最后的UI修改`
  - `d16eefc Add enterprise login and usage quotas`
- Existing RLCR loop directory: `.humanize/rlcr/2026-06-07_17-30-57`. This draft does not edit any existing `.humanize/rlcr/*/state.md`.
- Project structure observed:
  - `apps/limira-agent`: agent runtime and research graph contracts.
  - `apps/limira-runner`: aiohttp runner API, task store, pipeline helpers, archive writer, auth adapter, tests.
  - `apps/limira-standalone`: browser workbench, standalone server, static frontend assets.
  - `apps/limira-web`: FastAPI backend and large `limira.py` router.
  - `libs/limira-tools`: MCP tools, artifact recorder, automatic evidence ledger.
  - `deploy/limira`, `docker-compose.limira.yml`: Postgres/Redis/MinIO/runner/web deployment.
  - `docs/langchain-research-workflow-plan.md`: earlier workflow upgrade plan.

## Current Code State

Limira is not a bare demo. It has a product shell and many guardrails already:

- The standalone frontend has a real workbench with login/signup, enterprise login, history, task status, artifact tabs, upload control, scenario selector, stream progress, PDF export, archive download, and evidence preview modal.
- The standalone `server.mjs` only proxies `/api/limira` and `/api/limira/*`; it rejects generic `/api/*` and `/limira-runner/*`, preserving the intended browser boundary.
- The web backend contains authentication, enterprise/account routes, research task routes, task history, SSE proxy, artifacts, upload, archive, admin task access, and PDF generation. The file is very large: `apps/limira-web/backend/limira_backend/routers/limira.py` is about 9,326 lines.
- The runner API has service-token authentication, trusted user headers, owner checks, admin access, queued task creation, stream execution, cancellation, terminal state handling, archive download, secret scrubbing, and archive finalization.
- The runner task store supports SQLite for explicit local fallback and a Postgres implementation that targets `limira_research_tasks`. The default environment now favors Postgres and requires explicit SQLite fallback.
- Deployment uses Postgres, Redis, MinIO, web, runner, and nginx, with health checks and required secret environment variables.
- The Postgres migration includes research tasks, artifact events, task event logs, evidence items, uploaded documents with vector columns, media assets, generated reports, entities, relations, and timeline tables.
- `apps/limira-agent/src/core/research_graph.py` defines `ResearchBrief`, `ResearchPlan`, `ResearchUnit`, `EvidenceItem`, `CompressedFinding`, `VerifiedClaim`, and bootstrap events. The compatibility executor receives the graph-derived prompt.
- `libs/limira-tools/src/limira_tools/limira_evidence.py` derives evidence artifacts from search and scrape tool results automatically.
- Existing tests cover runner API behavior, task store/auth, research graph bootstrap, archive writing, artifact parsing, frontend contract, deployment contract, and many web route behaviors.

## Most Likely Engineering Risks

P0 risks to actively look for and fix:

- Browser-visible responses, archives, event streams, logs, or frontend bundles could leak service tokens, API keys, cookies, Authorization headers, object keys, or internal runner URLs.
- Owner isolation could fail on task history, events, archive download, uploads, reports, admin routes, or runner headers.
- Archive/download code could allow path traversal, unsafe ZIP members, unintended files, or hidden internal state leakage.
- Task state transitions could corrupt terminal state, double-complete a task, or report success after failure.
- Deployment could fail at startup because required services, migrations, import paths, or environment variable contracts are inconsistent.

P1 risks that block real use:

- Runner task execution is still started by `GET /limira-runner/tasks/{task_id}/events`; SSE is not yet a pure observer. This complicates background execution, retries, resume/replay, multi-tab semantics, and production queueing.
- Research graph contracts exist, but the workflow still uses a compatibility single-agent executor rather than actual graph nodes for planner, research unit, compressor, verifier, writer, and reconciliation.
- Automatic evidence is created as artifact events from tool results, but durable evidence lifecycle and claim-to-evidence enforcement are not fully closed across backend persistence, report generation, and UI.
- Uploaded document retrieval has schema support and frontend entry points, but hybrid retrieval is not yet the default closed loop for actual research units.
- `limira.py` is too large and combines models, repositories, auth, runner clients, storage, PDF/archive helpers, and routes, which increases regression risk for security-sensitive changes.
- Docker Compose is present, but real deployment readiness still depends on contract checks, startup validation, and migration idempotency.

P2 risks to improve where practical:

- Error handling and status events should remain consistent across runner, backend, frontend, archives, and history after cancellation, disconnect, render failure, archive failure, and runner failure.
- Tests should focus on negative/multi-user/security paths, not only happy path and smoke contracts.
- Frontend global state should move toward reducer/API/SSE boundaries over time; short-term fixes should target real workflow bugs without a framework rewrite.
- Internal research-quality evals are absent, so prompt/model/tool changes can regress evidence grounding without a route-level failure.
- The default agent config still looks like `agent=demo`, and `sub_agents` may remain empty in common setups.

P3 risks:

- Naming, local style, and module layout cleanup should not be the main goal unless they are low-risk and directly support a P0/P1/P2 fix.

## Most Likely Development Blockers

- The code already contains several partial closures from the original plan, so new work must inspect current behavior before adding abstractions.
- The highest-value fixes are cross-boundary: runner state, backend API contracts, archives, persistence, and frontend behavior. These need focused tests to avoid breaking old log/trace/archive behavior.
- Directly replacing the agent loop with a full graph is too risky for one pass. RLCR should first strengthen contracts and durable evidence/state boundaries.
- Decoupling execution from SSE likely needs a careful compatibility path because current frontend expects EventSource to begin work after task creation.
- Splitting `limira.py` wholesale is too risky; extract only low-risk helpers or add tests around security boundaries before moving behavior.
- Running full Docker Compose may be expensive or unavailable in the local environment; use deploy contract tests and targeted service tests first, then run compose checks only if feasible.

## RLCR Priority Order

1. P0 security and isolation audit: browser response shaping, service token/API key scrubbing, object key exposure, archive ZIP member safety, path traversal, owner isolation, admin-only access.
2. P1 task lifecycle closure: cancellation, reconnect, duplicate EventSource, terminal state, failed archive/report paths, and clear runner/backend/frontend contract for task states.
3. P1 evidence persistence and citation closure: ensure automatic tool evidence survives through backend storage, artifacts, archives, and UI; reject or warn on model artifacts that cite missing evidence where feasible.
4. P1 frontend interaction closure: real clickable controls for history, uploads, scenarios, PDF, archive, evidence preview, and error/empty states; no text-only dead ends.
5. P1/P2 deploy readiness: Postgres migration idempotency, required environment variables, Docker/runner/web import paths, MinIO bucket init, health checks, and local fallback rules.
6. P2 upload retrieval and quality: strengthen lexical fallback and add tests for vector-disabled, provider-failed, and user-isolated upload searches before broader hybrid retrieval.
7. P2 quality eval skeleton: add small offline regression cases and metrics for claim/evidence ratio, source count, forbidden claims, and citation grounding without depending on live external APIs.
8. P3 low-risk module extraction only when it directly reduces risk around a touched area.

## Verification Expectations

Each RLCR round must:

- Read the current `.humanize/rlcr/*/round-N-prompt.md`.
- Identify the highest-priority concrete issue for that round.
- Make real code changes, not documentation-only changes.
- Add or update targeted tests for the fixed risk.
- Run relevant tests or verification commands and record the exact commands in the round summary.
- Commit the round changes.
- Write `.humanize/rlcr/*/round-N-summary.md` with risks found, fixes, files changed, tests, commands, unresolved risks, and next priority.
- Exit normally so the native Humanize Stop hook can review and generate the next prompt.

## Explicit Constraints

- Do not rewrite the core agent/orchestrator/search/scrape/LLM main loop unless there is a confirmed bug.
- Do not delete functionality to make tests pass.
- Do not leak secrets into code, logs, archives, frontend bundles, or test snapshots.
- Do not expose raw hidden chain-of-thought as product behavior.
- Do not manually edit `.humanize/rlcr/*/state.md`.
- Do not manually skip the hook or replace it with ad hoc review.
- Keep old logs, trace viewer expectations, archives, PDF paths, and existing development fallbacks working unless a verified security issue requires tightening.
