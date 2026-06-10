# RLCR Continuation Plan: Feature-Flagged Agent Graph Execution

## Goal Description

Continue the Humanize RLCR work after `docs/plans/rlcr-plan.md` completed its Round 0 TaskContext milestone and passed Finalize. This continuation focuses on the original plan's AC-8 and milestone 6: add a safe, feature-flagged agent graph execution path so Limira starts moving from "research graph as prompt contract" toward an executable research graph.

The implementation must preserve the current legacy single-agent executor by default. The new graph path should be an incremental serial adapter around the existing research graph state and orchestrator, not a full rewrite of the agent loop. It must make graph phases explicit in code and observable in stream events when the flag is enabled.

Conservative assumptions:

- The previous RLCR loop already completed TaskContext propagation from Web to Runner to Agent and must not be reworked unless a test proves it regressed.
- The default production path remains legacy compatibility execution.
- This continuation may introduce a local serial graph executor without adding a hard LangGraph dependency if the repository does not already depend on LangGraph.
- The feature flag should be read from existing config shape where practical, with a safe default of disabled.

## Acceptance Criteria

- AC-G1: Legacy execution remains the default and unchanged for callers that do not enable graph execution.
  - Positive Tests (expected to PASS):
    - Existing `execute_task_pipeline` graph bootstrap behavior still emits the initial brief and plan events.
    - Existing fake orchestrator tests still show the legacy executor receiving the compatibility graph prompt when the flag is absent or false.
  - Negative Tests (expected to FAIL):
    - A default config that bypasses `Orchestrator.run_main_agent` should fail tests.

- AC-G2: A feature-flagged serial graph executor exists and is exercised by pipeline code.
  - Positive Tests (expected to PASS):
    - With the graph execution flag enabled, `execute_task_pipeline` uses a graph executor path instead of directly calling `Orchestrator.run_main_agent` from the pipeline body.
    - The graph executor emits phase events for at least `scope`, `plan`, `research`, `verify`, `write`, and `complete`.
    - The executor returns the same tuple contract as the legacy path: final summary, final boxed answer, and optional failure experience summary.
  - Negative Tests (expected to FAIL):
    - Enabling the graph flag without producing a final summary/final answer should fail.
    - Enabling the graph flag while skipping graph phase events should fail.

- AC-G3: Graph node contracts stay compatible with `ResearchGraphState`.
  - Positive Tests (expected to PASS):
    - Graph executor helpers accept and return `ResearchGraphState` or a narrowly scoped execution result without mutating unrelated payload shapes.
    - Phase transitions are deterministic and serial.
    - Failure in the research node propagates through the existing pipeline error handling instead of being swallowed.
  - Negative Tests (expected to FAIL):
    - Introducing graph execution that changes Runner/SSE/archive event schemas for existing bootstrap events should fail.

- AC-G4: Tests are deterministic and do not require live LLM, live search, or external API keys.
  - Positive Tests (expected to PASS):
    - Tests use fake orchestrators, fake queues, and local config fixtures.
    - `cd apps/limira-runner && uv run pytest tests/test_research_graph.py` covers the new feature-flagged path.
  - Negative Tests (expected to FAIL):
    - Tests that require live network services for graph executor acceptance should fail review.

## Path Boundaries

### Upper Bound (Maximum Scope)

Add a feature-flagged serial research graph executor, graph phase event helpers, focused config flag detection, deterministic tests, and minimal pipeline routing. The executor may wrap the existing orchestrator for the research/write step while making phase boundaries explicit.

### Lower Bound (Minimum Scope)

At least one real code path must change so that enabling a graph execution flag uses a dedicated graph executor helper instead of the pipeline directly invoking the orchestrator. Tests must prove both disabled and enabled behavior.

## Allowed Choices

- Can use: existing `ResearchGraphState`, `ResearchPhase`, `graph_task_description`, `graph_bootstrap_events`, `Orchestrator`, stream queues, and pytest fake orchestrators.
- Can use: a local serial adapter such as `execute_research_graph(...)` or `SerialResearchGraphExecutor`.
- Can use: config flags under `cfg.agent.research_graph.enabled`, `cfg.agent.research_graph_execution.enabled`, or another narrowly documented existing-compatible config path.
- Can use: compatibility events with new event names for graph phases as long as existing bootstrap event schemas are preserved.
- Cannot use: a hard dependency on LangGraph unless it already exists in the repository dependency graph and tests can run locally.
- Cannot use: a rewrite of the orchestrator, tool managers, Runner API, SSE contract, archive writer, Web frontend contract, or authentication boundary.
- Cannot use: live LLM/search/network-dependent tests as acceptance gates.
- Cannot edit `.humanize/rlcr/*/state.md` or skip Humanize hooks.

## Explicit Non-Goals

- Do not implement full parallel graph execution or bounded parallelism in this continuation.
- Do not replace the existing orchestrator internals.
- Do not implement evidence candidate/item closure, unresolved evidence reference blocking, durable worker leases, upload vector retrieval, or eval harness cases unless a review finding proves the graph execution work depends on them.
- Do not change default production behavior.

## Dependencies and Sequence

1. Bootstrap continuation RLCR:
   - Commit this continuation plan.
   - Run `/home/limira/.codex/skills/humanize/scripts/setup-rlcr-loop.sh docs/plans/rlcr-graph-execution-plan.md --yolo --track-plan-file --codex-model gpt-5.5:high --codex-timeout 7200 --full-review-round 2`.

2. Round 0 graph execution implementation:
   - Read the generated round prompt and initialize the goal tracker.
   - Create the round contract with one mainline objective: feature-flagged serial graph execution.
   - Inspect current config access patterns and choose the smallest safe feature flag helper.
   - Add graph executor code in `apps/limira-agent/src/core/research_graph.py` or a sibling module if that is cleaner.
   - Route `execute_task_pipeline` through the graph executor only when the flag is enabled; keep the default legacy call path.
   - Add deterministic tests in `apps/limira-runner/tests/test_research_graph.py`.
   - Commit implementation and write the round summary.

3. Review and finalize:
   - Let the native Humanize Stop hook run.
   - Address only blocking review findings.
   - In Finalize, simplify only behavior-equivalent code and rerun focused tests.

## Test and Verification Requirements

- Always run `git status --short` before each commit.
- For graph execution changes:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_research_graph.py`
- For pipeline contract confidence when touched:
  - `cd apps/limira-runner && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_runner_api.py -k "research"`
- Always run:
  - `git diff --check`
- Round summaries must include exact commands, pass/fail results, changed files, commit hash, and remaining risks.

## Implementation Notes

- Keep names product-neutral; do not write AC labels into runtime strings.
- Prefer simple serial phase functions over a large abstraction unless the code clearly needs a class.
- New graph events should be additive and should not rename or remove `research_brief_created` or `research_plan_created`.
- Treat the new executor as a compatibility bridge: it should make the graph executable enough for tests and future nodes, while still letting the existing orchestrator perform the model/tool work.
