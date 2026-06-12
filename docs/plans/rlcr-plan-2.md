# Limira LangGraph Deep Research Agent Hardening Plan

## Goal Description

Upgrade Limira from the current serial graph harness with a legacy agent adapter into a configurable, recoverable, auditable, testable graph-native deep research agent.

The current system already has durable Runner context, leases, checkpoints, event replay, a typed `ResearchGraphState`, a feature-flagged serial graph executor, source/evidence lifecycle artifacts, uploaded document source payloads, server-side Web ingestion, and offline eval coverage. This plan keeps those contracts and hardens the next layer: a product-routable `langgraph` executor path with explicit retriever, research-unit, evidence, verifier, writer, reconciler, and resumable checkpoint behavior.

Completion means `agent.research_graph.executor = legacy | serial | langgraph` is an explicit product configuration; the `langgraph` path uses graph state and node boundaries instead of treating the legacy orchestrator as the main brain; uploaded documents can be actively retrieved by query; strict evidence mode can block invalid final reports; verifier output is more expressive than "has refs therefore supports"; and offline evals cover the new routing, retrieval, verification, strict-mode, scenario, and resume behavior.

Assumption: the repo currently does not appear to include a LangGraph dependency. The implementation should prefer adding a scoped LangGraph dependency for the agent package. If dependency risk blocks the first slice, an internal `StateGraph`-compatible adapter may be used temporarily, but it must live behind the `langgraph` executor value and preserve the same state/node/checkpoint contract so a real LangGraph implementation can replace it without changing public behavior.

## Acceptance Criteria

- AC-1: Graph executor routing is explicit and configuration-driven.
  - Positive Tests (expected to PASS):
    - `agent.research_graph.executor = legacy` routes through the legacy orchestrator without invoking the serial or langgraph graph executors.
    - `agent.research_graph.executor = serial` routes through the existing `execute_research_graph(...)` serial executor.
    - `agent.research_graph.executor = langgraph` routes through the new LangGraph-compatible executor module.
    - The previous boolean `agent.research_graph.enabled` remains backward compatible by mapping to the serial executor unless an explicit executor value is set.
    - Runner/Web task execution can surface the selected executor in non-secret operational metadata or checkpoint executor state.
  - Negative Tests (expected to FAIL):
    - An invalid executor value silently falls back to legacy execution.
    - `langgraph` configuration succeeds when the langgraph executor module cannot be imported or initialized.
    - Tests can satisfy the `langgraph` route by monkeypatching only the serial executor.

- AC-2: The LangGraph executor reuses the existing graph contract and state.
  - Positive Tests (expected to PASS):
    - A new sibling module such as `apps/limira-agent/src/core/research_langgraph.py` or `langgraph_executor.py` defines the LangGraph-compatible executor without replacing `research_graph.py`.
    - The executor consumes or extends `ResearchGraphState` rather than introducing an unrelated state model.
    - State includes, directly or by existing nested models, `task_id`, `query`, `scenario`, `source_policy`, `upload_scope`, `brief`, `plan`, `current_unit_id`, `research_units`, `retrieved_sources`, `source_candidates`, `evidence`, `findings`, `claims`, `verified_claims`, `report_sections`, and `warnings`.
    - The `langgraph` path emits the same stable phase, artifact, report, and checkpoint event shapes that Runner and Web ingestion already understand.
    - The final graph report is streamed before completion and preserved in Web history/archive through existing ingestion.
  - Negative Tests (expected to FAIL):
    - The LangGraph executor stores raw model prompts, API keys, runner tokens, or unbounded model internals in checkpoint payloads.
    - The `langgraph` path produces checkpoint fields that Runner's public operational-status scrubber cannot summarize safely.
    - Existing serial graph tests break when the new module is imported.

- AC-3: LangGraph research units are decomposed into retrieval, retrieval-content, promotion, and synthesis steps.
  - Positive Tests (expected to PASS):
    - For each planned research unit, the `langgraph` path runs explicit sub-steps equivalent to `candidate_sources = retriever.search(unit)`, `retrieved_sources = retriever.retrieve(candidate_sources)`, `evidence_items = compressor.promote(retrieved_sources)`, and `findings = unit_synthesizer.summarize(evidence_items)`.
    - Snippet-only search results remain `source_candidate` records and cannot be promoted to evidence.
    - Content-bearing retrievals create `retrieved_source` records before evidence promotion.
    - Research-unit checkpoints include the current unit ID, completed unit IDs, source candidate count, retrieved source count, evidence count, and resumability marker.
    - The legacy orchestrator may be invoked only as a bounded fallback retriever/researcher node, not as the sole implementation of the `langgraph` executor.
  - Negative Tests (expected to FAIL):
    - A `langgraph` run can complete by delegating the whole task to a single legacy `run_main_agent(...)` call.
    - A research unit produces findings without any retrieved source or explicit weak/insufficient warning.
    - A source candidate is promoted to evidence without content-bearing source metadata.

- AC-4: Retriever registry is implemented and query-driven.
  - Positive Tests (expected to PASS):
    - A `RetrieverRegistry` exists in the agent core and can register/resolve retrievers by stable names.
    - The registry supports at least `web_search`, `page_visit_or_jina_summary`, `uploaded_document_search`, and `legacy_agent_adapter`.
    - Upload document retrieval can search task-scoped uploaded text by query/unit terms at execution time, not only consume startup prompt excerpts or preloaded `upload_scope.source_payloads`.
    - Scenario/source policy can alter retriever priority, for example preferring uploaded documents or scenario-specific source types.
    - Retriever outputs map cleanly into existing source candidate and retrieved source artifact schemas.
  - Negative Tests (expected to FAIL):
    - Upload facts are cited when the retriever only has document IDs and no retrieved text.
    - An unowned upload document ID reaches the retriever registry.
    - A disabled or unknown retriever silently returns empty success without a warning or recoverable error path.

- AC-5: Evidence strict mode supports warning and blocking behavior.
  - Positive Tests (expected to PASS):
    - A backend setting such as `limira.evidence.strict = warn | block` is parsed with a clear default of `warn`.
    - In `warn` mode, current unresolved evidence-reference behavior remains compatible: warnings are recorded and artifacts/reports are not rejected solely because of unresolved refs.
    - In `block` mode, final reports that reference nonexistent `EVID-*` IDs fail through an explicit compatible error path or are prevented from entering final archive/report history.
    - The block path records a structured, non-secret trace/event-log diagnostic that explains the missing refs.
    - Existing report-section and generated final-report validation share the same strict-mode decision point where practical.
  - Negative Tests (expected to FAIL):
    - `block` mode silently stores a final report containing unresolved `EVID-*` refs.
    - `warn` mode changes existing successful task completion behavior.
    - Invalid/truncated evidence refs are normalized into valid refs before strict validation.

- AC-6: Verifier support classification is rule-hardened beyond "has refs means supports".
  - Positive Tests (expected to PASS):
    - Verifier output can represent `supported`, `contradicted`, `insufficient`, `weak`, and `invalid_ref` or equivalent explicit failure/warning classifications.
    - No evidence yields `insufficient`.
    - Referencing a missing evidence ID yields `invalid_ref` or a blocking validation error in strict paths.
    - Claims supported only by source candidates yield `weak`.
    - Claims with content-bearing evidence yield `supported` when there is no contradiction.
    - Multiple source/evidence items with obvious opposing assertions can yield a deterministic `contradicted` classification in the rule-based verifier.
  - Negative Tests (expected to FAIL):
    - A high-confidence claim can be marked supported with only a source candidate.
    - A claim can omit evidence refs and still be treated as supported.
    - A contradicted claim is emitted as a clean supported claim without warning or conflict metadata.

- AC-7: LangGraph checkpoints are resumable, not only inspectable.
  - Positive Tests (expected to PASS):
    - LangGraph checkpoints identify the last completed graph node, current node, current research unit, completed unit IDs, pending unit IDs, source/evidence ledger state, and a resume policy.
    - Runner stale recovery can prefer a safe LangGraph resume attempt over immediate terminal failure when the stored checkpoint is resumable.
    - A restarted worker can resume from the last completed node and skip already completed research units.
    - If the current failed unit is safe to retry, resume continues from that unit without duplicating existing artifacts.
    - If the checkpoint is not resumable, the task fails through the existing recoverable reason/error/archive contract.
  - Negative Tests (expected to FAIL):
    - Killing a worker during a LangGraph run always makes the task terminal failed even when the last checkpoint is resumable.
    - Resume duplicates already-recorded source/evidence/report artifacts.
    - Resume overwrites terminal task state or steals a healthy active lease.

- AC-8: Writer output is based on verified claims and exposes uncertainty/conflict structure.
  - Positive Tests (expected to PASS):
    - The writer uses verified claims as its primary input rather than raw findings.
    - The report contains sections equivalent to `answer`, `key findings`, `evidence table`, `uncertainties`, `conflicts`, and `source notes`.
    - Every high-confidence claim in the report has evidence refs.
    - Weak, insufficient, invalid-ref, and contradicted claims are either excluded from high-confidence conclusions or clearly represented as uncertainty/conflict.
    - Writer output still emits existing report-section/final-report artifact events consumed by Web ingestion and archives.
  - Negative Tests (expected to FAIL):
    - The writer can cite raw findings not present in verified claims.
    - A high-confidence claim appears without evidence refs.
    - Contradicted or insufficient claims are presented as settled conclusions.

- AC-9: Offline evals become a quality gate for the hardened architecture.
  - Positive Tests (expected to PASS):
    - `apps/limira-runner/tests/test_deep_research_offline_eval.py` includes deterministic local cases for `case_resume_from_checkpoint`, `case_upload_search_used`, `case_contradiction_detected`, `case_strict_missing_ref_blocks_report`, `case_langgraph_executor_routes`, `case_scenario_changes_source_policy`, and `case_snippet_only_cannot_support_claim`.
    - Eval cases use fixtures/fakes and do not require live LLM, live search, external network, or API keys.
    - The eval matrix covers executor routing, retriever registry behavior, strict evidence blocking, verifier classifications, writer evidence closure, and resume behavior.
    - Existing full deep-research harness eval cases continue to pass.
  - Negative Tests (expected to FAIL):
    - The eval suite can pass with only the legacy executor.
    - Upload search evals pass without the upload retriever being queried.
    - Strict missing-ref blocking is tested only as a warning.

## Path Boundaries

### Upper Bound (Maximum Scope)

- Add a production-ready LangGraph-backed executor path with explicit graph node boundaries, retriever registry, active uploaded-document retrieval, rule-hardened verifier, verified-claim writer, strict evidence blocking mode, resumable LangGraph checkpoints, Runner resume integration, and deterministic offline/integration coverage.
- Make `agent.research_graph.executor=langgraph` suitable as the staging default while keeping production on serial/legacy fallback until rollout is explicitly enabled.
- Keep Runner, Web artifact ingestion, archive scrubbing, Postgres migrations, SQLite tests, and fakes aligned with any new checkpoint, strict-mode, retriever, or verifier metadata.

### Lower Bound (Minimum Scope)

- Implement the `legacy | serial | langgraph` executor routing contract.
- Add a LangGraph-compatible executor module that reuses `ResearchGraphState` and emits existing Runner/Web-compatible events/checkpoints.
- Add retriever registry with web/page/upload/legacy adapters and make LangGraph research units use it.
- Add strict evidence mode and a basic deterministic verifier with the required classifications.
- Add resumable checkpoint metadata and at least one tested resume path from the last completed node.
- Extend offline evals for the named quality-gate cases.

### Allowed Choices

- Add a scoped LangGraph dependency to the agent/runtime package if dependency resolution and packaging remain local and testable.
- If LangGraph cannot be safely added in the first implementation slice, create an internal `StateGraph`-compatible adapter behind the `langgraph` executor value, with a clear replacement boundary.
- Reuse `ResearchGraphState`, existing graph node models, Runner checkpoint envelope, source/evidence artifact schemas, upload source provider code, and Web ingestion/report validation helpers.
- Use deterministic Python fakes for web search, page retrieval/Jina summary, upload search, verifier, and writer tests.
- Keep legacy orchestrator fallback as a registry retriever/researcher adapter for compatibility and narrow gaps.

### Explicit Non-Goals

- Do not rewrite the Web UI.
- Do not replace Runner, TaskStore, artifact storage, archive writer, or Postgres schema architecture wholesale.
- Do not vendor or integrate GPT Researcher as the core executor.
- Do not require live LLM calls, live search, external network, or API keys for acceptance tests.
- Do not remove the legacy executor fallback.
- Do not relax existing evidence, upload ownership, archive scrub, or server-side ingestion checks to make the LangGraph route pass.
- Do not store raw prompts, API keys, service tokens, model internals, or unbounded research text in public checkpoints or archives.

## Dependencies and Sequence

### Milestone 1: Config and Executor Routing

1. Add a normalized executor-setting helper in `pipeline.py` or a small config helper module.
2. Support `agent.research_graph.executor = legacy | serial | langgraph`.
3. Preserve backward compatibility for existing `agent.research_graph.enabled`.
4. Add tests proving route selection and invalid-value failures.

### Milestone 2: LangGraph Skeleton

1. Add `research_langgraph.py` or `langgraph_executor.py` in `apps/limira-agent/src/core/`.
2. Define the LangGraph-compatible state adapter around `ResearchGraphState`.
3. Wire scope, planner, research-unit, compressor, verifier, writer, reconciler, and completion nodes.
4. Emit existing phase, checkpoint, artifact, report, and error events.
5. Add tests that the `langgraph` path cannot be satisfied by the serial executor.

### Milestone 3: Retriever Registry

1. Implement `RetrieverRegistry` and stable retriever interfaces.
2. Add `web_search`, `page_visit_or_jina_summary`, `uploaded_document_search`, and `legacy_agent_adapter`.
3. Make LangGraph research units call registry search/retrieve steps before promotion/synthesis.
4. Ensure uploaded document search is query-driven and ownership/task scoped.
5. Add tests for upload search use, scenario priority, disabled/unknown retrievers, and snippet-only candidate separation.

### Milestone 4: Evidence Strict Mode and Verifier Hardening

1. Add strict-mode config parsing and default `warn` behavior.
2. Route final report and report-section evidence-ref checks through warn/block policy.
3. Upgrade verifier classifications to supported, contradicted, insufficient, weak, and invalid-ref equivalents.
4. Update writer/report gates to respect unsupported and invalid claim classifications.
5. Add focused tests for strict missing-ref blocking, candidate-only weakness, missing evidence insufficiency, and contradiction detection.

### Milestone 5: Durable LangGraph Resume

1. Extend LangGraph checkpoint payloads with completed node/unit state and resume policy.
2. Add resume entry points that rebuild graph state from durable checkpoint and event/artifact ledgers.
3. Integrate Runner stale recovery so resumable LangGraph checkpoints are attempted before terminal failure.
4. Ensure resume is idempotent for artifacts, evidence, reports, and terminal status.
5. Add tests for worker kill/restart, last-completed-node resume, current-unit retry, duplicate prevention, and non-resumable failure.

### Milestone 6: Product Rollout and Eval Gate

1. Add or update dev/staging configuration for `agent.research_graph.executor=langgraph`; keep production on serial or explicit fallback unless rollout is requested.
2. Extend offline eval matrix with all named cases.
3. Run focused Runner, graph, artifact, Web ingestion/archive, deploy-contract, standalone contract, and offline eval suites.
4. Document rollout flags, fallback behavior, and operational status fields.

## Test and Verification Requirements

- Required focused suites:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_research_graph.py -k "executor or langgraph or graph or retriever or verifier or writer or checkpoint or resume"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_deep_research_offline_eval.py -k "resume_from_checkpoint or upload_search_used or contradiction_detected or strict_missing_ref_blocks_report or langgraph_executor_routes or scenario_changes_source_policy or snippet_only_cannot_support_claim"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_runner_api.py -k "checkpoint or resume or lease or recover or stale or event_stream"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_artifacts.py tests/test_archive_writer.py -k "source or evidence or candidate or verification or artifact or strict"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_web_routes.py -k "artifact or evidence or report or ingestion or archive or strict"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_frontend_contract.py -k "artifact or source or evidence or status"`
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_limira_deploy_contract.py -q`
- Required baseline command from the draft:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_research_graph.py tests/test_deep_research_offline_eval.py tests/test_runner_api.py tests/test_limira_artifacts.py -q`
- Required static checks:
  - `git diff --check`
  - `python3 -m py_compile` for changed Python modules in `apps/limira-agent`, `apps/limira-runner`, `apps/limira-web`, and `libs/limira-tools`.
  - If JavaScript changes are made, `node --check apps/limira-standalone/public/app.js`.
- Required contract checks:
  - Tests must prove invalid executor values fail clearly.
  - Tests must prove `langgraph` route uses the new executor module, not the serial executor.
  - Tests must prove strict `block` mode prevents unresolved final report evidence refs from entering final archive/report history.
  - Tests must prove checkpoint resume does not duplicate source/evidence/report artifacts.
  - Tests must prove archive/status scrubbers do not expose raw retriever prompts, API keys, worker tokens, or model internals after adding new metadata.

## Implementation Notes

- Keep plan terminology such as `AC-1` out of production code; use domain names like `ResearchGraphExecutor`, `RetrieverRegistry`, `VerifiedClaim`, `EvidenceStrictMode`, and `GraphResumeCheckpoint`.
- Keep `research_graph.py` as the schema and serial-executor contract owner. Put the LangGraph implementation in a sibling module to avoid destabilizing existing serial behavior.
- Prefer additive configuration and schema changes.
- Keep `warn` strict-mode default compatible with current users.
- Treat legacy orchestrator as a fallback node/tool, not the main implementation of the `langgraph` product path.
- Keep all new evals deterministic and local.
