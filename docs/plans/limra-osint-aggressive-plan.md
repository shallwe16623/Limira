# limra OSINT Aggressive Implementation Plan

## Goal Description

在当前 `limra-aggressive` worktree 中，把已有 MiroThinker Runner API、Archive Writer 和 Open WebUI Pipe Function 基础推进为可部署的 `limra` OSINT 研判 MVP。

第一阶段目标不是继续包装文本链接，也不是把 Open WebUI Pipelines 作为默认方案，而是交付一个以 Open WebUI 认证、用户和会话能力为基础的产品骨架：浏览器只访问 `/api/limra/*`，后端代理私有 Runner API，生产任务和 artifacts 存入 Postgres 16 + PostGIS + pgvector，运行态使用 Redis，对象文件使用 MinIO，前端以聊天框为主体验并提供右侧 Evidence / Entities / Graph / Timeline / Map / Report artifact drawer。

当前仓库锚点已核对：

- 当前 worktree: `/home/limira/MiroLimira/MiroThinker-limra-aggressive`
- 当前分支: `limra-aggressive`
- 保守版 worktree: `/home/limira/MiroLimira/MiroThinker`
- 已有 Runner API 和 Archive Writer 位于 `apps/gradio-demo/runner_api.py`、`apps/gradio-demo/archive_writer.py`、`apps/gradio-demo/task_store.py`
- 已有测试位于 `apps/gradio-demo/tests/`
- 已有 Open WebUI Pipe Function 适配位于 `apps/open-webui-mirothinker/`
- 当前仓库未包含完整 Open WebUI SvelteKit 前端源码；激进版需要 vendor/fork 到 `apps/limra-web` 或等价清晰路径后再做产品化 UI 改造
- MiroThinker core agent 位于 `apps/miroflow-agent/src/core/`，本计划默认只读，不重写 orchestrator/search/fetch/tool/LLM 主循环

第一阶段 OSINT MVP 边界：

- 用 `limra` 替换所有用户可见 Open WebUI 品牌
- 保留 Open WebUI 的认证、用户和会话能力
- 以聊天框作为主体验
- 右侧 artifact drawer 包含 Evidence、Entities、Graph、Timeline、Map、Report
- 浏览器只访问 `/api/limra/*`
- 浏览器不能直接访问私有 Runner API
- 实现可信 archive 下载代理 `/api/limra/tasks/{task_id}/archive.zip`
- 使用 Postgres 16 + PostGIS + pgvector 作为生产默认任务和 artifact 数据库
- 使用 Redis 保存 runtime state
- 使用 MinIO 保存上传文件、PDF、归档对象和未来媒体资产
- 使用 MapLibre GL JS 展示地图 artifacts
- 使用 Cytoscape.js 展示实体关系图
- 使用 Playwright Chromium 导出 PDF
- 支持 PDF/text 上传解析，原始文件入 MinIO，extracted text 入 Postgres
- MiroThinker agent 通过轻量 artifact tool/event layer 输出结构化 artifact events
- Docker Compose 可启动 `limra-web`、`limra-runner`、`postgres`、`redis`、`minio`、`reverse-proxy`
- 至少完成一个真实 demo research run 验收
- 不重写 MiroThinker core orchestrator/search/fetch/tool/LLM 主循环

## Acceptance Criteria

- AC-1: worktree 和分支边界被持续保护。
  - Positive Tests (expected to PASS):
    - `pwd` 输出 `/home/limira/MiroLimira/MiroThinker-limra-aggressive`。
    - `git branch --show-current` 输出 `limra-aggressive`。
    - `git worktree list` 显示保守版和激进版是两个独立路径。
  - Negative Tests (expected to FAIL):
    - 在 `/home/limira/MiroLimira/MiroThinker` 中生成或修改激进版文件。
    - 切回 `limra-conservative` 或在保守版 worktree 内提交本计划范围的代码。

- AC-2: Open WebUI 前端源码被 vendor/fork 并作为 `limra` web shell 改造。
  - Positive Tests (expected to PASS):
    - 如果仓库仍缺少完整 Open WebUI SvelteKit frontend，则新增 `apps/limra-web` 或等价路径，包含可构建的 Open WebUI 前后端 shell。
    - `apps/limra-web` 保留 Open WebUI 认证、用户、会话和后端 FastAPI 能力。
    - 激进版 UI 改动只发生在 `apps/limra-web` 或明确的 limra 集成路径内。
  - Negative Tests (expected to FAIL):
    - 只修改 `apps/open-webui-mirothinker/` Pipe Function 文本，把它冒充为产品化 UI。
    - 用 Gradio 作为正式入口替代 Open WebUI shell。

- AC-3: 所有用户可见 Open WebUI 品牌替换为 `limra`。
  - Positive Tests (expected to PASS):
    - login、register、sidebar、top nav、page title、favicon alt text、empty states、error pages、mobile layout、browser title 中可见品牌为纯文本 `limra`。
    - 自动化 UI brand test 确认可见页面不再出现 `Open WebUI`。
    - 技术依赖、包名或内部注释中确需保留 upstream 名称时不出现在最终用户可见 UI。
  - Negative Tests (expected to FAIL):
    - 只改一个页面或一个标题。
    - 登录页、错误页、移动布局或浏览器标题仍显示 `Open WebUI`。

- AC-4: `limra` research page 以 chat 为主体验，并提供 artifact drawer。
  - Positive Tests (expected to PASS):
    - 页面可从已登录 Open WebUI session 进入。
    - 用户可提交 research query，并看到聊天式 stream。
    - 右侧 drawer 有 Evidence、Entities、Graph、Timeline、Map、Report tabs。
    - drawer 在桌面端为右侧面板，在移动端为可访问的 overlay 或底部抽屉。
  - Negative Tests (expected to FAIL):
    - research page 只是静态文档或外部链接列表。
    - artifacts 只显示在聊天文本里，没有可点击的右侧结构化面板。

- AC-5: 浏览器面对的 API 全部位于 `/api/limra/*`。
  - Positive Tests (expected to PASS):
    - 前端提交任务、读取任务、读取 artifacts、上传文件、导出 PDF、下载 archive 都调用 `/api/limra/*`。
    - Playwright route/assertion 或 frontend tests 确认浏览器未直接调用 runner host/port。
    - 后端服务端代码负责调用私有 Runner API，并注入服务端凭据。
  - Negative Tests (expected to FAIL):
    - 前端 bundle、页面源码、网络请求或归档中出现 `RUNNER_SERVICE_TOKEN`。
    - 浏览器直接访问 `limra-runner:8091`、`localhost:8091` 或原 `/mirothinker/*` 私有 endpoint。

- AC-6: 可信 archive 下载代理满足用户隔离和 zip 合同。
  - Positive Tests (expected to PASS):
    - `GET /api/limra/tasks/{task_id}/archive.zip` 从当前 Open WebUI authenticated user 解析身份。
    - 普通用户只能下载自己的 task archive。
    - 管理员必须通过显式 admin route/role 查看或下载其他用户 archive。
    - 服务端代理注入 Runner service headers；浏览器永远拿不到 service token。
    - zip 只包含 `trace.json`、`report.md`、`metadata.json`、`report.html`。
  - Negative Tests (expected to FAIL):
    - 普通用户通过猜测 task_id 下载他人 archive。
    - zip 包含 `.env`、日志目录、数据库文件、缓存、绝对路径、`..` 路径或其他额外文件。

- AC-7: 生产任务状态默认使用 Postgres，不再依赖临时 SQLite。
  - Positive Tests (expected to PASS):
    - Docker Compose 和生产配置默认连接 Postgres 16。
    - SQLite 仅作为 local dev fallback 明确保留，并且不会被 production compose 使用。
    - 本地 sqlite、archives、cache、runtime output 已被 `.gitignore` 覆盖。
  - Negative Tests (expected to FAIL):
    - 多用户任务归属只保存在内存或 SQLite 临时文件中。
    - production compose 启动后 Runner 仍默认写 `tasks.sqlite3` 作为唯一任务来源。

- AC-8: migrations 创建所有 limra tables、extensions 和关键索引。
  - Positive Tests (expected to PASS):
    - migrations 创建 `postgis` 和 `vector` extensions。
    - migrations 创建 `limra_research_tasks`、`limra_evidence_items`、`limra_entities`、`limra_entity_relations`、`limra_timeline_events`、`limra_generated_reports`、`limra_uploaded_documents`、`limra_media_assets`。
    - geometry fields 使用 PostGIS 类型；document/evidence retrieval 需要的 embedding 字段使用 pgvector。
    - task/user/status/time、artifact task_id、evidence ref、entity relation、timeline time 和 geometry 字段有必要索引。
  - Negative Tests (expected to FAIL):
    - migrations 只创建任务表，缺少 artifact tables。
    - geometry 存为不可查询的纯文本，或 embedding 存为无类型 JSON 字符串。

- AC-9: Limra 数据模型覆盖 OSINT MVP 的证据、实体、关系、时间线、报告、上传和媒体预留。
  - Positive Tests (expected to PASS):
    - `limra_research_tasks` 包含 task ownership、query、status、archive_status、timestamps、errors、model_summary、scenario。
    - `limra_evidence_items` 包含 URL/title、publisher、published/collected time、original/translated text、summary、language、credibility、confidence、cross verification、conflict notes、tool/model names、human confirmation、metadata。
    - `limra_entities` 支持 country、agency、company、person、policy、bill、sanction target、technology、project、location、event。
    - `limra_entity_relations` 支持 sanctions、regulates、affects industry、owns、partners with、located in、supply chain dependency、mentions、conflicts with。
    - `limra_timeline_events` 包含 event title、type、time、location、geometry、risk level、confidence、evidence refs。
    - `limra_generated_reports` 包含 report type、markdown、html、pdf object key、evidence refs、creator、timestamps。
    - `limra_uploaded_documents` 包含 original file metadata、MinIO object key、extracted text、language、task linkage。
    - `limra_media_assets` 为未来 audio/image/video assets 预留。
  - Negative Tests (expected to FAIL):
    - artifact payload 只存在 SSE transient stream 中，重启后无法恢复。
    - evidence refs 和 report refs 无法关联或查询。

- AC-10: Runner API、Open WebUI 代理和数据库写入形成清晰的服务边界。
  - Positive Tests (expected to PASS):
    - `limra-web` 只暴露 `/api/limra/*` 给浏览器。
    - `limra-web` 通过服务端环境变量调用 `limra-runner` 私有 API。
    - `limra-runner` 可复用现有 `stream_events_optimized()` 路径，但不暴露给浏览器。
    - 任务创建、artifact persist、archive metadata 以 Postgres 为权威状态来源。
  - Negative Tests (expected to FAIL):
    - 由前端传入 `user_id` 决定任务归属。
    - 任务完成状态只由浏览器内存判断。

- AC-11: 新增 `record_research_artifact` tool/event layer，且不重写 core orchestrator。
  - Positive Tests (expected to PASS):
    - 新增轻量 tool 或适配层，输入包含 `artifact_type`、`payload`、`evidence_refs`、`confidence`、`notes`。
    - 支持 `evidence`、`entity`、`relation`、`timeline_event`、`map_feature`、`verification`、`report_section`。
    - prompt 更新只要求 agent 在 search/scrape、verification、entity/relation extraction、final report sections 前后记录 artifacts。
    - artifact validation failure 作为 non-fatal structured extraction warning 记录，研究任务继续。
  - Negative Tests (expected to FAIL):
    - 为 artifact 输出重写 orchestrator/search/fetch/tool selection/LLM 调用主循环。
    - validation failure 直接导致整次 research task failed。

- AC-12: artifact events 被 stream、persist、归档并反映在 UI tabs。
  - Positive Tests (expected to PASS):
    - 成功 artifact tool calls 转为 SSE events: `evidence_collected`、`entity_extracted`、`relation_extracted`、`timeline_event_added`、`map_feature_added`、`verification_result`、`report_section_generated`。
    - 每个事件流向 UI，同时持久化到 Postgres，写入 `trace.json`，并反映在右侧 tab。
    - UI 可按 evidence refs 点击跳转到对应证据。
  - Negative Tests (expected to FAIL):
    - artifact 只出现在最终 markdown 中。
    - SSE 中有 artifact，但数据库和 archive 缺失。

- AC-13: PDF/text 上传解析端到端可用。
  - Positive Tests (expected to PASS):
    - PDF/text upload 经 `/api/limra/uploads` 或等价 `/api/limra/*` endpoint 创建 `limra_uploaded_documents` 记录。
    - 原始文件存入 MinIO，object key 由服务端生成。
    - extracted text 存入 Postgres。
    - 可选 embedding 在配置启用时写入 pgvector 字段。
    - 用户只能读取自己上传的 document 或与自己 task 关联的 document。
  - Negative Tests (expected to FAIL):
    - 原始文件只保存在容器临时文件系统。
    - 前端可指定任意 MinIO object key 或读取其他用户文件。

- AC-14: Report HTML 和 PDF export 可部署、可下载。
  - Positive Tests (expected to PASS):
    - report markdown 和 HTML 存入 `limra_generated_reports`。
    - Playwright Chromium 在部署镜像或 worker 中把 report HTML 渲染为 PDF。
    - PDF object key 存入 Postgres，PDF 文件存入 MinIO。
    - PDF 包含 report 和 clickable evidence references。
  - Negative Tests (expected to FAIL):
    - PDF export 依赖开发者本机浏览器。
    - PDF 中缺失 evidence refs 或包含 secret。

- AC-15: Map 和 Graph panels 使用真实前端库并有明确 empty state。
  - Positive Tests (expected to PASS):
    - Graph tab 使用 Cytoscape.js 展示 entity/relation artifacts。
    - Map tab 使用 MapLibre GL JS 展示 timeline/map_feature geometry。
    - 没有 geometry 时，Map tab 显示明确 empty state，而不是报错或空白 canvas。
  - Negative Tests (expected to FAIL):
    - Graph/Map 只是静态文本占位，没有真实组件集成。
    - 空数据导致前端 crash。

- AC-16: 内置三个 OSINT demo scenarios，至少一个真实 demo run 完成验收。
  - Positive Tests (expected to PASS):
    - 内置 prompt templates 包含制裁与出口管制追踪、地缘风险研判、关键矿产国际竞争。
    - 每个 demo 设计为产出 evidence records、entities、timeline events、map 或明确 map empty state、带 `[EVID-001]` 形式 clickable refs 的 report。
    - 至少使用真实配置的 LLM/Search/Scrape env 跑通一个 demo task。
    - 真实 run 验收确认 sources 出现在 Evidence Ledger，final report 引用 evidence IDs，PDF 可打开，浏览器 archive download 成功，重启后 users/tasks/artifacts/archive metadata 仍存在。
  - Negative Tests (expected to FAIL):
    - 只用 mock stream 宣称完成真实 demo。
    - final report 没有 evidence IDs。

- AC-17: Docker Compose 可启动完整 aggressive stack，且端口不与保守版冲突。
  - Positive Tests (expected to PASS):
    - `COMPOSE_PROJECT_NAME=limra_aggressive docker compose up --build` 可启动 `limra-web`、`limra-runner`、`postgres`、`redis`、`minio`、`reverse-proxy`。
    - 默认端口为 `limra-web:3001`、`limra-runner:8091`、`postgres:5433`、`redis:6380`、`minio api:9002`、`minio console:9003`。
    - Postgres 有 persistent volume、healthcheck、PostGIS、pgvector。
    - Redis 有 healthcheck。
    - MinIO 有 persistent volume 和 default bucket initialization。
    - `.env.example` 包含必需变量但不包含真实 secrets。
  - Negative Tests (expected to FAIL):
    - compose 使用保守版默认端口导致冲突。
    - compose 缺少数据库、Redis 或 MinIO 服务。

- AC-18: secret scrub 覆盖 API keys、Authorization、cookies 和 Open WebUI/LLM/Search secrets。
  - Positive Tests (expected to PASS):
    - 自动化测试注入 API keys、Authorization、Bearer、cookies、Open WebUI tokens、Serper/Jina/E2B/OpenAI/DeepSeek keys 后，archive、trace、metadata、report、PDF 和 logs 中不会出现原始 secret。
    - `RUNNER_SERVICE_TOKEN` 只存在服务端环境，前端 bundle、网络响应、归档和日志不可见。
  - Negative Tests (expected to FAIL):
    - scrubber 只处理顶层字段。
    - secret 出现在 zip 解压结果、PDF 文本、frontend bundle 或 browser network payload。

- AC-19: 自动化测试覆盖 migration、用户隔离、upload、artifact、archive、secret、brand 和 UI smoke。
  - Positive Tests (expected to PASS):
    - Migration tests 验证 tables/extensions。
    - 用户隔离 tests 验证 User A 不能 read/download User B task/archive；admin 只能走显式 admin route/role。
    - Upload tests 验证 PDF/text upload、MinIO object、Postgres extracted text、optional embedding。
    - Structured artifact tests 验证 tool calls validate、persist、stream、appear in task artifacts。
    - Archive zip tests 验证成员固定为四个文件。
    - Secret scrub tests 覆盖主要 secret 类型。
    - UI brand tests 确认可见 UI 不出现 `Open WebUI`。
    - Playwright smoke 覆盖 login、open limra page、submit research query、chat stream、artifact tabs、click evidence ref、download archive。
  - Negative Tests (expected to FAIL):
    - 只提供手工 smoke checklist，没有可执行测试。
    - 只测试 Runner API，不测试用户隔离或 browser-facing `/api/limra/*`。

- AC-20: Gradio 只作为开发 fallback 保留。
  - Positive Tests (expected to PASS):
    - 现有 Gradio fallback 测试继续通过。
    - 正式入口文档、compose 和用户路径指向 `limra-web`。
  - Negative Tests (expected to FAIL):
    - 把 Gradio 当正式多用户产品入口。
    - 为 limra UI 改造破坏现有 Gradio regression tests。

## Path Boundaries

### Upper Bound (Maximum Scope)

本计划最大可接受范围是完成一个真实可部署、真实可点击、真实多用户隔离、有数据库、有对象存储、有右侧 artifact 面板的 limra OSINT MVP：

1. Vendor/fork Open WebUI 到 `apps/limra-web` 或等价路径，并完成 `limra` 品牌替换。
2. 在 Open WebUI FastAPI shell 中新增 `/api/limra/*` authenticated routes。
3. 新增或改造 `limra-runner` 服务，复用当前 Runner API、Archive Writer 和 `stream_events_optimized()`。
4. 新增 Postgres/PostGIS/pgvector migrations 和 artifact persistence。
5. 新增 Redis runtime state 和 MinIO object storage。
6. 新增 chat + artifact drawer UI，Graph 使用 Cytoscape.js，Map 使用 MapLibre GL JS。
7. 新增 upload parsing、structured artifact events、PDF export。
8. 新增 docker compose stack 和自动化测试。
9. 跑通至少一个真实 demo research run。

即使做到最大范围，也不能把 Open WebUI Pipelines 作为默认新方案，不能把 Gradio 当正式入口，不能把 service token 暴露给浏览器，不能保存 raw hidden chain-of-thought，不能重写 MiroThinker core agent 主循环。

### Lower Bound (Minimum Scope)

Round 0 最小可交付范围是可部署骨架和数据合同，不要求一次完成全部 OSINT 产品能力：

- 定位或 vendor Open WebUI frontend source，建立 `apps/limra-web` 或等价路径。
- 建立 aggressive docker compose stack。
- 建立 Postgres/PostGIS/pgvector、Redis、MinIO 服务和 `.env.example`。
- 建立 limra migrations 和 repository/service skeleton。
- 建立 `/api/limra/*` authenticated route skeleton。
- 建立可信 archive download proxy 的最小实现。
- 建立前端 limra research page 和 artifact drawer skeleton。
- 建立 migration/proxy/archive/user-isolation 第一批 tests。

Round 0 不能退化为只写文档、只写 smoke checklist、只包装 Pipe Function 文本链接。

### In Scope Paths

- `apps/limra-web/`
  - Open WebUI vendor/fork source 和 limra UI/API 改造。
- `apps/limra-runner/` 或现有 `apps/gradio-demo/`
  - Runner 服务 packaging、Postgres task store adapter、artifact event bridge、archive proxy support。
- `apps/gradio-demo/runner_api.py`
  - 允许作为私有 Runner API 基础继续使用，但不能作为 browser-facing API。
- `apps/gradio-demo/archive_writer.py`
  - 保留 archive contract；可扩展为 MinIO/object storage 写入或被 limra-runner 复用。
- `apps/gradio-demo/task_store.py`
  - 可新增 Postgres implementation；SQLite 仅保留 dev fallback。
- `apps/gradio-demo/tests/`
  - 保留现有 tests，新增 limra runner/proxy/artifact tests 或迁移到新 app tests。
- `apps/open-webui-mirothinker/`
  - 只作为 legacy/compatibility 参考或保留，不作为 aggressive 默认正式方案。
- `deploy/`, `docker-compose*.yml`, `.env.example`
  - aggressive stack、reverse proxy、healthcheck、bucket init。
- `migrations/`, `alembic/`, `apps/limra-web/backend/...`
  - 取决于最终 Open WebUI source 布局，新增 limra migrations 和 API routes。
- `docs/`
  - 保留计划、API 合同、demo run 记录和部署验收说明。

### Out of Scope Paths

以下路径第一阶段默认只读，除非后续发现无法通过适配层完成并单独记录原因：

- `apps/miroflow-agent/src/core/orchestrator.py`
- `apps/miroflow-agent/src/core/pipeline.py`
- `apps/miroflow-agent/src/core/tool_executor.py`
- `apps/miroflow-agent/src/llm/`
- `libs/miroflow-tools/`

### Invariants

- 当前工作目录必须保持在 `/home/limira/MiroLimira/MiroThinker-limra-aggressive`。
- 当前分支必须保持为 `limra-aggressive`。
- Open WebUI 认证、用户、会话能力是 limra web 的身份基础。
- `/api/limra/*` 是唯一 browser-facing limra API namespace。
- Runner service token 只允许服务端使用。
- `trace.json` 不包含 heartbeat，不包含 secrets。
- `archive.zip` 只包含 `trace.json`、`report.md`、`metadata.json`、`report.html`。
- SQLite 只允许 local dev fallback。
- Gradio 只作为开发 fallback。
- 实现代码不能把 plan 术语作为产品文案。

## Allowed Choices

- Web shell:
  - Recommended: `apps/limra-web` vendor/fork Open WebUI，并在该路径完成 brand、route、UI、API 改造。
  - Allowed: 等价清晰路径，但必须不混淆保守版和 legacy Pipe Function。
  - Not Allowed: 只改 `apps/open-webui-mirothinker/` Pipe Function。

- Backend API:
  - Recommended: 在 Open WebUI FastAPI backend shell 中新增 `/api/limra/*` authenticated routes。
  - Allowed: 若 vendor/fork 初期尚未完成，可先在 limra backend skeleton 中实现同合同 routes，并在同一 round 内接入 Open WebUI auth stub/facade tests。
  - Not Allowed: 浏览器调用私有 Runner API 或直接暴露 `/mirothinker/*`。

- Runner:
  - Recommended: 用现有 `apps/gradio-demo/runner_api.py` 和 `stream_events_optimized()` 做私有 runner service 基础。
  - Allowed: 新增 `apps/limra-runner` 包装现有 runner code，前提是 Gradio fallback 不破坏。
  - Not Allowed: 重写 core orchestrator/search/fetch/tool/LLM 主循环。

- Database:
  - Required: Production default Postgres 16 + PostGIS + pgvector。
  - Allowed: SQLite local dev fallback。
  - Not Allowed: Production state 只依赖 SQLite、内存或 archive filesystem。

- Object storage:
  - Required: MinIO S3-compatible API for uploads, PDFs, archive objects, future media assets。
  - Not Allowed: 用户上传只落容器临时磁盘。

- Frontend visualization:
  - Required: MapLibre GL JS for Map，Cytoscape.js for Graph。
  - Allowed: Round 0 skeleton 可先显示 empty state 和 typed artifact lists，但 dependencies/slots 必须建立。
  - Not Allowed: 永久静态文本占位冒充真实 graph/map implementation。

## Dependencies and Sequence

### Milestone 0: Baseline Plan and RLCR Setup

1. 保存 draft 到 `docs/drafts/limra-osint-aggressive-draft.md`。
2. 生成本计划到 `docs/plans/limra-osint-aggressive-plan.md`。
3. 审阅计划是否满足 OSINT MVP 产品化边界。
4. 提交 baseline commit。
5. 用 `setup-rlcr-loop.sh` 启动新的 RLCR loop，跟踪本 plan 文件。

### Milestone 1: Round 0 Deployable Skeleton and Data Contract

1. 前端源码定位或 vendor:
   - 检查仓库是否已有完整 Open WebUI source。
   - 如无，vendor/fork 到 `apps/limra-web`。
   - 保证可安装、可构建、可运行最小页面。

2. Compose stack:
   - 新增 aggressive compose file 和 `.env.example`。
   - 服务包括 `limra-web`、`limra-runner`、`postgres`、`redis`、`minio`、`reverse-proxy`。
   - 使用 `COMPOSE_PROJECT_NAME=limra_aggressive` 和非冲突端口。

3. Database migrations:
   - 创建 PostGIS/pgvector extensions。
   - 创建全部 limra tables 和索引。
   - 新增 migration test。

4. Storage/runtime skeleton:
   - Redis client/config skeleton。
   - MinIO client/config skeleton。
   - bucket init in compose。

5. `/api/limra/*` skeleton:
   - Authenticated current-user dependency。
   - `POST /api/limra/research`
   - `GET /api/limra/tasks/{task_id}`
   - `GET /api/limra/tasks/{task_id}/events`
   - `GET /api/limra/tasks/{task_id}/artifacts`
   - `GET /api/limra/tasks/{task_id}/archive.zip`
   - explicit admin route for all-user access。

6. Archive proxy:
   - Map Open WebUI user -> task ownership。
   - Inject Runner service headers server-side。
   - Enforce `archive_status == ready`。
   - Validate fixed zip members where feasible。

7. UI skeleton:
   - limra route/page。
   - chat input/stream area。
   - right-side artifact drawer with six tabs。
   - first brand replacement pass and brand test scaffold。

8. Tests:
   - migration tests。
   - route auth/user isolation tests。
   - archive proxy tests。
   - zip contract tests。
   - UI skeleton/brand smoke tests where buildable。

### Milestone 2: Structured Artifact Event Pipeline

1. Add `record_research_artifact` schema and validation.
2. Add prompt changes that encourage artifact recording at required phases.
3. Convert successful tool calls into typed SSE events.
4. Persist typed artifact events to Postgres.
5. Include artifact events in `trace.json`.
6. Display Evidence/Entities/Timeline/Report list data in drawer.
7. Add non-fatal warning handling for invalid artifact payloads.
8. Add unit tests for validation, persistence, stream output and trace inclusion.

### Milestone 3: Artifact-Rich UI

1. Evidence Ledger with clickable refs such as `[EVID-001]`。
2. Entities tab with typed entity cards/table。
3. Graph tab using Cytoscape.js for relations。
4. Timeline tab with ordered events and evidence refs。
5. Map tab using MapLibre GL JS with geometry-backed features and empty state。
6. Report tab with rendered report sections and evidence refs。
7. Playwright tests for tab visibility, click-through evidence refs and layout.

### Milestone 4: Uploads, MinIO and Retrieval Data

1. Implement PDF/text upload route under `/api/limra/*`。
2. Store original file in MinIO。
3. Extract text with lightweight parser。
4. Store extracted text and metadata in Postgres。
5. Optionally create pgvector embedding when configured。
6. Link uploads to tasks。
7. Add tests for upload ownership, extraction and storage.

### Milestone 5: Report HTML/PDF and Archive Objects

1. Persist generated reports to Postgres。
2. Render report HTML safely。
3. Use Playwright Chromium in image/worker to export PDF。
4. Store PDF in MinIO。
5. Make archive metadata survive restart。
6. Verify secret scrub across report HTML/PDF/archive.

### Milestone 6: Demo Scenarios and Real Run Acceptance

1. Add three built-in OSINT prompt templates。
2. Add demo selector UI。
3. Run at least one real demo with real LLM/Search/Scrape env。
4. Record acceptance evidence in docs or smoke results:
   - sources in Evidence Ledger
   - final report evidence refs
   - PDF opens and contains refs
   - browser archive download succeeds
   - restart preserves users/tasks/artifacts/archive metadata

## Test Plan

### Unit and Integration Tests

Run from the relevant app directory after implementation:

```bash
uv run pytest
```

Required focused suites:

```bash
uv run pytest tests/test_limra_migrations.py
uv run pytest tests/test_limra_user_isolation.py
uv run pytest tests/test_limra_archive_proxy.py
uv run pytest tests/test_limra_artifacts.py
uv run pytest tests/test_limra_uploads.py
uv run pytest tests/test_limra_secret_scrub.py
```

Existing Gradio/Runner regression suites must keep passing:

```bash
cd apps/gradio-demo
uv run pytest tests/test_archive_writer.py tests/test_runner_api.py tests/test_task_store_and_auth.py tests/test_gradio_regression.py
```

### Frontend and Browser Tests

From `apps/limra-web` or its equivalent:

```bash
npm run lint
npm run test
npm run build
npx playwright test
```

Required browser assertions:

- login works with Open WebUI auth。
- `limra` page opens。
- research query can be submitted。
- chat stream appears。
- Evidence、Entities、Graph、Timeline、Map、Report tabs are visible。
- evidence ref click selects the matching Evidence item。
- archive download goes through `/api/limra/tasks/{task_id}/archive.zip`。
- browser network does not call private Runner API directly。
- visible UI does not show `Open WebUI`。

### Compose Verification

```bash
COMPOSE_PROJECT_NAME=limra_aggressive docker compose up --build
```

Required checks:

- `limra-web` reachable on `http://localhost:3001`。
- `limra-runner` private service reachable only from compose network or protected host port `8091` for local debug。
- Postgres reachable on localhost `5433` and has `postgis` and `vector` extensions。
- Redis reachable on localhost `6380` and passes healthcheck。
- MinIO API reachable on `9002` and console on `9003`。
- Default bucket exists after init。

### Secret and Archive Verification

```bash
unzip -l archive.zip
rg -i "Authorization|Bearer|Cookie|Set-Cookie|RUNNER_SERVICE_TOKEN|SERPER_API_KEY|JINA_API_KEY|E2B_API_KEY|OPENAI_API_KEY|DEEPSEEK_API_KEY|sk-[A-Za-z0-9]|eyJ[A-Za-z0-9_-]+" <archive-or-extracted-dir>
```

Expected:

- zip members exactly `trace.json`、`report.md`、`metadata.json`、`report.html`。
- no secret values appear in archive files, PDFs, frontend bundle or logs。

## Implementation Notes

- Do not use `/flow` commands.
- Do not continue an old completed RLCR loop.
- Do not edit `.humanize/rlcr/*/state.md`.
- Do not manually skip hooks.
- Do not put Open WebUI Pipelines forward as the default aggressive solution.
- Do not expose `RUNNER_SERVICE_TOKEN` to browser, frontend bundle, logs or archives.
- Do not use raw hidden chain-of-thought as a product feature.
- Keep code changes scoped to aggressive worktree.
- Commit each RLCR round after tests, then let the native Stop hook review naturally.
