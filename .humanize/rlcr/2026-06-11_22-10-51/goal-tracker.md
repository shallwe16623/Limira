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
重构 Limira 原生 iOS 紧凑布局的移动端 shell，让 iPhone 上的工作台、菜单、设置入口、云盘、已归档对话、单位管理、历史搜索、历史文件和成果页拥有清晰的移动端层级模型。

### Acceptance Criteria
<!-- Each criterion must be independently verifiable -->

1. 紧凑移动端存在单一 shell 状态源：workspace、菜单、历史文件、历史搜索、云盘、已归档对话、单位管理和成果详情都由同一个 compact presentation/route 状态协调；切换 destination 时关闭旧 overlay/sheet。
2. Drawer/Menu 使用独立移动端 presentation 层：菜单不再和 workspace 同级堆在旧 `ZStack` 内，打开时不会被 workspace toast、composer、滚动条或键盘控件穿透覆盖。
3. 子页面具备 iOS 导航语义：云盘、已归档对话、单位管理从菜单进入时表现为明确页面跳转，返回 workspace 后保留当前对话、草稿、任务状态和 composer。
4. Composer 与内容区不再靠 magic padding 防遮挡：消息区通过安全区和 composer intrinsic layout 留空间，不再依赖 `.padding(.bottom, 220)` 或同类大数值避让。
5. 点击区域和可访问性稳定：主要入口都有 accessibility identifier，UI test 能通过 `isHittable` 稳定点击，视觉区域和 hit-test 区域一致。
6. Toast/状态提示不再跨 route 乱盖：状态提示挂在当前 route 的合适层级，workspace toast 不覆盖 menu，子页面提示不盖返回按钮，同时保留 UI test probe。
7. 后端和数据边界保持不变：默认连接 `https://limira-inc.com`，继续复用 `/api/limira/*`，本地只保存 Keychain token、选中组织、轻量 UI 状态和草稿。

---

## MUTABLE SECTION
<!-- Update each round with justification for changes -->

### Plan Version: 1 (Updated: Round 0)

#### Plan Evolution Log
<!-- Document any changes to the plan with justification -->
| Round | Change | Reason | Impact on AC |
|-------|--------|--------|--------------|
| 0 | Initial plan anchored from `docs/plans/limira-ios-compact-shell-navigation-plan.md` | Start RLCR with tracked plan file | None |

#### Active Tasks
<!-- Mainline tasks only: each task must directly advance the current round objective and carry routing metadata -->
| Task | Target AC | Status | Tag | Owner | Notes |
|------|-----------|--------|-----|-------|-------|
| Replace scattered compact route/sheet booleans with a single compact shell presentation state | AC-1 | pending | coding | claude | Mainline foundation for route and overlay cleanup |
| Move drawer/menu out of workspace `ZStack` into an independent mobile presentation | AC-2, AC-6 | pending | coding | claude | Must prevent toast/composer/workspace scroll content from overlaying menu |
| Convert cloud drive, archived chats, enterprise admin, and artifact destinations to mobile navigation destinations | AC-3 | pending | coding | claude | Preserve workspace task, messages, draft, and composer when returning |
| Rework composer/message safe-area layout and remove magic bottom padding | AC-4 | pending | coding | claude | Keep keyboard, selected-file chips, voice state, and composer from covering content |
| Stabilize hit testing and accessibility identifiers for compact controls and rows | AC-5 | pending | coding | claude | UI tests should be able to click visible controls reliably |
| Preserve online backend/data boundaries while refactoring compact UI | AC-7 | pending | coding | claude | No backend route changes, no local user database, no WebView wrapper |

### Blocking Side Issues
<!-- Only issues that directly block current mainline progress belong here -->
| Issue | Discovered Round | Blocking AC | Resolution Path |
|-------|-----------------|-------------|-----------------|

### Queued Side Issues
<!-- Non-blocking issues stay queued and must NOT replace the round objective -->
| Issue | Discovered Round | Why Not Blocking | Revisit Trigger |
|-------|-----------------|------------------|-----------------|
| BitLesson selector returned placeholder text because the project BitLesson file contains only the entry template and no real lessons | 0 | Round 0 can continue because there are no actual lessons to apply; summary will record Lesson ID(s): NONE | Revisit if future rounds add real BitLesson entries or selector output remains invalid with real entries |

### Completed and Verified
<!-- Only move tasks here after Codex verification -->
| AC | Task | Completed Round | Verified Round | Evidence |
|----|------|-----------------|----------------|----------|

### Explicitly Deferred
<!-- Items here require strong justification -->
| Task | Original AC | Deferred Since | Justification | When to Reconsider |
|------|-------------|----------------|---------------|-------------------|
