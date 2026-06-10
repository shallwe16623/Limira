# Deep Research Harness Implementation Plan

## Goal Description

把当前 `dev` 分支上的 Limira research product harness 推进到可审计、可恢复、可验证的 deep research harness。计划聚焦四条闭环：Runner 任务上下文从 Web 入口持久化并传到 Agent；证据 ID、证据类型和报告引用校验一致；上传文档和 scenario 能影响研究策略；Runner 长任务恢复能力有清晰的最低实现和测试边界。

本计划基于 `docs/drafts/rlcr-draft.md` 和当前仓库代码生成。原 draft 中的绝对路径来自旧本地路径，实施时统一使用当前仓库相对路径，例如 `apps/limira-runner/runner_api.py`、`apps/limira-agent/src/core/research_graph.py`、`apps/limira-web/backend/limira_backend/routers/limira_parts/limira_part_003.pyfrag` 和 `libs/limira-tools/src/limira_tools/limira_evidence.py`。

保守假设：

- 第一轮优先修复可在本地确定性测试的 P1 闭环，不把完整 LangGraph 迁移、生产级 upload retrieval 或 durable worker lease 一次性塞进 Round 0。
- `TaskContext` 是最小地基，至少包含 `query`、`scenario`、`conversation_id`、`document_ids`、`upload_scope`、`source_policy`，并在 Runner task store、Runner worker、pipeline helper、Agent research graph seed 中保持可序列化。
- 搜索摘要先降级为 `SourceCandidate` 或低信任候选，只有 scrape/Jina summary/upload chunk/PDF parse 等内容型来源才能晋升为正式 `EvidenceItem`。
- 报告引用校验必须接受现有 hash 型 `EVID-xxxxxxxxxxxx`，并对不存在的 `EVID-*` 产生 warning 或阻断保存，具体策略以当前 artifact/report 保存路径的最小兼容实现为准。

## Acceptance Criteria

- AC-1: Draft 和正式 plan 完整落地并可被 RLCR setup 跟踪。
  - Positive Tests (expected to PASS):
    - `docs/drafts/rlcr-draft.md` 包含用户提供的 review draft 原文。
    - `docs/plans/rlcr-plan.md` 包含 `Goal Description`、`Acceptance Criteria`、`Path Boundaries`、`Allowed Choices`、`Explicit Non-Goals`、`Dependencies and Sequence`、`Test and Verification Requirements`。
    - `git status --short` 在提交 plan 后保持干净。
  - Negative Tests (expected to FAIL):
    - 输出路径已存在时不得覆盖旧 draft 或旧 plan。
    - plan 与当前仓库路径、模块或测试结构无关时不得启动 RLCR。

- AC-2: Web -> Runner -> Agent 的 TaskContext 合同闭环。
  - Positive Tests (expected to PASS):
    - Web research request 的 `scenario`、`conversation_id`、`document_ids` 被传入 Runner request payload，且不允许浏览器传入 trusted user fields。
    - Runner `TaskStore` 持久化 task context，`TaskRecord.to_dict()` 返回可 JSON 序列化的 context 字段。
    - Runner worker 调用 stream pipeline 时传入 context，而不是固定传 `None`。
    - Agent graph seed 能读取 context 中的 `scenario` 和 source policy，并在 scope/plan prompt 或 bootstrap event 中体现。
  - Negative Tests (expected to FAIL):
    - 创建带 `scenario`、`conversation_id`、`document_ids` 的 research task 后，Runner record 只保留 `query` 时测试失败。
    - Runner worker 仍把 pipeline context 参数传成 `None` 时测试失败。

- AC-3: Evidence ID 格式和报告引用抽取一致。
  - Positive Tests (expected to PASS):
    - Markdown 引用抽取能识别 `EVID-001` 和 hash 型 `EVID-xxxxxxxxxxxx`。
    - 工具层生成的 `EVID-<12 hex>` 能被 Web artifact/report 引用解析和归档路径识别。
    - 重复引用去重并保持原始出现顺序。
  - Negative Tests (expected to FAIL):
    - `EVID-abcdef123456` 这类当前工具生成 ID 被忽略时测试失败。
    - 非 evidence token 或格式截断 token 被错误识别时测试失败。

- AC-4: Search result candidate 与正式 evidence 的语义分层可测试。
  - Positive Tests (expected to PASS):
    - Google/search snippet 结果被标记为 candidate、snippet/source-candidate 或低 confidence 记录，不能默认等价为已核验正文证据。
    - scrape/Jina/upload chunk/PDF parse 这类内容型来源仍可形成正式 evidence artifact/event。
    - artifact payload 保留 source type、retrieved_at、content_hash、tool name 和 source URL。
  - Negative Tests (expected to FAIL):
    - 只有搜索摘要就支撑 high-confidence claim 时测试失败或产生明确 warning。
    - evidence payload 缺少 source type 或 retrieved timestamp 时测试失败。

- AC-5: 报告保存前有 unresolved evidence reference 校验。
  - Positive Tests (expected to PASS):
    - report section 或 final report 引用存在的 evidence id 时保存成功。
    - report section 或 final report 引用不存在的 `EVID-*` 时产生 artifact warning，或按当前保存路径的兼容策略拒绝正式入库。
    - warning 进入 artifact trace/event，便于 UI/history/audit 后续展示。
  - Negative Tests (expected to FAIL):
    - 报告引用不存在的 evidence id 但没有 warning、没有阻断、没有可审计 trace 时测试失败。

- AC-6: 上传文档成为 research source contract 的一部分。
  - Positive Tests (expected to PASS):
    - Web 端已归属校验通过的 document ids 会进入 TaskContext 的 `document_ids` 或 `upload_scope`。
    - Agent prompt/graph scope 能看到上传文档存在，并改变 source priority 或 required sources 描述。
    - 在缺少生产级 vector retrieval 时，有明确 fallback：记录 context、生成 source policy、避免承诺已自动检索上传正文。
  - Negative Tests (expected to FAIL):
    - Web 已 attach document，但 Runner/Agent context 完全不可见时测试失败。
    - 未做归属校验的 document id 被传给 Runner 或 Agent 时测试失败。

- AC-7: Durable runner 的最低恢复合同明确并逐步实现。
  - Positive Tests (expected to PASS):
    - Task store 的 queued/running/completed/failed/cancelled 状态转换仍有原子 claim 和终态保护。
    - 至少新增 recover/reconcile helper 或测试合同，能识别启动时遗留的 stale running task。
    - event replay 和 archive 状态的持久化差距被记录为 unresolved risk，直到实现 DB event log/checkpoint。
  - Negative Tests (expected to FAIL):
    - 同一个 queued task 被多个 worker claim 时测试失败。
    - stale running task 在无 heartbeat/worker owner 的情况下被误报 completed 时测试失败。

- AC-8: Feature-flagged graph execution 只做安全增量。
  - Positive Tests (expected to PASS):
    - legacy single-agent executor 默认保持可用。
    - 新 graph execution flag 关闭时，现有 `tests/test_research_graph.py` 和 pipeline bootstrap 行为不回退。
    - graph nodes 的状态模型与 `ResearchGraphState` 契约兼容。
  - Negative Tests (expected to FAIL):
    - 为引入 LangGraph 破坏 legacy runner/SSE/archive/frontend contract 时测试失败。
    - graph prompt 合同和状态模型漂移且无测试覆盖时测试失败。

- AC-9: Eval harness 覆盖 draft 中列出的最低场景。
  - Positive Tests (expected to PASS):
    - 添加或规划确定性 eval cases：`missing_ref`、`snippet_only`、`upload_doc`、`scenario_policy`、`restart_recovery`。
    - 能用本地 fixture 或 unit/contract tests 验证，不依赖 live search、live LLM 或外部 API。
  - Negative Tests (expected to FAIL):
    - eval 只跑 happy path，不能暴露 missing evidence、snippet-only、upload context 或 restart recovery 风险时测试失败。

## Path Boundaries

### Upper Bound (Maximum Scope)

在多个 RLCR round 内完成：TaskContext 全链路持久化；evidence candidate/item 分层；report evidence 引用校验；upload source contract；stale running task reconciliation；feature-flagged serial graph nodes；offline eval harness。可以增加小范围 helper、Pydantic/dataclass contract、SQLite schema migration 兼容逻辑和 focused tests。

### Lower Bound (Minimum Scope)

Round 0 至少完成一个真实 P1 代码修复和配套测试，优先顺序为：TaskContext 全链路、evidence ID 解析/校验、或 upload source contract。不得只写文档。提交必须包含相关测试和 round summary。

## Allowed Choices

- Can use: 现有 aiohttp Runner、FastAPI Web route parts、Pydantic/dataclass、SQLite task store、Postgres task store contract、现有 pytest suites、research graph models、tool evidence ledger、artifact trace warnings。
- Can use: schema 向后兼容迁移；context JSON 字段；小范围 helper extraction；feature flag；deterministic local fixtures；warning-first enforcement；legacy fallback。
- Can use: 串行 graph node shell 或 contract-only graph adapter，只要默认不破坏 legacy executor。
- Cannot use: 一次性重写 orchestrator/agent 主循环；依赖 live LLM/search 才能过测试；删除现有 Web/Runner/API 行为来通过测试；把未归属校验的 document id 直接传给 Agent；把 snippet-only 结果标成高置信正式证据；手动编辑 `.humanize/rlcr/*/state.md`；手动跳过 hook；用 ad hoc Codex review 替代 Humanize Stop hook。

## Explicit Non-Goals

- 不要求 Round 0 完整实现 LangGraph 全节点执行。
- 不要求 Round 0 完整实现生产级 durable queue、worker heartbeat、lease、checkpoint 和 DB event log。
- 不要求 Round 0 完整实现 upload vector retrieval、reranker 或 PDF parser。
- 不要求迁移所有历史 task records，但新字段必须有向后兼容默认值。
- 不改变认证信任边界：用户身份仍由 Web/Runner auth adapter 决定，不接受浏览器传来的 trusted user fields。
- 不引入需要真实外部 API key 的测试作为核心验收。

## Dependencies and Sequence

### Milestones

1. Planning and RLCR bootstrap:
   - Create `docs/drafts/rlcr-draft.md` from the supplied draft.
   - Generate `docs/plans/rlcr-plan.md` with ACs, boundaries, choices, non-goals, sequence and verification requirements.
   - Commit draft and plan.
   - Run `/home/limira/.codex/skills/humanize/scripts/setup-rlcr-loop.sh docs/plans/rlcr-plan.md --yolo --track-plan-file --codex-model gpt-5.5:high --codex-timeout 7200 --full-review-round 2`.

2. Round 0 priority implementation:
   - Read `.humanize/rlcr/*/round-0-prompt.md`.
   - Implement the highest-priority requirement from the generated prompt.
   - Prefer TaskContext and evidence parsing/validation because they are confirmed P1 breaks with small deterministic tests.
   - Add or update tests before or alongside the fix.
   - Run the smallest relevant pytest subset, then broader affected tests where feasible.
   - Commit code and write `.humanize/rlcr/*/round-0-summary.md`.

3. Evidence closure:
   - Align evidence ID extraction with tool-generated IDs.
   - Add unresolved evidence reference warning/block path.
   - Split candidate/evidence semantics at tool ledger and artifact boundary without breaking existing UI/history consumers.

4. Context and upload closure:
   - Persist TaskContext in Runner task store.
   - Pass context to pipeline helper and graph seed.
   - Add Web route/client tests proving scenario/conversation/upload ids survive to Runner.
   - Add graph/prompt tests proving context changes source policy.

5. Durable runner closure:
   - Keep claim/final-state protection intact.
   - Add stale-running detection/reconciliation helper and tests.
   - Defer full DB event log/checkpoint until the context/evidence contracts are stable.

6. Graph execution closure:
   - Add a feature-flagged serial graph adapter only after context and evidence contracts are reliable.
   - Preserve legacy fallback and existing runner/SSE/archive behavior.

7. Eval harness closure:
   - Add deterministic cases for `missing_ref`, `snippet_only`, `upload_doc`, `scenario_policy`, `restart_recovery`.
   - Record unresolved production gaps when a case is only partially enforceable.

## Test and Verification Requirements

- Always run `git status --short` before each commit.
- For Runner context/store/API changes:
  - `cd apps/limira-runner && uv run pytest tests/test_task_store_and_auth.py`
  - `cd apps/limira-runner && uv run pytest tests/test_runner_api.py`
- For Web route/client/context changes:
  - `cd apps/limira-runner && uv run pytest tests/test_limira_web_routes.py -k "research or upload or document"`
  - `cd apps/limira-runner && uv run pytest tests/test_limira_frontend_contract.py -k "document_ids or conversation"`
- For research graph/pipeline changes:
  - `cd apps/limira-runner && uv run pytest tests/test_research_graph.py`
- For evidence/artifact/report changes:
  - `cd apps/limira-runner && uv run pytest tests/test_limira_artifacts.py`
  - `cd apps/limira-runner && uv run pytest tests/test_limira_web_routes.py -k "artifact or evidence or report"`
- For durable runner changes:
  - `cd apps/limira-runner && uv run pytest tests/test_task_store_and_auth.py tests/test_runner_api.py -k "claim or cancel or terminal or recover or stale"`
- Round summaries must include exact commands, pass/fail results, changed files, commit hash, and remaining risks.
- A round is not complete if there are uncommitted changes, missing tests for touched behavior, or no summary file.

## Implementation Notes

- Do not write plan terminology such as `AC-2` into product code or user-facing strings.
- Keep new context/evidence helpers narrowly scoped and covered by focused tests.
- Preserve existing archive, SSE and frontend contracts unless the round prompt explicitly authorizes a breaking change with tests.
