# Limira Deep Research Harness Product Hardening Plan

## Goal Description

Harden Limira from a strong Deep Research harness foundation into a product-ready, graph-native research harness that can safely run concurrent tasks, route real product traffic into LangGraph when configured, retrieve source-bearing evidence without relying on legacy fallback, verify claims with stronger evidence semantics, close LLM resources on failure, and preserve an auditable archive across resume.

This plan starts from the current `dev` branch, where the previous RLCR loop already delivered durable Runner tasks, event replay, artifacts, checkpoints, TaskContext, typed research graph state, LangGraph routing, resumable checkpoints, verified-claim writing, and offline evals. The remaining gap is not the harness shell. The remaining gap is product readiness: task isolation, LangGraph rollout configuration, real retrieval, stronger verifier semantics, failure cleanup, and archive auditability after resume.

Conservative assumptions from the draft:

- Existing `legacy`, `serial`, and `langgraph` executor routes should remain supported.
- Production should not silently switch all traffic to LangGraph unless explicitly configured.
- Tests must remain deterministic and must not require live LLMs, live search, external network, browser automation, or API keys.
- Caching immutable configuration or tool definitions is allowed, but mutable per-task runtime objects must not be shared across concurrent tasks.
- If a retriever cannot access live services in tests, deterministic fake search/scrape/Jina retrievers should exercise the same registry and promotion paths.

## Acceptance Criteria

Following TDD philosophy, each criterion includes positive and negative tests for deterministic verification. The `AC-*` items are current RLCR completion gates for this implementation loop.

- AC-1: Runner task execution isolates mutable tool and formatter state per task.
  - Positive Tests (expected to PASS):
    - Two concurrently running Runner tasks each receive distinct `ToolManager` instances for mutable task runtime state.
    - Each task's `set_task_log(...)` call affects only that task's manager and cannot overwrite another task's log owner.
    - Browser/session state, tool traces, evidence ledger updates, and formatted tool outputs produced by one task do not appear in the other task's trace or archive.
    - The preload path may still cache immutable config, tool definitions, and safe factory inputs.
  - Negative Tests (expected to FAIL):
    - Two tasks share the same mutable `main_agent_tool_manager` object.
    - A later task can overwrite an earlier task's `task_log` or browser session.
    - A concurrent-task regression can pass while asserting only sequential behavior.

- AC-2: LangGraph is product-routable through explicit configuration and operational metadata.
  - Positive Tests (expected to PASS):
    - `LIMIRA_RESEARCH_GRAPH_EXECUTOR=legacy|serial|langgraph` or an equivalent repository-native config override maps into `agent.research_graph.executor`.
    - Invalid executor environment/config values fail through the existing compatible error path instead of silently falling back.
    - Runner/Web task status exposes the selected executor through existing non-secret operational status or checkpoint fields.
    - Runner archive metadata or trace includes the selected executor in a scrubbed, non-secret form.
    - A Web/Runner task created under the `langgraph` setting actually invokes the LangGraph route, not the legacy or serial executor.
  - Negative Tests (expected to FAIL):
    - Setting the executor to `langgraph` still runs the legacy path.
    - The selected executor is visible only in transient logs and missing from status/archive outputs.
    - An invalid environment value is ignored and defaults to legacy.

- AC-3: LangGraph retrievers retrieve source-bearing content without requiring legacy fallback.
  - Positive Tests (expected to PASS):
    - `web_search` can produce source candidates through the existing retriever registry using deterministic fake search results in tests.
    - `page_visit_or_jina_summary` or an equivalent page/scrape/Jina retriever can turn a candidate into a content-bearing retrieved source in tests.
    - Snippet-only search results remain `source_candidate` artifacts and cannot become evidence.
    - Content-bearing scraped/Jina/upload retrieved sources can be promoted into evidence and used in a source-backed report.
    - With `legacy_agent_adapter` disabled or omitted from retriever order, a deterministic fake search plus fake scrape/Jina path can still complete a source-backed LangGraph report.
  - Negative Tests (expected to FAIL):
    - A candidate with only snippet text is promoted to evidence.
    - The LangGraph path calls `legacy_agent_adapter` even when legacy fallback is disabled for the test.
    - A final report is marked source-backed when all retrieval attempts returned only candidates and no content-bearing source.

- AC-4: Verifier classification uses claim-evidence entailment semantics stronger than ref-exists checks.
  - Positive Tests (expected to PASS):
    - Verified claims include enough structured information to distinguish claim text, evidence refs, evidence spans or excerpts, support type, contradiction or counterevidence, and relevant temporal context when available.
    - Evidence that mentions the same entity but does not support the conclusion is classified as `insufficient`, `weak`, or an equivalent non-supported status.
    - Evidence with stale dates or incompatible time scope is not treated as high-confidence current support.
    - Background-only sources do not support judgment claims unless the evidence excerpt directly entails the claim.
    - Contradictory evidence remains represented as contradiction/conflict and is not emitted as a settled supported claim.
    - Writer high-confidence sections only use claims with valid, content-bearing, temporally compatible support.
  - Negative Tests (expected to FAIL):
    - A claim is marked `supported` merely because it has a valid `EVID-*` ref.
    - A source that only provides background context supports a prescriptive or comparative conclusion.
    - Stale evidence is presented as current support without warning or downgraded classification.

- AC-5: LLM client resources are closed on success and failure paths.
  - Positive Tests (expected to PASS):
    - `ClientFactory.close()` is called after successful legacy, serial, and LangGraph pipeline execution.
    - `ClientFactory.close()` is called when graph execution raises after the client is created.
    - `ClientFactory.close()` is called when legacy orchestration raises after the client is created.
    - Close behavior is safe when client creation fails before an instance exists.
  - Negative Tests (expected to FAIL):
    - A graph/orchestrator exception skips client close.
    - Failure cleanup hides the original task failure or changes the existing streamed error contract.
    - Cleanup attempts to close an uninitialized client and raises a secondary error.

- AC-6: Resume archive output preserves or explicitly accounts for pre-resume task history.
  - Positive Tests (expected to PASS):
    - After a stale resumable LangGraph task is requeued and completed, the final `trace.json` includes pre-resume durable events or an explicit restored checkpoint ledger that accounts for pre-resume source/evidence/report state.
    - Archive trace output makes the resume boundary auditable without exposing raw secrets or unbounded model internals.
    - Resume archive generation remains idempotent and does not duplicate already-recorded source, evidence, report, or terminal events.
    - Existing failed, cancelled, and archive-failed diagnostic archive behavior remains compatible.
  - Negative Tests (expected to FAIL):
    - A resumed task archive contains only post-restart events with no restored checkpoint/source/evidence context.
    - Resume archive repair duplicates pre-resume artifacts or terminal events.
    - The archive includes raw task owner identity, runner service tokens, API keys, or unbounded prompt/model payloads.

## Path Boundaries

### Upper Bound (Maximum Scope)

The implementation may complete all six readiness gaps with production-grade hooks:

- Per-task manager/formatter factories or clone methods that retain preload performance while preventing mutable state sharing.
- Environment/config-driven LangGraph rollout with explicit status and archive metadata.
- Registry-backed deterministic retriever integrations for search, page/Jina/scrape, upload chunks, and disabled legacy fallback.
- Structured verifier output with spans, support type, contradiction, temporal caveats, writer gating, and offline eval coverage.
- Robust client cleanup in `finally` blocks without changing user-visible error contracts.
- Resume-aware archive traces that preserve prior durable events or checkpoint ledger context safely.

### Lower Bound (Minimum Scope)

The minimum acceptable implementation must still satisfy every AC:

- Replace shared mutable preload objects with fresh per-task runtime instances or equivalent clone boundaries.
- Add one supported product configuration path for `legacy|serial|langgraph`, including invalid-value tests and executor visibility in status/archive.
- Add deterministic fake-backed retriever tests proving LangGraph can finish a source-backed report without the legacy adapter.
- Upgrade rule-based verifier behavior enough to fail the specific negative entailment, stale-date, and background-only cases.
- Ensure LLM client close is called on success and exception paths.
- Add resume archive coverage proving pre-resume history is preserved or explicitly represented.

### Allowed Choices

- Can use repository-native config mechanisms, OmegaConf overrides, environment-variable mapping, or startup config adapters.
- Can use factories, clone methods, or constructor-data caching to create per-task `ToolManager` and `OutputFormatter` instances.
- Can use deterministic fake retrievers and fake page/Jina/scrape responses for tests while preserving the production registry interface.
- Can extend existing graph state, verified claim models, artifact payloads, checkpoint payloads, and archive trace payloads when fields are bounded and scrubbed.
- Can keep the legacy adapter as an explicit fallback retriever, as long as tests can disable it and still validate the new source-backed retrieval path.
- Cannot require live network, live LLMs, live browser sessions, external API keys, or manually provisioned services for acceptance tests.
- Cannot remove `legacy` or `serial` executor compatibility.
- Cannot weaken upload ownership checks, evidence-ref validation, archive scrubbing, task authorization, terminal-state guards, or durable event replay semantics.
- Cannot store raw prompts, API keys, service tokens, browser credentials, unbounded page contents, or model internals in public checkpoints/status/archive output.

## Explicit Non-Goals

- Do not rewrite the Web UI or change the core user experience.
- Do not replace Runner, TaskStore, archive storage, or Web ingestion architecture wholesale.
- Do not make LangGraph the production default without an explicit config setting.
- Do not integrate a full external benchmark runner in this loop.
- Do not replace the existing deterministic offline eval framework.
- Do not remove the legacy adapter; only make it explicit and avoid depending on it for all LangGraph success.
- Do not introduce live-provider dependencies into CI-style tests.

## Feasibility Hints and Suggestions

### Conceptual Approach

One viable path is:

1. Split preload into immutable cached data and per-task runtime creation.
2. Thread executor config from env/startup into the existing `agent.research_graph.executor` resolver.
3. Add fakeable retriever interfaces that exercise the same search, retrieve, promote, verify, and writer paths as production.
4. Extend verifier logic conservatively with deterministic span/temporal checks before considering any LLM entailment provider.
5. Wrap pipeline client lifecycle in a `try/finally` that preserves current streamed error behavior.
6. During archive finalization, merge durable pre-resume events or checkpoint ledger summaries into trace context through existing scrubbers.

### Relevant References

- `apps/limira-runner/pipeline_helpers.py` - Runner preload cache, per-task pipeline invocation, stream bridge.
- `apps/limira-agent/src/core/pipeline.py` - executor selection, ToolManager task-log binding, LLM client lifecycle.
- `libs/limira-tools/src/limira_tools/manager.py` - mutable ToolManager task log and browser/session state.
- `apps/limira-agent/src/core/research_graph.py` - retriever registry, LangGraph research unit node, verifier/writer integration.
- `apps/limira-agent/src/core/research_graph_support.py` - verification helper and checkpoint restore helpers.
- `apps/limira-runner/runner_api.py` - Runner status/checkpoint exposure, stale resume recovery, durable events.
- `apps/limira-runner/archive_writer.py` - archive trace/report/metadata writing and scrubbing.
- `apps/limira-runner/tests/test_research_graph.py` - graph unit and retriever tests.
- `apps/limira-runner/tests/test_research_graph_pipeline.py` - pipeline executor routing and graph integration tests.
- `apps/limira-runner/tests/test_deep_research_offline_eval.py` - deterministic offline quality gates.
- `apps/limira-runner/tests/test_runner_api.py` - Runner status, checkpoint, archive, stale recovery, and streaming tests.
- `apps/limira-runner/tests/test_archive_writer.py` - archive trace and scrubbing tests.

## Dependencies and Sequence

### Milestones

1. Safety foundations: task isolation and client cleanup.
   - Add or refactor per-task runtime factory boundaries in Runner preload.
   - Add concurrent task isolation regression.
   - Move LLM client cleanup into safe failure-aware lifecycle handling.
   - Add success/failure close regressions.

2. Product routing: explicit LangGraph rollout surface.
   - Add environment/config mapping for `legacy|serial|langgraph`.
   - Preserve existing config precedence and invalid-value errors.
   - Add status/archive executor visibility tests.
   - Add a Web/Runner integration-style test proving configured `langgraph` enters the LangGraph route.

3. Real retrieval path: source-backed LangGraph without legacy fallback.
   - Add fakeable search and page/Jina/scrape retriever implementations or adapters.
   - Preserve snippet-only candidate semantics.
   - Disable legacy fallback in a focused test and prove source-backed completion.
   - Ensure upload chunk retrieval remains task scoped.

4. Stronger verification and writer gating.
   - Extend verified claim payloads with bounded evidence excerpts/spans and temporal/support metadata.
   - Add deterministic entailment rules for unrelated same-entity evidence, stale evidence, and background-only evidence.
   - Ensure writer high-confidence conclusions use only supported claims.
   - Add negative offline eval cases.

5. Resume archive audit.
   - Identify where pre-resume durable events or checkpoint ledgers are available at finalization.
   - Add scrubbed trace fields or event merge behavior.
   - Add tests for resumed archive completeness, idempotence, and secret safety.

6. Final verification gate.
   - Run focused suites for changed areas.
   - Run the draft baseline suites.
   - Run `py_compile` and `git diff --check`.

## Task Breakdown

| Task ID | Description | Target AC | Tag (`coding`/`analyze`) | Depends On |
|---------|-------------|-----------|----------------------------|------------|
| task1 | Refactor Runner preload so cached data is immutable and each task gets fresh mutable tool/formatter runtime state. | AC-1 | coding | - |
| task2 | Add concurrent task isolation tests for tool logs, evidence traces, and browser/session state. | AC-1 | coding | task1 |
| task3 | Make LLM client cleanup failure-safe and add close-on-error regressions. | AC-5 | coding | - |
| task4 | Add environment/config mapping for product executor routing and invalid-value behavior. | AC-2 | coding | - |
| task5 | Add Runner/Web status and archive assertions for selected executor under configured LangGraph. | AC-2 | coding | task4 |
| task6 | Connect fakeable search and page/Jina/scrape retrieval through the existing retriever registry. | AC-3 | coding | - |
| task7 | Add source-backed LangGraph report test with legacy fallback disabled and snippet-only evidence rejection. | AC-3 | coding | task6 |
| task8 | Extend verifier output and rules for evidence spans, support type, contradiction, and temporal compatibility. | AC-4 | coding | task6 |
| task9 | Add verifier/writer negative evals for same-entity unsupported evidence, stale evidence, and background-only evidence. | AC-4 | coding | task8 |
| task10 | Add resume archive trace or checkpoint-ledger representation for pre-resume task history. | AC-6 | coding | - |
| task11 | Add resumed archive completeness, idempotence, and scrub-safety tests. | AC-6 | coding | task10 |
| task12 | Run and document focused and baseline verification commands. | AC-1, AC-2, AC-3, AC-4, AC-5, AC-6 | coding | task2, task3, task5, task7, task9, task11 |

## Future Work / Out of Scope

- FUT-1: Run external Deep Research benchmark suites such as xbench or DeepResearch bench.
  - Current-loop handoff: AC-2, AC-3, AC-4, AC-6.
  - Promotion trigger: product owners select a benchmark target and provide benchmark harness requirements.
- FUT-2: Add optional LLM-based entailment verification.
  - Current-loop handoff: AC-4.
  - Promotion trigger: deterministic verifier gaps are documented and a provider/cost policy exists.
- FUT-3: Make LangGraph the production default.
  - Current-loop handoff: AC-2.
  - Promotion trigger: staging soak passes and rollback metrics are defined.

## Claude-Codex Deliberation

### Agreements

- The draft is relevant to the current repository and follows directly from the previous LangGraph hardening loop.
- The current loop should target product readiness gaps, not repeat the already-completed graph executor foundation.
- Deterministic tests should drive all acceptance criteria.

### Resolved Disagreements

- Scope of "complete Deep Research harness": the loop should not claim a full benchmark-grade research agent. It should close the concrete readiness gaps named in the draft.
- LangGraph defaulting: this loop should add explicit product routing and staging-ready configuration, but should not silently make LangGraph the production default.

### Convergence Status

- Final Status: `converged`

## Pending User Decisions

- None. The plan uses conservative defaults from the draft: explicit LangGraph routing, no live test dependencies, no production default switch without configuration, and no weakening of existing safety boundaries.

## Test and Verification Requirements

- Required focused tests by area:
  - `cd apps/limira-runner && .venv/bin/python -m pytest -q tests/test_research_graph_pipeline.py -k "client or close or executor or langgraph"`
  - `cd apps/limira-runner && .venv/bin/python -m pytest -q tests/test_runner_api.py -k "executor or archive or resume or stale or checkpoint or event_stream"`
  - `cd apps/limira-runner && .venv/bin/python -m pytest -q tests/test_research_graph.py -k "retriever or source or evidence or verifier or writer or entailment or temporal"`
  - `cd apps/limira-runner && .venv/bin/python -m pytest -q tests/test_deep_research_offline_eval.py -k "retriever or verifier or stale or temporal or background or resume or archive"`
  - `cd apps/limira-runner && .venv/bin/python -m pytest -q tests/test_archive_writer.py -k "trace or archive or scrub"`
- Required baseline tests from the draft:
  - `cd apps/limira-runner && .venv/bin/python -m pytest -q tests/test_task_store_and_auth.py tests/test_runner_api.py`
  - `cd apps/limira-runner && .venv/bin/python -m pytest -q tests/test_deep_research_offline_eval.py`
  - `cd apps/limira-runner && .venv/bin/python -m pytest -q tests/test_research_graph.py tests/test_research_graph_pipeline.py`
- Required static checks:
  - `git diff --check`
  - `apps/limira-runner/.venv/bin/python -m py_compile` for every changed Python file in `apps/limira-agent`, `apps/limira-runner`, and `libs/limira-tools`.
- Summary requirements for every RLCR round:
  - List changed files.
  - List commands run and outcomes.
  - State which ACs advanced or completed.
  - Document any skipped broad test with a concrete reason.

## Implementation Notes

### Code Style Requirements

- Implementation code and comments must not contain plan-specific workflow markers such as acceptance-criterion or milestone labels.
- Keep changes localized to Runner, agent graph/pipeline, tool manager boundaries, archive writer, and relevant tests unless a dependency is directly required.
- Prefer existing repository patterns and fakes over new frameworks.
- Preserve public API and archive/status compatibility unless a bounded, scrubbed field is explicitly added for this plan.
