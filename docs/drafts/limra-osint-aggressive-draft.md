# 激进版 limra OSINT 产品化大修

## 总目标

在独立 worktree 上实现激进版：以 `limra` 品牌替换 Open WebUI 可见品牌，保留 Open WebUI 的认证、用户和会话能力；以聊天框为主体验，右侧提供 Evidence / Entity Graph / Timeline / Map / Report artifact 面板；用 MiroThinker 真实跑公开网研究任务，并让 agent 通过结构化事件输出证据、实体、关系、时间线和报告段落。

第一轮聚焦国际开源情报 OSINT MVP，不做安全生产、基层治理、完整语音、多模态视频和 Neo4j。必须真实部署数据库容器，并完成真实 API 研究任务验收。

## 固定技术栈

- Web shell：如果当前仓库缺少完整 Open WebUI 前端源码，则 vendor/fork Open WebUI 到本仓库，再改造成 `limra`。
- Frontend：使用 Open WebUI 现有 SvelteKit / TypeScript / Tailwind 技术栈。
- Backend shell：使用 Open WebUI 现有 FastAPI 后端，新增 `/api/limra/*` authenticated routes。
- Runner：保留当前 MiroThinker Runner API 和 `stream_events_optimized()` 路径；除非不可避免，不要重写 core orchestrator loop。
- 结构化 agent 输出：新增轻量 Limra artifact recording tool / event layer，让 agent 在研究过程中输出 evidence、entity、timeline、report artifacts。
- 数据库：PostgreSQL 16 + PostGIS + pgvector。
- Runtime state：Redis。
- 对象存储：MinIO，使用 S3-compatible API。
- 地图：MapLibre GL JS。
- 实体图谱：Cytoscape.js。
- PDF export：在部署镜像或 worker 中使用 Playwright Chromium，把 report HTML 渲染为 PDF。
- 上传解析：支持 PDF/text 上传；先做轻量 extraction，再把原始文件存入 MinIO，把 extracted text 存入 Postgres。

## 实现要求

1. 创建并使用独立 worktree 和分支，例如 `limra-aggressive`。不得修改保守版 worktree。

2. 如果 Open WebUI frontend source 缺失：
   - vendor upstream Open WebUI source 到清晰 app 路径，例如 `apps/limra-web`
   - 接入当前仓库
   - 在该路径应用所有 UI 改造

3. 全局品牌替换：
   - 把所有用户可见 Open WebUI 品牌替换成纯文本 `limra`
   - 覆盖 login、register、sidebar、top nav、page title、favicon alt text、empty states、error pages、mobile layout、browser title
   - 不允许只改一个页面

4. 新增 `limra` research page：
   - 主交互仍然是 chat / research stream
   - 右侧 artifact drawer 包含 tabs：
     - Evidence
     - Entities
     - Graph
     - Timeline
     - Map
     - Report

5. Browser-facing APIs 必须放在：
   `/api/limra/*`

6. 浏览器不能直接访问私有 Runner API。

7. 新增可信 archive 下载代理：
   `/api/limra/tasks/{task_id}/archive.zip`
   - 使用 Open WebUI 当前 authenticated user
   - 服务端注入 Runner service headers
   - 普通用户不能下载其他用户归档
   - 管理员访问必须走显式 admin route / role
   - 不允许把 service token 暴露到浏览器

8. 保持归档合同：
   - `trace.json`
   - `report.md`
   - `metadata.json`
   - `report.html`
   - `archive.zip`
   - zip 只能包含前四个文件
   - trace 不包含 heartbeat
   - 不允许 secrets

9. 生产任务状态不能继续依赖临时 SQLite：
   - SQLite 只允许作为 local dev fallback
   - 生产默认使用 Postgres
   - 本地 sqlite、archives、cache、runtime output 必须 gitignore

## 数据模型

实现 migrations，创建以下 Postgres tables：

1. `limra_research_tasks`
   - task ownership
   - query
   - status
   - archive_status
   - timestamps
   - errors
   - model_summary
   - scenario

2. `limra_evidence_items`
   - source URL/title
   - publisher
   - published time
   - collected time
   - original text
   - translated text
   - summary
   - language
   - credibility
   - confidence
   - cross verification
   - conflict notes
   - tool/model names
   - human confirmation
   - metadata

3. `limra_entities`
   - country
   - agency
   - company
   - person
   - policy
   - bill
   - sanction target
   - technology
   - project
   - location
   - event

4. `limra_entity_relations`
   - sanctions
   - regulates
   - affects industry
   - owns
   - partners with
   - located in
   - supply chain dependency
   - mentions
   - conflicts with

5. `limra_timeline_events`
   - event title
   - type
   - time
   - location
   - geometry
   - risk level
   - confidence
   - evidence refs

6. `limra_generated_reports`
   - report type
   - markdown
   - html
   - pdf object key
   - evidence refs
   - creator
   - timestamps

7. `limra_uploaded_documents`
   - original file metadata
   - MinIO object key
   - extracted text
   - language
   - task linkage

8. `limra_media_assets`
   - 为未来 audio/image/video assets 预留
   - 本轮不实现完整媒体管线

使用 PostGIS 存 geometry fields。仅在 upload retrieval 需要的 document/evidence embeddings 上使用 pgvector。

## Agent 输出合同

在不大规模重写 orchestrator 的前提下，新增结构化 artifact output path：

1. 新增 Limra artifact tool，例如：
   `record_research_artifact`

2. tool 输入：
   - `artifact_type`
   - `payload`
   - `evidence_refs`
   - `confidence`
   - `notes`

3. 支持 artifact types：
   - `evidence`
   - `entity`
   - `relation`
   - `timeline_event`
   - `map_feature`
   - `verification`
   - `report_section`

4. 更新 prompts，使 agent 在以下阶段记录 artifacts：
   - search/scrape results 之后
   - evidence verification 之后
   - entity/relation extraction 之后
   - final report sections 之前

5. Runner 把成功 artifact tool calls 转为 SSE events：
   - `evidence_collected`
   - `entity_extracted`
   - `relation_extracted`
   - `timeline_event_added`
   - `map_feature_added`
   - `verification_result`
   - `report_section_generated`

6. 每个事件必须：
   - streamed to UI
   - persisted to Postgres
   - included in `trace.json`
   - reflected in right-side artifact tabs

7. artifact validation failure 只能记录 non-fatal structured extraction warning。研究任务必须继续。

## Demo Scenarios

实现三个内置 prompt templates，用于真实 demo：

1. 制裁与出口管制追踪
   - 过去六个月美国、欧盟、日本影响中国半导体、AI、先进制造的出口管制变化

2. 地缘风险研判
   - 红海航运风险及其对能源运输、保险成本、中国海外项目的影响

3. 关键矿产国际竞争
   - lithium、nickel、rare earth 在美国、欧盟、澳大利亚、印度尼西亚、非洲资源国家的政策变化

每个 demo 必须产出：
   - evidence records
   - entities
   - timeline events
   - map 或明确 map empty state
   - 带 clickable evidence references 的 report，例如 `[EVID-001]`

## 部署要求

新增 Docker Compose，包含：

- `limra-web`
- `limra-runner`
- `postgres`
- `redis`
- `minio`
- `reverse-proxy`

要求：

- `docker compose up --build` 能启动整个 stack
- Postgres 有 persistent volume、healthcheck、PostGIS、pgvector
- Redis 有 healthcheck
- MinIO 有 persistent volume 和 default bucket initialization
- `.env.example` 包含所有必需变量，但不包含真实 secrets
- Runner 沿用现有 env 风格：
  - `BASE_URL`
  - `DEFAULT_MODEL_NAME`
  - `SERPER_API_KEY`
  - `JINA_API_KEY`
  - `E2B_API_KEY`
  - summary LLM vars
  - service token
- 生产浏览器下载必须经过：
  `/api/limra/tasks/{task_id}/archive.zip`
  不能直接暴露 Runner URL

## 测试计划

必须实现自动化测试：

1. Database migrations 创建所有 Limra tables 和 extensions。

2. 用户隔离：
   - User A 不能 read/download User B task/archive
   - Admin 只能通过显式 admin route/role access all tasks

3. Upload：
   - PDF/text upload 创建 document record
   - 原始文件进入 MinIO
   - extracted text 进入 Postgres
   - optional embedding 可创建

4. Structured artifact：
   - tool calls validate
   - persist
   - stream
   - appear in task artifacts

5. Archive zip：
   - 只能包含 `trace.json`、`report.md`、`metadata.json`、`report.html`

6. Secret scrub：
   - 捕获 API keys
   - Authorization
   - Bearer
   - cookies
   - Open WebUI tokens
   - Serper/Jina/E2B/OpenAI/DeepSeek keys

7. UI brand：
   - user-facing UI 不再出现 `Open WebUI`
   - 可见品牌统一为 `limra`

8. Playwright UI smoke：
   - login
   - open limra page
   - submit research query
   - see chat stream
   - see right-side Evidence/Graph/Timeline/Map/Report tabs
   - click evidence ref
   - download archive

## 真实运行验收

至少运行一个真实 demo task，不能只有 mock：

- 使用真实配置的 LLM/Search/Scrape env
- 确认真 sources 出现在 Evidence Ledger
- 确认 final report 引用 evidence IDs
- 确认 PDF export 可打开，并包含 report 和 evidence references
- 确认浏览器 archive download 成功
- 确认重启后 users、tasks、artifacts、archive metadata 仍然存在

## 明确假设

- 第一版是 OSINT MVP for think-tank work，不是完整 command-center map product。
- Chat 仍然是主交互面；右侧 artifact panels 让它适合研究工作。
- Public web search 和 user-uploaded documents 在 scope 内。
- Full safety-production product line、grassroots governance、real-time voice、video pipeline、Neo4j 不在第一版激进 worktree scope 内。
- Real-run validation 是强制项，但 deterministic unit/browser tests 可以使用 fake streams。

## 第一轮建议切分

Round 0 不要试图一次做完全部内容。第一轮应优先建立可部署骨架和数据合同：

- vendor 或定位 Open WebUI 前端源码
- 建立 apps/limra-web 或等价路径
- 建立 docker compose stack
- 建立 Postgres/PostGIS/pgvector/Redis/MinIO 服务
- 建立 limra 数据库 migrations
- 建立 /api/limra/* authenticated route 骨架
- 建立可信 archive download proxy 的最小实现
- 建立前端 limra research page 和 artifact drawer skeleton
- 建立第一批 migration/proxy/archive/user-isolation tests

后续 rounds 再推进：
- structured artifact tool
- SSE artifact persistence
- evidence/entity/timeline/map/report UI
- upload parsing
- PDF export
- real demo run
- Playwright browser smoke
