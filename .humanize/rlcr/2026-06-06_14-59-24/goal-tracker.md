# Goal Tracker

<!--
This file tracks the ultimate goal, acceptance criteria, and plan evolution.
It prevents goal drift by maintaining a persistent anchor across all rounds.

RULES:
- IMMUTABLE SECTION: Do not modify after initialization
- MUTABLE SECTION: Update each round, but document all changes
- Every task must be in one of: Active, Completed, or Deferred
- Deferred items require explicit justification
-->

## IMMUTABLE SECTION
<!-- Do not modify after initialization -->

### Ultimate Goal

Evolve the current MiroThinker Gradio Deep Research demo into a backend that Open WebUI can call, while generating a local, downloadable, diagnostic, secret-scrubbed archive for every research task. Phase 1 must prioritize a curl-testable Runner API and independent Archive Writer before any Open WebUI frontend work. The Gradio demo remains a development fallback, and the MiroThinker core agent, tool selection, search logic, model invocation logic, and existing `TaskLog`/`logs/api-server/task_*.json` behavior must not be rewritten or replaced.

### Acceptance Criteria
<!-- Each criterion must be independently verifiable -->
<!-- Claude must extract or define these in Round 0 -->

- AC-1: Gradio fallback and old trace logs remain compatible: `gradio_run()`, `stop_current()`, `build_demo()`, `stream_events_optimized()`, and `TaskLog.save()` behavior are preserved, and core agent/orchestrator/LLM/tool logic is not rewritten.
- AC-2: Runner API is independently curl/test-client verifiable: it can start tasks, stream events over SSE, report task status, and download archives without requiring Open WebUI frontend changes.
- AC-3: Archive Writer is an independent, fake-event-testable component that creates `trace.json`, `report.md`, `metadata.json`, `report.html`, and `archive.zip` under a backend-generated archive directory.
- AC-4: Archive contracts are strict and safe: `trace.json` omits heartbeat events, `metadata.json` only stores provider/model/base_url_host summaries, `report.md` uses existing render-state output or minimal diagnostics, `report.html` is safe to open, and `archive.zip` contains only the four expected relative files.
- AC-5: Secret scrubber recursively redacts sensitive keys and string values, including Authorization/Bearer/cookie/API-key patterns, before anything is written to archive artifacts.
- AC-6: Task ownership and archive download permissions are enforced through persisted task records; ordinary users cannot access others' archives, and admin access requires explicit checks.
- AC-7: Failed, cancelled, and archive-failed paths are diagnosable: failed/cancelled tasks still produce scrubbed diagnostic artifacts when possible, and archive creation failures set `archive_status: "failed"` without converting a completed research task into failed.

---

## MUTABLE SECTION
<!-- Update each round with justification for changes -->

### Plan Version: 1 (Updated: Round 0)

#### Plan Evolution Log
<!-- Document any changes to the plan with justification -->
| Round | Change | Reason | Impact on AC |
|-------|--------|--------|--------------|
| 0 | Initial plan | - | - |

#### Active Tasks
<!-- Mainline tasks only: each task must directly advance the current round objective and carry routing metadata -->
| Task | Target AC | Status | Tag | Owner | Notes |
|------|-----------|--------|-----|-------|-------|
| Task Store and trusted identity adapter | AC-2, AC-6 | pending | coding | claude | Phase 1 Iteration 2 |
| Runner API start/status/SSE | AC-2 | pending | coding | claude | Phase 1 Iteration 3 |
| Archive finalization and download endpoint | AC-2, AC-6, AC-7 | pending | coding | claude | Phase 1 Iteration 4 |
| Gradio fallback and old trace regression | AC-1 | pending | coding | claude | Phase 1 Iteration 5 |

### Blocking Side Issues
<!-- Only issues that directly block current mainline progress belong here -->
| Issue | Discovered Round | Blocking AC | Resolution Path |
|-------|-----------------|-------------|-----------------|

### Queued Side Issues
<!-- Non-blocking issues stay queued and must NOT replace the round objective -->
| Issue | Discovered Round | Why Not Blocking | Revisit Trigger |
|-------|-----------------|------------------|-----------------|
| `bitlesson-selector` command is not exposed in PATH and no BitLesson entries exist yet | 0 | BitLesson KB is empty, so selected lesson set is `NONE`; does not block implementation | Revisit if a selector script appears or lessons are added |

### Completed and Verified
<!-- Only move tasks here after Codex verification -->
| AC | Task | Completed Round | Verified Round | Evidence |
|----|------|-----------------|----------------|----------|
| AC-3, AC-5 | Archive Writer and Secret Scrubber | 0 | pending verification | `cd apps/gradio-demo && uv run pytest tests/test_archive_writer.py`; `uv tool run ruff@0.8.0 check archive_writer.py tests/test_archive_writer.py`; `uv tool run ruff@0.8.0 format --check archive_writer.py tests/test_archive_writer.py` |

### Explicitly Deferred
<!-- Items here require strong justification -->
| Task | Original AC | Deferred Since | Justification | When to Reconsider |
|------|-------------|----------------|---------------|-------------------|
