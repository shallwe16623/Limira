# Limira OSINT Research Workbench

这是当前项目的主仓库说明。这个仓库现在服务的是一套独立的网页研究工作台：浏览器只进入我们自己写的前端，前端只代理 `limira` API 到自研后端，不再把旧的通用聊天壳作为用户入口。

## 当前定位

Limira 是面向 OSINT / 深度研究工作流的网页应用。它把研究提问、实时进展、历史聊天、资料上传、成果查看、PDF 导出和归档下载放在一个独立工作台里。

当前主链路是：

```text
Browser
  -> apps/limira-standalone
  -> /api/limira/*
  -> apps/limira-web/backend/limira_native.py
  -> limira runner / database / object storage
```

## 主要功能

- 独立网页登录、注册、会话恢复和退出登录
- 新建研究任务并实时查看进展
- 历史聊天列表和任务恢复
- 上传本地资料，并在研究时检索上传内容
- 查看结构化研究成果、报告片段和证据成果
- 下载完整归档 zip，归档内包含报告 PDF
- 后端保留事件日志，普通用户前端不展示底层事件字符串
- 管理接口可按任务查看后台事件日志
- 下载、归档、PDF、上传文件和任务详情都按用户归属做边界检查

## 目录结构

```text
apps/limira-standalone/
  独立用户前端。public/ 内是浏览器页面，server.mjs 负责静态文件和 API 代理。

apps/limira-web/backend/
  自研 FastAPI 后端入口。limira_native.py 挂载 /api/limira 路由。

apps/limira-runner/
  runner 服务、研究 pipeline helper 和测试。当前网页入口不从这里启动。

apps/limira-agent/
  runner 依赖的最小 agent runtime。只保留当前研究链路需要的 src/ 和 Hydra 配置。

libs/limira-tools/
  runner 使用的最小工具库：搜索/抓取、Jina 摘要、结构化成果记录和 Python 沙盒。

deploy/limira/
  Docker 部署所需的 Postgres、MinIO、runner 和 nginx 配置。

docker-compose.limira.yml
  本项目的生产式本地部署编排。
```

## 快速启动

### 1. 准备环境

需要：

- Python 3.12
- uv
- Node.js 18 或更高版本
- bash、curl，以及可选的 lsof 或 fuser（`scripts/start-local.sh` 用来清理占用端口的旧进程）
- Docker 和 Docker Compose，推荐用于完整本地部署

前端 `apps/limira-standalone/server.mjs` 只使用 Node.js 内置模块，当前没有 `package.json`，不需要 `npm install`。

### 2. 安装项目依赖

本地开发和服务启动使用 `apps/limira-runner` 的 Python 环境。它的 `pyproject.toml` 会把 runner、agent、tools 三部分依赖一次装齐，包括 FastAPI/aiohttp、LLM SDK、MCP/FastMCP、PDF/Office 文档解析、PDF 导出、对象存储、Postgres 驱动、测试工具、PDF 上传抽文本需要的 `pypdf`，以及语音输入后端转写使用的开源 `faster-whisper`。

在每个 worktree 里执行：

```bash
cd apps/limira-runner
uv sync --locked
```

如果只安装生产运行依赖，可以使用：

```bash
cd apps/limira-runner
uv sync --locked --no-dev
```

PDF 导出需要 Playwright 的 Chromium 浏览器二进制。Python 包会由 `uv sync --locked` 安装，但浏览器二进制需要单独安装：

```bash
cd apps/limira-runner
uv run playwright install chromium
```

如果机器缺 Chromium 运行所需的系统库，再执行：

```bash
cd apps/limira-runner
uv run playwright install-deps chromium
```

`install-deps` 可能需要系统包管理权限。服务器上如果已经有可用的 Playwright 运行时，也可以通过 `LIMIRA_PLAYWRIGHT_RUNTIME_PATH` 指向它。

当前 Python 顶层依赖清单由这三个文件维护，不要手工逐个 `pip install`：

```text
apps/limira-runner/pyproject.toml
apps/limira-agent/pyproject.toml
libs/limira-tools/pyproject.toml
```

只要这些文件或 `apps/limira-runner/uv.lock` 变化，就在对应 worktree 里重新运行 `cd apps/limira-runner && uv sync --locked`。

### 3. 配置环境变量

复制环境变量模板：

```bash
cp .env.example .env
```

本地启动脚本会按下面顺序读取环境变量文件；每个 worktree 都有自己独立的一组文件，不会自动共享其它 worktree 的配置：

```text
<worktree>/.env
  本地服务端口、RUNNER_SERVICE_TOKEN、LIMIRA_AUTH_SECRET、CORS 等整套服务通用配置。

<worktree>/apps/limira-agent/.env
  agent 工具配置：SERPER_API_KEY、JINA_API_KEY、E2B_API_KEY、SUMMARY_LLM_* 等。

<worktree>/apps/limira-runner/.env
  runner 模型配置：DEFAULT_LLM_PROVIDER、DEFAULT_MODEL_NAME、BASE_URL、API_KEY 等。
```

当前服务器上常用的四个 worktree 对应路径是：

```text
/home/limira/MiroLimira/MiroThinker-limra-aggressive/
/home/limira/MiroLimira/dev/
/home/limira/MiroLimira/ios/
/home/limira/MiroLimira/android/
```

如果要让某个 worktree 跑真实研究任务，至少要确保这个 worktree 下的 `apps/limira-agent/.env` 和 `apps/limira-runner/.env` 已存在并包含有效密钥。`.env` 文件属于本机密钥配置，已被 git 忽略，不要提交到仓库。

至少需要填写：

```bash
LIMIRA_AUTH_SECRET=
RUNNER_SERVICE_TOKEN=
POSTGRES_PASSWORD=
MINIO_ROOT_PASSWORD=
AWS_SECRET_ACCESS_KEY=
```

如果要真实跑研究任务，还需要配置模型和工具相关变量，例如：

```bash
BASE_URL=https://api.deepseek.com
DEFAULT_LLM_PROVIDER=openai
DEFAULT_MODEL_NAME=deepseek-v4-pro
API_KEY=
SERPER_API_KEY=
JINA_API_KEY=
E2B_API_KEY=
```

语音输入的后端转写接口 `/api/limira/speech/transcribe` 使用 `faster-whisper` 按需加载模型。默认使用 CPU 上的 `tiny` 模型；如需调整模型或使用 GPU，可在当前 worktree 的 `.env` 中配置：

```bash
LIMIRA_SPEECH_WHISPER_MODEL=tiny
LIMIRA_SPEECH_WHISPER_DEVICE=cpu
LIMIRA_SPEECH_WHISPER_COMPUTE_TYPE=int8
LIMIRA_SPEECH_MAX_AUDIO_BYTES=26214400
```

浏览器允许麦克风权限时，前端会直接录音并上传给后端 Whisper 转写；如果当前是 `http://IP` 这类浏览器禁止麦克风的访问环境，页面只提示切换到 HTTPS 域名或 localhost，不会弹出本地音频文件选择。

### 4. 一键启动本地开发服务

本地开发和调试推荐直接运行启动脚本：

```bash
./scripts/start-local.sh
```

脚本会自动启动：

- runner：`http://127.0.0.1:8091`
- 后端：`http://127.0.0.1:8080`
- 独立前端：`http://127.0.0.1:5173/limira`

它会先停止当前端口上的旧进程，再重新启动三段服务，并把日志写到：

```text
limira-runtime/logs/
```

PID 文件写到：

```text
limira-runtime/pids/
```

常用命令：

```bash
./scripts/start-local.sh          # 默认 restart
./scripts/start-local.sh restart
./scripts/start-local.sh stop
./scripts/start-local.sh status
```

脚本默认使用 SQLite、本地文件对象存储和内存 runtime state，适合本机开发。模型 API、搜索 API 等真实研究所需变量仍从 `.env`、`apps/limira-agent/.env` 和 `apps/limira-runner/.env` 读取。

### 5. 绑定域名和 HTTPS

项目提供 Caddy 反向代理脚本：

```bash
./scripts/start-https.sh
```

默认域名是：

```text
limira-inc.com
```

使用前需要先在 DNS 服务商处添加 A 记录：

```text
Type: A
Host: @
Value: 服务器公网 IPv4
```

脚本会把 `https://limira-inc.com` 反向代理到本地前端：

```text
127.0.0.1:5173
```

并让 Caddy 自动申请和续期证书。常用命令：

```bash
./scripts/start-https.sh restart
./scripts/start-https.sh stop
./scripts/start-https.sh status
```

如果当前用户没有绑定 80/443 端口的权限，脚本会提示执行一次：

```bash
sudo setcap cap_net_bind_service=+ep limira-runtime/caddy/caddy
```

之后重新运行：

```bash
./scripts/start-https.sh restart
```

可以通过这些变量覆盖默认配置：

```bash
LIMIRA_DOMAIN=limira-inc.com
CADDY_ACME_EMAIL=admin@limira-inc.com
LIMIRA_FRONTEND_UPSTREAM=127.0.0.1:5173
```

### 6. Docker Compose 部署

如果要跑更接近生产的 Postgres、Redis、MinIO 组合，可以用 Docker Compose：

```bash
docker compose -f docker-compose.limira.yml up --build
```

默认后端 API 会发布到：

```text
http://127.0.0.1:3001/api/limira
```

Docker 模式下独立前端可以另开一个终端启动，并代理到 compose 后端：

```bash
LIMIRA_BACKEND_URL=http://127.0.0.1:3001 \
node apps/limira-standalone/server.mjs
```

前端服务只允许代理 `/api/limira/*`，其它通用 API 路径会被拒绝。

## 开发模式

如果你只想改后端代码，也可以在 `apps/limira-web/backend` 下直接启动 FastAPI：

```bash
cd apps/limira-web/backend
uvicorn limira_native:app --host 0.0.0.0 --port 8080 --reload
```

然后启动前端：

```bash
LIMIRA_BACKEND_URL=http://127.0.0.1:8080 \
node apps/limira-standalone/server.mjs
```

注意：真实研究任务仍然需要 runner、数据库、Redis 和对象存储。单独启动后端只适合调试 API、登录、前端交互或使用测试替身。

## 用户入口

当前用户只应该访问：

```text
/limira
```

不要再把旧的通用应用页面作为产品入口。登录、历史聊天、上传资料、研究任务、PDF 导出和归档下载都在独立前端里完成。

## API 概览

后端统一挂载在：

```text
/api/limira
```

常用接口：

```text
POST /auth/signup
POST /auth/signin
POST /auth/signout
GET  /auth/session

GET  /scenarios
POST /research
GET  /tasks
GET  /tasks/{task_id}
GET  /tasks/{task_id}/events
GET  /tasks/{task_id}/artifacts
GET  /tasks/{task_id}/archive.zip

GET  /uploads
POST /uploads
GET  /uploads/search
GET  /uploads/{document_id}
GET  /uploads/{document_id}/download

POST /tasks/{task_id}/reports/pdf
GET  /tasks/{task_id}/reports/{report_id}/pdf

GET  /admin/tasks/{task_id}
GET  /admin/tasks/{task_id}/event-logs
GET  /admin/tasks/{task_id}/archive.zip
```

普通用户前端不展示事件日志；事件日志保留给后端和运维视角使用。

## PDF 导出

前端只有一个用户操作：

```text
导出并下载 PDF
```

它会先请求后端生成报告 PDF，再下载后端保存的同一份 PDF 文件，避免导出和下载分裂成两个用户动作。

后端优先使用 Playwright 渲染 PDF；如果不可用，会走文本 fallback。可以设置下面的变量保存 PDF 调试文件：

```bash
LIMIRA_PDF_DEBUG_DIR=/tmp/limira-pdf-debug
```

调试文件和本地运行产物不要提交到 Git。

## 数据与存储

生产式部署使用：

- Postgres：任务、报告、上传资料和事件日志
- Redis：运行时状态和流状态
- MinIO / S3：上传文件、报告 PDF、归档 zip
- SQLite：本地认证数据，默认位置由 `LIMIRA_AUTH_SQLITE_PATH` 控制

本地开发时可以用测试替身或内存后端，但不要在生产 Compose 里启用内存存储。

## 测试

常用后端和前端契约测试：

```bash
cd apps/limira-runner
UV_CACHE_DIR=/tmp/uv-cache uv run python -m py_compile \
  ../limira-web/backend/limira_native.py \
  ../limira-web/backend/limira_backend/routers/limira.py \
  tests/test_limira_web_routes.py \
  tests/test_limira_frontend_contract.py
```

```bash
cd apps/limira-runner
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/test_limira_deploy_contract.py \
  tests/test_limira_web_routes.py \
  tests/test_limira_frontend_contract.py \
  tests/test_task_store_and_auth.py \
  -q -k 'not test_limira_standalone_proxy_only_forwards_limira_api_namespace'
```

如果本机有 Node：

```bash
node --check apps/limira-standalone/public/app.js
```

如果环境允许本地 socket，再单独运行 standalone proxy 的 socket 契约测试。

## 日常开发流程

查看状态：

```bash
git status --short --branch
```

提交：

```bash
git add .
git commit -m "Describe the change"
```

推送到当前 GitHub 仓库：

```bash
git push
```

当前仓库 remote 应指向：

```text
https://github.com/shallwe16623/Limira.git
```

## 注意事项

- 不要提交 `.env`、数据库文件、虚拟环境、缓存、PDF 调试目录或本地运行产物。
- 用户入口只保留独立前端；旧的通用 UI 不应再作为产品入口。
- 前端只代理 `/api/limira/*`，不要扩大代理范围。
- 新增下载或导出能力时，必须检查用户归属、对象 key、文件内容和浏览器可见 payload。
- 新增事件类型时，如果它会产生用户可见成果，要同步更新前端 artifact 刷新契约测试。
