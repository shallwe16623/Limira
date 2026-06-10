# Dev Risk Hardening And Completion Plan

## Goal Description

对当前 `dev` 分支做一轮软件工程风险审查、堵点修复和未完成开发补完的 RLCR 循环。目标不是重写 Limira，而是在保留现有产品壳、权限边界、归档/PDF、SSE、对象存储、runner、agent runtime、MCP 工具和已有测试契约的前提下，找出并修复会导致真实部署、真实运行、真实多用户使用、稳定测试或研究结果闭环失败的问题。

当前代码已经有研究图契约、工具层自动证据事件、Postgres/Redis/MinIO 部署骨架、runner owner isolation、archive secret scrubbing 和真实前端入口。本计划要求 RLCR 每轮基于实际代码选择最高优先级风险，做真实代码改动和测试验证，不允许只写文档或只包装 smoke test。

## Acceptance Criteria

Following TDD philosophy, each criterion includes positive and negative tests for deterministic verification.

- AC-1: 启动审查和计划文件完整落地。
  - Positive Tests (expected to PASS):
    - `docs/drafts/dev-risk-hardening-and-completion-draft.md` 记录当前目录、分支、工作区状态、最近提交、项目结构、已有 RLCR loop、实际代码状态、风险和堵点。
    - `docs/plans/dev-risk-hardening-and-completion-plan.md` 包含 `Goal Description`、`Acceptance Criteria`、`Path Boundaries`、`Allowed Choices`、`Explicit Non-Goals`、`Dependencies and Sequence`、`Test and Verification Requirements`、`Risk Priority Rules`。
  - Negative Tests (expected to FAIL):
    - draft 或 plan 缺少当前代码实际状态时，不能启动 RLCR。
    - 输出计划路径已存在或 IO 校验失败时，不能覆盖旧计划继续执行。

- AC-2: 浏览器和前端边界不泄露 runner、服务 token、API key、cookie、Authorization、对象 key 或内部下载路径。
  - Positive Tests (expected to PASS):
    - standalone proxy 仅允许 `/api/limira` 和 `/api/limira/*`，拒绝 `/api/*` 的其他路径和 `/limira-runner/*`。
    - web/runner 返回给浏览器的 JSON、SSE、archive、PDF、error 和 frontend state 都经过敏感字段过滤或显式白名单。
  - Negative Tests (expected to FAIL):
    - 浏览器请求体包含 `user_id`、`owner_user_id`、`object_key`、`archive_object_key`、`pdf_object_key` 等信任字段时被拒绝或忽略。
    - archive、event log、error detail、frontend bundle 中出现 service token、API key、Authorization header、cookie 或 raw object key 时测试失败。

- AC-3: 多用户和管理员权限隔离闭环。
  - Positive Tests (expected to PASS):
    - 用户只能读取、订阅、下载、搜索和导出自己的 task/upload/report/archive/artifact。
    - 管理员路径显式使用 admin role/header/session，普通用户不能调用 admin-only task/event/archive endpoints。
  - Negative Tests (expected to FAIL):
    - 用户 A 可以访问用户 B 的 task、event stream、archive、upload、PDF 或 artifact 时测试失败。
    - 普通用户不带 admin 身份可见全局数据时测试失败。

- AC-4: archive、download、ZIP 和文件路径安全闭环。
  - Positive Tests (expected to PASS):
    - archive 只包含允许的成员，成员内容被 scrub，文件名和 task id 被校验，下载必须通过 owner/admin 授权。
    - archive 生成失败、render 失败、ZIP 失败、下载文件丢失时状态和错误保持一致。
  - Negative Tests (expected to FAIL):
    - ZIP 包含 `.env`、隐藏内部文件、路径穿越成员、未 scrub secret 或跨用户对象时测试失败。
    - 任意 `../`、绝对路径或反斜杠逃逸 task/archive 根目录时测试失败。

- AC-5: 任务状态机和 runner 生命周期不会卡住、重复完成或误报成功。
  - Positive Tests (expected to PASS):
    - queued、running、completed、failed、cancelled、archive pending/ready/failed 的状态转换有原子 claim、终态保护和一致的 timestamps/error/warnings。
    - 断开连接、取消、pipeline 抛错、archive 抛错、render 抛错、重复 EventSource、终态后订阅等场景都有明确响应。
  - Negative Tests (expected to FAIL):
    - 同一 queued task 被两个 worker/stream 同时 claim 时测试失败。
    - failed/cancelled task 被后续代码覆盖为 completed 或 archive ready 时测试失败。

- AC-6: 研究图和工具证据账本的已实现骨架被保留并继续闭环。
  - Positive Tests (expected to PASS):
    - `ResearchBrief`、`ResearchPlan`、`ResearchUnit`、`EvidenceItem`、`CompressedFinding`、`VerifiedClaim` 可序列化，bootstrap events 仍在 legacy executor 前发出。
    - search/scrape/local retrieval 工具结果能自动形成 evidence artifact/event，并在 backend/artifact/archive/UI 中可追踪。
  - Negative Tests (expected to FAIL):
    - 模型生成 report section 引用不存在的 evidence id 时必须产生 warning、拒绝正式入库，或在测试中被显式标记为 unresolved risk。
    - 工具结果含 secret 或内部 header 时不允许进入 evidence payload。

- AC-7: 数据持久化、迁移和部署配置支持真实部署。
  - Positive Tests (expected to PASS):
    - runner/web 默认使用 Postgres/Redis/MinIO 的生产配置，SQLite fallback 需要显式开关。
    - Postgres migration 可重复执行，表、索引、扩展、JSON/vector 字段和 owner indexes 与 repository SQL 合同一致。
    - Docker Compose 的必要环境变量、健康检查、服务依赖和端口边界被 contract tests 覆盖。
  - Negative Tests (expected to FAIL):
    - 缺少 required secret 时 compose 配置 silently fallback 到 insecure default。
    - runner Postgres SQL 写入旧 SQLite-only 表名或错误字段时测试失败。

- AC-8: 前后端真实交互和入口可用，不退化成文本链接或死按钮。
  - Positive Tests (expected to PASS):
    - 登录、创建研究、订阅事件、历史任务、artifact tabs、upload、upload search、PDF export、archive download、evidence preview、enterprise admin 控件有真实 DOM 入口和 API 调用路径。
    - 错误态、空态、终态和 archive not ready 状态都有稳定 UI 行为。
  - Negative Tests (expected to FAIL):
    - 关键控件只有说明文字、没有事件绑定、没有 API 路径或状态无法恢复时测试失败。
    - 浏览器可直接调用 runner internal route 时测试失败。

- AC-9: 测试从 happy path 扩展到真实风险路径。
  - Positive Tests (expected to PASS):
    - 每轮修复都新增或更新针对 P0/P1/P2 风险的单元、契约、路由、frontend contract、archive、runner 或 deploy 测试。
    - 相关测试命令在 round summary 中记录，并能在本地得到可解释结果。
  - Negative Tests (expected to FAIL):
    - 只修改文档或只跑无关 smoke test 的 round 不满足计划。
    - 为通过测试删除功能、放宽权限或跳过安全校验时测试失败。

- AC-10: 旧行为和开发 fallback 不被无意破坏。
  - Positive Tests (expected to PASS):
    - 旧 trace/archive/report/PDF/front-end contract/runner API tests 继续通过或有明确兼容说明。
    - SQLite/local fallback、legacy auth path、existing frontend app shell、old logs/trace viewer expectations 保持可用，除非有明确 P0 安全理由收紧。
  - Negative Tests (expected to FAIL):
    - 未经说明移除旧 endpoint、旧 archive member、旧 contract text 或旧 fallback 时测试失败。

## Path Boundaries

### Upper Bound (Maximum Acceptable Scope)

完成多个 RLCR round，逐步修复确认的 P0/P1/P2 风险：安全泄露、多用户串数据、archive/download 越权、任务状态损坏、runner lifecycle 缺口、证据持久化/引用闭环、部署配置缺口、前端真实交互缺口和关键测试缺失。可以做小规模模块提取，但只限于降低风险、改善合同测试或隔离已触及的安全敏感逻辑。

### Lower Bound (Minimum Acceptable Scope)

至少完成计划文件提交、启动新的 Humanize RLCR loop，并完成第一轮真实代码修复：选择一个当前代码中可确认的 P0/P1/P2 问题，添加或更新测试，运行相关验证，提交代码，并写对应 round summary。第一轮不得只修改文档。

## Allowed Choices

- Can use: 现有 FastAPI/aiohttp/Pydantic/Postgres/Redis/MinIO/SQLite fallback/vanilla JS/test 工具链；现有 runner/web/frontend contract patterns；小范围 helper extraction；白名单式 response shaping；原子状态更新；targeted negative tests；offline eval skeleton。
- Can use: 串行实现新的 graph/evidence/retrieval contracts，再逐步引入 bounded parallelism；保留 legacy executor 兼容层；先以 warnings/contract enforcement 补证据闭环。
- Cannot use: 无边界重写 agent/orchestrator/search/scrape/LLM 主循环；直接替换前端框架；删除功能来通过测试；暴露 runner internal route 给浏览器；把 service token/API key/object key 写入前端、日志、归档、测试快照；手动编辑 `.humanize/rlcr/*/state.md`；手动跳过 hook；用 ad hoc review 代替 Humanize Stop hook。

## Explicit Non-Goals

- 不在本计划中一次性完成完整 LangGraph/LangChain 迁移。
- 不把所有 `limira.py` 路由一次性拆完。
- 不把所有上传资料检索立即改成生产级 vector/rerank 系统；先补合同和 fallback。
- 不依赖 live external API、真实搜索或真实 LLM 才能验证核心安全/状态/部署风险。
- 不把 raw hidden chain-of-thought 或内部模型 trace 当成产品功能暴露。
- 不把普通用户默认提升为管理员，也不增加隐式全局数据可见性。
- 不改变已有公开路由、archive 格式或 fallback 行为，除非修复明确安全问题且配套测试。

## Feasibility Hints and Suggestions

### Conceptual Approach

每个 RLCR round 使用同一决策顺序：

1. 读当前 round prompt 和最近 review result。
2. 审查代码和测试，列出可确认风险。
3. 按 P0 > P1 > P2 > P3 选择一个可在本轮闭环的问题。
4. 先写或更新能暴露风险的测试。
5. 实现最小可靠修复。
6. 跑相关测试和静态/contract 检查。
7. 提交代码。
8. 写 round summary，记录命令、文件、剩余风险和下一轮建议。

### Relevant References

- `apps/limira-web/backend/limira_backend/routers/limira.py` - FastAPI 产品壳、auth、tasks、SSE proxy、archive/PDF/upload/admin 路由，当前最大工程债。
- `apps/limira-runner/runner_api.py` - runner task creation, SSE-triggered execution, cancellation, archive finalization, owner/admin checks。
- `apps/limira-runner/task_store.py` - SQLite/Postgres task store, claim/cancel/update semantics。
- `apps/limira-runner/archive_writer.py` - archive member whitelist, secret scrubbing, task id/path validation。
- `apps/limira-agent/src/core/research_graph.py` - research graph contracts and bootstrap prompt/events。
- `libs/limira-tools/src/limira_tools/limira_evidence.py` - automatic tool-derived evidence ledger。
- `apps/limira-standalone/server.mjs` - browser proxy boundary.
- `apps/limira-standalone/public/app.js` and `index.html` - real browser workbench and global state.
- `deploy/limira/postgres/migrations/001_limira_osint_schema.sql` - durable schema and vector/evidence/upload/report tables。
- `docker-compose.limira.yml` - production deployment contract。
- `apps/limira-runner/tests/` - existing runner/web/frontend/deploy/archive/research graph contract tests。

## Dependencies and Sequence

### Milestones

1. Planning and loop bootstrap:
   - Generate draft and plan from current `dev` state.
   - Commit the plan files.
   - Run `setup-rlcr-loop.sh` with the requested flags.
   - Read the new `round-0-prompt.md`.

2. P0 safety pass:
   - Verify browser boundary, trusted headers, forbidden browser fields, archive scrubbing, download auth, path traversal rejection, and owner/admin isolation.
   - Patch confirmed leaks or missing negative tests first.

3. P1 lifecycle and persistence pass:
   - Review runner state transitions, SSE-triggered execution semantics, cancellation, duplicate subscribers, terminal task behavior, report/archive failure behavior, and Postgres task store contract.
   - Patch confirmed stuck-state or misreporting bugs before broader graph work.

4. P1 evidence and artifact closure:
   - Ensure tool evidence events survive filtering, backend persistence, archive generation, UI artifact rendering, and evidence_refs validation.
   - Add tests for missing evidence refs and secret-bearing tool payloads where behavior is currently weak.

5. P1 frontend/API closure:
   - Verify key controls are clickable and wired to real API calls.
   - Patch dead controls, inconsistent empty/error states, unsafe download URLs, or broken SSE restoration.

6. P1/P2 deploy and migration closure:
   - Verify compose, Dockerfiles, health checks, env vars, migration idempotency, runner/web DB URLs, MinIO bucket init, and explicit fallback rules.
   - Patch contract drift or missing tests.

7. P2 retrieval and eval closure:
   - Strengthen upload search fallback, vector-disabled/provider-failed behavior, owner isolation, and offline evidence-quality eval skeleton.
   - Keep work bounded and deterministic.

Dependencies: P0 fixes override all later work. P1 lifecycle and persistence should precede major research graph expansion. Evidence persistence should precede citation enforcement. Frontend changes should not assume backend behavior not yet implemented. Deploy changes should preserve local fallback tests.

## Test and Verification Requirements

- Always run `git status --short` before committing each round.
- For Python changes, run the smallest relevant pytest subset first, then broader affected suites when feasible, for example:
  - `cd apps/limira-runner && uv run pytest tests/test_runner_api.py`
  - `cd apps/limira-runner && uv run pytest tests/test_task_store_and_auth.py`
  - `cd apps/limira-runner && uv run pytest tests/test_archive_writer.py`
  - `cd apps/limira-runner && uv run pytest tests/test_limira_web_routes.py -k '<focused expression>'`
  - `cd apps/limira-runner && uv run pytest tests/test_research_graph.py`
- For frontend/proxy changes, run relevant contract checks and syntax checks, for example:
  - `node --check apps/limira-standalone/public/app.js`
  - `node --check apps/limira-standalone/server.mjs`
  - `cd apps/limira-runner && uv run pytest tests/test_limira_frontend_contract.py`
- For deployment changes, run deploy contract tests and config validation where feasible:
  - `cd apps/limira-runner && uv run pytest tests/test_limira_deploy_contract.py`
  - `docker compose -f docker-compose.limira.yml config` when Docker is available.
- For archive/security changes, tests must include negative cases for secret leakage, unsafe member names, unauthorized users, and archive not-ready states.
- Round summaries must record exact commands, pass/fail result, and any test that could not be run with the reason.
- A round cannot be considered complete if it has uncommitted changes, missing summary, or only documentation/smoke work.

## Risk Priority Rules

- P0: 会导致安全泄露、权限绕过、数据串用户、无法启动、无法部署、任务状态损坏的问题，必须修。Examples: service token/API key leak, browser access to runner internal API, cross-user task/archive/upload access, path traversal, unsafe ZIP, terminal state corruption, missing required deployment secret.
- P1: 会导致核心功能无法闭环、真实用户无法使用、下载/归档/数据库/前端入口不可用的问题，必须修。Examples: SSE starts execution in a way that breaks reconnect semantics, archive/PDF route unreachable, Postgres repository drift, evidence not persisted into artifacts/archives, dead frontend controls.
- P2: 会导致可靠性差、错误处理差、测试缺失、部署体验差的问题，尽量修。Examples: weak error messaging, missing negative tests, poor fallback behavior, unverified compose contract, missing offline eval skeleton.
- P3: 纯风格、命名、局部重构，不作为主要目标，除非顺手且低风险。

When multiple issues are found, choose the highest-priority confirmed issue that can be fixed and tested in the current round. Do not speculate large rewrites when a smaller confirmed risk can be closed.

## Task Breakdown

Each task has exactly one routing tag.

| Task ID | Description | Target AC | Tag (`coding`/`analyze`) | Depends On |
|---------|-------------|-----------|----------------------------|------------|
| task1 | Generate and commit draft/plan files from current `dev` state | AC-1 | coding | - |
| task2 | Bootstrap RLCR loop and read the generated round prompt | AC-1 | coding | task1 |
| task3 | Audit P0 browser/secret/object-key/archive/owner isolation risks | AC-2, AC-3, AC-4 | analyze | task2 |
| task4 | Fix the highest-priority confirmed P0 issue and add negative tests | AC-2, AC-3, AC-4, AC-9 | coding | task3 |
| task5 | Audit P1 runner state/SSE/cancellation/archive terminal-state risks | AC-5 | analyze | task4 |
| task6 | Fix the highest-priority confirmed P1 lifecycle issue and add tests | AC-5, AC-9, AC-10 | coding | task5 |
| task7 | Audit evidence/artifact persistence and citation-reference closure | AC-6 | analyze | task6 |
| task8 | Fix the highest-priority evidence closure gap and add tests | AC-6, AC-9 | coding | task7 |
| task9 | Audit frontend real interaction, deploy, upload retrieval, and eval gaps | AC-7, AC-8, AC-9 | analyze | task8 |
| task10 | Fix the highest-priority remaining P1/P2 gap and add tests | AC-7, AC-8, AC-9, AC-10 | coding | task9 |

## Claude-Codex Deliberation

### Agreements

- Preserve the existing product shell and security boundaries.
- Avoid broad rewrites; focus each round on confirmed P0/P1/P2 risks.
- Tests must cover negative and multi-user/security paths.
- The existing research graph and evidence ledger are useful partial implementations, not reasons to skip risk review.

### Resolved Disagreements

- None. This plan was generated directly from the user-provided product plan and current repository inspection.

### Convergence Status

- Final Status: `converged`

## Pending User Decisions

- None. The user explicitly requested the RLCR loop, setup command, priorities, constraints, and summary requirements.

## Implementation Notes

### Code Style Requirements

- Implementation code and comments must not contain plan-specific labels such as `AC-`, `Milestone`, `Task ID`, or `Phase` unless they are test fixture text or documentation.
- Use existing repository naming and helper patterns before adding new abstractions.
- Prefer structured parsers and typed models over ad hoc string handling for security-sensitive data.
- Keep comments concise and only where they clarify non-obvious safety behavior.
- Do not modify `.humanize/rlcr/*/state.md`.
