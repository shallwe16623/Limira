# Limira LangChain Research Workflow Upgrade Plan

## Objective

Move Limira from a single-agent research loop toward a mainstream LangChain/LangGraph-style workflow while preserving the existing product shell: authentication, ownership boundaries, task history, SSE, object storage, PDF/export, archives, enterprise metering, and browser-safe response shaping.

## Architecture Target

The target research workflow is:

```text
scope -> plan -> parallel research units -> evidence compression -> verification -> final report -> artifact reconciliation
```

The existing single main-agent executor remains the compatibility runner until each graph stage has durable state, tests, and user-visible value.

## Phase 1: Graph Contract And Compatibility Events

Add structured research graph models and deterministic bootstrap planning:

- `ResearchBrief`
- `ResearchPlan`
- `ResearchUnit`
- `EvidenceItem`
- `CompressedFinding`
- `VerifiedClaim`
- `ResearchGraphState`

Emit browser-safe graph bootstrap events before the existing single-agent executor starts:

- `research_brief_created`
- `research_plan_created`

Success criteria:

- Existing runner behavior and archives still pass current tests.
- The graph state is serializable and safe to persist later.
- The planner creates bounded research units without making network calls.

## Phase 2: Tool-Layer Evidence Ledger

Move evidence source creation out of model discretion and into tool execution:

- Search tools automatically create source records.
- Scrape tools attach fetched text/hash/snapshot metadata.
- Upload retrieval creates local-document evidence candidates.
- Model-created artifacts must reference existing evidence IDs when applicable.

Success criteria:

- Every report claim can be traced to source candidates or an explicit no-source warning.
- Artifact tabs remain model-friendly but become audit-backed.

## Phase 3: Real Research Units

Replace the compatibility single loop with explicit graph nodes:

- Scope node
- Planner node
- Research unit workers
- Evidence compressor
- Verifier
- Writer

Success criteria:

- Research units can run serially first, then in bounded parallel.
- SSE events expose phase transitions and unit progress.
- Task cancellation and resume still preserve terminal-state guarantees.

## Phase 4: Hybrid Local Retrieval

Make uploaded material a first-class source:

- Chunk uploaded documents by default.
- Add lexical + vector + rerank retrieval.
- Feed selected chunks into research units as evidence candidates.

Success criteria:

- Local documents influence plans and claims without relying on manual upload search.
- Dimension mismatch and provider failure paths remain fail-closed or lexical-fallback as currently tested.

## Phase 5: Verification And Quality Evals

Add an internal research-quality benchmark:

- Required claims
- Forbidden claims
- Required source domains
- Minimum source counts
- Claim-to-evidence ratio
- Citation grounding checks
- Cost and latency metrics

Success criteria:

- Prompt/model/tool changes can be evaluated beyond route-contract correctness.
- Regressions in source grounding block merges.

## Non-Goals For The First Pass

- Do not remove current task, archive, PDF, upload, frontend, or enterprise paths.
- Do not expose runner URLs or service tokens to the browser.
- Do not make SSE subscription responsible for product-facing authorization decisions.
- Do not introduce unbounded parallelism.
