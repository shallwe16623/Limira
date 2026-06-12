import CoreLocation
import MapKit
import QuickLook
import SwiftUI
import UIKit
import UniformTypeIdentifiers

struct ContentView: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        Group {
            if model.user == nil {
                AuthView()
            } else {
                MainWorkspaceView()
            }
        }
        .overlay(alignment: .bottom) {
            if model.user == nil {
                StatusToastView(text: model.statusMessage)
                    .accessibilityHidden(true)
                    .allowsHitTesting(false)
            }
        }
        .background(alignment: .topLeading) {
            if AppConfiguration.isUITestProbeEnabled {
                UITestProbeHost()
            }
        }
    }
}

struct UITestProbeHost: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(spacing: 0) {
            probe("StatusMessage", model.statusMessage)
            probe("TaskStatusProbe", model.status)
            probe("SelectedArtifactTabProbe", model.selectedTab.rawValue)
            probe("CompactRouteProbe", model.compactRoute.rawValue)
            probe("CompactArtifactModeProbe", model.compactShowingArtifacts ? "artifacts" : "conversation")
            probe("VoiceStatusProbe", model.voiceMessage)
            probe("QueryDraftProbe", model.queryDraft)
        }
        .frame(width: 1, height: 7, alignment: .topLeading)
        .allowsHitTesting(false)
        .accessibilitySortPriority(-1)
    }

    private func probe(_ id: String, _ label: String) -> some View {
        Text(label.isEmpty ? " " : label)
            .font(.system(size: 1))
            .foregroundStyle(.clear)
            .lineLimit(1)
            .frame(width: 1, height: 1)
            .accessibilityElement(children: .ignore)
            .accessibilityIdentifier(id)
            .accessibilityLabel(label)
    }
}

struct AccessibilityProbe: View {
    var id: String

    var body: some View {
        Color.clear
            .frame(width: 1, height: 1)
            .accessibilityElement(children: .ignore)
            .accessibilityIdentifier(id)
    }
}

struct AuthView: View {
    @EnvironmentObject private var model: AppViewModel
    @FocusState private var focusedField: AuthField?
    @State private var identifier = ""
    @State private var password = ""
    @State private var signupEmail = ""
    @State private var signupUsername = ""
    @State private var signupName = ""
    @State private var verifyToken = ""
    @State private var resetEmail = ""
    @State private var resetToken = ""
    @State private var resetPassword = ""

    var body: some View {
        ScrollView {
            VStack(spacing: 32) {
                VStack(spacing: 0) {
                    Text("研究、证据与导出集中在一个工作台。")
                        .font(.system(size: 28, weight: .semibold))
                        .multilineTextAlignment(.center)
                        .lineLimit(3)
                        .minimumScaleFactor(0.72)
                        .foregroundStyle(.primary)
                        .frame(maxWidth: .infinity)
                }
                .padding(.top, 72)

                VStack(spacing: 20) {
                    Picker("账号类型", selection: $model.authScope) {
                        Text("个人方式登录").tag(AuthScope.personal)
                        Text("企业登录").tag(AuthScope.enterprise)
                    }
                    .pickerStyle(.segmented)
                    .accessibilityIdentifier("AuthScopePicker")

                    if model.authScope == .enterprise {
                        AuthLabeledControl(title: "单位类别") {
                            Menu {
                                ForEach(model.organizationCategoryOptions) { category in
                                    Button(category.label) {
                                        model.setSelectedOrganizationCategory(category.value)
                                    }
                                }
                            } label: {
                                AuthSelectLabel(title: selectedOrganizationCategoryLabel)
                            }
                            .accessibilityIdentifier("OrganizationCategoryPicker")
                        }

                        AuthLabeledControl(title: "单位") {
                            Menu {
                                if model.organizationsForSelectedCategory.isEmpty {
                                    Text("暂无可选单位")
                                } else {
                                    ForEach(model.organizationsForSelectedCategory) { organization in
                                        Button(organization.name) {
                                            model.selectedOrganizationId = organization.id
                                        }
                                    }
                                }
                            } label: {
                                AuthSelectLabel(title: selectedOrganizationName)
                            }
                            .accessibilityIdentifier("OrganizationPicker")
                        }
                    }

                    AuthLabeledControl(title: model.authScope == .enterprise ? "用户名" : "用户名") {
                        TextField("请输入用户名", text: $identifier)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .focused($focusedField, equals: .identifier)
                            .modifier(AuthInputModifier())
                            .accessibilityIdentifier("AuthIdentifierField")
                    }

                    AuthLabeledControl(title: "密码") {
                        SecureField("", text: $password)
                            .textInputAutocapitalization(.never)
                            .focused($focusedField, equals: .password)
                            .modifier(AuthInputModifier())
                            .accessibilityIdentifier("AuthPasswordField")
                    }

                    Button {
                        focusedField = nil
                        Task { await model.signIn(identifier: identifier, password: password) }
                    } label: {
                        Text(model.authScope == .enterprise ? "登录单位账号" : "登录")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(AuthPrimaryButtonStyle())
                    .disabled(model.isBusy)
                    .accessibilityIdentifier("SignInButton")

                    if model.authScope == .enterprise {
                        VStack(spacing: 14) {
                            Text("如需开通单位账号，请通过以下方式联系团队。")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)

                            HStack(spacing: 16) {
                                Link(destination: URL(string: "tel:+8617267052536")!) {
                                    Image(systemName: "phone")
                                        .frame(width: 40, height: 40)
                                }
                                .buttonStyle(AuthIconLinkStyle())
                                .accessibilityLabel("电话联系销售")

                                Link(destination: URL(string: "mailto:admin@limira-inc.com")!) {
                                    Image(systemName: "envelope")
                                        .frame(width: 40, height: 40)
                                }
                                .buttonStyle(AuthIconLinkStyle())
                                .accessibilityLabel("邮件联系销售")
                            }
                        }
                    }

                    if model.authScope == .personal {
                        PersonalAuthTools(
                            signupEmail: $signupEmail,
                            signupUsername: $signupUsername,
                            signupName: $signupName,
                            password: $password,
                            verifyToken: $verifyToken,
                            resetEmail: $resetEmail,
                            resetToken: $resetToken,
                            resetPassword: $resetPassword
                        )
                    }

                    if model.googleOAuth.enabled || model.wechatOAuth.enabled {
                        VStack(spacing: 10) {
                            if model.googleOAuth.enabled {
                                Button("使用 Google 登录") {}
                                    .buttonStyle(AuthSecondaryButtonStyle())
                            }
                            if model.wechatOAuth.enabled {
                                Button("使用微信登录") {}
                                    .buttonStyle(AuthSecondaryButtonStyle())
                            }
                        }
                    }
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 24)
            }
            .frame(maxWidth: 560)
            .padding(.horizontal, 24)
            .padding(.bottom, 40)
            .frame(maxWidth: .infinity)
        }
        .background(Color(.systemBackground))
        .overlay(alignment: .topTrailing) {
            if model.isBusy {
                ProgressView()
                    .padding()
            }
        }
        .task {
            await model.loadAuthOptions()
        }
    }

    private var selectedOrganization: LimiraOrganization? {
        model.organizationsForSelectedCategory.first { $0.id == model.selectedOrganizationId }
            ?? model.organizationsForSelectedCategory.first
    }

    private var selectedOrganizationName: String {
        selectedOrganization?.name.nonEmpty ?? "Limira"
    }

    private var selectedOrganizationCategoryLabel: String {
        model.organizationCategoryOptions.first { $0.value == model.selectedOrganizationCategory }?.label ?? "企业"
    }
}

private enum AuthField: Hashable {
    case identifier
    case password
}

private struct AuthLabeledControl<Content: View>: View {
    var title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct AuthSelectLabel: View {
    var title: String

    var body: some View {
        HStack {
            Text(title)
                .foregroundStyle(.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer()
            Image(systemName: "chevron.down")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.secondary)
        }
        .frame(minHeight: 50)
        .padding(.horizontal, 16)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color(.systemBackground))
                .stroke(Color(.separator).opacity(0.45), lineWidth: 1)
        )
    }
}

private struct AuthInputModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .font(.body)
            .frame(minHeight: 50)
            .padding(.horizontal, 16)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color(.systemBackground))
                    .stroke(Color(.separator).opacity(0.45), lineWidth: 1)
            )
    }
}

private struct AuthPrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline.weight(.medium))
            .foregroundStyle(.white)
            .frame(minHeight: 52)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color.black.opacity(isEnabled ? (configuration.isPressed ? 0.82 : 1) : 0.45))
            )
    }
}

private struct AuthSecondaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.medium))
            .foregroundStyle(.primary)
            .frame(maxWidth: .infinity)
            .frame(minHeight: 44)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color(.systemBackground))
                    .stroke(Color(.separator).opacity(isEnabled ? 0.45 : 0.2), lineWidth: 1)
            )
            .opacity(configuration.isPressed ? 0.75 : 1)
    }
}

private struct AuthIconLinkStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 18, weight: .medium))
            .foregroundStyle(.secondary)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color(.systemBackground))
                    .stroke(Color(.separator).opacity(0.45), lineWidth: 1)
            )
            .opacity(configuration.isPressed ? 0.75 : 1)
    }
}

private struct PersonalAuthTools: View {
    @EnvironmentObject private var model: AppViewModel
    @Binding var signupEmail: String
    @Binding var signupUsername: String
    @Binding var signupName: String
    @Binding var password: String
    @Binding var verifyToken: String
    @Binding var resetEmail: String
    @Binding var resetToken: String
    @Binding var resetPassword: String

    var body: some View {
        VStack(spacing: 12) {
            DisclosureGroup("注册") {
                VStack(spacing: 12) {
                    TextField("用户名", text: $signupUsername)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .modifier(AuthInputModifier())
                    TextField("邮箱", text: $signupEmail)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.emailAddress)
                        .autocorrectionDisabled()
                        .modifier(AuthInputModifier())
                    TextField("姓名", text: $signupName)
                        .modifier(AuthInputModifier())
                    Button("注册") {
                        Task {
                            await model.signUp(username: signupUsername, email: signupEmail, password: password, name: signupName)
                        }
                    }
                    .buttonStyle(AuthSecondaryButtonStyle())
                }
                .padding(.top, 12)
            }

            DisclosureGroup("邮箱与密码") {
                VStack(spacing: 12) {
                    TextField("验证 Token", text: $verifyToken)
                        .textInputAutocapitalization(.never)
                        .modifier(AuthInputModifier())
                    Button("验证邮箱") {
                        Task { await model.verifyEmail(token: verifyToken) }
                    }
                    .buttonStyle(AuthSecondaryButtonStyle())
                    TextField("重发验证邮箱", text: $resetEmail)
                        .textInputAutocapitalization(.never)
                        .modifier(AuthInputModifier())
                    HStack(spacing: 12) {
                        Button("重发验证邮件") {
                            Task { await model.resendVerification(email: resetEmail) }
                        }
                        .buttonStyle(AuthSecondaryButtonStyle())
                        Button("发送重置邮件") {
                            Task { await model.requestPasswordReset(email: resetEmail) }
                        }
                        .buttonStyle(AuthSecondaryButtonStyle())
                    }
                    TextField("重置 Token", text: $resetToken)
                        .textInputAutocapitalization(.never)
                        .modifier(AuthInputModifier())
                    SecureField("新密码", text: $resetPassword)
                        .modifier(AuthInputModifier())
                    Button("确认重置") {
                        Task { await model.confirmPasswordReset(token: resetToken, password: resetPassword) }
                    }
                    .buttonStyle(AuthSecondaryButtonStyle())
                }
                .padding(.top, 12)
            }
        }
        .font(.subheadline.weight(.medium))
    }
}

struct MainWorkspaceView: View {
    @EnvironmentObject private var model: AppViewModel
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @State private var previewURL: URL?

    var body: some View {
        if horizontalSizeClass == .compact {
            CompactShellView(previewURL: $previewURL)
            .quickLookPreview($previewURL)
        } else {
            NavigationSplitView {
                SidebarView()
            } detail: {
                WorkspaceDetailContent(previewURL: $previewURL)
                    .navigationTitle(model.selectedTask?.query.nonEmpty ?? "工作台")
            }
            .overlay(alignment: .bottom) {
                StatusToastView(text: model.statusMessage)
                    .accessibilityHidden(true)
                    .allowsHitTesting(false)
            }
            .quickLookPreview($previewURL)
        }
    }
}

struct StatusToastView: View {
    var text: String

    var body: some View {
        Group {
            if !text.isEmpty {
                Text(text)
                    .font(.footnote)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(.regularMaterial)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .padding()
                    .accessibilityIdentifier("StatusToast")
            }
        }
        .allowsHitTesting(false)
    }
}

struct CompactShellView: View {
    @EnvironmentObject private var model: AppViewModel
    @Binding var previewURL: URL?

    var body: some View {
        NavigationStack(path: compactPathBinding) {
            CompactWorkspaceScreen(previewURL: $previewURL)
                .navigationBarHidden(true)
                .navigationDestination(for: CompactShellDestination.self) { destination in
                    compactDestination(destination)
                }
        }
        .background(Color(.systemBackground))
        .overlay(alignment: .bottom) {
            if model.compactPresentation.modal == nil {
                StatusToastView(text: model.statusMessage)
                    .accessibilityHidden(true)
                    .allowsHitTesting(false)
            }
        }
        .fullScreenCover(isPresented: compactModalBinding(.menu)) {
            CompactMenuView()
        }
        .sheet(item: compactSheetBinding) { modal in
            switch modal {
            case .historyFiles:
                CompactHistoryFilesSheet()
                    .presentationDetents([.medium, .large])
            case .historySearch:
                CompactHistorySearchSheet()
                    .presentationDetents([.medium, .large])
            case .menu, .fileImporter:
                EmptyView()
            }
        }
        .fileImporter(isPresented: compactModalBinding(.fileImporter), allowedContentTypes: [.data], allowsMultipleSelection: false) { result in
            model.dismissCompactModal()
            handleImport(result)
        }
    }

    private var compactPathBinding: Binding<[CompactShellDestination]> {
        Binding(
            get: { model.compactPresentation.path },
            set: { model.setCompactDestinationPath($0) }
        )
    }

    private var compactSheetBinding: Binding<CompactShellModal?> {
        Binding(
            get: {
                switch model.compactPresentation.modal {
                case .historyFiles, .historySearch:
                    return model.compactPresentation.modal
                case .menu, .fileImporter, nil:
                    return nil
                }
            },
            set: { model.setCompactModal($0) }
        )
    }

    private func compactModalBinding(_ modal: CompactShellModal) -> Binding<Bool> {
        Binding(
            get: { model.isCompactModalPresented(modal) },
            set: { isPresented in
                if isPresented {
                    model.setCompactModal(modal)
                } else if model.isCompactModalPresented(modal) {
                    model.dismissCompactModal()
                }
            }
        )
    }

    @ViewBuilder
    private func compactDestination(_ destination: CompactShellDestination) -> some View {
        switch destination {
        case .cloudDrive:
            CompactCloudDriveView(previewURL: $previewURL)
                .navigationBarHidden(true)
        case .archivedChats:
            CompactArchivedHistoryView()
                .navigationBarHidden(true)
        case .enterpriseAdmin:
            CompactEnterpriseAdminView()
                .navigationBarHidden(true)
        case .artifacts:
            CompactArtifactDestinationView(previewURL: $previewURL)
                .navigationBarHidden(true)
        }
    }

    private func handleImport(_ result: Result<[URL], Error>) {
        switch result {
        case .success(let urls):
            guard let url = urls.first else { return }
            Task {
                let scoped = url.startAccessingSecurityScopedResource()
                defer {
                    if scoped {
                        url.stopAccessingSecurityScopedResource()
                    }
                }
                await model.uploadDocument(url: url)
            }
        case .failure(let error):
            model.statusMessage = error.localizedDescription
        }
    }
}

struct CompactWorkspaceScreen: View {
    @EnvironmentObject private var model: AppViewModel
    @Binding var previewURL: URL?

    var body: some View {
        VStack(spacing: 0) {
            CompactWorkspaceHeader {
                model.presentCompactMenu()
            }
            Divider()
            ScrollView {
                CompactConversationCanvas(previewURL: $previewURL)
                    .padding(.horizontal, 20)
                    .padding(.bottom, 16)
            }
            .scrollDismissesKeyboard(.interactively)
            .safeAreaInset(edge: .bottom) {
                CompactComposer(
                    uploadAction: { model.presentCompactFileImporter() },
                    historyAction: { model.presentCompactHistoryFiles() }
                )
            }
        }
        .background(Color(.systemBackground))
    }
}

struct CompactArtifactDestinationView: View {
    @EnvironmentObject private var model: AppViewModel
    @Binding var previewURL: URL?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                ArtifactTabsView(compact: true, showBackButton: true)
                if let file = model.downloadedFile {
                    DownloadPanel(file: file, previewURL: $previewURL)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 24)
        }
        .scrollDismissesKeyboard(.interactively)
        .background {
            Color(.systemBackground)
            AccessibilityProbe(id: "CompactArtifactDestinationView")
                .allowsHitTesting(false)
        }
    }
}

struct CompactWorkspaceHeader: View {
    var openSidebar: () -> Void

    var body: some View {
        HStack(spacing: 18) {
            Button(action: openSidebar) {
                ZStack {
                    Rectangle()
                        .fill(Color(.systemBackground).opacity(0.001))
                    Image(systemName: "line.3.horizontal")
                        .font(.system(size: 22, weight: .semibold))
                }
                .frame(width: 52, height: 52)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(.primary)
            .accessibilityLabel("打开侧边栏")
            .accessibilityIdentifier("MainSidebarOpenButton")

            Text("limira OSINT")
                .font(.title3.weight(.bold))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer()
        }
        .padding(.horizontal, 20)
        .frame(height: 64)
        .background(Color(.systemBackground))
    }
}

struct CompactConversationCanvas: View {
    @EnvironmentObject private var model: AppViewModel
    @Binding var previewURL: URL?

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            if model.compactShowingArtifacts {
                ArtifactTabsView(compact: true, showBackButton: true)
                if let file = model.downloadedFile {
                    DownloadPanel(file: file, previewURL: $previewURL)
                }
            } else if hasConversationActivity {
                CompactMessageTimeline()
                if model.isStreaming || model.status != "ready" {
                    CompactThinkingStatus()
                }
                if let file = model.downloadedFile {
                    DownloadPanel(file: file, previewURL: $previewURL)
                }
            } else {
                Color.clear
                    .frame(minHeight: 520)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, hasConversationActivity ? 28 : 0)
    }

    private var hasConversationActivity: Bool {
        !model.messages.isEmpty
            || model.selectedTask != nil
            || model.status != "ready"
            || model.isBusy
            || model.isStreaming
            || !model.finalReportMarkdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

}

struct CompactMessageTimeline: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            ForEach(model.messages.suffix(80)) { message in
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: icon(for: message.role))
                            .font(.system(size: 20, weight: .semibold))
                            .foregroundStyle(color(for: message.role))
                            .frame(width: 28, height: 28)
                        VStack(alignment: .leading, spacing: 8) {
                            Text(message.text)
                                .font(.body)
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            CompactMessageActions(message: message)
                            if message.isReport {
                                CompactReportControls(message: message)
                            }
                        }
                    }
                }
            }
        }
        .accessibilityIdentifier("MessageTimeline")
    }

    private func icon(for role: AppMessage.Role) -> String {
        switch role {
        case .user: return "person.fill"
        case .assistant: return "sparkles"
        case .system: return "gearshape"
        case .error: return "exclamationmark.triangle.fill"
        }
    }

    private func color(for role: AppMessage.Role) -> Color {
        role == .error ? .red : .accentColor
    }
}

struct CompactMessageActions: View {
    @EnvironmentObject private var model: AppViewModel
    var message: AppMessage

    var body: some View {
        HStack(spacing: 14) {
            Button {
                UIPasteboard.general.string = message.text
                model.statusMessage = "已复制。"
            } label: {
                Label("复制", systemImage: "doc.on.doc")
            }
            .buttonStyle(.plain)

            if message.role == .user && message.id == latestUserMessageId {
                Button {
                    model.queryDraft = message.text
                } label: {
                    Label("修改后再次发送", systemImage: "arrow.uturn.backward")
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("ReuseLastUserMessageButton")
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private var latestUserMessageId: UUID? {
        model.messages.last(where: { $0.role == .user })?.id
    }
}

struct CompactReportControls: View {
    @EnvironmentObject private var model: AppViewModel
    var message: AppMessage

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 88), spacing: 8)], alignment: .leading, spacing: 8) {
                ForEach(model.compactArtifactTabs()) { tab in
                    Button {
                        model.activateCompactArtifacts(tab: tab, taskId: message.taskId)
                    } label: {
                        Text("\(tab.rawValue) \(count(for: tab))")
                            .font(.subheadline.weight(.medium))
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .background(Color(.secondarySystemBackground))
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .contentShape(Rectangle())
                    .accessibilityIdentifier("CompactArtifactControl-\(tab.rawValue)")
                }
                Button {
                    Task { await model.downloadArchive(taskId: message.taskId) }
                } label: {
                    Label(archiveTitle, systemImage: "archivebox")
                        .font(.subheadline.weight(.medium))
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(Color(.secondarySystemBackground))
                        .clipShape(Capsule())
                }
                .buttonStyle(.plain)
                .contentShape(Rectangle())
                .accessibilityIdentifier("DownloadArchiveButton")
            }
        }
        .accessibilityIdentifier("CompactReportControls")
    }

    private func count(for tab: ArtifactTab) -> Int {
        message.artifactCounts[tab.rawValue] ?? model.artifactCount(for: tab)
    }

    private var archiveTitle: String {
        let status = model.archiveStatus(for: message.taskId ?? "")
        return "归档 \(["pending": "等待中", "ready": "可下载", "failed": "失败"][status] ?? status)"
    }
}

struct CompactThinkingStatus: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: model.isStreaming ? "dot.radiowaves.left.and.right" : "circle.fill")
                .font(.caption)
            Text(statusLabel)
                .font(.subheadline.weight(.medium))
            if let taskId = model.selectedTask?.taskId {
                Text(taskId)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .accessibilityIdentifier("TaskStatusStrip")
    }

    private var statusLabel: String {
        [
            "starting": "启动中",
            "queued": "排队中",
            "running": "运行中",
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
            "stream reconnecting": "正在重连"
        ][model.status] ?? model.status
    }
}

struct CompactComposer: View {
    @EnvironmentObject private var model: AppViewModel
    @FocusState private var focused: Bool
    var uploadAction: () -> Void
    var historyAction: () -> Void

    var body: some View {
        VStack(spacing: 8) {
            if !model.selectedDocuments.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(model.selectedDocuments) { document in
                            HStack(spacing: 6) {
                                Image(systemName: "paperclip")
                                    .font(.caption)
                                Text(document.filename)
                                    .font(.caption.weight(.medium))
                                    .lineLimit(1)
                                Button {
                                    model.removeSelectedDocument(document.documentId)
                                } label: {
                                    Image(systemName: "xmark.circle.fill")
                                        .font(.caption)
                                }
                                .buttonStyle(.plain)
                                .accessibilityLabel("移除 \(document.filename)")
                                .accessibilityIdentifier("SelectedDocumentRemoveButton-\(document.documentId)")
                            }
                            .padding(.horizontal, 10)
                            .frame(height: 30)
                            .background(Color(.tertiarySystemBackground))
                            .clipShape(Capsule())
                            .accessibilityIdentifier("SelectedDocumentChip-\(document.documentId)")
                        }
                    }
                    .padding(.horizontal, 18)
                }
                .accessibilityIdentifier("SelectedDocumentChips")
            }

            HStack(alignment: .bottom, spacing: 8) {
                Menu {
                    Button(action: uploadAction) {
                        Label("上传文件", systemImage: "paperclip")
                    }
                    .accessibilityIdentifier("UploadFileMenuItem")
                    Button(action: historyAction) {
                        Label("引用历史文件", systemImage: "clock.arrow.circlepath")
                    }
                    .accessibilityIdentifier("HistoryFilesMenuItem")
                } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 22, weight: .regular))
                        .frame(width: 38, height: 38)
                        .background(Circle().fill(Color(.systemBackground)))
                        .overlay(Circle().stroke(Color(.separator).opacity(0.28), lineWidth: 1))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("添加")
                .accessibilityIdentifier("UploadMenuButton")

                ZStack(alignment: .topLeading) {
                    if model.queryDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Text("发送消息以开始 OSINT 研究...")
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5)
                            .padding(.top, 10)
                            .allowsHitTesting(false)
                    }
                    TextEditor(text: $model.queryDraft)
                        .focused($focused)
                        .font(.body)
                        .scrollContentBackground(.hidden)
                        .background(Color.clear)
                        .frame(height: 46)
                        .accessibilityIdentifier("QueryEditor")
                }
                .frame(minHeight: 46)

                CompactVoiceButton()

                Button {
                    focused = false
                    Task { await model.submitResearch() }
                } label: {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 22, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 42, height: 42)
                        .background(Circle().fill(sendButtonColor))
                }
                .disabled(sendDisabled)
                .accessibilityLabel("发送")
                .accessibilityIdentifier("SubmitResearchButton")
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(
                RoundedRectangle(cornerRadius: 18)
                    .fill(Color(.secondarySystemBackground))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 18)
                    .stroke(Color(.separator).opacity(0.22), lineWidth: 1)
            )
            .padding(.horizontal, 18)

            if !model.voiceMessage.isEmpty {
                Text(model.voiceMessage)
                    .font(.caption)
                    .foregroundStyle(model.isVoiceRecording ? .red : .secondary)
                    .lineLimit(2)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 24)
                    .accessibilityIdentifier("VoiceStatusText")
            }

            Text("limira OSINT 可能会犯错。请核查重要信息。")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
        }
        .padding(.top, 10)
        .padding(.bottom, 8)
        .background(Color(.systemBackground))
    }

    private var sendDisabled: Bool {
        model.isBusy || model.isVoiceTranscribing || model.queryDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var sendButtonColor: Color {
        sendDisabled ? Color(.systemGray3) : .black
    }
}

struct CompactVoiceButton: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        Button {
            Task { await model.toggleVoiceInput() }
        } label: {
            ZStack {
                Circle()
                    .fill(Color(.systemBackground).opacity(0.001))
                Image(systemName: iconName)
                    .font(.system(size: 21, weight: .medium))
            }
            .frame(width: 44, height: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .frame(width: 44, height: 44)
        .contentShape(Rectangle())
        .foregroundStyle(model.isVoiceRecording ? .red : .secondary)
        .disabled(model.isVoiceTranscribing)
        .accessibilityLabel(label)
        .accessibilityIdentifier("VoiceInputButton")
    }

    private var iconName: String {
        if model.isVoiceTranscribing { return "waveform" }
        return model.isVoiceRecording ? "stop.circle.fill" : "mic"
    }

    private var label: String {
        if model.isVoiceTranscribing { return "正在转写语音" }
        return model.isVoiceRecording ? "停止录音并转写" : "语音输入"
    }
}

struct CompactSidebarSheet: View {
    @EnvironmentObject private var model: AppViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button {
                        Task {
                            await model.startNewChat()
                            dismiss()
                        }
                    } label: {
                        Label("新研究", systemImage: "square.and.pencil")
                    }
                    .accessibilityIdentifier("NewConversationButton")

                    Button {
                        Task { await model.refreshSignedInData() }
                    } label: {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }
                }

                Section {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(model.user?.displayName ?? "")
                            .font(.headline)
                        Text(accountText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Button(role: .destructive) {
                        Task {
                            await model.signOut()
                            dismiss()
                        }
                    } label: {
                        Label("退出", systemImage: "rectangle.portrait.and.arrow.right")
                    }
                }

                Section("历史") {
                    TextField("搜索历史", text: $model.historySearchQuery)
                        .textInputAutocapitalization(.never)
                        .onSubmit {
                            Task { await model.loadTasks(query: model.historySearchQuery) }
                        }
                    Button {
                        Task { await model.loadTasks(query: model.historySearchQuery) }
                    } label: {
                        Label("搜索", systemImage: "magnifyingglass")
                    }
                    ForEach(model.tasks) { task in
                        CompactTaskHistoryRow(task: task)
                    }
                }

                Section("已归档") {
                    Button {
                        Task { await model.loadTasks(archived: true) }
                    } label: {
                        Label("刷新归档", systemImage: "archivebox")
                    }
                    ForEach(model.archivedTasks) { task in
                        CompactTaskHistoryRow(task: task, archivedList: true)
                    }
                }
            }
            .navigationTitle("limira OSINT")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("关闭") { dismiss() }
                }
            }
        }
    }

    private var accountText: String {
        let account = model.user?.accountType == "enterprise" ? "企业" : "个人"
        let role = model.user?.organizationRole ?? model.user?.role ?? "user"
        return "\(account) · \(role)"
    }
}

struct CompactMenuView: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("limira OSINT")
                    .font(.headline)
                Spacer()
                Button {
                    model.dismissCompactModal()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 16, weight: .semibold))
                        .frame(width: 36, height: 36)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("关闭")
                .accessibilityIdentifier("CompactSidebarCloseButton")
            }
            .padding(.horizontal, 20)
            .padding(.top, 18)
            .padding(.bottom, 12)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(spacing: 0) {
                        Button {
                            Task { await model.startNewChat() }
                        } label: {
                            Label("新研究", systemImage: "square.and.pencil")
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .contentShape(Rectangle())
                            .padding(.vertical, 14)
                        .accessibilityIdentifier("NewConversationButton")

                        Divider()

                        Button {
                            Task { await model.refreshSignedInData() }
                        } label: {
                            Label("刷新", systemImage: "arrow.clockwise")
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .contentShape(Rectangle())
                            .padding(.vertical, 14)
                        .accessibilityIdentifier("CompactMenuRefreshButton")
                    }
                    .foregroundStyle(Color.accentColor)
                    .padding(.horizontal, 18)
                    .background(Color(.secondarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 8))

                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Button {
                                withAnimation(.easeInOut(duration: 0.16)) {
                                    model.historyExpanded.toggle()
                                }
                            } label: {
                                Label("对话历史", systemImage: model.historyExpanded ? "chevron.down" : "chevron.right")
                                    .font(.headline)
                            }
                            .buttonStyle(.plain)
                            Spacer()
                            Button("搜索") {
                                model.presentCompactHistorySearch()
                            }
                            .font(.subheadline)
                            .accessibilityIdentifier("HistorySearchButton")
                            Button(model.showArchivedHistory ? "当前" : "已归档") {
                                Task { await model.toggleHistoryArchiveFilter() }
                            }
                            .font(.subheadline)
                            .accessibilityIdentifier("HistoryArchiveToggleButton")
                        }

                        if model.historyExpanded {
                            let source = model.showArchivedHistory ? model.archivedTasks : model.tasks
                            if source.isEmpty {
                                Text(model.showArchivedHistory ? "暂无已归档对话" : "暂无对话历史")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.vertical, 18)
                            } else {
                                VStack(alignment: .leading, spacing: 0) {
                                    ForEach(source) { task in
                                        CompactTaskHistoryRow(
                                            task: task,
                                            archivedList: model.showArchivedHistory || task.historyArchived == true,
                                            closeAction: { model.dismissCompactModal() }
                                        )
                                        Divider()
                                    }
                                }
                            }
                        }
                    }
                    .padding(.horizontal, 18)

                    CompactSettingsMenu()
                        .padding(.horizontal, 18)

                    Spacer(minLength: 24)
                }
                .padding(.bottom, 24)
            }
        }
        .safeAreaInset(edge: .bottom) {
            VStack(alignment: .leading, spacing: 8) {
                if !model.statusMessage.isEmpty {
                    Text(model.statusMessage)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .padding(.bottom, 4)
                        .accessibilityIdentifier("CompactMenuStatusMessage")
                }
                Text(model.user?.displayName ?? "")
                    .font(.headline)
                Text(accountText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
            .background(Color(.systemBackground))
            .overlay(alignment: .top) {
                Divider()
            }
        }
        .background {
            Color(.systemGroupedBackground)
            AccessibilityProbe(id: "CompactMenuView")
                .allowsHitTesting(false)
        }
    }

    private var accountText: String {
        let account = model.user?.accountType == "enterprise" ? "企业" : "个人"
        let role = model.user?.organizationRole ?? model.user?.role ?? "user"
        return "\(account) · \(role)"
    }
}

struct CompactSettingsMenu: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("设置")
                .font(.headline)

            VStack(alignment: .leading, spacing: 8) {
                Text("云空间")
                    .font(.subheadline.weight(.medium))
                if let storage = model.storage {
                    Text("已用 \(storage.usedBytes.byteString) / \(storage.quotaBytes.byteString)，剩余 \(storage.remainingBytes.byteString)。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    ProgressView(value: min(max(storage.usageRatio, 0), 1))
                        .accessibilityIdentifier("CompactStorageProgress")
                } else {
                    Text("正在等待云空间数据。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Divider()

            CompactSettingsButton(title: "管理云盘", systemImage: "externaldrive", identifier: "CompactCloudDriveButton") {
                Task {
                    await model.openCompactRoute(.cloudDrive)
                }
            }

            CompactSettingsButton(title: "已归档对话", systemImage: "archivebox", identifier: "CompactArchivedChatsButton") {
                Task {
                    await model.openCompactRoute(.archivedChats)
                }
            }

            if model.user?.isEnterpriseAdmin == true {
                CompactSettingsButton(title: "单位管理", systemImage: "building.2", identifier: "CompactEnterpriseAdminButton") {
                    Task {
                        await model.openCompactRoute(.enterpriseAdmin)
                    }
                }
            }

            Divider()

            Button(role: .destructive) {
                Task {
                    await model.signOut()
                }
            } label: {
                Label("退出登录", systemImage: "rectangle.portrait.and.arrow.right")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .contentShape(Rectangle())
            .accessibilityIdentifier("CompactSignOutButton")
        }
        .padding(18)
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct CompactSettingsButton: View {
    var title: String
    var systemImage: String
    var identifier: String
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .foregroundStyle(.primary)
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        .accessibilityIdentifier(identifier)
    }
}

struct CompactTaskHistoryRow: View {
    @EnvironmentObject private var model: AppViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var confirmDelete = false
    var task: LimiraTask
    var archivedList = false
    var closeAction: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                Task {
                    await model.selectTask(task)
                    closeAction?()
                    dismiss()
                }
            } label: {
                VStack(alignment: .leading, spacing: 4) {
                    Text(task.query)
                        .lineLimit(2)
                    Text("\(task.status) · \(task.archiveStatus ?? "pending")")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .contentShape(Rectangle())
            .accessibilityIdentifier("CompactHistoryRow-\(task.taskId)")

            HStack {
                Button(archivedList || task.historyArchived == true ? "恢复" : "归档") {
                    Task {
                        if archivedList || task.historyArchived == true {
                            await model.restore(task)
                        } else {
                            await model.archive(task)
                        }
                    }
                }
                .accessibilityIdentifier("CompactHistoryArchiveButton-\(task.taskId)")
                Button("删除", role: .destructive) {
                    confirmDelete = true
                }
                .accessibilityIdentifier("CompactHistoryDeleteButton-\(task.taskId)")
            }
            .font(.caption)
            .buttonStyle(.borderless)
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        .alert("删除这条对话？", isPresented: $confirmDelete) {
                Button("取消", role: .cancel) {}
                    .accessibilityIdentifier("CompactHistoryDeleteCancelButton-\(task.taskId)")
                Button("删除", role: .destructive) {
                    Task { await model.delete(task) }
                }
                .accessibilityIdentifier("CompactHistoryDeleteConfirmButton-\(task.taskId)")
            } message: {
                Text("删除后无法从 iOS 端恢复。")
            }
    }
}

struct CompactHistoryFilesSheet: View {
    @EnvironmentObject private var model: AppViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button {
                        Task { await model.loadCloudFiles() }
                    } label: {
                        Label("刷新历史文件", systemImage: "arrow.clockwise")
                    }
                    .contentShape(Rectangle())
                    .accessibilityIdentifier("HistoryFilesRefreshButton")
                }

                Section("可引用文件") {
                    if model.cloudFiles.isEmpty {
                        ContentUnavailableView("暂无历史文件", systemImage: "tray")
                    } else {
                        ForEach(model.cloudFiles) { document in
                            Button {
                                toggle(document)
                            } label: {
                                HStack(spacing: 12) {
                                    Image(systemName: model.selectedDocumentIds.contains(document.documentId) ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(Color.accentColor)
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(document.filename)
                                            .foregroundStyle(.primary)
                                            .lineLimit(1)
                                        Text("\((document.byteSize ?? 0).byteString) \(document.snippet ?? "")")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(2)
                                    }
                                }
                            }
                            .buttonStyle(.plain)
                            .contentShape(Rectangle())
                            .accessibilityIdentifier("HistoryFileToggle-\(document.documentId)")
                        }
                    }
                }
            }
            .accessibilityIdentifier("HistoryFilesList")
            .navigationTitle("引用历史文件 \(model.selectedDocumentIds.count)")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("完成") { dismiss() }
                        .accessibilityIdentifier("HistoryFilesDoneButton")
                }
            }
        }
    }

    private func toggle(_ document: LimiraUploadedDocument) {
        if model.selectedDocumentIds.contains(document.documentId) {
            model.selectedDocumentIds.remove(document.documentId)
        } else {
            model.selectedDocumentIds.insert(document.documentId)
        }
    }
}

struct CompactUtilityHeader: View {
    var title: String
    var subtitle: String?
    var refreshAction: (() -> Void)?
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            Button {
                Task { await model.openCompactRoute(.workspace) }
            } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 18, weight: .semibold))
                    .frame(width: 40, height: 40)
            }
            .contentShape(Rectangle())
            .accessibilityLabel("返回工作台")
            .accessibilityIdentifier("CompactRouteBackButton")

            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.title3.weight(.bold))
                    .lineLimit(1)
                if let subtitle {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            if let refreshAction {
                Button(action: refreshAction) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 18, weight: .semibold))
                        .frame(width: 40, height: 40)
                }
                .contentShape(Rectangle())
                .accessibilityLabel("刷新")
                .accessibilityIdentifier("CompactRouteRefreshButton")
            }
        }
        .padding(.horizontal, 18)
        .frame(height: 64)
        .background(Color(.systemBackground))
    }
}

struct CompactCloudDriveView: View {
    @EnvironmentObject private var model: AppViewModel
    @Binding var previewURL: URL?

    var body: some View {
        VStack(spacing: 0) {
            CompactUtilityHeader(title: "管理云盘", subtitle: "线上文件与云空间") {
                Task {
                    await model.loadStorage()
                    await model.loadCloudFiles()
                }
            }
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let storage = model.storage {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("云空间")
                                .font(.headline)
                            Text("已用 \(storage.usedBytes.byteString) / \(storage.quotaBytes.byteString)，剩余 \(storage.remainingBytes.byteString)。")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                            ProgressView(value: min(max(storage.usageRatio, 0), 1))
                        }
                        .accessibilityIdentifier("CompactCloudStorageSummary")
                    }

                    VStack(alignment: .leading, spacing: 12) {
                        Text("云文件")
                            .font(.headline)
                        if model.cloudFiles.isEmpty {
                            ContentUnavailableView("暂无云文件", systemImage: "tray")
                                .frame(maxWidth: .infinity)
                        } else {
                            ForEach(model.cloudFiles) { document in
                                CompactCloudFileRow(document: document)
                                Divider()
                            }
                        }
                    }

                    if let file = model.downloadedFile {
                        DownloadPanel(file: file, previewURL: $previewURL)
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .background {
            Color(.systemBackground)
            AccessibilityProbe(id: "CompactCloudDriveView")
                .allowsHitTesting(false)
        }
    }
}

struct CompactCloudFileRow: View {
    @EnvironmentObject private var model: AppViewModel
    var document: LimiraUploadedDocument

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "doc.text")
                .font(.title3)
                .foregroundStyle(Color.accentColor)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 5) {
                Text(document.filename)
                    .font(.subheadline.weight(.medium))
                    .lineLimit(2)
                Text("\((document.byteSize ?? 0).byteString) \(document.snippet ?? "")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                HStack(spacing: 14) {
                    Button(model.selectedDocumentIds.contains(document.documentId) ? "取消引用" : "引用") {
                        model.toggleSelectedDocument(document)
                    }
                    .contentShape(Rectangle())
                    .accessibilityIdentifier("CompactCloudFileReferenceButton-\(document.documentId)")
                    Button("下载") {
                        Task { await model.downloadUpload(document) }
                    }
                    .disabled(document.downloadUrl == nil)
                    .contentShape(Rectangle())
                    .accessibilityIdentifier("CompactCloudFileDownloadButton-\(document.documentId)")
                }
                .font(.caption.weight(.medium))
            }
            Spacer()
        }
        .padding(.vertical, 6)
        .background(alignment: .topLeading) {
            AccessibilityProbe(id: "CompactCloudFileRow-\(document.documentId)")
                .allowsHitTesting(false)
        }
    }
}

struct CompactArchivedHistoryView: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(spacing: 0) {
            CompactUtilityHeader(title: "已归档对话", subtitle: "恢复或删除历史研究") {
                Task { await model.loadTasks(archived: true) }
            }
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if model.archivedTasks.isEmpty {
                        ContentUnavailableView("暂无已归档对话", systemImage: "archivebox")
                            .frame(maxWidth: .infinity, minHeight: 360)
                    } else {
                        ForEach(model.archivedTasks) { task in
                            CompactTaskHistoryRow(task: task, archivedList: true)
                            Divider()
                        }
                    }
                }
                .padding(20)
            }
        }
        .background {
            Color(.systemBackground)
            AccessibilityProbe(id: "CompactArchivedHistoryView")
                .allowsHitTesting(false)
        }
    }
}

struct CompactEnterpriseAdminView: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(spacing: 0) {
            CompactUtilityHeader(title: "单位管理", subtitle: "成员与 usage") {
                Task { await model.loadEnterpriseAdmin() }
            }
            Divider()
            ScrollView {
                if model.user?.isEnterpriseAdmin == true {
                    EnterpriseAdminPanel()
                        .padding(20)
                } else {
                    ContentUnavailableView("当前账号没有单位管理权限", systemImage: "lock")
                        .frame(maxWidth: .infinity, minHeight: 420)
                }
            }
        }
        .background {
            Color(.systemBackground)
            AccessibilityProbe(id: "CompactEnterpriseAdminView")
                .allowsHitTesting(false)
        }
    }
}

struct CompactHistorySearchSheet: View {
    @EnvironmentObject private var model: AppViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    TextField("搜索历史", text: $model.historySearchQuery)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit {
                            Task { await model.searchHistory() }
                        }
                        .accessibilityIdentifier("HistorySearchField")
                    Button {
                        Task { await model.searchHistory() }
                    } label: {
                        Label("搜索", systemImage: "magnifyingglass")
                    }
                    .accessibilityIdentifier("HistorySearchSubmitButton")
                }

                Section("结果") {
                    if model.isSearchingHistory {
                        ProgressView()
                    } else if model.historySearchResults.isEmpty {
                        ContentUnavailableView("暂无结果", systemImage: "magnifyingglass")
                    } else {
                        ForEach(model.historySearchResults) { task in
                            CompactTaskHistoryRow(task: task, archivedList: task.historyArchived == true) {
                                dismiss()
                            }
                        }
                    }
                }
            }
            .navigationTitle("搜索历史")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("完成") { dismiss() }
                        .accessibilityIdentifier("HistorySearchDoneButton")
                }
            }
        }
        .task {
            if !model.historySearchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                await model.searchHistory()
            }
        }
    }
}

struct WorkspaceDetailContent: View {
    @EnvironmentObject private var model: AppViewModel
    @Binding var previewURL: URL?
    var pinnedComposer = false

    var body: some View {
        if pinnedComposer {
            VStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 14) {
                    HeaderBar()
                    ResearchComposer()
                    StatusStrip()
                }
                .padding()
                Divider()
                ScrollView {
                    WorkspaceResultContent(previewURL: $previewURL, includeStatus: false)
                }
            }
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HeaderBar()
                    ResearchComposer()
                    WorkspaceResultContent(previewURL: $previewURL)
                }
                .padding()
                .frame(maxWidth: 1180, alignment: .leading)
            }
        }
    }
}

struct WorkspaceResultContent: View {
    @EnvironmentObject private var model: AppViewModel
    @Binding var previewURL: URL?
    var includeStatus = true

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if includeStatus {
                StatusStrip()
            }
            MessageTimeline()
            ArtifactTabsView()
            UploadsPanel()
            ReportsPanel()
            if model.user?.isEnterpriseAdmin == true {
                EnterpriseAdminPanel()
            }
            if let file = model.downloadedFile {
                DownloadPanel(file: file, previewURL: $previewURL)
            }
        }
        .padding()
        .frame(maxWidth: 1180, alignment: .leading)
    }
}

struct SidebarView: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 4) {
                    Text(model.user?.displayName ?? "")
                        .font(.headline)
                    Text(accountText)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Button {
                    Task { await model.signOut() }
                } label: {
                    Label("退出", systemImage: "rectangle.portrait.and.arrow.right")
                }
            }

            Section("历史") {
                TextField("搜索历史", text: $model.historySearchQuery)
                    .textInputAutocapitalization(.never)
                    .onSubmit {
                        Task { await model.loadTasks(query: model.historySearchQuery) }
                    }
                Button {
                    Task { await model.loadTasks(query: model.historySearchQuery) }
                } label: {
                    Label("搜索", systemImage: "magnifyingglass")
                }
                ForEach(model.tasks) { task in
                    TaskRow(task: task)
                }
            }

            Section("已归档") {
                Button {
                    Task { await model.loadTasks(archived: true) }
                } label: {
                    Label("刷新归档", systemImage: "archivebox")
                }
                ForEach(model.archivedTasks) { task in
                    TaskRow(task: task, archivedList: true)
                }
            }
        }
        .navigationTitle("Limira")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Task { await model.refreshSignedInData() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .accessibilityLabel("刷新")
            }
        }
    }

    private var accountText: String {
        let account = model.user?.accountType == "enterprise" ? "企业" : "个人"
        let role = model.user?.organizationRole ?? model.user?.role ?? "user"
        return "\(account) · \(role)"
    }
}

struct TaskRow: View {
    @EnvironmentObject private var model: AppViewModel
    var task: LimiraTask
    var archivedList = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                Task { await model.selectTask(task) }
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Text(task.query)
                        .lineLimit(2)
                    Text("\(task.status) · \(task.archiveStatus ?? "pending")")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            HStack {
                Button(archivedList || task.historyArchived == true ? "恢复" : "归档") {
                    Task {
                        if archivedList || task.historyArchived == true {
                            await model.restore(task)
                        } else {
                            await model.archive(task)
                        }
                    }
                }
                Button("删除", role: .destructive) {
                    Task { await model.delete(task) }
                }
            }
            .font(.caption)
            .buttonStyle(.borderless)
        }
    }
}

struct HeaderBar: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        HStack {
            VStack(alignment: .leading) {
                Text("Limira")
                    .font(.title.bold())
                Text("API: \(AppConfiguration.apiBaseURL().absoluteString)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if model.isBusy {
                ProgressView()
            }
        }
    }
}

struct ResearchComposer: View {
    @EnvironmentObject private var model: AppViewModel
    @FocusState private var queryFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Picker("场景", selection: $model.selectedScenarioId) {
                    ForEach(model.scenarios) { scenario in
                        Text(scenario.title).tag(scenario.id)
                    }
                }
                .accessibilityIdentifier("ScenarioPicker")
                Button {
                    if let scenario = model.scenarios.first(where: { $0.id == model.selectedScenarioId }) {
                        model.queryDraft = scenario.defaultQuery ?? model.queryDraft
                    }
                } label: {
                    Label("套用", systemImage: "text.badge.plus")
                }
                .accessibilityIdentifier("ApplyScenarioButton")
            }
            TextEditor(text: $model.queryDraft)
                .focused($queryFocused)
                .frame(minHeight: 110)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(.quaternary)
                )
                .accessibilityIdentifier("QueryEditor")
            if !model.cloudFiles.isEmpty {
                Menu {
                    ForEach(model.cloudFiles) { document in
                        Button {
                            if model.selectedDocumentIds.contains(document.documentId) {
                                model.selectedDocumentIds.remove(document.documentId)
                            } else {
                                model.selectedDocumentIds.insert(document.documentId)
                            }
                        } label: {
                            Label(document.filename, systemImage: model.selectedDocumentIds.contains(document.documentId) ? "checkmark.circle.fill" : "circle")
                        }
                    }
                } label: {
                    Label("引用历史文件 \(model.selectedDocumentIds.count)", systemImage: "paperclip")
                }
            }
            Button {
                queryFocused = false
                Task { await model.submitResearch() }
            } label: {
                Label("开始研究", systemImage: "play.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(model.isBusy || model.queryDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .keyboardShortcut(.return, modifiers: [.command])
            .accessibilityIdentifier("SubmitResearchButton")
        }
    }
}

struct StatusStrip: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        HStack {
            Image(systemName: model.isStreaming ? "dot.radiowaves.left.and.right" : "circle.fill")
                .accessibilityHidden(true)
            Text(statusLabel(model.status))
                .accessibilityIdentifier("TaskStatusText")
            Spacer()
            Text("归档 \(archiveLabel(model.archiveStatus))")
            if let taskId = model.selectedTask?.taskId {
                Text(taskId)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
        .font(.subheadline)
        .padding(10)
        .background(.thinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .accessibilityIdentifier("TaskStatusStrip")
    }

    private func statusLabel(_ status: String) -> String {
        [
            "ready": "就绪",
            "starting": "启动中",
            "queued": "排队中",
            "running": "运行中",
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
            "stream reconnecting": "正在重连"
        ][status] ?? status
    }

    private func archiveLabel(_ status: String) -> String {
        ["pending": "等待中", "ready": "可下载", "failed": "失败"][status] ?? status
    }
}

struct MessageTimeline: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("消息")
                .font(.headline)
            if model.messages.isEmpty {
                ContentUnavailableView("暂无消息", systemImage: "text.bubble")
            } else {
                ForEach(model.messages.suffix(80)) { message in
                    HStack(alignment: .top) {
                        Image(systemName: icon(for: message.role))
                            .foregroundStyle(color(for: message.role))
                            .frame(width: 24)
                        Text(message.text)
                            .font(.body)
                            .textSelection(.enabled)
                        Spacer()
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        .accessibilityIdentifier("MessageTimeline")
    }

    private func icon(for role: AppMessage.Role) -> String {
        switch role {
        case .user: return "person.fill"
        case .assistant: return "sparkles"
        case .system: return "gear"
        case .error: return "exclamationmark.triangle.fill"
        }
    }

    private func color(for role: AppMessage.Role) -> Color {
        role == .error ? .red : .accentColor
    }
}

struct ArtifactTabsView: View {
    @EnvironmentObject private var model: AppViewModel
    var compact = false
    var showBackButton = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            AccessibilityProbe(id: "ArtifactTabsView")
            if showBackButton {
                HStack {
                    Button {
                        model.backToConversation()
                    } label: {
                        Label("回到对话", systemImage: "chevron.left")
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("BackToConversationButton")
                    Spacer()
                    Text(model.selectedTask?.query.nonEmpty ?? "成果")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }
            Picker("成果", selection: $model.selectedTab) {
                ForEach(tabs) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .accessibilityIdentifier("ArtifactTabPicker")

            switch model.selectedTab {
            case .evidence:
                ArtifactList(title: "证据", items: model.artifacts.evidence)
            case .entities:
                ArtifactList(title: "实体", items: model.artifacts.entities)
            case .graph:
                GraphView(relations: model.artifacts.relations)
            case .timeline:
                ArtifactList(title: "时间线", items: model.artifacts.timelineEvents)
            case .map:
                MapArtifactsView(features: model.artifacts.mapFeatures)
            case .report:
                ReportMarkdownView(markdown: model.currentReportMarkdown())
            }
        }
        .onAppear(perform: normalizeSelection)
        .onChange(of: compact) { _, _ in
            normalizeSelection()
        }
    }

    private var tabs: [ArtifactTab] {
        compact ? model.compactArtifactTabs() : ArtifactTab.allCases
    }

    private func normalizeSelection() {
        if compact && !tabs.contains(model.selectedTab) {
            model.selectedTab = .evidence
        }
    }
}

struct ArtifactList: View {
    var title: String
    var items: [ResearchArtifact]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.headline)
            if items.isEmpty {
                ContentUnavailableView("暂无\(title)", systemImage: "tray")
            } else {
                ForEach(items) { item in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(item.title)
                            .font(.headline)
                        if !item.subtitle.isEmpty {
                            Text(item.subtitle)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Text(jsonSummary(item.fields))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    .padding(10)
                    .background(.background)
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
                }
            }
        }
        .accessibilityIdentifier("ArtifactList-\(title)")
    }
}

struct GraphView: View {
    var relations: [ResearchArtifact]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("图谱")
                .font(.headline)
            if relations.isEmpty {
                ContentUnavailableView("暂无关系", systemImage: "point.3.connected.trianglepath.dotted")
            } else {
                Canvas { context, size in
                    let nodes = graphNodes()
                    guard !nodes.isEmpty else { return }
                    let radius = min(size.width, size.height) * 0.36
                    let center = CGPoint(x: size.width / 2, y: size.height / 2)
                    var positions: [String: CGPoint] = [:]
                    for (index, node) in nodes.enumerated() {
                        let angle = Double(index) / Double(max(nodes.count, 1)) * Double.pi * 2
                        positions[node] = CGPoint(x: center.x + cos(angle) * radius, y: center.y + sin(angle) * radius)
                    }
                    for relation in relations {
                        guard let source = relation.fields.string("source", "from"),
                              let target = relation.fields.string("target", "to"),
                              let start = positions[source],
                              let end = positions[target] else { continue }
                        var path = Path()
                        path.move(to: start)
                        path.addLine(to: end)
                        context.stroke(path, with: .color(.secondary), lineWidth: 1)
                    }
                    for (node, point) in positions {
                        let rect = CGRect(x: point.x - 48, y: point.y - 14, width: 96, height: 28)
                        context.fill(Path(roundedRect: rect, cornerRadius: 8), with: .color(.accentColor.opacity(0.12)))
                        context.draw(Text(node).font(.caption), in: rect)
                    }
                }
                .frame(height: 260)
                ArtifactList(title: "关系列表", items: relations)
            }
        }
        .accessibilityIdentifier("GraphArtifactsView")
    }

    private func graphNodes() -> [String] {
        var nodes: [String] = []
        for relation in relations {
            for key in ["source", "from", "target", "to"] {
                if let node = relation.fields.string(key), !nodes.contains(node) {
                    nodes.append(node)
                }
            }
        }
        return nodes
    }
}

struct MapArtifactsView: View {
    var features: [ResearchArtifact]

    var points: [MapPoint] {
        features.compactMap { feature in
            guard let coordinate = feature.coordinate() else { return nil }
            return MapPoint(title: feature.title, coordinate: coordinate)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("地图")
                .font(.headline)
            if points.isEmpty {
                ArtifactList(title: "地图记录", items: features)
            } else {
                Map {
                    ForEach(points) { point in
                        Marker(point.title, coordinate: point.coordinate)
                    }
                }
                .frame(height: 280)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                ArtifactList(title: "地图记录", items: features)
            }
        }
        .accessibilityIdentifier("MapArtifactsView")
    }
}

struct MapPoint: Identifiable {
    var id = UUID()
    var title: String
    var coordinate: CLLocationCoordinate2D
}

struct ReportMarkdownView: View {
    var markdown: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("报告")
                .font(.headline)
                .accessibilityIdentifier("ReportTabTitle")
            if markdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                ContentUnavailableView("暂无报告", systemImage: "doc.text")
            } else {
                Text(markdown)
                    .font(.body.monospaced())
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                    .background(.background)
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
            }
        }
    }
}

struct UploadsPanel: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var importerPresented = false
    @State private var searchQuery = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            AccessibilityProbe(id: "UploadsPanel")
            HStack {
                Text("文件")
                    .font(.headline)
                Spacer()
                if let storage = model.storage {
                    Text("\(storage.usedBytes.byteString) / \(storage.quotaBytes.byteString)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            HStack {
                Button {
                    importerPresented = true
                } label: {
                    Label("上传", systemImage: "square.and.arrow.up")
                }
                .accessibilityIdentifier("UploadDocumentButton")
                TextField("搜索文件", text: $searchQuery)
                    .textInputAutocapitalization(.never)
                    .onSubmit {
                        Task { await model.searchUploads(query: searchQuery) }
                    }
                    .accessibilityIdentifier("UploadSearchField")
                Button {
                    Task { await model.searchUploads(query: searchQuery) }
                } label: {
                    Image(systemName: "magnifyingglass")
                }
                .accessibilityIdentifier("UploadSearchButton")
            }
            .fileImporter(isPresented: $importerPresented, allowedContentTypes: [.data], allowsMultipleSelection: false) { result in
                if case .success(let urls) = result, let url = urls.first {
                    let scoped = url.startAccessingSecurityScopedResource()
                    Task {
                        await model.uploadDocument(url: url)
                        if scoped {
                            url.stopAccessingSecurityScopedResource()
                        }
                    }
                }
            }
            UploadList(title: "当前任务文件", documents: model.uploads)
            UploadList(title: "云文件", documents: model.cloudFiles, selectable: true)
        }
    }
}

struct UploadList: View {
    @EnvironmentObject private var model: AppViewModel
    var title: String
    var documents: [LimiraUploadedDocument]
    var selectable = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.subheadline.bold())
            if documents.isEmpty {
                Text("暂无文件")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(documents) { document in
                    HStack {
                        if selectable {
                            Button {
                                if model.selectedDocumentIds.contains(document.documentId) {
                                    model.selectedDocumentIds.remove(document.documentId)
                                } else {
                                    model.selectedDocumentIds.insert(document.documentId)
                                }
                            } label: {
                                Image(systemName: model.selectedDocumentIds.contains(document.documentId) ? "checkmark.circle.fill" : "circle")
                            }
                        }
                        VStack(alignment: .leading) {
                            Text(document.filename)
                            Text("\((document.byteSize ?? 0).byteString) \(document.snippet ?? "")")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if document.downloadUrl != nil {
                            Button {
                                Task { await model.downloadUpload(document) }
                            } label: {
                                Image(systemName: "arrow.down.circle")
                            }
                            .accessibilityLabel("下载 \(document.filename)")
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        .accessibilityIdentifier("UploadList-\(title)")
    }
}

struct ReportsPanel: View {
    @EnvironmentObject private var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            AccessibilityProbe(id: "ReportsPanel")
            Text("导出")
                .font(.headline)
            HStack {
                Button {
                    Task { await model.exportPDF() }
                } label: {
                    Label("导出 PDF", systemImage: "doc.richtext")
                }
                .accessibilityIdentifier("ExportPDFButton")
                Button {
                    Task { await model.downloadArchive() }
                } label: {
                    Label("下载归档", systemImage: "archivebox")
                }
                .disabled(model.selectedTask?.downloadUrl == nil)
                .accessibilityIdentifier("DownloadArchiveButton")
            }
            if !model.reports.isEmpty {
                ForEach(model.reports) { report in
                    HStack {
                        Text(report.reportId)
                        Spacer()
                        if let pdfUrl = report.pdfUrl {
                            Button {
                                Task {
                                    model.downloadedFile = try? await model.service.download(relativeOrAbsolutePath: pdfUrl, suggestedFilename: "\(report.reportId).pdf")
                                }
                            } label: {
                                Image(systemName: "arrow.down.doc")
                            }
                        }
                    }
                }
            }
        }
    }
}

struct EnterpriseAdminPanel: View {
    @EnvironmentObject private var model: AppViewModel
    @State private var username = ""
    @State private var email = ""
    @State private var name = ""
    @State private var password = ""
    @State private var role = "member"

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            AccessibilityProbe(id: "EnterpriseAdminPanel")
            HStack {
                Text("企业后台")
                    .font(.headline)
                Spacer()
                Button {
                    Task { await model.loadEnterpriseAdmin() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
            }
            if let usage = model.enterpriseUsage {
                Text(jsonSummary(usage.usage))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .accessibilityIdentifier("EnterpriseUsageText")
            }
            Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 8) {
                GridRow {
                    TextField("用户名", text: $username)
                        .accessibilityIdentifier("EnterpriseMemberUsernameField")
                    TextField("邮箱", text: $email)
                        .accessibilityIdentifier("EnterpriseMemberEmailField")
                }
                GridRow {
                    TextField("姓名", text: $name)
                        .accessibilityIdentifier("EnterpriseMemberNameField")
                    SecureField("密码", text: $password)
                        .accessibilityIdentifier("EnterpriseMemberPasswordField")
                }
                GridRow {
                    Picker("角色", selection: $role) {
                        Text("member").tag("member")
                        Text("admin").tag("admin")
                    }
                    .accessibilityIdentifier("EnterpriseMemberRolePicker")
                    Button {
                        Task {
                            await model.createEnterpriseMember(username: username, email: email, password: password, name: name, role: role)
                            username = ""
                            email = ""
                            name = ""
                            password = ""
                        }
                    } label: {
                        Label("创建成员", systemImage: "person.badge.plus")
                    }
                    .accessibilityIdentifier("CreateEnterpriseMemberButton")
                }
            }
            ForEach(model.enterpriseMembers) { member in
                HStack {
                    Text(member.displayName)
                    Spacer()
                    Text(member.organizationRole ?? member.role)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}

struct DownloadPanel: View {
    var file: DownloadedFile
    @Binding var previewURL: URL?

    var body: some View {
        HStack {
            Label(file.filename, systemImage: "doc")
            Spacer()
            Button {
                previewURL = file.url
            } label: {
                Label("预览", systemImage: "eye")
            }
            .accessibilityIdentifier("PreviewDownloadedFileButton")
            ShareLink(item: file.url) {
                Label("分享", systemImage: "square.and.arrow.up")
            }
        }
        .padding(10)
        .background(.thinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .accessibilityIdentifier("DownloadPanel")
    }
}

func jsonSummary(_ fields: [String: JSONValue]) -> String {
    fields
        .sorted { $0.key < $1.key }
        .prefix(8)
        .map { key, value in "\(key): \(shortValue(value))" }
        .joined(separator: " · ")
}

private func shortValue(_ value: JSONValue) -> String {
    switch value {
    case .string(let string):
        return string.count > 120 ? String(string.prefix(120)) + "..." : string
    case .number(let number):
        return number.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(number)) : String(number)
    case .bool(let bool):
        return bool ? "true" : "false"
    case .object(let object):
        return "{\(object.keys.sorted().prefix(4).joined(separator: ","))}"
    case .array(let array):
        return "[\(array.count)]"
    case .null:
        return "null"
    }
}

private extension Int {
    var byteString: String {
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useKB, .useMB, .useGB]
        formatter.countStyle = .file
        return formatter.string(fromByteCount: Int64(self))
    }
}
