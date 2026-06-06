Read and execute below with ultrathink

## Goal Tracker Setup (REQUIRED FIRST STEP)

Before starting implementation, you MUST initialize the Goal Tracker:

1. Read @/home/limira/MiroLimira/MiroThinker/.humanize/rlcr/2026-06-06_14-59-24/goal-tracker.md
2. If the "Ultimate Goal" section says "[To be extracted...]", extract a clear goal statement from the plan
3. If the "Acceptance Criteria" section says "[To be defined...]", define 3-7 specific, testable criteria
4. Populate the "Active Tasks" table with MAINLINE tasks from the plan, mapping each to an AC and filling Tag/Owner
5. Record any already-known side issues in either "Blocking Side Issues" or "Queued Side Issues"
6. Write the updated goal-tracker.md

## Round Contract Setup (REQUIRED BEFORE CODING)

Before starting implementation, create @/home/limira/MiroLimira/MiroThinker/.humanize/rlcr/2026-06-06_14-59-24/round-0-contract.md with:

1. **One mainline objective** for this round
2. **Target ACs** (1-2 ACs only)
3. **Blocking side issues in scope** for this round
4. **Queued side issues out of scope** for this round
5. **Round success criteria**

Use this contract to keep the round focused. Do NOT let non-blocking bugs or cleanup work replace the mainline objective.

**IMPORTANT**: The IMMUTABLE SECTION can only be modified in Round 0. After this round, it becomes read-only.

---

## Implementation Plan

For all tasks that need to be completed, please use the Task system (TaskCreate, TaskUpdate, TaskList).

Every task MUST start with exactly one lane tag:
- `[mainline]` for plan-derived work that directly advances the round objective
- `[blocking]` for issues that prevent the mainline objective from succeeding safely
- `[queued]` for non-blocking bugs, cleanup, or follow-up work

Rules:
- `[mainline]` tasks are the primary success condition for the round
- `[blocking]` tasks may be resolved in the round only if they truly block mainline progress
- `[queued]` tasks must NOT become the round objective and do NOT need to be cleared before moving on
- If a new issue is not blocking the current objective, tag it `[queued]` and keep moving on the mainline

## Task Tag Routing (MUST FOLLOW)

Each task must have one routing tag from the plan: `coding` or `analyze`.

- Tag `coding`: Claude executes the task directly.
- Tag `analyze`: Claude must execute via `/humanize:ask-codex`, then integrate Codex output.
- Keep Goal Tracker "Active Tasks" columns **Tag** and **Owner** aligned with execution (`coding -> claude`, `analyze -> codex`).
- If a task has no explicit tag, default to `coding` (Claude executes directly).

# Open WebUI MiroThinker Archive Plan

## Goal Description

把当前 MiroThinker Gradio Deep Research demo 演进为可被 Open WebUI 调用的 Deep Research 后端，同时为每次研究任务生成可下载、可诊断、经过敏感信息清理的本地归档。

本计划只定义执行路线，不启动实现。第一阶段必须优先交付 Runner API 和 Archive Writer，让能力可以用 curl 独立验收；Open WebUI 前端接入放在后续阶段，不能一上来改前端。Gradio demo 必须保留为开发 fallback，MiroThinker core agent、工具选择、搜索逻辑、模型调用逻辑和现有 `TaskLog` 旧日志行为不重写。

当前仓库锚点已读：

- `apps/gradio-demo/main.py`
  - `stream_events_optimized(task_id, query, _, disconnect_check)` 返回结构化 dict event，不是 SSE 字符串；内部调用 `execute_task_pipeline()`，并使用 `log_dir=os.getenv("LOG_DIR", "logs/api-server")`。
  - `_update_state_with_event(state, message)` 消费现有 `{"event": ..., "data": ...}` 事件并忽略 heartbeat。
  - `_render_markdown(state)` 从当前 UI state 生成最终 Markdown 报告。
  - `gradio_run()` 是当前 Gradio 正式入口，使用同一条 stream 路径和 cancel flag。
- `apps/miroflow-agent/src/logging/task_logger.py`
  - `TaskLog.save()` 写出 `task_{task_id}_{timestamp}.json`，这是旧 trace viewer 使用的 JSON 日志来源。
- `apps/miroflow-agent/src/config/settings.py`
  - `get_env_info()` 记录模型/provider/base URL 和 key 是否存在，不记录 key 值；新 `metadata.json` 仍要采用更严格的 host-only 摘要。
- `apps/miroflow-agent/src/core/stream_handler.py`
  - 当前事件类型包括 `start_of_workflow`、`end_of_workflow`、`start_of_agent`、`end_of_agent`、`message`、`tool_call` 等；事件本身没有统一 timestamp，Runner API 需要补充接收时间。
- `apps/gradio-demo/logs/api-server/task_*.json`
  - 当前已有样例日志，证明旧日志路径相对 Gradio 运行目录生效。新归档不能替代或删除这些文件。

Open WebUI 依据已核对：

- Open WebUI Extensibility: https://docs.openwebui.com/features/extensibility/
- Open WebUI Functions: https://docs.openwebui.com/features/extensibility/plugin/functions/
- Open WebUI API Endpoints: https://docs.openwebui.com/reference/api-endpoints/
- Open WebUI API Keys: https://docs.openwebui.com/features/authentication-access/api-keys/
- Open WebUI Pipelines legacy note: https://docs.openwebui.com/features/extensibility/pipelines/

## Acceptance Criteria

- AC-1: 现有 Gradio fallback 保持可运行。
  - Positive Tests (expected to PASS):
    - 直接运行或导入 `apps/gradio-demo/main.py` 后，`gradio_run()`、`stop_current()`、`build_demo()` 仍存在，函数签名保持兼容。
    - 通过 Gradio 路径启动一次研究时，仍调用 `stream_events_optimized()`，旧 `logs/api-server/task_*.json` 仍生成。
  - Negative Tests (expected to FAIL):
    - 删除 `gradio_run()`、改变其返回 tuple 结构、或让 Gradio 入口依赖 Open WebUI 登录态。
    - 修改 MiroThinker core agent 的工具选择、LLM 调用、orchestrator 主循环来适配 Runner API。

- AC-2: Runner API 能独立启动研究任务。
  - Positive Tests (expected to PASS):
    - `POST /mirothinker/research` 接收 `{"query": "...", "client_options": {"stream": true}}`，返回 `task_id`、`status`、`stream_url`、`task_url`。
    - 任务记录保存 `task_id`、可信 `user_id`、`query`、`status`、`archive_status`、创建时间。
    - Phase 1 不依赖 Open WebUI 前端，可以用 curl 或 API test client 完成启动。
  - Negative Tests (expected to FAIL):
    - 请求体中传入 `user_id` 可以覆盖真实用户身份。
    - 空 query、超长 query、非 JSON 请求体被静默接受。

- AC-3: Runner API 以 SSE 暴露研究过程事件流。
  - Positive Tests (expected to PASS):
    - `GET /mirothinker/tasks/{task_id}/events` 将 `stream_events_optimized()` 的 dict event 转为标准 SSE。
    - SSE data 中包含 `task_id`、`type`、UTC ISO timestamp 和 scrub 后 payload。
    - 每个非 heartbeat event 同时写入 trace buffer，并传给 `_update_state_with_event()` 累计最终 state。
    - disconnect 或 cancel 能触发底层 `disconnect_check`，避免后台线程无限运行。
  - Negative Tests (expected to FAIL):
    - heartbeat 被写入 `trace.json`。
    - SSE 消费其他用户 task_id 时返回事件内容。

- AC-4: Archive Writer 是独立组件，不和前端展示逻辑耦死。
  - Positive Tests (expected to PASS):
    - 新增独立模块，例如 `apps/gradio-demo/archive_writer.py`，提供 `ResearchArchiveWriter` 或等价接口。
    - 通过 fake events、fake state、fake model summary 即可单元测试归档生成，无需真实 LLM、Serper、Jina、E2B。
    - 归档目录为 `apps/gradio-demo/archives/<YYYYMMDD-HHMMSS>_<task_id>/`，由后端生成。
  - Negative Tests (expected to FAIL):
    - 前端或请求参数可指定任意 archive path。
    - `task_id` 中的路径穿越片段导致文件写到 archive root 外部。

- AC-5: `trace.json` 合同明确且可验证。
  - Positive Tests (expected to PASS):
    - 文件是合法 JSON，顶层至少包含 `version: 1`、`task_id`、`events`。
    - `events` 只包含非 heartbeat event，每条包含 `type`、`timestamp`、`payload`。
    - `payload` 保留当前原始 event 结构，至少能还原 `{"event": ..., "data": ...}`。
    - 写入前递归执行 secret scrub。
  - Negative Tests (expected to FAIL):
    - `trace.json` 含 `heartbeat`。
    - `trace.json` 中出现 `Authorization`、`Bearer <token>`、`Cookie`、`Set-Cookie`、`API_KEY` 明文或实际 key 前缀。

- AC-6: `metadata.json` 合同明确且不泄漏 secret。
  - Positive Tests (expected to PASS):
    - 文件是合法 JSON，顶层至少包含 `version`、`task_id`、`query`、`user_id`、`start_time`、`end_time`、`status`、`archive_status`、`archive_filename`、`model`、`error`。
    - `model` 只保存 `provider`、`model`、`base_url_host`，例如 `api.deepseek.com`。
    - `status` 只允许 `queued`、`running`、`completed`、`failed`、`cancelled` 的最终合法值。
    - `archive_status` 只允许 `pending`、`ready`、`failed`。
  - Negative Tests (expected to FAIL):
    - 保存 API key、完整 Authorization header、Bearer token、cookie。
    - 保存带 path、query string 或 secret 的完整 base URL。

- AC-7: `report.md` 使用当前报告渲染逻辑。
  - Positive Tests (expected to PASS):
    - 成功任务的 `report.md` 来自 `_render_markdown(state)`，不另写第二套报告生成器。
    - 失败或取消任务仍生成最小 `report.md`，包含任务状态、错误摘要和已有进度。
  - Negative Tests (expected to FAIL):
    - 归档中没有 `report.md`。
    - 失败任务只写空文件或抛错导致整次归档中断。

- AC-8: `report.html` 安全可打开。
  - Positive Tests (expected to PASS):
    - 文件是完整 HTML，包含 `<!doctype html>`、`<meta charset="utf-8">` 和 `<main>`。
    - 第一版可以把 Markdown escape 后放入 `<pre>`，优先保证安全和可读。
    - 如引入 Markdown 渲染库，输出必须经过 sanitizer。
  - Negative Tests (expected to FAIL):
    - Markdown 中的 `<script>`、事件 handler 或危险 URL 未转义/未清理直接进入 HTML。
    - `report.html` 依赖远程资源才能显示基本内容。

- AC-9: `archive.zip` 内容固定且不包含敏感额外文件。
  - Positive Tests (expected to PASS):
    - zip 内只包含相对路径文件：`trace.json`、`report.md`、`metadata.json`、`report.html`。
    - `unzip -l archive.zip` 不显示绝对路径、`..`、`.env`、`logs/`、缓存目录或数据库文件。
    - 创建 zip 失败不会把研究任务从 completed 改成 failed；任务接口显示 `archive_status: "failed"` 并记录 warning。
  - Negative Tests (expected to FAIL):
    - zip 打包整个 archive root、旧日志目录、`.env`、`__pycache__` 或任意额外敏感文件。
    - zip 创建失败导致已经完成的研究任务被标记为研究失败。

- AC-10: secret scrubber 覆盖 dict、list 和 string。
  - Positive Tests (expected to PASS):
    - key 名命中 `api_key`、`authorization`、`cookie`、`token`、`secret`、`SERPER_API_KEY`、`JINA_API_KEY`、`E2B_API_KEY`、`OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY` 时，value 替换为 `[REDACTED]`。
    - string value 中的 `Bearer <token>`、cookie 片段和明显长随机 secret 被保守脱敏。
    - scrubber 对嵌套 list/dict 生效且不破坏合法 JSON 序列化。
  - Negative Tests (expected to FAIL):
    - 只清理顶层字段。
    - 清理后仍可在归档目录或 zip 解压目录中 `rg` 到实际 secret。

- AC-11: 任务状态和下载权限可预测。
  - Positive Tests (expected to PASS):
    - `GET /mirothinker/tasks/{task_id}` 返回 query、user_id、status、archive_status、download_url、created_at、completed_at、error。
    - `GET /mirothinker/tasks/{task_id}/archive.zip` 在 `archive_status != "ready"` 时返回 `409 archive_not_ready`。
    - 普通用户只能下载自己的归档；不存在或不属于当前用户的 task_id 返回 `404`。
    - 管理员路径必须有显式管理员权限判断。
  - Negative Tests (expected to FAIL):
    - 普通用户能通过猜 task_id 下载他人 zip。
    - 未完成任务返回半成品 zip。

- AC-12: 任务/归档记录持久化足够支撑多用户隔离。
  - Positive Tests (expected to PASS):
    - 至少有等价于 `mirothinker_research_tasks` 的持久化模型，字段包括 `task_id`、`user_id`、`query`、`status`、`archive_status`、`archive_dir`、`archive_zip_path`、`created_at`、`started_at`、`completed_at`、`error`、`model_summary`。
    - `task_id` 唯一，`user_id + created_at` 可查询。
    - 文件系统路径只由后端生成和读取。
  - Negative Tests (expected to FAIL):
    - 仅用内存状态作为生产任务归属来源。
    - 下载接口不查任务归属表/模型。

- AC-13: Phase 1 可以在没有 Open WebUI 前端的情况下验收。
  - Positive Tests (expected to PASS):
    - curl 或 API test client 可以启动任务、消费 SSE、查询状态、下载 `archive.zip`。
    - 使用 fake stream 的自动化测试可以稳定验证 API 和归档合同。
  - Negative Tests (expected to FAIL):
    - Phase 1 必须先完成 Open WebUI 页面才能验证。
    - Runner API 需要浏览器 session 才能跑单元测试。

- AC-14: Open WebUI 接入只在 Runner API 合同稳定后开始。
  - Positive Tests (expected to PASS):
    - Phase 2 新增独立 Deep Research 页面或消息动作，使用 Open WebUI 当前用户身份调用 Runner API。
    - 页面能显示进度、最终报告和下载按钮。
    - 下载按钮只在 `archive_status == "ready"` 时启用；失败/取消但已有诊断归档时可显示 `Download Diagnostic ZIP`。
  - Negative Tests (expected to FAIL):
    - 前端直接提交可伪造 user_id。
    - Open WebUI 前端改动先于 Phase 1 API/Archive Writer 验收进入主线。

- AC-15: 失败和取消也有诊断归档。
  - Positive Tests (expected to PASS):
    - `failed` 和 `cancelled` 任务写出 `metadata.json`、`trace.json`、`report.md`、`report.html`，并尽量生成 `archive.zip`。
    - error 摘要经过 scrub 后进入 `metadata.json` 和最小 `report.md`。
  - Negative Tests (expected to FAIL):
    - 失败/取消任务没有任何归档。
    - error 中的 Authorization、Bearer、cookie 或 API key 明文进入归档。

- AC-16: 不承诺保存 raw hidden chain-of-thought。
  - Positive Tests (expected to PASS):
    - 归档内容限定为用户可见 research progress、工具状态、验证过程、搜索/抓取结果和最终报告。
    - 文档和 UI 文案不把 hidden chain-of-thought 作为产品能力。
  - Negative Tests (expected to FAIL):
    - 为满足归档而尝试从模型或 agent 内部提取隐藏思维链。

## Path Boundaries

### Upper Bound (Maximum Scope)

本计划最大可接受范围是完成三阶段交付：

1. Runner API + Archive Writer + 本地任务持久化 + curl 可验收。
2. Open WebUI 独立 Deep Research 页面或消息动作接入，复用 Open WebUI 用户、登录、会话和权限能力。
3. 多用户隔离、管理员查看/下载、失败/取消归档、归档失败 warning、secret scrub 回归测试。

即使走到最大范围，也不能重写 MiroThinker core agent，不能替换 `TaskLog` 旧日志体系，不能把 Gradio 改造成正式多用户系统。

### Lower Bound (Minimum Scope)

最小可交付范围是 Phase 1：

- 在现有仓库中新增 Runner API。
- 包装 `stream_events_optimized()` 为任务启动、SSE、状态查询和 zip 下载接口。
- 新增 Archive Writer，生成 `trace.json`、`report.md`、`metadata.json`、`report.html`、`archive.zip`。
- 保留 `gradio_run()` 可运行。
- 保留旧 `logs/api-server/task_*.json` 行为。
- 用 fake stream 自动化测试和至少一条 curl smoke 路径证明不依赖 Open WebUI 前端。

### In Scope Paths

- `apps/gradio-demo/main.py`
  - 允许新增小型兼容导出或提取纯 helper，但不能破坏现有 Gradio 行为。
- `apps/gradio-demo/archive_writer.py`
  - 建议新增 Archive Writer 和 secret scrubber。
- `apps/gradio-demo/runner_api.py` 或 `apps/gradio-demo/api.py`
  - 建议新增 Runner API 第一版，复用 Gradio demo 运行环境。
- `apps/gradio-demo/task_store.py` 或等价模块
  - 保存任务和归档元数据。
- `apps/gradio-demo/tests/`
  - 新增 archive writer、scrubber、runner API、权限、zip 合同测试。
- `apps/gradio-demo/pyproject.toml`
  - 仅在直接使用 FastAPI、sse-starlette、markdown/sanitizer 等库时添加显式依赖。
- `docs/`
  - 记录 API 合同、curl 验收步骤和部署说明。

### Out of Scope Paths

- `apps/miroflow-agent/src/core/orchestrator.py`
- `apps/miroflow-agent/src/core/pipeline.py`
- `apps/miroflow-agent/src/core/tool_executor.py`
- `apps/miroflow-agent/src/llm/`
- `libs/miroflow-tools/`

这些路径第一阶段只读。除非后续发现无法通过适配层完成且用户单独批准，否则不得修改。

### Invariants

- `stream_events_optimized()` 是 Phase 1 的后端研究流入口。
- `_update_state_with_event()` 是 state 累计入口。
- `_render_markdown()` 是 `report.md` 的成功任务报告来源。
- `TaskLog.save()` 和 `logs/api-server/task_*.json` 行为必须保留。
- Open WebUI 是正式用户系统；Runner API 不实现独立注册、登录和用户数据库。

## Allowed Choices

- Runner API 位置：
  - Recommended: 第一版放在 `apps/gradio-demo`，复用现有依赖、Hydra 配置和 demo 环境。
  - Allowed: 新增 `apps/mirothinker-runner`，但必须证明不会增加迁移风险。

- API framework：
  - Allowed: FastAPI/Starlette + SSE response，或 aiohttp。
  - Required if chosen: 在 `pyproject.toml` 中显式声明直接依赖，不依赖偶然的 transitive dependency。

- Streaming：
  - Recommended: SSE。
  - Not Phase 1: WebSocket，除非 SSE 无法满足 Open WebUI 接入。

- Authentication bridge：
  - Phase 1 allowed: dev-only trusted header + shared service secret，用于模拟 Open WebUI 后端代理注入用户身份。
  - Phase 2 required: 从 Open WebUI 后端可信上下文、JWT/API key 校验结果或服务端代理获取当前用户。
  - Never allowed: 信任浏览器或请求体直接提交的 `user_id`。

- Persistence：
  - Phase 1 allowed: SQLite、JSONL、轻量文件型 task store，前提是下载接口必须通过它确认归属。
  - Phase 2 preferred: 与 Open WebUI 用户/权限上下文集成，避免新增独立用户系统。

- `report.html`：
  - Recommended for Phase 1: escaped Markdown in `<pre>`。
  - Allowed later: Markdown renderer + sanitizer。
  - Not allowed: 未清理 HTML 直出。

- Zip creation：
  - Recommended: Python stdlib `zipfile`，显式写入四个文件名。
  - Not allowed: 递归压缩整个目录。

- Testing:
  - Required: fake stream tests 不依赖外部 LLM/tool API。
  - Manual/integration: 真实 Deep Research 端到端测试可依赖配置好的 `BASE_URL`、`API_KEY`、Serper、Jina、E2B。

## Dependencies and Sequence

### Phase 0: Contract Locking (Plan Review)

1. 用户确认本计划。
2. 确认 Phase 1 Runner API 放在 `apps/gradio-demo` 还是新 app。
3. 确认 Phase 1 dev auth 模式：建议 shared service secret + trusted user header，仅开发/内网使用。
4. 确认 task store 第一版形态：建议 SQLite 或 JSONL，生产下载权限不能只靠内存。

Exit Gate:

- 用户确认后才进入实现。
- 未确认前不改 Runner、Archive Writer、API、Gradio 或 Open WebUI 前端代码。

### Phase 1: Runner API + Archive Writer

#### Iteration 1: Archive Writer and Secret Scrubber

- 新增 Archive Writer 模块。
- 新增递归 secret scrubber。
- 用 fake events 验证 `trace.json`、`metadata.json`、`report.md`、`report.html`、`archive.zip` 合同。
- 先做 unit tests，再接 Runner API。

Exit Gate:

- Archive Writer 单元测试覆盖成功、失败、取消、zip 失败、path traversal、secret scrub。

#### Iteration 2: Task Store and Auth Adapter

- 新增任务记录模型或文件 store。
- 新增 dev-only trusted identity adapter。
- 明确生产模式必须由 Open WebUI 后端代理/JWT/API key 校验提供可信用户身份。
- 禁止请求体 `user_id`。

Exit Gate:

- 任务归属、状态转换、下载权限判断可单元测试。

#### Iteration 3: Runner API Start/Status/SSE

- 新增 `POST /mirothinker/research`。
- 新增 `GET /mirothinker/tasks/{task_id}`。
- 新增 `GET /mirothinker/tasks/{task_id}/events`。
- SSE 包装 `stream_events_optimized()`，每个非 heartbeat event 同步进入 trace buffer 和 `_update_state_with_event()`。
- Runner API 补充接收 timestamp，底层原始 event 保存在 payload。

Exit Gate:

- 使用 fake stream 的 API tests 能启动任务、消费事件、完成 state 渲染。

#### Iteration 4: Archive Finalization and Download

- 任务完成、失败、取消时调用 Archive Writer。
- 新增 `GET /mirothinker/tasks/{task_id}/archive.zip`。
- zip ready 前返回 `409 archive_not_ready`。
- 他人 task 或不存在 task 返回 `404`。
- archive 写入失败只影响 `archive_status`，不改写研究任务最终状态。

Exit Gate:

- API tests 验证 completed/failed/cancelled/archive_failed 四类状态。

#### Iteration 5: Gradio Fallback Regression and Old Trace Check

- 验证 `gradio_run()` 仍可运行。
- 验证旧 `logs/api-server/task_*.json` 仍由 `TaskLog.save()` 生成。
- 确认新归档写入 `apps/gradio-demo/archives/`，不污染 `logs/api-server/`。

Exit Gate:

- Phase 1 curl smoke 和测试报告通过后，才能进入 Open WebUI 前端接入。

### Phase 2: Open WebUI Integration

1. 在 Open WebUI 中新增独立 Deep Research 页面，默认优先于聊天消息动作。
2. Open WebUI 后端以当前登录用户身份调用 Runner API。
3. 页面展示 Research Progress、最终报告和下载按钮。
4. 下载按钮根据 `status` 和 `archive_status` 启用/禁用。
5. 保留 Functions/Tools/Actions 作为后续更深集成方向，不默认使用 Pipelines。

Exit Gate:

- 登录用户完成一次 Deep Research。
- 普通用户只能看到和下载自己的归档。
- Open WebUI 前端不传入可伪造 `user_id`。

### Phase 3: Multi-User Hardening and Regression

1. 完成普通用户隔离测试。
2. 增加管理员查看/下载全部归档的显式权限路径。
3. 增加 secrets 泄漏扫描。
4. 增加失败/取消/归档失败 warning 的回归测试。
5. 评估是否让新 `trace.json` 被旧 trace viewer 或新 viewer 消费；此项不得破坏旧 `TaskLog` JSON。

Exit Gate:

- 用户 A 无法下载用户 B 归档。
- 管理员权限路径与普通用户路径测试分离。
- 归档和 zip 解压目录中无 secret 命中。

## Testing Plan

### Unit Tests

- Archive Writer:
  - fake successful state 生成完整归档目录。
  - fake failed/cancelled state 生成诊断 `report.md` 和 zip。
  - zip failure 返回/记录 `archive_status: "failed"`。
  - task_id/path traversal 被拒绝或安全规范化。
- Secret Scrubber:
  - 嵌套 dict/list/string 全部脱敏。
  - `Authorization`、`Bearer`、`Cookie`、`Set-Cookie`、`API_KEY`、`SERPER_API_KEY`、`JINA_API_KEY`、`E2B_API_KEY`、`OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY` 覆盖。
- Metadata:
  - `base_url_host` 从 URL 提取 host。
  - 完整 URL、query string、secret 不进入 metadata。
- Report HTML:
  - `<script>` 被 escape 或 sanitizer 移除。
  - HTML 可被解析，基本内容不依赖远程资源。

Suggested command after implementation:

```bash
cd apps/gradio-demo
uv run pytest tests/test_archive_writer.py tests/test_secret_scrubber.py
```

### API Tests

- 使用 monkeypatch/fake stream 替代真实 `stream_events_optimized()`。
- 验证 `POST /mirothinker/research`、`GET /events`、`GET /task`、`GET /archive.zip`。
- 验证 heartbeat 只出现在 SSE 保活，不进入 `trace.json`。
- 验证 owner/admin/non-owner 权限分支。
- 验证 archive not ready 返回 `409`，不存在或无权返回 `404`。

Suggested command after implementation:

```bash
cd apps/gradio-demo
uv run pytest tests/test_runner_api.py
```

### Contract Checks

对每个生成的归档目录执行：

```bash
test -f trace.json
test -f report.md
test -f metadata.json
test -f report.html
test -f archive.zip
python -m json.tool trace.json >/dev/null
python -m json.tool metadata.json >/dev/null
unzip -l archive.zip
```

Zip 内容必须只列出：

```text
trace.json
report.md
metadata.json
report.html
```

### Secret Leakage Checks

对归档目录和 zip 解压目录执行大小写不敏感扫描：

```bash
rg -i "API_KEY|Authorization|Bearer|Cookie|Set-Cookie|SERPER_API_KEY|JINA_API_KEY|E2B_API_KEY|OPENAI_API_KEY|DEEPSEEK_API_KEY|TENCENTCLOUD_SECRET_ID|TENCENTCLOUD_SECRET_KEY|sk-[A-Za-z0-9]|eyJ[A-Za-z0-9_-]+" apps/gradio-demo/archives/<archive-dir>
```

预期：只允许字段名在测试 fixture 或已脱敏上下文中出现；任何实际 secret value 命中都失败。

### Manual Phase 1 Smoke

在实现后，用真实或 fake runner 环境执行：

```bash
curl -X POST http://localhost:<port>/mirothinker/research \
  -H "Content-Type: application/json" \
  -H "X-MiroThinker-Service-Token: <dev-shared-secret>" \
  -H "X-OpenWebUI-User-Id: user-a" \
  -d '{"query":"测试研究问题","client_options":{"stream":true}}'

curl -N http://localhost:<port>/mirothinker/tasks/<task_id>/events \
  -H "X-MiroThinker-Service-Token: <dev-shared-secret>" \
  -H "X-OpenWebUI-User-Id: user-a"

curl http://localhost:<port>/mirothinker/tasks/<task_id> \
  -H "X-MiroThinker-Service-Token: <dev-shared-secret>" \
  -H "X-OpenWebUI-User-Id: user-a"

curl -OJ http://localhost:<port>/mirothinker/tasks/<task_id>/archive.zip \
  -H "X-MiroThinker-Service-Token: <dev-shared-secret>" \
  -H "X-OpenWebUI-User-Id: user-a"
```

### Gradio and Old Log Regression

- 从 `apps/gradio-demo` 启动原 Gradio demo。
- 完成或 fake 完成一条研究任务。
- 确认旧日志仍生成在当前 `LOG_DIR` 语义对应的 `logs/api-server/task_*.json`。
- 确认新归档在 `apps/gradio-demo/archives/`，且不会删除或移动旧日志。

### Open WebUI Phase 2 Tests

- 用户 A 登录后启动 Deep Research，能看到事件流、最终报告、下载按钮。
- 用户 B 无法访问用户 A 的 task status 和 archive zip。
- 管理员路径单独测试，不和普通用户路径复用。
- 下载按钮：
  - `archive_status == "pending"` 禁用。
  - `archive_status == "ready"` 启用。
  - `status in ["failed", "cancelled"]` 且有诊断归档时显示诊断下载文案。

## Implementation Notes

- Runner API 的事件格式可以归一化为：

```json
{
  "task_id": "uuid",
  "type": "tool_call",
  "timestamp": "2026-06-06T12:00:00Z",
  "payload": {
    "event": "tool_call",
    "data": {}
  }
}
```

- `trace.json` 使用同一归一化事件列表，heartbeat 不写入。
- 如果直接 import `apps/gradio-demo/main.py` 的副作用影响测试，可以把 `_init_render_state()`、`_update_state_with_event()`、`_render_markdown()` 等纯渲染 helper 提取到新模块，再让 `main.py` 和 Runner API 同时复用；提取必须保持 Gradio 行为兼容。
- `metadata.json` 的 `model.base_url_host` 应从当前 cfg 或 env 的 base URL 解析 host，例如 `https://api.deepseek.com/chat/completions` 只保存 `api.deepseek.com`。
- 第一版 `report.html` 优先选择 escaped `<pre>`，不要为了漂亮 HTML 引入 XSS 风险。
- 不把 raw hidden chain-of-thought 写入任何归档、接口响应或 UI 文案。

---

## BitLesson Selection (REQUIRED FOR EACH TASK)

Before executing each task or sub-task, you MUST:

1. Read @/home/limira/MiroLimira/MiroThinker/.humanize/bitlesson.md
2. Run `bitlesson-selector` for each task/sub-task to select relevant lesson IDs
3. Follow the selected lesson IDs (or `NONE`) during implementation

Include a `## BitLesson Delta` section in your summary with:
- Action: none|add|update
- Lesson ID(s): NONE or comma-separated IDs
- Notes: what changed and why (required if action is add or update)

Reference: @/home/limira/MiroLimira/MiroThinker/.humanize/bitlesson.md

---

## Goal Tracker Rules

Throughout your work, you MUST maintain the Goal Tracker:

1. **Before starting a round**: Re-anchor on the original plan and current round contract
2. **Before starting a task**: Mark the relevant mainline task as "in_progress" in Active Tasks
   - Confirm Tag/Owner routing is correct before execution
3. **Active Tasks** are MAINLINE tasks only - side issues do not belong there
4. **Blocking Side Issues** are reserved for issues that truly stop mainline progress
5. **Queued Side Issues** are non-blocking and must not take over the round
6. **After completing a mainline task**: Move it to "Completed and Verified" with evidence (but mark as "pending verification")
7. **If you discover the plan has errors**:
   - Do NOT silently change direction
   - Add entry to "Plan Evolution Log" with justification
   - Explain how the change still serves the Ultimate Goal
8. **If you need to defer a task**:
   - Move it to "Explicitly Deferred" section
   - Provide strong justification
   - Explain impact on Acceptance Criteria
9. **If you discover new issues**:
   - Add to "Blocking Side Issues" only if mainline progress is blocked
   - Otherwise add to "Queued Side Issues" or keep them as `[queued]` tasks/backlog

---

Note: You MUST NOT try to exit `start-rlcr-loop` loop by lying or edit loop state file or try to execute `cancel-rlcr-loop`

After completing the work, please:
0. If you have access to the `code-simplifier` agent, use it to review and optimize the code you just wrote
1. Finalize @/home/limira/MiroLimira/MiroThinker/.humanize/rlcr/2026-06-06_14-59-24/goal-tracker.md (this is Round 0, so you are initializing it - see "Goal Tracker Setup" above)
2. Write your round contract into @/home/limira/MiroLimira/MiroThinker/.humanize/rlcr/2026-06-06_14-59-24/round-0-contract.md
3. Commit your changes with a descriptive commit message
4. Write your work summary into @/home/limira/MiroLimira/MiroThinker/.humanize/rlcr/2026-06-06_14-59-24/round-0-summary.md
