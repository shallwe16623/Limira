# Deep Research Harness Follow-Up Draft

## 结论

哈哈，可以报喜，但建议团队内部说法要准一点：**现在 `dev` 已经明显更接近 Deep Research harness 了，尤其 Runner/TaskContext/Artifact/Checkpoint 这些“产品 harness 地基”很像样；但执行内核还没有完全变成成熟 graph-native Deep Research。**

我的判断：**框架壳 7/10，真实 graph-native research kernel 4.5-5/10，benchmark harness readiness 6/10。** 也就是说：方向对了，地基真有了，但还不能说已经是完整 DeepResearch harness。

## Findings

### [P1] 并发任务可能串日志/工具状态

`pipeline_helpers.py` 会把 `main_agent_tool_manager` / `output_formatter` 放进全局 `_preload_cache`，后续每个任务都复用它们；但 `pipeline.py` 每次任务都会对同一个 ToolManager 调 `set_task_log()`。ToolManager 自身有可变 `task_log` 和 `browser_session`。两个长任务同时跑时，后启动的任务可能覆盖前一个任务的日志归属，trace/evidence/browser session 都有串线风险。

给 Humanize：缓存 `cfg` 和 tool definitions 可以，但每个 runner task 应创建 fresh ToolManager/OutputFormatter，或实现明确的 per-task clone；补一个并发双任务测试，证明两个任务的 tool logs/evidence ledger 不互相污染。

### [P1] LangGraph 路径已经实现，但目前不是默认/产品可路由路径

`pipeline.py` 支持 `legacy | serial | langgraph`，但默认 config 没有 `agent.research_graph.executor`；`pipeline_helpers.py` 又是 `load_limira_config(None)` 一次性预加载，所以生产默认大概率还是 legacy。

给 Humanize：加 `LIMIRA_RESEARCH_GRAPH_EXECUTOR=legacy|serial|langgraph` 或 staging config；Runner status/archive 里明确记录 selected executor；加端到端测试证明 Web 创建任务后实际进入 `langgraph`。

### [P1] LangGraph retriever 仍有 placeholder 成分，benchmark 口径要小心

`WebSearchRetriever` 现在只产出 snippet candidate，没有真实 retrieve；`PageVisitOrJinaSummaryRetriever` 会生成 deterministic `jina://...` summary；拿不到内容时再落回 legacy adapter。这意味着 LangGraph 现在更像“graph harness + retriever skeleton + legacy fallback”，还不是完整自主 deep search/retrieve harness。

给 Humanize：把 search/Jina/scrape 工具真实接进 retriever registry；snippet 只能 candidate，scrape/Jina/upload chunk 才能 evidence；加 fake search/scrape integration test，禁用 legacy fallback 时仍能完成一个 source-backed report。

### [P2] verifier 现在还是规则化浅验证，不足以支撑高分 Deep Research

`_verification_for_finding` 基本是“有合法 evidence ref 就 supported”，矛盾检测也主要靠关键词 polarity。这对 offline eval 够用，但对真实研究会把“相关证据”误当“支持证据”。

给 Humanize：升级成 claim-evidence entailment 层：每个 claim 需要 evidence span、支持类型、反证、时间口径；补 negative eval，比如证据提到同一实体但不支持结论、日期过期、来源只说明背景不说明判断。

### [P2] 失败路径上 LLM client 可能不关闭

`pipeline.py` 创建 `ClientFactory`，但只在成功路径 close；如果 graph/orchestrator 抛异常，client 不会 close。长任务失败多了会有连接/资源泄漏。

给 Humanize：`llm_client = None`，在 `finally` 里 close；加一个 orchestrator 抛错测试，断言 close 被调用。

### [P2] resume 能恢复状态，但最终 archive 的完整时间线还需要确认

Runner 可以把 stale LangGraph task 重新排队，checkpoint 会恢复 source/evidence ledgers。但 archive writer 每次 start 会重置本次 writer events。如果任务进程崩过一次，最终 `trace.json` 是否包含崩溃前完整事件链，需要专门测。

给 Humanize：补 “resume 后 archive trace 包含 pre-resume durable events 或明确包含 restored checkpoint ledgers” 的测试，不要只测 status/checkpoint。

## 可以给团队的说法

现在可以说：**Limira 已经从“会跑研究任务的 agent demo”升级成“有 durable task、事件 replay、artifact archive、TaskContext、typed research graph、LangGraph 路由和 offline eval 的 Deep Research harness 雏形”。**

但更精确的边界是：**harness 地基已经接近 DeepResearch 项目，graph-native 执行内核还在半路上。** 下一轮 Humanize 如果把并发隔离、LangGraph 默认路由、真实 retriever、强 verifier、resume archive 审计补上，就会更像可以拿去打 xbench/DeepResearch bench 的系统。

## 我跑过的验证

- `tests/test_task_store_and_auth.py tests/test_runner_api.py`：57 passed
- `tests/test_deep_research_offline_eval.py`：17 passed
- `tests/test_research_graph.py tests/test_research_graph_pipeline.py`：55 passed

没有改代码；工作区仍只有原来的未跟踪 `TECHNICAL_HANDOFF.md`。
