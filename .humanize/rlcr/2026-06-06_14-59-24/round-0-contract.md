# Round 0 Contract

## Mainline Objective

Implement the Phase 1 Iteration 1 Archive Writer and Secret Scrubber foundation for MiroThinker research archives, with fake-event tests proving that the writer can safely generate the required archive artifacts without invoking the real agent, LLM, search, scrape, or Open WebUI frontend paths.

## Target ACs

- AC-3: Archive Writer is an independent, fake-event-testable component that creates `trace.json`, `report.md`, `metadata.json`, `report.html`, and `archive.zip` under a backend-generated archive directory.
- AC-5: Secret scrubber recursively redacts sensitive keys and string values, including Authorization/Bearer/cookie/API-key patterns, before anything is written to archive artifacts.

## Blocking Side Issues In Scope

None known at round start.

## Queued Side Issues Out Of Scope

- Runner API start/status/SSE endpoints.
- Task store and trusted Open WebUI identity adapter.
- Archive download endpoint and user/admin permission checks.
- Open WebUI frontend integration.
- Gradio end-to-end smoke with a real research task.
- Old trace viewer support for the new `trace.json` format.
- `bitlesson-selector` command availability; current BitLesson selection is `NONE` because the KB is empty and no selector executable is exposed.

## Round Success Criteria

- `apps/gradio-demo/archive_writer.py` exists and provides an independent Archive Writer plus secret scrubber.
- Archive Writer skips heartbeat events, normalizes events with `type`, `timestamp`, and original payload, and writes scrubbed `trace.json`.
- Archive Writer writes scrubbed `metadata.json` with model `provider`, `model`, and `base_url_host` only.
- Archive Writer writes `report.md`, a safe escaped `report.html`, and an `archive.zip` containing only `trace.json`, `report.md`, `metadata.json`, and `report.html`.
- Failure/cancelled statuses produce diagnostic report content.
- Unit tests cover successful archive creation, heartbeat exclusion, secret redaction, path traversal rejection, HTML escaping, zip contents, and zip failure status behavior.
