# Remaining Deep Research Harness Completion Plan

## Goal Description

Complete the remaining deep research harness work that was intentionally left out of the earlier TaskContext and graph-execution RLCR loops. The previous completed work is treated as prerequisite foundation, not as final completion. This plan focuses on four required closure areas: evidence/reference integrity, candidate-vs-evidence semantics, uploaded documents as usable research sources, durable runner stale-running recovery, and deterministic eval coverage.

Final completion is only valid when all acceptance criteria in this plan are implemented and verified. A single successful Round 0 fix is not sufficient for Finalize unless every required remaining AC below is already satisfied.

Prerequisites already completed and must not be regressed:

- Web -> Runner -> Agent `TaskContext` propagation.
- Feature-flagged serial research graph execution with legacy fallback.
- Runner task terminal-state guard and atomic queued-task claim behavior.

## Acceptance Criteria

- AC-R1: Evidence ID parsing and unresolved reference validation are consistent.
  - Positive Tests (expected to PASS):
    - Markdown/reference extraction recognizes both numeric IDs such as `EVID-001` and hash-style IDs such as `EVID-abcdef123456`.
    - Tool-generated `EVID-<12 hex>` IDs are accepted by Web artifact/report parsing and archive/report paths.
    - Duplicate evidence references are deduped while preserving first-seen order.
    - Report section or final report artifacts referencing existing evidence IDs save normally.
    - Report section or final report artifacts referencing missing `EVID-*` produce an auditable warning event or are blocked by an explicit compatible error path.
  - Negative Tests (expected to FAIL):
    - Hash-style evidence IDs are ignored.
    - Invalid/truncated evidence tokens are accepted as valid references.
    - Missing references are silently persisted without warning, block, or trace.

- AC-R2: Source candidates are distinct from verified evidence items.
  - Positive Tests (expected to PASS):
    - Google/search snippets are represented as `source_candidate` or equivalent low-confidence records, not as fully verified evidence.
    - Content-bearing source outputs such as scrape, Jina summary, upload chunk, or parsed document text can be promoted to evidence.
    - Source payloads preserve source type, retrieved timestamp, content hash, tool name, URL, and confidence/source-state metadata.
  - Negative Tests (expected to FAIL):
    - Snippet-only search output is marked as high-confidence verified evidence.
    - Evidence/candidate payloads omit source type or retrieval timestamp.

- AC-R3: Uploaded documents become deterministic research source inputs.
  - Positive Tests (expected to PASS):
    - Attached, ownership-checked document IDs are visible in TaskContext and graph source policy without accepting untrusted browser identity fields.
    - Locally available uploaded document text can be emitted as upload source candidates or evidence artifacts with document ID, source type, retrieved timestamp, and content hash.
    - If uploaded document retrieval is unavailable, the task records a clear `context_only` or equivalent retrieval status and avoids claiming uploaded text was retrieved.
  - Negative Tests (expected to FAIL):
    - An attached document remains invisible to Runner/Agent source handling.
    - Upload facts are claimed as retrieved/cited when only document IDs were available.
    - Unowned document IDs bypass ownership validation and reach Agent source handling.

- AC-R4: Runner stale-running recovery has a deterministic minimum contract.
  - Positive Tests (expected to PASS):
    - Existing atomic queued-task claim and terminal-state protection still pass.
    - A deterministic helper detects `running` tasks without active worker ownership or with stale heartbeat/started time.
    - Startup/status reconciliation marks stale running tasks failed, cancelled, or retryable according to an explicit local policy and records a warning/error reason.
  - Negative Tests (expected to FAIL):
    - A stale running task is reported as completed without worker output.
    - A terminal task is overwritten by stale recovery.
    - A healthy active running task is incorrectly failed by stale recovery.

- AC-R5: Offline eval harness covers the minimum deep-research failure modes.
  - Positive Tests (expected to PASS):
    - Deterministic local cases exist for `missing_ref`, `snippet_only`, `upload_doc`, `scenario_policy`, and `restart_recovery`.
    - Each case fails before its corresponding contract is implemented or asserts the contract directly with local fixtures.
    - The eval set runs without live LLM, live search, external network, or API keys.
  - Negative Tests (expected to FAIL):
    - Eval coverage only tests happy paths.
    - Eval cases cannot detect missing evidence, snippet-only claims, upload source visibility, scenario source policy, or stale runner recovery.

## Path Boundaries

### Upper Bound (Maximum Scope)

Implement evidence-reference helpers, candidate/evidence source-state metadata, unresolved reference validation at artifact/report boundaries, upload-source candidate/evidence emission from locally available text, stale-running reconciliation helpers and tests, and a deterministic offline eval harness that groups the required cases.

### Lower Bound (Minimum Scope)

All AC-R1 through AC-R5 must be satisfied before final completion. Round 0 may choose the first implementation slice, but Finalize is not acceptable until every required AC in this plan has deterministic passing tests and no known review blockers.

## Allowed Choices

- Can use: existing `limira_tools.limira_evidence`, `limira_tools.limira_artifacts`, Runner archive/event state, Web route artifact/report parsers, existing upload repository helpers, Runner `TaskStore`, `ResearchGraphState`, and pytest fixtures.
- Can use: warning-first enforcement when blocking persistence would be too invasive for the current UI/API contract.
- Can use: local source-state fields such as `source_state`, `source_type`, `candidate`, `confidence`, `retrieved_at`, and `content_hash`.
- Can use: deterministic helper modules for eval fixtures if they avoid live services.
- Cannot use: live search, live LLM, or external API keys for required tests.
- Cannot use: broad rewrites of the orchestrator, Web frontend, authentication model, or Runner archive/SSE contracts.
- Cannot bypass ownership checks for uploaded document IDs.
- Cannot silently treat snippets as high-confidence verified evidence.
- Cannot edit `.humanize/rlcr/*/state.md`, skip Humanize hooks, or substitute ad hoc review for the Stop hook.

## Explicit Non-Goals

- Production-grade distributed queue leasing, DB event log, and checkpoint replay are not required beyond the deterministic stale-running recovery contract.
- Full upload vector retrieval, reranking, and PDF parser integration are not required if locally available extracted text can be used and unavailable retrieval is explicitly recorded.
- Full LangGraph replacement of the orchestrator is already handled by the graph-execution continuation and is not part of this plan.
- UI redesign is out of scope unless a test requires a contract-safe warning/event field.

## Dependencies and Sequence

### Milestones

1. Evidence reference closure:
   - Add or consolidate evidence reference extraction for numeric and hash-style IDs.
   - Validate report/report_section references against task evidence IDs where artifact state is available.
   - Emit warnings or compatible errors for unresolved references.
   - Add focused artifact/report tests.

2. Candidate/evidence semantic closure:
   - Split search snippet outputs from verified evidence by adding source-state metadata or `source_candidate` artifacts.
   - Preserve content-bearing source outputs as evidence.
   - Add tool-ledger tests for snippet-only and content-bearing promotion.

3. Upload source closure:
   - Emit deterministic upload source candidates/evidence from locally available uploaded document text.
   - Preserve ownership validation and context-only fallback.
   - Add route/graph/tool tests for upload source handling.

4. Durable runner recovery closure:
   - Add stale-running detection and reconciliation helper.
   - Keep terminal-state guard and queued claim behavior intact.
   - Add SQLite and Postgres-contract tests where feasible.

5. Eval harness closure:
   - Add deterministic local eval fixtures/cases for `missing_ref`, `snippet_only`, `upload_doc`, `scenario_policy`, and `restart_recovery`.
   - Wire them into existing pytest suites or a clearly named offline eval test module.

6. Final completion gate:
   - Run all focused suites required by touched components.
   - Confirm AC-R1 through AC-R5 are completed in the goal tracker before Finalize.

## Test and Verification Requirements

- Always run `git status --short` before each commit.
- Evidence/artifact/report changes:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_artifacts.py`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_web_routes.py -k "artifact or evidence or report"`
- Tool evidence changes:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_artifacts.py tests/test_archive_writer.py -k "evidence or artifact or source"`
- Upload source changes:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_web_routes.py -k "upload or document or research"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_research_graph.py`
- Durable runner changes:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_task_store_and_auth.py tests/test_runner_api.py -k "claim or cancel or terminal or recover or stale or running"`
- Eval harness changes:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests -k "missing_ref or snippet_only or upload_doc or scenario_policy or restart_recovery"`
- Always run `git diff --check`.
- Round summaries must include exact commands, pass/fail results, changed files, commit hash, and remaining risks.

## Implementation Notes

- Do not write AC labels into runtime product strings.
- Prefer small helper APIs with focused tests over broad rewrites.
- Preserve existing public response shapes unless adding a warning/event field is required for auditability.
- Keep legacy fallback behavior working while tightening the deep-research contracts.
