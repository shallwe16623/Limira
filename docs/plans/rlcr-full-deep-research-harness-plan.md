# Full Deep Research Harness Plan

## Goal Description

Build Limira from the current research-product shell into an auditable, resumable, verifiable deep research harness that implements the complete intent of `docs/drafts/rlcr-draft.md`.

The previous RLCR loops are prerequisites, not final completion. They established TaskContext propagation, feature-flagged serial graph execution, evidence reference integrity, source-candidate separation, uploaded document source visibility, stale-running recovery, and offline eval coverage. This plan finishes the broader draft requirements by replacing prompt-only orchestration with real graph-node execution, making Runner ownership and replay durable in the database, turning evidence promotion and verification into an enforceable lifecycle, making uploaded documents first-class agent sources, and moving Web artifact/report ingestion off browser-dependent SSE consumption.

Completion means Limira can run long deep-research jobs with durable task ownership, checkpoint/recovery, server-side artifact ingestion, graph-node phase boundaries, source/evidence verification gates, uploaded document retrieval, and deterministic tests/evals that prove those contracts.

## Acceptance Criteria

- AC-F1: Runner durable job ownership, heartbeat, checkpoint, and replay state are database-backed.
  - Positive Tests (expected to PASS):
    - A queued task is atomically claimed with a durable worker lease or ownership record that includes worker ID, lease expiration, heartbeat timestamp, and attempt number.
    - Active workers periodically persist heartbeats without relying only on process memory.
    - Runner emits or persists task events to a durable event log before acknowledging them to subscribers.
    - A restarted Runner can rebuild replay state for a task from the database event log.
    - Startup reconciliation can distinguish healthy leased tasks, expired leases, retryable failures, and terminal tasks without overwriting terminal records.
    - Checkpoints record graph phase, current research unit, source/evidence ledger state, and enough executor state to resume or explicitly fail with a recoverable reason.
  - Negative Tests (expected to FAIL):
    - A running task can be lost or reported completed after process restart with no durable event record.
    - A terminal task can be overwritten by lease recovery.
    - Two workers can simultaneously own the same running task lease.
    - Replay after restart depends on in-memory event dictionaries.

- AC-F2: Research graph execution uses real graph nodes behind a feature flag with legacy fallback.
  - Positive Tests (expected to PASS):
    - A `StateGraph` or equivalent local graph executor runs explicit serial nodes: `ScopeNode`, `PlannerNode`, `ResearchUnitNode`, `EvidenceCompressorNode`, `VerifierNode`, `WriterNode`, and `ReconcilerNode`.
    - Each node reads and writes typed graph state rather than passing only a long prompt to the legacy single-agent loop.
    - The graph path emits stable phase events and checkpoints for scope, plan, research, compress, verify, write, reconcile, and complete.
    - The legacy single-agent executor remains the default fallback unless the graph feature flag is enabled.
    - If the graph flag is enabled and a required node output is missing or invalid, the task fails through the existing error/event/archive contract.
    - The graph prompt adapter is removed from the enabled graph path except as a bounded fallback node implementation.
  - Negative Tests (expected to FAIL):
    - The graph-enabled path still delegates the full plan to one legacy `run_main_agent` call as the main executor.
    - Missing verifier or writer output is treated as success.
    - Graph node state is not checkpointed and cannot be inspected from task trace/archive metadata.

- AC-F3: Evidence lifecycle separates source candidates, retrieved sources, evidence items, findings, and verified claims.
  - Positive Tests (expected to PASS):
    - Search snippets create `source_candidate` records only and cannot support high-confidence final claims by themselves.
    - Scraped pages, Jina summaries, parsed PDFs, uploaded chunks, and other content-bearing retrievals create retrieved-source records with source type, URL/document ID, retrieved timestamp, content hash, tool name, and confidence metadata.
    - Evidence promotion requires content-bearing source material and creates `EvidenceItem` records with stable `EVID-*` IDs.
    - Verifier output links claims to evidence IDs and marks support as `supports`, `contradicts`, `contextual`, or `weak`.
    - Report generation blocks or emits an auditable warning for unsupported high-confidence claims and unresolved `EVID-*` references.
    - Archive and Web artifact APIs expose the full lifecycle without merging candidates into verified evidence.
  - Negative Tests (expected to FAIL):
    - Snippet-only candidates are promoted to evidence without retrieved content.
    - Evidence records omit content hash or retrieval timestamp when source policy requires them.
    - Final reports silently cite nonexistent evidence IDs.
    - Verified claims omit evidence links or support classification.

- AC-F4: Uploaded documents are first-class research sources available to graph nodes and tools.
  - Positive Tests (expected to PASS):
    - Ownership-checked uploaded documents are attached to tasks with task-scoped IDs and immutable source metadata.
    - Text-bearing uploads are retrievable through a local source provider/tool used by `ResearchUnitNode`, not only included as prompt excerpts.
    - Uploaded text chunks can become source candidates, retrieved sources, and promoted evidence with document ID, chunk ID, content hash, retrieved timestamp, and source type.
    - Context-only uploads are explicitly marked `context_only` and cannot be cited as retrieved evidence.
    - Scenario/source policy can prioritize uploaded documents over web sources when requested.
  - Negative Tests (expected to FAIL):
    - A graph node claims retrieved upload facts when only document IDs or bounded prompt excerpts exist.
    - Unowned document IDs reach Runner or Agent source handling.
    - Uploaded document chunks are unavailable to the executor after process restart.

- AC-F5: Web artifact/report ingestion is server-side and not browser-SSE dependent.
  - Positive Tests (expected to PASS):
    - Runner writes durable event/artifact records or exposes a durable event cursor that Web can ingest independently of browser connections.
    - A server-side ingestion worker records task events, artifacts, final reports, PDFs, and archive status into the Web repository.
    - Opening the browser SSE stream is not required for Web artifact history, report history, or archive consistency.
    - Ingestion is idempotent across duplicate events, reconnects, and restarts.
    - Web history and Runner archive metadata converge on the same task status, artifact counts, evidence refs, and report identifiers.
  - Negative Tests (expected to FAIL):
    - If no browser consumes SSE, Web history misses artifacts or final reports.
    - Duplicate ingestion creates duplicate artifact rows for the same task/local artifact.
    - A completed Runner archive exists but Web history remains permanently pending without an ingestion error.

- AC-F6: Durable Runner and server-side ingestion have explicit operational observability.
  - Positive Tests (expected to PASS):
    - Task status endpoints expose lease/checkpoint/recovery metadata needed to debug long-running jobs without leaking secrets.
    - Recovery and ingestion failures produce structured warning/error events visible in trace/archive metadata.
    - Archive downloads scrub secrets and internal model identifiers after the new event/checkpoint fields are added.
    - Postgres schema migrations, SQLite test schema, and in-memory fakes support the same durable contract shape.
  - Negative Tests (expected to FAIL):
    - Lease IDs, worker IDs, or checkpoint payloads expose API keys, runner service tokens, raw environment values, or internal model summaries.
    - Postgres constraints reject newly emitted durable event, checkpoint, source, or artifact rows.

- AC-F7: End-to-end and offline eval coverage proves draft-level deep-research behavior.
  - Positive Tests (expected to PASS):
    - Offline evals cover `missing_ref`, `snippet_only`, `upload_doc`, `scenario_policy`, `restart_recovery`, `graph_node_failure`, `lease_takeover`, `checkpoint_resume`, and `server_side_ingestion`.
    - Eval cases use local fixtures/fakes and run without live LLM, live search, external network, or API keys.
    - Integration tests cover SQLite, Postgres SQL-contract, Runner API, graph executor, Web ingestion, standalone artifact UI, and archive trace behavior.
    - Existing tests from the prior RLCR loops remain green.
  - Negative Tests (expected to FAIL):
    - Eval coverage only tests happy paths.
    - A prompt-only graph executor can satisfy graph-node tests.
    - Browser-only SSE consumption can satisfy ingestion tests.

## Path Boundaries

### Upper Bound (Maximum Scope)

- Implement a production-ready serial deep-research graph with real typed nodes, deterministic checkpointing, durable Runner leases/events, server-side Web ingestion, source/evidence/claim lifecycle enforcement, uploaded document retrieval, and comprehensive offline/integration tests.
- Add bounded parallel research-unit execution only after the serial graph is stable and covered by tests.
- Use Postgres migrations as the production contract and SQLite/fakes as deterministic local test adapters.

### Lower Bound (Minimum Scope)

- Keep legacy execution as a fallback, but the feature-flagged graph path must execute real node boundaries and typed state, not only wrap a legacy single-agent call.
- Persist enough Runner lease, heartbeat, checkpoint, and event-log state to recover or fail long-running tasks deterministically after restart.
- Provide server-side artifact/report ingestion that works without browser SSE.
- Preserve existing AC-R1 through AC-R5 behavior from previous loops.

### Allowed Choices

- Use LangGraph if available or add it as a scoped dependency for `apps/limira-agent`; if dependency risk is high, implement a small internal serial graph runner with the same typed node boundary contract first.
- Use existing `TaskStore`, `PostgresTaskStore`, Web repository, artifact tools, and archive writer patterns where possible.
- Add Postgres migrations and SQLite schema evolution helpers when new durable tables/columns are needed.
- Add fakes and deterministic fixtures for LLM/search/tool execution in tests.
- Use feature flags for new graph execution, durable recovery behavior, and ingestion workers while preserving default compatibility during rollout.

### Explicit Non-Goals

- Do not build a fully distributed queue system unrelated to the current Runner/Web architecture.
- Do not replace the whole Web UI or standalone app beyond the artifact/status surfaces required by this plan.
- Do not require live external search, live LLM calls, or real API keys for acceptance tests.
- Do not remove the legacy executor fallback until the graph path is stable and explicitly selected.
- Do not silently relax evidence, source-policy, or upload ownership checks to make tests pass.

## Dependencies and Sequence

### Milestone 1: Durable Runner Foundation

1. Add database schema for runner leases, heartbeats, checkpoints, durable task event log, and attempt metadata.
2. Extend `TaskStore` and `PostgresTaskStore` with atomic lease claim/renew/release, checkpoint write/read, event append/list cursor, and recovery helpers.
3. Update Runner worker lifecycle to use durable ownership and append durable events before replay.
4. Add startup/status/event-stream reconciliation from durable state.
5. Verify terminal-state protection, lease exclusivity, event replay after restart, and checkpoint persistence.

### Milestone 2: Real Serial Graph Executor

1. Define node interfaces and typed state transitions for scope, planner, research unit, compressor, verifier, writer, reconciler, and complete.
2. Implement feature-flagged serial graph execution using LangGraph or an internal serial graph runner.
3. Move legacy single-agent orchestration behind fallback or bounded node adapters.
4. Persist graph checkpoints after every node and emit durable phase events.
5. Enforce required node outputs and failure propagation through Runner error/archive contracts.

### Milestone 3: Source and Evidence Lifecycle

1. Formalize source candidate, retrieved source, evidence item, finding, and verified claim schemas across tools, Runner archives, and Web artifacts.
2. Implement promotion rules from content-bearing sources to evidence.
3. Implement verifier rules that classify support and prevent unsupported high-confidence claims.
4. Ensure final reports validate `EVID-*` refs and claim support before save/archive.
5. Update artifact APIs, standalone UI, archive trace, and tests for the full lifecycle.

### Milestone 4: Uploaded Document Source Provider

1. Add a local uploaded-document retrieval provider/tool available to graph research nodes.
2. Store task-scoped chunk metadata and retrieval status durably enough for restart/resume.
3. Promote retrieved upload chunks through the same source/evidence lifecycle as web retrievals.
4. Enforce context-only fallback when text/chunks are unavailable.
5. Verify scenario/source policy can prioritize uploaded sources.

### Milestone 5: Server-Side Web Ingestion

1. Define a durable Runner event cursor or shared event-log reader for Web ingestion.
2. Add an idempotent server-side ingestion worker/path for task events, artifacts, reports, PDFs, and archive status.
3. Reconcile Web task history and Runner archive/status without browser SSE.
4. Add error/warning trace events for ingestion failures.
5. Verify duplicate events, reconnects, restarts, and no-browser-consumer flows.

### Milestone 6: Eval and Regression Harness

1. Extend offline evals for graph node failure, lease takeover, checkpoint resume, and server-side ingestion.
2. Add Postgres SQL-contract tests for all new schema constraints.
3. Add focused Runner/Web/Agent integration tests for the new durable and graph contracts.
4. Run previous RLCR focused suites as regression gates.

## Test and Verification Requirements

- Required focused suites:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_task_store_and_auth.py tests/test_runner_api.py -k "claim or lease or heartbeat or checkpoint or event_log or recover or stale or running"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_research_graph.py -k "graph or node or checkpoint or verifier or writer"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_artifacts.py tests/test_archive_writer.py -k "source or evidence or candidate or verification or artifact"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_web_routes.py -k "artifact or evidence or report or upload or document or research or ingestion or archive"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_frontend_contract.py -k "artifact or source or evidence or status"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests -k "missing_ref or snippet_only or upload_doc or scenario_policy or restart_recovery or graph_node_failure or lease_takeover or checkpoint_resume or server_side_ingestion"`
- Required static checks:
  - `git diff --check`
  - `python3 -m py_compile` for changed Python modules in `apps/limira-runner`, `apps/limira-agent`, `apps/limira-web`, and `libs/limira-tools`.
  - `node --check apps/limira-standalone/public/app.js` if standalone JavaScript changes.
- Required contract checks:
  - SQLite and Postgres SQL-contract tests must cover every new table/column/check constraint involved in leases, checkpoints, events, source lifecycle, and ingestion.
  - Archive trace and download tests must prove new metadata is present when needed and secrets are scrubbed.
  - Existing previous-loop focused tests for AC-R1 through AC-R5 must continue to pass.

## Implementation Notes

- Treat prior completed work as prerequisite state. Do not reimplement it unless a new full-draft acceptance criterion requires deeper integration.
- Keep feature flags narrow and explicit. Tests must exercise both fallback and enabled paths where behavior diverges.
- Prefer additive schema changes and idempotent migrations.
- Keep graph node state typed and serializable. Avoid storing raw model prompts or secrets in checkpoints.
- Code should not contain plan terminology such as `AC-F1`; use domain names like `TaskLease`, `GraphCheckpoint`, `SourceCandidate`, `EvidenceItem`, `VerifiedClaim`, and `ArtifactIngestionWorker`.
