# Codebase Risk Audit Hardening Plan

## Objective

Perform a focused codebase hardening pass on the current repository. The goal is to find and fix real, confirmed risks in reliability, security, permissions, persistence, deployment, tests, error handling, secret handling, path handling, concurrency, API contracts, and frontend interaction behavior.

This plan is not a feature expansion plan. Changes should be narrowly scoped to risks that can be confirmed from current code, tests, runtime behavior, or deployment configuration.

## Risk Review Scope

- Server authentication and authorization boundaries, including trusted service-token paths, user ownership checks, admin-only behavior, and request-body user spoofing.
- Browser-facing API contracts, especially whether internal service URLs, object keys, credentials, stack traces, or private identifiers can leak to clients.
- Download, upload, archive, PDF, object-storage, and file-path handling, including path traversal, unsafe zip members, stale object reuse, and unsafe content types.
- Secret scrubbing in logs, reports, archives, PDFs, metadata, tests, frontend-visible payloads, and persisted records.
- Task repository persistence and state consistency across in-memory, SQLite/local, and Postgres-backed implementations.
- Database migrations and repository SQL contracts, including owner scoping, vector dimensions, archive/report metadata, and write invalidation semantics.
- Runtime state and concurrency behavior around task streams, cancellation, terminal state handling, reconnects, and stale active-worker state.
- Runner integration boundaries, including service-token headers, event normalization, artifact persistence, final-answer persistence, and archive generation.
- Frontend interaction risks, including stale cache behavior, disabled/enabled button correctness, rendering safety, empty/error states, refresh behavior, and download/export flows.
- Docker, compose, and environment configuration risks that would make a deployed stack unavailable or unsafe by default.
- Tests that appear to pass while only checking source strings or smoke wrappers instead of exercising the risk boundary.

## Explicitly Out Of Scope

- Large UI redesigns, new research capabilities, new agent strategies, or new scenario workflows unless required to fix a confirmed risk.
- Broad refactors, framework migration, or replacement of core legacy UI, Runner, storage, or repository architecture.
- Cosmetic style-only changes, copyediting, naming-only changes, or formatting-only churn.
- Adding external services or changing production infrastructure assumptions beyond hardening existing configuration.
- Reading or printing secret values from local `.env` files, databases, cookies, tokens, or service credentials.
- Manual edits to Humanize RLCR loop state files.

## Priority Rules

- **P0 must fix:** Confirmed authentication bypass, cross-user data access, secret leakage, path traversal, unsafe arbitrary file access, broken deployed startup, data loss, or a bug that blocks the main limira workflow from completing safely.
- **P1 must fix:** Confirmed reliability, persistence, API contract, archive/PDF/download, frontend interaction, or concurrency/state bug with realistic user impact and a bounded fix.
- **P2 fix when low cost:** Defensive improvements, missing focused tests around known-sensitive boundaries, clearer error handling, minor deployment hardening, or non-blocking consistency gaps.
- **Do not fix:** Pure style concerns, speculative issues without evidence, large rewrites, or behavior changes that are not tied to a confirmed risk.

## Acceptance Criteria

- Confirmed P0/P1 risks found during the audit are fixed or explicitly documented as blocked with evidence and a safe next step.
- Any P2 fixes included are small, low-risk, and directly improve a reviewed hardening boundary.
- Each code change has a clear risk explanation, affected files, and verification evidence in the round summary.
- Existing behavior is preserved unless the current behavior is itself the confirmed risk.
- Browser-facing APIs remain under the limira namespace and do not expose internal Runner URLs, object keys, secrets, stack traces, cookies, service tokens, or raw provider credentials.
- User-scoped resources remain owner-isolated for tasks, artifacts, uploads, reports, PDFs, archives, and search results.
- Generated archives, reports, PDFs, and metadata remain scrubbed for known secret patterns.
- Repository persistence remains restart-safe for the configured local and production-style backends covered by tests.
- Relevant tests pass, and any environment-limited tests are clearly identified with the exact limitation.

## Testing Requirements

- Add or update focused tests for every confirmed P0/P1 fix when the behavior can be exercised locally.
- Prefer route-level, repository-level, storage-level, and contract tests that exercise real data flow over source-string-only checks.
- Run the smallest focused test set that proves the fix, plus a broader limira contract subset when the touched surface is shared.
- Run syntax/compile checks for edited Python and JavaScript files.
- Do not add tests that snapshot or print secrets, tokens, cookies, `.env` values, or Authorization headers.
- If full-suite execution is blocked by the environment, run the largest relevant non-blocked subset and document the blocker.

## Audit Method

1. Inspect current git state and classify pre-existing dirty changes before editing.
2. Review high-risk surfaces first: auth/user isolation, downloads/uploads/archive/PDF, persistence, event streams, secret scrub, and deployed config.
3. For each suspected issue, confirm with code reading, focused tests, or a local probe before editing.
4. Fix confirmed P0/P1 issues with minimal changes and focused regression coverage.
5. Keep queued or speculative issues documented without letting them replace confirmed fixes.
6. Commit each round with a descriptive message and write a concise summary with risks found, fixes made, tests run, and remaining blocked items.
