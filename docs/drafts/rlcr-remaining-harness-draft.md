# Remaining Deep Research Harness Draft

The previous RLCR loops completed two foundations:

- Web -> Runner -> Agent `TaskContext` propagation.
- A feature-flagged serial research graph executor that preserves legacy fallback.

The remaining deep research harness work must not be treated as optional follow-up. A new RLCR plan should make these remaining items required before final completion:

- Evidence ID and report reference closure:
  - Web/report parsing must accept numeric `EVID-001` and hash-style `EVID-xxxxxxxxxxxx`.
  - Report artifacts and final reports must warn or block when they reference evidence IDs not present in the task evidence ledger.
  - Repeated references should be deduped while preserving first-seen order.

- Candidate vs evidence semantics:
  - Search snippets should become source candidates or low-confidence records, not fully verified evidence.
  - Content-bearing sources such as scrape, Jina summary, upload chunks, and parsed documents can become evidence items.
  - Artifact payloads should preserve source type, retrieved timestamp, content hash, tool name, URL, and confidence/source-state metadata.

- Uploaded documents as research sources:
  - Uploaded documents already enter TaskContext, but they need a deterministic source contract beyond prompt text.
  - The harness should expose upload-source candidates or evidence artifacts from attached document text where locally available.
  - When production retrieval is unavailable, the system must record that state and avoid claiming uploaded facts were retrieved.

- Durable runner recovery:
  - Task store already has atomic claim and terminal-state guards.
  - The runner still needs a deterministic stale-running reconciliation contract that identifies tasks stuck in `running` without active worker ownership/heartbeat and marks or recovers them safely.
  - Full production DB event log/checkpoint can remain a later production hardening item, but the minimum recovery contract must be executable and tested.

- Eval harness:
  - Add deterministic local tests or eval fixtures for `missing_ref`, `snippet_only`, `upload_doc`, `scenario_policy`, and `restart_recovery`.
  - These cases must run without live LLM, live search, or external API keys.

The new plan must keep legacy behavior and user/auth boundaries intact, avoid live-network tests, and preserve Runner/SSE/archive/frontend contracts. It should use multiple RLCR rounds if needed, but final completion is only valid after every required remaining acceptance criterion has passing deterministic tests.
