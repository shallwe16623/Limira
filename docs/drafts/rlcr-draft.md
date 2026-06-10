结论先说：目前云端 `dev` 分支已经比之前更像一个“研究产品 harness”了，尤其 Runner 现在有后台 task worker、事件 replay、历史会话这些壳子。但作为 deep research 项目，它还不是一个成熟的 deep research harness，更准确是：

```text
Web/Runner/Artifact/History 壳子不错
+ Agent 仍主要靠 legacy single-agent loop
+ research graph 目前更多是计划和 prompt 合同
+ 证据、上下文、上传资料、验证闭环还没真正贯通
```

**主要问题**

[P1] Harness 不是 durable long-running job harness。
Runner 现在用进程内 dict 保存 worker、事件 log、subscriber，并通过 `asyncio.create_task` 启动后台任务，见 [runner_api.py](/Users/shallwe/Documents/Limira-dev/apps/limira-runner/runner_api.py:49)、[runner_api.py](/Users/shallwe/Documents/Limira-dev/apps/limira-runner/runner_api.py:119)、[runner_api.py](/Users/shallwe/Documents/Limira-dev/apps/limira-runner/runner_api.py:446)。这对普通 demo 可以，但 deep research 往往是长任务：进程重启、部署滚动、worker 崩溃后，内存里的 event replay / worker ownership 会丢。建议加持久化 lease、heartbeat、startup reconciliation、checkpoint，最少也要让 task 能从 DB 状态恢复。

[P1] research graph 还没真正执行。
[research_graph.py](/Users/shallwe/Documents/Limira-dev/apps/limira-agent/src/core/research_graph.py:1) 自己说明 executor 仍是 compatibility single-agent loop；[pipeline.py](/Users/shallwe/Documents/Limira-dev/apps/limira-agent/src/core/pipeline.py:101) 创建 graph 后，[pipeline.py](/Users/shallwe/Documents/Limira-dev/apps/limira-agent/src/core/pipeline.py:131) 还是把 graph prompt 丢给 orchestrator。也就是说现在是：

```text
生成研究计划
-> 包装成一段 prompt
-> 交给单 agent 自己跑
```

还不是：

```text
planner node
-> researcher node
-> compressor node
-> verifier node
-> writer node
-> reconciler node
```

这里确实适合引入 LangGraph：先 feature flag 一个 `StateGraph`，保留 legacy fallback。

[P1] Web -> Runner -> Agent 的上下文合同断了。
Web request 已经有 `scenario`、`document_ids`、`conversation_id`，见 [limira_part_003.pyfrag](/Users/shallwe/Documents/Limira-dev/apps/limira-web/backend/limira_backend/routers/limira_parts/limira_part_003.pyfrag:769)，但传给 runner 后，runner task store 只保存 `query`，见 [task_store.py](/Users/shallwe/Documents/Limira-dev/apps/limira-runner/task_store.py:18)。真正执行时还把第三个 context 参数传成 `None`，见 [runner_api.py](/Users/shallwe/Documents/Limira-dev/apps/limira-runner/runner_api.py:289)，而 [pipeline_helpers.py](/Users/shallwe/Documents/Limira-dev/apps/limira-runner/pipeline_helpers.py:295) 也忽略这个参数。结果是 scenario 和上传资料只停在 Web 层，没有成为 agent 的研究策略输入。

[P1] evidence 闭环仍不稳。
搜索结果会被直接记录成 evidence，见 [limira_evidence.py](/Users/shallwe/Documents/Limira-dev/libs/limira-tools/src/limira_tools/limira_evidence.py:93)；工具生成的是 hash 型 `EVID-xxxxxxxxxxxx`，见 [limira_evidence.py](/Users/shallwe/Documents/Limira-dev/libs/limira-tools/src/limira_tools/limira_evidence.py:255)；但后端 Markdown 抽取只认数字型 `EVID-001`，见 [limira_part_005.pyfrag](/Users/shallwe/Documents/Limira-dev/apps/limira-web/backend/limira_backend/routers/limira_parts/limira_part_005.pyfrag:616)。这会导致报告引用、证据账本、归档审计对不上。

[P2] 上传文档还不是 agent source。
Web 后端有 attach/search upload 的能力，见 [limira_part_003.pyfrag](/Users/shallwe/Documents/Limira-dev/apps/limira-web/backend/limira_backend/routers/limira_parts/limira_part_003.pyfrag:1316)，但 agent 默认工具只有 web search、Jina、artifact recorder、Python，见 [default.yaml](/Users/shallwe/Documents/Limira-dev/apps/limira-agent/conf/agent/default.yaml:1) 和 [settings.py](/Users/shallwe/Documents/Limira-dev/apps/limira-agent/src/config/settings.py:40)。所以“上传资料参与研究”现在不是自动闭环。

[P2] 后端 artifact/report 归档过度依赖事件流消费。
后端是在 stream 事件时记录 artifact/report，见 [limira_part_005.pyfrag](/Users/shallwe/Documents/Limira-dev/apps/limira-web/backend/limira_backend/routers/limira_parts/limira_part_005.pyfrag:235)。如果前端或后端没有稳定消费 runner stream，Web 侧归档可能滞后。Runner 自己有 archive，但 Web artifact repo 和 UI history 的一致性需要一个 server-side ingestion worker，而不是只靠浏览器打开 SSE。

**修改建议**

第一步先修 harness 地基：给 Runner 加 `TaskContext`，字段至少包括 `query`、`scenario`、`conversation_id`、`upload_scope`、`source_policy`。存进 `TaskStore`，并一路传到 agent pipeline。

第二步修 evidence：拆成 `SourceCandidate` 和 `EvidenceItem`。搜索 snippet 只能是 candidate，网页访问、Jina summary、上传 chunk、PDF parse 之后才能晋升 evidence。报告保存前必须校验所有 `EVID-*` 是否真实存在。

第三步引入 LangGraph，但别一口吞太大。建议：

```text
ScopeNode
-> PlannerNode
-> ResearchUnitNode
-> EvidenceCompressorNode
-> VerifierNode
-> WriterNode
-> ReconcilerNode
```

先串行跑，再加 bounded parallelism；先规则 verifier，再加 LLM verifier。

第四步做 durable runner：任务 claim、worker heartbeat、checkpoint、event log 都进 DB。进程启动时扫描 `running` 但 heartbeat 过期的任务，标记 failed/retry/recover。

第五步补 eval harness，不只是跑通测试。至少加这些 case：

```text
missing_ref: 报告引用不存在的 EVID 必须 warning/block
snippet_only: 只有搜索摘要不能支撑 high-confidence claim
upload_doc: 上传文档里的事实必须能被自动检索并引用
scenario_policy: 不同 scenario 会改变 source priority
restart_recovery: runner 重启后任务状态可恢复
```

我的判断：Limira 现在最强的是产品壳和审计方向，最弱的是 deep research 的执行内核。下一阶段不要再堆 prompt，应该把 “状态、节点、证据、校验、恢复” 做实。这样它才会从“会研究的聊天 agent”变成“可审计、可恢复、可验证的 deep research system”。

本次我只做了代码 review，没有改文件，也没有跑测试。
