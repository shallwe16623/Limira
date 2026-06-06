# Open WebUI MiroThinker Smoke Checklist

This checklist is the real Open WebUI runtime gate for the installable Pipe Function in this directory.

Current status: externally blocked in this repository because no Open WebUI runtime, browser session, or Open WebUI source tree is present here. The fake-runner harness verifies the repository-owned integration contract, but it does not replace this manual runtime smoke.

## Prerequisites

- Open WebUI is running and reachable by an operator account.
- MiroThinker Runner API is running and reachable from the Open WebUI server.
- Runner API has `MIROTHINKER_SERVICE_TOKEN` configured.
- Runner API archive root is writable.
- Open WebUI can install a Function from `apps/open-webui-mirothinker/mirothinker_deep_research.py`.

## Install

1. In Open WebUI, open the admin Functions area.
2. Create or import a Function from `mirothinker_deep_research.py`.
3. Confirm Open WebUI detects it as a Pipe Function named `MiroThinker Deep Research`.
4. Enable the Function for the intended users or test workspace.

## Valve Configuration

Set these admin Valves:

- `RUNNER_BASE_URL`: internal Runner API URL, for example `http://mirothinker-runner:8081`.
- `RUNNER_SERVICE_TOKEN`: value matching Runner API `MIROTHINKER_SERVICE_TOKEN`.
- `DOWNLOAD_BASE_URL`: browser-reachable trusted proxy URL for archive downloads, or the Runner URL for local/dev use.
- `REQUEST_TIMEOUT_SECONDS`: start with `60`.

Do not expose `RUNNER_SERVICE_TOKEN` to browser JavaScript or user-configurable fields.

## Expected Observations

### User A Completed Research

1. Log in as ordinary user A.
2. Select `MiroThinker Deep Research`.
3. Submit a research query.
4. Confirm the message shows progress/status updates.
5. Confirm the final response includes `Research status: completed`.
6. Confirm `Download Trace ZIP` appears only after `archive_status == ready`.
7. Download the archive and confirm it contains only:
   - `trace.json`
   - `report.md`
   - `metadata.json`
   - `report.html`

### User B Non-Owner Denial

1. Log in as ordinary user B.
2. Attempt to access user A's task status, events, archive, or cancel route through the configured integration/proxy path.
3. Confirm user B receives not-found or denied behavior and does not receive task contents or archive bytes.

### Pending Archive Download Disabled

1. Run against a fake or controlled Runner task where `archive_status == pending`.
2. Confirm no archive URL is shown.
3. Confirm the response says archive download is disabled or pending.

### Failed Diagnostic Archive

1. Run against a fake or controlled Runner task ending with `status == failed` and `archive_status == ready`.
2. Confirm the final response includes `Research status: failed`.
3. Confirm the download text is `Download Diagnostic ZIP`.

### Cancelled Diagnostic Archive

1. Start a long-running task.
2. Trigger cancellation through the trusted server-side cancel path.
3. Confirm the final response includes `Research status: cancelled`.
4. Confirm the download text is `Download Diagnostic ZIP` when `archive_status == ready`.

## Result Recording

When this runtime smoke is executed, record:

- Open WebUI version.
- Runner API commit/version.
- Function artifact commit.
- User A result.
- User B denial result.
- Pending disabled result.
- Failed diagnostic result.
- Cancelled diagnostic result.
- Any deviations and links to logs/screenshots.
