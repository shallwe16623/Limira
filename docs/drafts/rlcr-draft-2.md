# Limira LangGraph Deep Research Agent Hardening Draft

下面这个 plan 是按“可以直接拆成 PR 执行”的粒度写的。目标不是再写一份架构愿景，而是把当前 Limira 从“serial graph harness + legacy agent adapter”推进到“成熟 deep research agent architecture”。

## Plan Title

Limira LangGraph Deep Research Agent Hardening Plan

## Goal Description

把现有 deep research 雏形升级为可配置、可恢复、可审计、可测试的 graph-native research agent。保留 legacy executor 作为 fallback，但新增 LangGraph executor 作为主路径，覆盖 planner、research unit、retriever、compressor、verifier、writer、reconciler，并让 scenario、upload documents、evidence ledger、checkpoint 都进入真实执行闭环。

## Current Baseline

当前已有：

```text
Web creates task + scenario + documents
-> Runner persists context / lease / checkpoint / events
-> Agent builds ResearchGraphState
-> feature flag enabled 时跑 serial graph executor
-> ResearchUnitNode 调 legacy orchestrator
-> compressor / verifier / writer 做规则化处理
```

目标改成：

```text
Web task context
-> Runner durable job harness
-> LangGraph StateGraph
-> Planner node
-> Retriever nodes: web / page / uploads
-> ResearchUnit workers
-> Evidence compressor
-> Claim verifier
-> Writer
-> Reconciler
-> strict artifact/evidence closure
```

## Acceptance Criteria

AC-1: Graph executor 成为明确产品路径。

`apps/limira-agent/src/core/pipeline.py` 必须支持：

```text
agent.research_graph.executor = legacy | serial | langgraph
```

Positive tests:
`legacy` 走旧 orchestrator；`serial` 走现有 serial executor；`langgraph` 走新 LangGraph executor。

Negative tests:
非法 executor 值启动失败或返回清晰配置错误。

AC-2: 引入 LangGraph state，但复用现有模型。

在 `apps/limira-agent/src/core/` 新增类似 `langgraph_executor.py` 或 `research_langgraph.py`。状态字段至少包含：

```text
task_id
query
scenario
source_policy
upload_scope
brief
plan
current_unit_id
research_units
retrieved_sources
source_candidates
evidence
findings
claims
verified_claims
report_sections
warnings
```

不要推翻 `research_graph.py`，优先复用 `ResearchGraphState`，必要时加小字段。

AC-3: Research unit 不再只是一整个 legacy call。

LangGraph 路径里 `ResearchUnitNode` 要拆成：

```text
for unit in plan.units:
    candidate_sources = retriever.search(unit)
    retrieved_sources = retriever.retrieve(candidate_sources)
    evidence_items = compressor.promote(retrieved_sources)
    findings = unit_synthesizer.summarize(evidence_items)
```

legacy orchestrator 可以作为 fallback retriever/researcher，但不能是唯一主实现。

AC-4: Retriever registry 落地。

新增 `RetrieverRegistry`，至少支持：

```text
web_search
page_visit_or_jina_summary
uploaded_document_search
legacy_agent_adapter
```

上传文档不能只靠启动时 `upload_scope.source_payloads` 注入；需要 agent 能按 query 主动检索上传资料。入口可以先做成内部 Python retriever，不一定第一版就 MCP 化。

AC-5: Evidence strict mode。

在 backend artifact 侧增加严格校验模式：

```text
limira.evidence.strict = warn | block
```

`warn` 保持当前行为；`block` 下 final report 如果引用不存在的 `EVID-*`，任务应失败或报告不进入 final archive。相关位置是 `limira_part_005.pyfrag` 的 final report evidence reference validation path。

AC-6: Verifier 从“有引用就 supports”升级。

当前 verifier 主要把 finding 包成 supports。第一版成熟 verifier 应输出：

```text
supported
contradicted
insufficient
weak
```

规则版先做：
没有 evidence -> insufficient
引用不存在 -> invalid_ref
只有 source_candidate -> weak
多个来源冲突 -> contradicted candidate
内容 bearing evidence -> supported candidate

第二版再接 LLM verifier。

AC-7: Checkpoint 不只是记录，要能 resume。

Runner 已经有 checkpoint 和 durable events，但 graph checkpoint 现在标记为 not resumable。LangGraph 路径需要实现：

```text
resume from last completed node
skip completed units
continue from current failed unit when safe
```

Acceptance:
杀掉 worker 后重启，任务不应只能标 failed；至少可从上一个 completed graph node 恢复。

AC-8: Writer 不能只拼固定模板。

Writer node 第一版可以继续模板化，但必须基于 verified claims，而不是原始 finding。报告结构建议：

```text
answer
key findings
evidence table
uncertainties
conflicts
source notes
```

每个 high-confidence claim 必须有 evidence refs。

AC-9: Offline eval 成为质量门。

扩展 `apps/limira-runner/tests/test_deep_research_offline_eval.py`，新增 case：

```text
case_resume_from_checkpoint
case_upload_search_used
case_contradiction_detected
case_strict_missing_ref_blocks_report
case_langgraph_executor_routes
case_scenario_changes_source_policy
case_snippet_only_cannot_support_claim
```

## Path Boundaries

Lower bound:
实现 LangGraph executor + retriever registry + strict evidence mode + 基础 verifier；默认仍可不开。

Upper bound:
LangGraph 成为 staging 默认路径，支持 checkpoint resume、upload search、claim verifier、report reconciler、offline eval 全绿。

Out of scope:
不要重写 Web UI；不要把 GPT Researcher 整包塞进来；不要推翻现有 Runner/Artifact/Postgres 结构。

## Milestones

1. PR-1: Config and Executor Routing
   改 `pipeline.py`，新增 executor enum：`legacy | serial | langgraph`。测试覆盖三条路径。

2. PR-2: LangGraph Skeleton
   新增 LangGraph StateGraph，节点先复用现有 `ScopeNode/PlannerNode/Compressor/Verifier/Writer/Reconciler` 逻辑，保证事件和 checkpoint 与现有 runner 兼容。

3. PR-3: Retriever Registry
   新增 retriever registry，先接 web search/page summary/upload search 三类。ResearchUnit 不再直接等同于一次 legacy orchestrator call。

4. PR-4: Evidence and Verifier Hardening
   实现 strict evidence mode；升级 verifier 支持 `supported/contradicted/insufficient/weak`；report/finding/claim 都校验 refs。

5. PR-5: Durable Resume
   让 LangGraph checkpoint 能恢复到最后 completed node；Runner stale recovery 优先尝试 resume，失败才 terminal failed。

6. PR-6: Product Rollout
   dev/staging 打开 `agent.research_graph.executor=langgraph`；production 先 shadow 或 serial fallback。

## Test Command

```bash
cd apps/limira-runner
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/test_research_graph.py \
  tests/test_deep_research_offline_eval.py \
  tests/test_runner_api.py \
  tests/test_limira_artifacts.py \
  -q
```

## Implementation Notes

优先别大改文件边界。`research_graph.py` 保留合同和模型；新 LangGraph 执行器放 sibling module。Runner 已经比较像成熟 harness，不要重写它，重点补 graph resume 和 executor routing。最关键的产品判断是：legacy agent 可以继续存在，但它应该退化成一个节点工具，而不是 deep research 的主脑。
