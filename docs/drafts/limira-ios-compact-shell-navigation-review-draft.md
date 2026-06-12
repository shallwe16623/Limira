# Limira iOS 紧凑移动端层级与导航 Review Draft

## 背景

本次是静态 review，没有改代码。结论很直接：现在 iOS 端不是“页面跳转细节不够顺”，而是移动端 shell 的层级模型错了。截图里的交叠问题能从代码里解释出来。

## Findings

### P1: 抽屉是手写 overlay，但没有独立层级边界

遮罩、抽屉、toast、滚动内容互相穿插。

`apps/limira-ios/Limira/Views.swift:470` 里 `CompactChatWorkspaceView` 用一个 `ZStack` 同时放 workspace、黑色遮罩、`GeometryReader` drawer；但没有 `zIndex`，也没有把 drawer 做成独立 full-screen modal/sheet。

外层 `apps/limira-ios/Limira/Views.swift:19` 的 `ContentView` 又全局 overlay 了 `StatusMessage`，所以 toast 会盖到 drawer 上，正是截图底部“已登录：Limira 管理员”浮在抽屉内容上的原因。

### P1: Drawer 内部布局不符合 iOS 直觉

一个 `ScrollView` 加固定底部账号栏，滚动条和内容层级会穿帮。

`apps/limira-ios/Limira/Views.swift:1073` 的 drawer 主体是 `ScrollView`，底部账号区域在同一个 `VStack` 里固定显示，外层还 `ignoresSafeArea(edges: .bottom)`。在长历史列表时，滚动条贴在右侧、底部账号/归档区/背景灰层容易视觉重叠。

移动端更适合：drawer 全屏 `NavigationStack` 或 `.sheet`，顶部固定、内容滚动、底部账号区用 `safeAreaInset` 管。

### P1: 路由状态和临时覆盖层状态分散

页面跳转没有“单一真相”。

`apps/limira-ios/Limira/AppViewModel.swift:292` 有 `compactRoute`，但 drawer 展开状态是 `@State sidebarPresented` 留在 view 里，history file sheet 又是另一个 `@State`，search sheet 是 model 状态。

网页端则明确把 `route`、`sidebarOverlayOpen`、`historySearchModalOpen` 分开渲染，并在切回 workspace 时关闭 overlay。iOS 现在容易出现：进入云盘/归档/单位管理时，旧的 overlay/sheet/toast 没被统一清理。

### P2: 子页面像“替换当前内容”，但缺少移动端导航栈语义

`apps/limira-ios/Limira/Views.swift:507` 直接 switch 到 `CompactCloudDriveView` / `CompactArchivedHistoryView` / `CompactEnterpriseAdminView`。这不是 iOS 用户熟悉的 push、sheet、fullScreenCover，返回也只是 `openCompactRoute(.workspace)`。

结果是页面跳转感觉像内容闪替，不像真正进入了一个页面。

### P2: 工作台 composer 与内容区用手动大 padding 规避遮挡

`apps/limira-ios/Limira/Views.swift:493` conversation scroll 底部 padding 被硬设为 `220`，同时 composer 用 `safeAreaInset`。这会在不同 iPhone 尺寸、键盘状态、语音状态、已选文件 chips 出现时继续漂。

正确做法是让 composer 只通过 `safeAreaInset` 占位，不再靠 magic number。

### P2: 很多按钮用了 `.plain` 或自绘容器，hit-test 不稳定

例如 drawer、history row、settings button 分布在 `apps/limira-ios/Limira/Views.swift:1058` 和后面多处。之前 UI test 里出现“元素存在但点了不触发”的现象，不是偶发：SwiftUI plain button + ScrollView + overlay 组合很容易让可点击区域和视觉区域不一致。

## 建议方向

不要继续在当前 `ZStack + Bool + padding` 上补丁式修。应该重做一个 `CompactShellView`：

- `workspace` 是根页面。
- drawer 是单一 `.sheet` 或自定义 full-screen cover。
- 云盘、归档、单位管理是 `NavigationStack` push/full-screen route。
- toast 挂在当前 route 内，而不是全局盖全 app。
- 所有临时面板统一由一个 `CompactPresentationState` 管。

这样才能把截图里的层级问题一次性解决，而不是今天修 drawer，明天又被 toast、sheet、keyboard、QuickLook 打穿。
