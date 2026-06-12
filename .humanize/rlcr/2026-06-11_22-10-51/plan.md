# Limira iOS 紧凑移动端 Shell 与导航计划

## Goal Description

重构 Limira 原生 iOS 紧凑布局的移动端 shell，让 iPhone 上的工作台、菜单、设置入口、云盘、已归档对话、单位管理、历史搜索、历史文件和成果页拥有清晰的移动端层级模型。

当前问题集中在 `CompactChatWorkspaceView`：workspace、drawer、遮罩、toast、sheet、route switch 和 composer padding 混在同一个视图层级里，导致页面跳转像内容替换，抽屉滚动层级穿帮，toast 覆盖 drawer，按钮视觉区域和可点击区域不一致。计划目标是用单一 presentation state 和 SwiftUI 原生导航语义替代这些局部 bool 和手写 overlay，同时保持现有线上 API、Keychain token、轻量 UI 状态和草稿保存策略不变。

## Acceptance Criteria

- AC-1: 紧凑移动端存在单一 shell 状态源
  - Positive Tests (expected to PASS):
    - Unit test 验证 `AppViewModel` 或新的 compact shell state 能表达 workspace、菜单、历史文件、历史搜索、云盘、已归档对话、单位管理、成果详情这些状态。
    - 打开云盘、已归档对话或单位管理时，菜单、历史文件 sheet、历史搜索 sheet 都会被关闭。
    - 执行“新研究”后回到 workspace，并清空当前任务、成果展示状态、选中文件、下载面板和临时 overlay。
  - Negative Tests (expected to FAIL):
    - 若 `sidebarPresented`、`historyFilesPresented` 继续作为 `CompactChatWorkspaceView` 的局部 `@State` 持有主要展示状态，测试应失败。
    - 若 route 切换后仍能保持旧 drawer 或旧 sheet 打开，测试应失败。

- AC-2: Drawer/Menu 使用独立移动端 presentation 层
  - Positive Tests (expected to PASS):
    - 点击顶部菜单按钮后，菜单以 `.sheet`、`.fullScreenCover` 或等价的独立全屏 presentation 展示，而不是和 workspace 同级堆在同一个 `ZStack` 内。
    - 菜单有固定 header、可滚动内容区和由 `safeAreaInset(edge: .bottom)` 管理的账号/设置区域。
    - 菜单打开时，workspace 背景不会把滚动条、toast、composer 或键盘控件穿到菜单之上。
  - Negative Tests (expected to FAIL):
    - 若 `StatusMessage` 可以覆盖菜单内容，测试应失败。
    - 若菜单仍依赖 workspace 内的 `Color.black.opacity` 遮罩和 `GeometryReader` 抽屉组合，测试应失败。

- AC-3: 子页面具备 iOS 导航语义
  - Positive Tests (expected to PASS):
    - 云盘、已归档对话、单位管理从菜单进入时表现为明确的页面跳转：有页面标题、返回工作台/返回菜单能力和刷新入口。
    - 返回 workspace 后保留当前对话内容、输入草稿、任务状态和可继续操作的 composer。
    - 单位管理仅对企业管理员显示入口；非企业管理员无法通过 UI 进入。
  - Negative Tests (expected to FAIL):
    - 若 `CompactChatWorkspaceView` 继续通过 `switch model.compactRoute` 直接把 workspace 内容替换成 `CompactCloudDriveView`、`CompactArchivedHistoryView` 或 `CompactEnterpriseAdminView`，测试应失败。
    - 若返回子页面会误清空当前任务或输入草稿，测试应失败。

- AC-4: Composer 与内容区不再靠 magic padding 防遮挡
  - Positive Tests (expected to PASS):
    - 对话滚动区只通过 `safeAreaInset`、键盘安全区和稳定布局约束给 composer 留空间。
    - 已选历史文件 chips、录音/转写状态、上传菜单、键盘展开收起都不会遮住最后一条消息或成果按钮。
    - iPhone 16、iPhone 17 Pro 模拟器的纵向布局均能看到底部 composer，滚动到底部时最后一条消息完整可见。
  - Negative Tests (expected to FAIL):
    - 若仍存在 `.padding(.bottom, 220)` 或同类大数值手动避让 composer，测试应失败。
    - 若键盘弹出后 composer 覆盖消息内容或 footer 文案，UI test 应失败。

- AC-5: 点击区域和可访问性稳定
  - Positive Tests (expected to PASS):
    - 菜单按钮、关闭按钮、新研究、刷新、历史搜索、归档/恢复/删除、云盘、单位管理、上传文件、历史文件、语音、发送和成果入口都有稳定 accessibility identifier。
    - UI test 使用 `isHittable` 点击这些入口时能触发对应状态变化或 API mock 调用。
    - 自绘 row 使用明确的 `Button`、`contentShape(Rectangle())` 或等价 hit-test 处理，视觉区域和点击区域一致。
  - Negative Tests (expected to FAIL):
    - 若元素存在但不可点击，或点击文字能触发而点击同一行空白不能触发，测试应失败。
    - 若主要入口缺少 accessibility identifier，测试应失败。

- AC-6: Toast/状态提示不再跨 route 乱盖
  - Positive Tests (expected to PASS):
    - 状态提示展示在当前 route 的合适区域：workspace toast 不覆盖 menu，menu 内操作提示不压到底部账号区，子页面提示不盖返回按钮。
    - 登录成功、刷新失败、转写失败、下载失败等状态仍能被用户看到。
    - UI test probe 仍能读取状态文本，便于自动化校验。
  - Negative Tests (expected to FAIL):
    - 若 `ContentView` 对登录后整个 app 做全局底部 toast overlay 并覆盖所有 compact presentations，测试应失败。
    - 若迁移后状态提示完全消失或 UI test 无法探测，测试应失败。

- AC-7: 后端和数据边界保持不变
  - Positive Tests (expected to PASS):
    - API base URL 仍默认 `https://limira-inc.com`。
    - 登录、任务历史、归档、云盘、单位管理、上传、历史文件、成果下载继续复用现有 `/api/limira/*` 客户端方法。
    - 本地只保存 Keychain token、选中组织、轻量 UI 状态和草稿；不新增本地用户数据库。
  - Negative Tests (expected to FAIL):
    - 若实现引入本地用户数据存储、切到本地后端作为真实调试目标，或新增后端路由作为前提，测试/检查应失败。

## Path Boundaries

### Upper Bound (Maximum Scope)

完成紧凑移动端 shell 的整体重构：新增或替换为 `CompactShellView`，集中管理 route 和 presentation state；将 drawer/menu 改为独立 presentation；将云盘、已归档对话、单位管理、成果详情纳入 `NavigationStack` 或等价移动端导航；重做 composer 安全区；补齐相关单元测试和 UI tests；在模拟器和 iPhone 16 真机上做 smoke test。

### Lower Bound (Minimum Scope)

至少解决截图暴露的层级 bug：菜单打开时 toast、composer、workspace 滚动条不再穿到菜单上；route 切换会关闭临时 overlay；子页面不再是同层 `switch` 内容替换；去掉 composer 的大数值底部 padding；主要按钮在 UI test 中稳定可点。

### Allowed Choices

- Can use:
  - SwiftUI `NavigationStack`、`.sheet`、`.fullScreenCover`、`safeAreaInset`、`toolbar`、`navigationDestination`。
  - 一个新的 compact presentation enum，例如 `CompactPresentationState`、`CompactModal` 或 `CompactDestination`。
  - 针对 `AppViewModel` 的状态机单元测试，以及使用 mock API 的 UI tests。
  - 保留现有 `LimiraAPIClient`、Keychain、upload/history/enterprise/download API。
- Cannot use:
  - WebView 包壳替代原生 SwiftUI。
  - 修改后端 API 或要求新增后端路由。
  - 使用本地用户数据库复制服务器用户数据。
  - 为了修紧凑移动端问题而重写桌面/iPad `NavigationSplitView` 主流程。

## Dependencies and Sequence

### Milestones

1. Milestone 1: 建立 compact shell 状态模型
   - Phase A: 梳理 `CompactWorkspaceRoute`、`compactShowingArtifacts`、`historySearchPresented`、局部 `@State` sheet/drawer 的职责。
   - Phase B: 新增一个集中 presentation 状态，覆盖 menu、history files、history search、destination route、artifact detail。
   - Phase C: 为 route 切换、新研究、退出登录、登录恢复写状态清理测试。

2. Milestone 2: 重做菜单 presentation
   - Phase A: 将 `CompactSidebarDrawer` 从 workspace `ZStack` 内移出，改成独立 sheet/full-screen cover 或同等封装。
   - Phase B: 菜单内部改成 header、scroll content、bottom account/settings safe area 三段式布局。
   - Phase C: 移除旧遮罩、`GeometryReader` 抽屉和会穿透的全局 toast 覆盖关系。

3. Milestone 3: 重建子页面导航
   - Phase A: 云盘、已归档对话、单位管理变为 compact shell destination，而不是 workspace 内容 switch。
   - Phase B: 每个子页面统一页面标题、返回工作台、刷新和权限状态。
   - Phase C: 确保返回后工作台状态、任务消息、草稿和 composer 不被重置。

4. Milestone 4: 修 composer 和 transient panels
   - Phase A: 去掉 `.padding(.bottom, 220)` 及同类 magic padding。
   - Phase B: 用 `safeAreaInset`、固定 composer intrinsic layout 和键盘交互规则处理消息区底部空间。
   - Phase C: 上传菜单、历史文件面板、语音状态和已选文件 chips 接入统一 presentation state。

5. Milestone 5: 稳定 hit-test 与自动化
   - Phase A: 为主要入口补齐 accessibility identifier。
   - Phase B: 对自绘 row 补 `contentShape` 或改成明确按钮样式。
   - Phase C: 更新 UI tests 覆盖菜单打开/关闭、子页面进入返回、历史搜索、历史文件、语音、成果跳转、归档入口。

6. Milestone 6: 验证与真机检查
   - Phase A: 运行 `xcodebuild -project apps/limira-ios/Limira.xcodeproj -scheme Limira -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build`。
   - Phase B: 运行 `xcodebuild -project apps/limira-ios/Limira.xcodeproj -scheme Limira -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test`。
   - Phase C: 安装到 iPhone 16，企业账号登录后核对菜单、历史、云盘、单位管理、语音、上传、成果和归档入口的移动端交互。

## Implementation Notes

- 代码里不要使用 `AC-1`、`Milestone 1` 等计划术语命名类型或变量。
- 保留网页端移动交互作为产品参照，但实现继续使用原生 SwiftUI。
- 先稳定 shell、route、presentation 和安全区，再补视觉细节；不要在旧 `ZStack + Bool + padding` 模型上继续局部打补丁。
- 测试应该验证用户能感知到的层级和跳转结果，而不只验证某个 bool 被设置。
- 所有线上调试仍连接 `https://limira-inc.com`；涉及删除历史、创建成员、上传文件等有副作用操作时，应使用 mock 或先得到用户明确同意。
