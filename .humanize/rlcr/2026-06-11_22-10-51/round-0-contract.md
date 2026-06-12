# Round 0 Contract

## One Mainline Objective

Initialize the RLCR control files for the Limira iOS compact shell navigation refactor so future implementation rounds stay anchored to the tracked plan and do not drift into unrelated UI or backend work.

## Target ACs

- AC-1: 紧凑移动端存在单一 shell 状态源。
- AC-2: Drawer/Menu 使用独立移动端 presentation 层。

Round 0 only prepares these ACs for implementation. It does not claim code-level completion for either AC.

## Blocking Side Issues in Scope

None.

## Queued Side Issues Out of Scope

- BitLesson selector returned placeholder text because `.humanize/bitlesson.md` has only the template and no real lessons. This is non-blocking for Round 0 because there are no real lesson IDs to apply.
- Full compact shell implementation, SwiftUI view rewrites, UI tests, simulator builds, and iPhone 16 smoke testing are out of scope for Round 0 and belong to later implementation/review rounds.

## Round Success Criteria

- `goal-tracker.md` has a filled immutable goal, 3-7 independently verifiable acceptance criteria, mainline active tasks mapped to ACs, routing tags, and owners.
- `round-0-contract.md` exists and limits the round to RLCR initialization with 1-2 target ACs.
- No backend routes, user data storage behavior, or iOS app source code are changed in Round 0.
- Changes are committed locally with a descriptive message.
- `round-0-summary.md` is written with implementation notes, files changed, validation, remaining items, and BitLesson Delta.
