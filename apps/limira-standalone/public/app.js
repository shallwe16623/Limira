const CONVERSATION_VIEW = '对话';
const BACK_TO_CHAT_LABEL = '回到对话';
const tabs = ['证据', '实体', '图谱', '时间线', '地图'];
const terminalStatuses = new Set(['completed', 'failed', 'cancelled']);
const artifactEvents = new Set([
	'evidence_collected',
	'entity_extracted',
	'relation_extracted',
	'timeline_event_added',
	'map_feature_added',
	'verification_result',
	'report_section_generated',
	'record_research_artifact'
]);

const STORAGE_KEY = 'limiraStandaloneWorkspace:v2';
const LEGACY_STORAGE_KEYS = ['limiraStandaloneWorkspace:v1'];
const MAX_STORED_MESSAGES = 100;
const MAX_THINKING_STEPS = 120;
const MAX_HISTORY_TASKS = 30;
const STATUS_LABELS = {
	ready: '就绪',
	starting: '启动中',
	queued: '排队中',
	running: '运行中',
	completed: '已完成',
	failed: '失败',
	cancelled: '已取消',
	'stream reconnecting': '正在重连'
};
const ROLE_LABELS = {
	assistant: '助手',
	user: '用户',
	system: '系统',
	error: '错误'
};
const LEGACY_TAB_LABELS = {
	Evidence: '证据',
	Entities: '实体',
	Graph: '图谱',
	Timeline: '时间线',
	Map: '地图',
	Report: CONVERSATION_VIEW
};
const ORGANIZATION_CATEGORY_OPTIONS = [
	{ value: 'enterprise', label: '企业' },
	{ value: 'public_institution', label: '事业单位' },
	{ value: 'university', label: '高校' },
	{ value: 'think_tank', label: '智库' },
	{ value: 'ministry', label: '国家部委' },
	{ value: 'local_government', label: '地方政府' }
];
const DEFAULT_ENTERPRISE_ORGANIZATION_CATEGORY = 'enterprise';
const DEFAULT_ENTERPRISE_ORGANIZATION_SLUG = 'limira';
const IFRAME_PREVIEW_TIMEOUT_MS = 2800;
const DIRECT_EXTERNAL_EVIDENCE_HOSTS = [
	'americancompass.org',
	'afsc.org',
	'bloomberg.com',
	'cfr.org',
	'chinasurvey.csis.org',
	'cnas.org',
	'crisisgroup.org',
	'csis.org',
	'facebook.com',
	'ft.com',
	'globalaffairs.org',
	'instagram.com',
	'linkedin.com',
	'nytimes.com',
	'pewresearch.org',
	'reuters.com',
	'theharrispoll.com',
	'twitter.com',
	'washingtonpost.com',
	'wsj.com',
	'x.com',
	'yougov.com'
];

const state = {
	authScope: 'personal',
	authMode: 'signin',
	token: localStorage.getItem('limiraToken') || '',
	user: null,
	pendingAuthEmail: '',
	googleAuthEnabled: false,
	wechatAuthEnabled: false,
	organizations: [],
	selectedOrganizationCategory: DEFAULT_ENTERPRISE_ORGANIZATION_CATEGORY,
	selectedOrganizationId: '',
	enterpriseMembers: [],
	enterpriseUsage: null,
	route: routeFromHash(),
	isLoadingEnterpriseAdmin: false,
	userSettingsOpen: false,
	uploadMenuOpen: false,
	historyFilesOpen: false,
	showArchivedHistory: false,
	savedUserId: '',
	query: '',
	taskId: '',
	status: 'ready',
	archiveStatus: 'pending',
	archiveDownloadUrl: '',
	activeTab: CONVERSATION_VIEW,
	isSubmitting: false,
	isUploading: false,
	currentUpload: null,
	isSearching: false,
	isLoadingHistory: false,
	isLoadingArchivedHistory: false,
	restoreBlocked: false,
	workspaceGeneration: 0,
	finalReportText: '',
	messages: initialMessages(),
	thinkingCollapsed: false,
	thinkingSteps: initialThinkingSteps(),
	artifacts: emptyArtifacts(),
	uploads: [],
	cloudFiles: [],
	cloudStorage: null,
	uploadResults: [],
	taskHistory: [],
	archivedTaskHistory: [],
	sandboxPreviewId: 0,
	sandboxPreviewLoaded: false,
	sandboxPreviewTimer: null,
	sandboxPreviewUrl: '',
	sandboxPreviewTitle: '',
	sandboxPreviewSummary: '',
	eventSource: null
};

const dom = {};

document.addEventListener('DOMContentLoaded', () => {
	cacheDom();
	bindEvents();
	renderShell();
	void boot();
});

function cacheDom() {
	for (const element of document.querySelectorAll('[id]')) {
		dom[element.id] = element;
	}
}

function bindEvents() {
	dom.authForm.addEventListener('submit', (event) => {
		event.preventDefault();
		void authenticate();
	});
	dom.personalScopeButton.addEventListener('click', () => setAuthScope('personal'));
	dom.enterpriseScopeButton.addEventListener('click', () => setAuthScope('enterprise'));
	dom.signinModeButton.addEventListener('click', () => setAuthMode('signin'));
	dom.signupModeButton.addEventListener('click', () => setAuthMode('signup'));
	dom.organizationCategorySelect.addEventListener('change', () => {
		state.selectedOrganizationCategory =
			dom.organizationCategorySelect.value || DEFAULT_ENTERPRISE_ORGANIZATION_CATEGORY;
		state.selectedOrganizationId = '';
		selectDefaultOrganizationForCategory();
		renderOrganizationOptions();
	});
	dom.organizationSelect.addEventListener('change', () => {
		state.selectedOrganizationId = dom.organizationSelect.value;
	});
	dom.forgotPasswordButton.addEventListener('click', () => setAuthMode('forgot'));
	dom.resendVerificationButton.addEventListener('click', () => void resendVerificationEmail());
	dom.googleSigninButton.addEventListener('click', googleSignIn);
	dom.wechatSigninButton.addEventListener('click', wechatSignIn);
	dom.userSettingsButton.addEventListener('click', (event) => {
		event.stopPropagation();
		state.userSettingsOpen = !state.userSettingsOpen;
		renderShell();
		if (state.userSettingsOpen && isEnterpriseAccount()) {
			void loadCloudStorage();
		}
	});
	window.addEventListener('hashchange', () => {
		syncRouteFromHash();
		renderShell();
		if (state.route === 'cloud-drive') {
			void loadUploads();
		} else if (state.route === 'archived-chats') {
			void loadArchivedTaskHistory();
		} else if (state.route === 'enterprise-admin') {
			void loadEnterpriseAdmin();
		}
	});
	dom.cloudDriveManageButton.addEventListener('click', (event) => {
		event.preventDefault();
		state.userSettingsOpen = false;
		window.location.hash = 'cloud-drive';
		syncRouteFromHash();
		renderShell();
		void loadUploads();
	});
	dom.cloudDriveBackButton.addEventListener('click', (event) => {
		event.preventDefault();
		window.location.hash = '';
		syncRouteFromHash();
		renderShell();
	});
	dom.archivedHistoryBackButton.addEventListener('click', (event) => {
		event.preventDefault();
		window.location.hash = '';
		syncRouteFromHash();
		renderShell();
	});
	dom.uploadMenuButton.addEventListener('click', (event) => {
		event.preventDefault();
		event.stopPropagation();
		const nextOpen = !state.uploadMenuOpen;
		window.setTimeout(() => setUploadMenuOpen(nextOpen), 0);
	});
	dom.uploadFileButton.addEventListener('click', (event) => {
		event.stopPropagation();
		setUploadMenuOpen(false);
		dom.uploadInput.click();
	});
	dom.historyFileButton.addEventListener('click', (event) => {
		event.preventDefault();
		event.stopPropagation();
		const nextOpen = !state.historyFilesOpen;
		window.setTimeout(() => setHistoryFilePanelOpen(nextOpen), 0);
	});
	dom.refreshHistoryFilesButton.addEventListener('click', (event) => {
		event.stopPropagation();
		void loadUploads();
	});
	dom.historyFileList.addEventListener('click', (event) => {
		const button = event.target.closest('[data-history-document-id]');
		if (!button) {
			return;
		}
		event.preventDefault();
		selectHistoryFile(button.dataset.historyDocumentId || '');
	});
	dom.uploadList.addEventListener('click', (event) => {
		const button = event.target.closest('[data-remove-upload-id]');
		if (!button) {
			return;
		}
		event.preventDefault();
		removeSelectedUpload(button.dataset.removeUploadId || '');
	});
	document.addEventListener('click', (event) => {
		if (
			state.userSettingsOpen &&
			!dom.userSettingsPanel.contains(event.target) &&
			!dom.userSettingsButton.contains(event.target)
		) {
			state.userSettingsOpen = false;
			renderShell();
		}
		if (
			state.uploadMenuOpen &&
			!eventInside(event, dom.uploadMenuWrapper)
		) {
			setUploadMenuOpen(false);
		}
		if (
			state.historyFilesOpen &&
			!eventInside(event, dom.uploadMenuWrapper)
		) {
			setHistoryFilesOpen(false);
		}
	});
	document.addEventListener('keydown', (event) => {
		if (event.key === 'Escape' && (state.uploadMenuOpen || state.historyFilesOpen)) {
			setUploadMenuOpen(false);
			setHistoryFilesOpen(false);
		}
	});
	dom.signOutButton.addEventListener('click', () => void signOut());
	dom.newChatButton.addEventListener('click', startNewChat);
	dom.archivedHistoryManageButton.addEventListener('click', (event) => {
		event.preventDefault();
		state.userSettingsOpen = false;
		window.location.hash = 'archived-chats';
		syncRouteFromHash();
		renderShell();
		void loadArchivedTaskHistory();
	});
	dom.enterpriseAdminManageButton.addEventListener('click', (event) => {
		event.preventDefault();
		state.userSettingsOpen = false;
		window.location.hash = 'enterprise-admin';
		syncRouteFromHash();
		renderShell();
		void loadEnterpriseAdmin();
	});
	dom.refreshArchivedHistoryButton.addEventListener('click', () => void loadArchivedTaskHistory());
	dom.enterpriseAdminBackButton.addEventListener('click', (event) => {
		event.preventDefault();
		window.location.hash = '';
		syncRouteFromHash();
		renderShell();
	});
	dom.archivedHistoryList.addEventListener('click', (event) => {
		const restoreButton = event.target.closest('[data-archived-restore-id]');
		if (restoreButton) {
			event.preventDefault();
			void restoreArchivedHistoryTask(restoreButton.dataset.archivedRestoreId || '');
			return;
		}
		const deleteButton = event.target.closest('[data-archived-delete-id]');
		if (deleteButton) {
			event.preventDefault();
			void deleteArchivedHistoryTask(deleteButton.dataset.archivedDeleteId || '');
		}
	});
	dom.historyArchiveToggleButton.addEventListener('click', () => {
		state.showArchivedHistory = !state.showArchivedHistory;
		void loadTaskHistory();
		renderHistory();
	});
	dom.refreshHistoryButton.addEventListener('click', () => void loadTaskHistory());
	dom.researchForm.addEventListener('submit', (event) => {
		event.preventDefault();
		void submitResearch();
	});
	dom.refreshArtifactsButton.addEventListener('click', () => void loadArtifacts());
	dom.thinkingToggleButton.addEventListener('click', () => {
		state.thinkingCollapsed = !state.thinkingCollapsed;
		saveWorkspace();
		renderThinking();
	});
	dom.clearStreamButton.addEventListener('click', () => {
		state.messages = [];
		state.thinkingSteps = initialThinkingSteps();
		saveWorkspace();
		renderMessages();
		renderThinking();
	});
	dom.refreshUploadsButton.addEventListener('click', () => void loadUploads());
	dom.uploadButton.addEventListener('click', () => void uploadDocument());
	dom.uploadInput.addEventListener('change', () => void uploadDocument());
	dom.uploadSearchButton.addEventListener('click', () => void searchUploads());
	dom.uploadSearchInput.addEventListener('keydown', (event) => {
		if (event.key === 'Enter') {
			event.preventDefault();
			void searchUploads();
		}
	});
	dom.refreshEnterpriseAdminButton.addEventListener('click', () => void loadEnterpriseAdmin());
	dom.enterpriseMemberForm.addEventListener('submit', (event) => {
		event.preventDefault();
		void createEnterpriseMember();
	});

	dom.artifactContent.addEventListener('click', (event) => {
		const link = event.target.closest('.sandbox-link');
		if (link) {
			event.preventDefault();
			const url = link.href;
			const title = link.getAttribute('data-title') || '网页预览';
			const summary = link.getAttribute('data-summary') || '';
			openEvidenceSource({ url, title, summary });
		}
	});

	dom.sandboxIframe.addEventListener('load', () => {
		if (!state.sandboxPreviewUrl) {
			return;
		}
		let loadedLocation = '';
		try {
			loadedLocation = dom.sandboxIframe.contentWindow?.location?.href || '';
		} catch {
			loadedLocation = 'cross-origin';
		}
		if (loadedLocation === 'about:blank') {
			redirectBlockedEvidenceSource({
				url: state.sandboxPreviewUrl,
				title: state.sandboxPreviewTitle,
				summary: state.sandboxPreviewSummary
			});
			return;
		}
		state.sandboxPreviewLoaded = true;
		clearSandboxPreviewTimer();
	});
	dom.sandboxIframe.addEventListener('error', () => {
		redirectBlockedEvidenceSource({
			url: state.sandboxPreviewUrl,
			title: state.sandboxPreviewTitle,
			summary: state.sandboxPreviewSummary
		});
	});
	dom.sandboxCloseButton.addEventListener('click', () => {
		closeSandboxPreview();
	});
}

async function boot() {
	clearLegacyWorkspaceStorage();
	restoreWorkspace();
	await loadAuthOptions();
	const authLinkState = await handleAuthLinkTokens();
	if (authLinkState === 'signed-in') {
		await loadTaskHistory();
		await resumeWorkspace();
		renderShell();
		return;
	}
	if (authLinkState === 'pending') {
		renderShell();
		return;
	}
	try {
		await loadSession();
		await loadTaskHistory();
		await loadEnterpriseAdmin();
		await resumeWorkspace();
	} catch {
		state.user = null;
	}
	renderShell();
}

function renderShell() {
	const signedIn = Boolean(state.user);
	const enterpriseAdmin = signedIn && isEnterpriseAdmin();
	const cloudDriveVisible = signedIn && state.route === 'cloud-drive';
	const archivedHistoryVisible = signedIn && state.route === 'archived-chats';
	const enterpriseAdminVisible = enterpriseAdmin && state.route === 'enterprise-admin';
	const utilityPageVisible = cloudDriveVisible || archivedHistoryVisible || enterpriseAdminVisible;
	dom.authPanel.classList.toggle('hidden', signedIn);
	dom.workspace.classList.toggle('hidden', !signedIn);
	dom.workspaceContent.classList.toggle('hidden', utilityPageVisible);
	dom.cloudDrivePage.classList.toggle('hidden', !cloudDriveVisible);
	dom.archivedHistoryPage.classList.toggle('hidden', !archivedHistoryVisible);
	dom.enterpriseAdminPage.classList.toggle('hidden', !enterpriseAdminVisible);
	dom.signOutButton.classList.toggle('hidden', !signedIn);
	dom.userSettingsButton.classList.toggle('hidden', !signedIn);
	if (!signedIn) {
		state.userSettingsOpen = false;
		state.archivedTaskHistory = [];
		state.enterpriseMembers = [];
		state.enterpriseUsage = null;
		state.route = 'workspace';
		setUploadMenuOpen(false);
		setHistoryFilesOpen(false);
	}
	dom.userSettingsPanel.classList.toggle('hidden', !signedIn || !state.userSettingsOpen);
	renderUploadMenu();
	renderHistoryFiles();
	renderFileControls();
	renderCloudStorage();
	renderArchivedHistory();
	const displayName = state.user?.name || state.user?.username || state.user?.email || '已登录';
	const fullSessionLabel = signedIn ? `${displayName} · ${accountLabel(state.user)}` : '未登录';
	dom.sessionLabel.textContent = signedIn ? displayName : '未登录';
	dom.sessionLabel.title = fullSessionLabel;
	renderAuthMode();
	renderStatus();
	renderHistory();
	renderMessages();
	renderThinking();
	renderTabs();
	renderUploads();
	renderReportControls();
	renderEnterpriseAdmin();
	renderCloudDrive();
}

function syncRouteFromHash() {
	state.route = routeFromHash();
}

function routeFromHash() {
	if (window.location.hash === '#cloud-drive') {
		return 'cloud-drive';
	}
	if (window.location.hash === '#archived-chats') {
		return 'archived-chats';
	}
	if (window.location.hash === '#enterprise-admin') {
		return 'enterprise-admin';
	}
	return 'workspace';
}

function setUploadMenuOpen(open) {
	state.uploadMenuOpen = Boolean(open);
	if (state.uploadMenuOpen) {
		state.historyFilesOpen = false;
	}
	renderUploadMenu();
}

function renderUploadMenu() {
	if (!dom.uploadMenu || !dom.uploadMenuButton) {
		return;
	}
	dom.uploadMenu.classList.toggle('hidden', !state.uploadMenuOpen);
	dom.uploadMenuButton.setAttribute('aria-expanded', state.uploadMenuOpen ? 'true' : 'false');
}

function setHistoryFilesOpen(open) {
	state.historyFilesOpen = Boolean(open);
	if (state.historyFilesOpen) {
		state.uploadMenuOpen = false;
	}
	renderUploadMenu();
	renderHistoryFiles();
}

function setHistoryFilePanelOpen(open) {
	state.uploadMenuOpen = false;
	state.historyFilesOpen = Boolean(open);
	renderUploadMenu();
	renderHistoryFiles();
}

function renderFileControls() {
	const visible = Boolean(state.user) && isEnterpriseAccount();
	dom.uploadMenuWrapper.classList.toggle('hidden', !visible);
	if (!visible) {
		setUploadMenuOpen(false);
		setHistoryFilesOpen(false);
	}
}

function eventInside(event, element) {
	if (!element) {
		return false;
	}
	const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
	return path.includes(element) || element.contains(event.target);
}

function renderAuthMode() {
	const personalScope = state.authScope === 'personal';
	const usernameVisible =
		!personalScope || state.authMode === 'signin' || state.authMode === 'signup';
	const emailVisible = personalScope && (state.authMode === 'signup' || state.authMode === 'forgot');
	const passwordVisible = !personalScope || state.authMode !== 'forgot';
	dom.personalScopeButton.classList.toggle('active', personalScope);
	dom.enterpriseScopeButton.classList.toggle('active', !personalScope);
	dom.authModeControl.classList.toggle('hidden', !personalScope);
	dom.signinModeButton.classList.toggle('active', state.authMode === 'signin');
	dom.signupModeButton.classList.toggle('active', state.authMode === 'signup');
	dom.organizationCategoryLabel.classList.toggle('hidden', personalScope);
	dom.organizationLabel.classList.toggle('hidden', personalScope);
	dom.enterpriseContactPrompt.classList.toggle('hidden', personalScope);
	dom.enterpriseContactActions.classList.toggle('hidden', personalScope);
	renderOrganizationCategoryOptions();
	selectDefaultOrganizationForCategory();
	dom.organizationSelect.disabled = personalScope;
	dom.organizationSelect.required = !personalScope;
	renderOrganizationOptions();
	dom.nameLabel.classList.toggle('hidden', !personalScope || state.authMode !== 'signup');
	dom.usernameLabel.classList.toggle('hidden', !usernameVisible);
	dom.emailLabel.classList.toggle('hidden', !emailVisible);
	dom.passwordLabel.classList.toggle('hidden', !passwordVisible);
	dom.resetTokenLabel.classList.toggle('hidden', !personalScope || state.authMode !== 'reset');
	dom.forgotPasswordButton.classList.toggle('hidden', !personalScope || state.authMode !== 'signin');
	dom.resendVerificationButton.classList.toggle(
		'hidden',
		!personalScope || state.authMode === 'reset' || (state.authMode === 'signin' && !state.pendingAuthEmail)
	);
	dom.usernameInput.disabled = !usernameVisible;
	dom.usernameInput.required = usernameVisible;
	dom.emailInput.disabled = !emailVisible;
	dom.emailInput.required = emailVisible;
	dom.passwordInput.disabled = personalScope && state.authMode === 'forgot';
	dom.passwordInput.required = passwordVisible;
	dom.resetTokenInput.disabled = !personalScope || state.authMode !== 'reset';
	dom.resetTokenInput.required = personalScope && state.authMode === 'reset';
	dom.googleSigninButton.classList.toggle(
		'hidden',
		!personalScope || !state.googleAuthEnabled || state.authMode === 'forgot' || state.authMode === 'reset'
	);
	dom.wechatSigninButton.classList.toggle(
		'hidden',
		!personalScope || !state.wechatAuthEnabled || state.authMode === 'forgot' || state.authMode === 'reset'
	);
	const submitText = {
		signin: '登录',
		signup: '注册',
		forgot: '发送重置邮件',
		reset: '重置密码'
	};
	dom.authSubmitButton.textContent = personalScope
		? submitText[state.authMode] || '登录'
		: '登录单位账号';
	dom.passwordInput.autocomplete =
		personalScope && state.authMode !== 'signin' ? 'new-password' : 'current-password';
}

async function loadAuthOptions() {
	try {
		const [googleConfig, wechatConfig, organizations] = await Promise.all([
			api('/api/limira/auth/google/config'),
			api('/api/limira/auth/wechat/config'),
			api('/api/limira/auth/organizations')
		]);
		state.googleAuthEnabled = Boolean(googleConfig?.enabled);
		state.wechatAuthEnabled = Boolean(wechatConfig?.enabled);
		state.organizations = Array.isArray(organizations?.organizations)
			? organizations.organizations
			: [];
		selectDefaultOrganizationForCategory();
	} catch {
		state.googleAuthEnabled = false;
		state.wechatAuthEnabled = false;
		state.organizations = [];
		state.selectedOrganizationId = '';
	}
}

function setAuthScope(scope) {
	state.authScope = scope === 'enterprise' ? 'enterprise' : 'personal';
	if (state.authScope === 'enterprise') {
		state.authMode = 'signin';
		selectDefaultOrganizationForCategory();
	}
	dom.authMessage.textContent = '';
	renderAuthMode();
}

function setAuthMode(mode) {
	state.authMode = mode;
	dom.authMessage.textContent = '';
	renderAuthMode();
}

function renderOrganizationOptions() {
	if (!dom.organizationSelect) {
		return;
	}
	const organizations = organizationsForSelectedCategory();
	if (!organizations.length) {
		dom.organizationSelect.innerHTML = '<option value="">暂无可选单位</option>';
		state.selectedOrganizationId = '';
		return;
	}
	dom.organizationSelect.innerHTML = organizations
		.map((organization) => {
			const id = String(organization.id || '');
			const selected = id === state.selectedOrganizationId ? ' selected' : '';
			return `<option value="${escapeAttr(id)}"${selected}>${escapeHtml(organization.name || id)}</option>`;
		})
		.join('');
}

function renderOrganizationCategoryOptions() {
	if (!dom.organizationCategorySelect) {
		return;
	}
	dom.organizationCategorySelect.innerHTML = ORGANIZATION_CATEGORY_OPTIONS
		.map((category) => {
			const selected = category.value === state.selectedOrganizationCategory ? ' selected' : '';
			return `<option value="${escapeAttr(category.value)}"${selected}>${escapeHtml(category.label)}</option>`;
		})
		.join('');
}

function organizationsForSelectedCategory() {
	return state.organizations.filter(
		(organization) =>
			String(organization.category || DEFAULT_ENTERPRISE_ORGANIZATION_CATEGORY) ===
			state.selectedOrganizationCategory
	);
}

function selectDefaultOrganizationForCategory() {
	const selectedStillVisible = organizationsForSelectedCategory().some(
		(organization) => String(organization.id || '') === state.selectedOrganizationId
	);
	if (selectedStillVisible) {
		return;
	}
	const preferred = state.organizations.find(
		(organization) =>
			String(organization.slug || '') === DEFAULT_ENTERPRISE_ORGANIZATION_SLUG &&
			String(organization.category || '') === state.selectedOrganizationCategory
	);
	const fallback = preferred || organizationsForSelectedCategory()[0];
	state.selectedOrganizationId = fallback ? String(fallback.id || '') : '';
}

function googleSignIn() {
	window.location.href = '/api/limira/auth/google/start';
}

function wechatSignIn() {
	window.location.href = '/api/limira/auth/wechat/start';
}

async function authenticate() {
	const username = dom.usernameInput.value.trim();
	const email = dom.emailInput.value.trim();
	const password = dom.passwordInput.value;
	const name = dom.nameInput.value.trim();
	dom.authMessage.textContent = '处理中...';
	try {
		if (state.authScope === 'enterprise') {
			if (!state.selectedOrganizationId) {
				dom.authMessage.textContent = '请先选择单位。';
				return;
			}
			const user = await api('/api/limira/auth/enterprise/signin', {
				method: 'POST',
				body: {
					organization_id: state.selectedOrganizationId,
					username,
					password
				}
			});
			await finishAuthenticated(user);
			dom.authMessage.textContent = '';
			return;
		}
		if (state.authMode === 'forgot') {
			await api('/api/limira/auth/password-reset/request', {
				method: 'POST',
				body: { email }
			});
			state.pendingAuthEmail = email;
			dom.authMessage.textContent = '如果这个邮箱已注册，我们已经发送了密码重置邮件。';
			return;
		}
		if (state.authMode === 'reset') {
			const user = await api('/api/limira/auth/password-reset/confirm', {
				method: 'POST',
				body: { token: dom.resetTokenInput.value.trim(), password }
			});
			await finishAuthenticated(user);
			dom.authMessage.textContent = '';
			return;
		}
		const path = state.authMode === 'signup' ? '/api/limira/auth/signup' : '/api/limira/auth/signin';
		const payload =
			state.authMode === 'signup'
				? { username, name: name || username, email, password }
				: { username, password };
		const user = await api(path, { method: 'POST', body: payload });
		if (state.authMode === 'signup' && user?.email_verification_required) {
			state.pendingAuthEmail = email;
			setAuthMode('signin');
			dom.usernameInput.value = username;
			dom.authMessage.textContent = '注册成功，请打开验证邮件完成邮箱验证后再登录。';
			return;
		}
		await finishAuthenticated(user);
		dom.authMessage.textContent = '';
	} catch (error) {
		if (error?.responseOk) {
			try {
				await loadSession();
				await finishAuthenticated(state.user);
				return;
			} catch {
				// Fall through to the controlled parse error below.
			}
		}
		dom.authMessage.textContent = errorMessage(error);
	}
}

async function finishAuthenticated(user) {
	setUser(user);
	dom.authMessage.textContent = '';
	await loadTaskHistory();
	await loadUploads();
	await loadEnterpriseAdmin();
	renderShell();
}

async function resendVerificationEmail() {
	const email = dom.emailInput.value.trim() || state.pendingAuthEmail;
	if (!email) {
		dom.authMessage.textContent = '请输入邮箱后再重发验证邮件。';
		return;
	}
	dom.authMessage.textContent = '正在发送验证邮件...';
	try {
		await api('/api/limira/auth/resend-verification', {
			method: 'POST',
			body: { email }
		});
		state.pendingAuthEmail = email;
		dom.authMessage.textContent = '如果这个邮箱需要验证，我们已经重新发送了验证邮件。';
	} catch (error) {
		dom.authMessage.textContent = errorMessage(error);
	}
}

async function handleAuthLinkTokens() {
	const url = new URL(window.location.href);
	const verifyToken = url.searchParams.get('verify_email_token');
	const resetToken = url.searchParams.get('reset_password_token');
	const authError = url.searchParams.get('auth_error');
	const googleAuth = url.searchParams.get('google_auth');
	const wechatAuth = url.searchParams.get('wechat_auth');
	if (authError) {
		clearAuthUrlParams(['auth_error']);
		setAuthMode('signin');
		dom.authMessage.textContent = localizedErrorDetail(authError);
		return 'pending';
	}
	if (googleAuth === 'success') {
		clearAuthUrlParams(['google_auth']);
		return '';
	}
	if (wechatAuth === 'success') {
		clearAuthUrlParams(['wechat_auth']);
		return '';
	}
	if (verifyToken) {
		setAuthMode('signin');
		dom.authMessage.textContent = '正在验证邮箱...';
		try {
			const user = await api('/api/limira/auth/verify-email', {
				method: 'POST',
				body: { token: verifyToken }
			});
			clearAuthUrlParams(['verify_email_token']);
			await finishAuthenticated(user);
			return 'signed-in';
		} catch (error) {
			clearAuthUrlParams(['verify_email_token']);
			dom.authMessage.textContent = `邮箱验证失败：${errorMessage(error)}`;
			return 'pending';
		}
	}
	if (resetToken) {
		setAuthMode('reset');
		dom.resetTokenInput.value = resetToken;
		dom.passwordInput.value = '';
		clearAuthUrlParams(['reset_password_token']);
		dom.authMessage.textContent = '请输入新密码完成重置。';
		return 'pending';
	}
	return '';
}

function clearAuthUrlParams(keys) {
	const url = new URL(window.location.href);
	for (const key of keys) {
		url.searchParams.delete(key);
	}
	window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
}

async function loadSession() {
	const user = await api('/api/limira/auth/session');
	setUser(user);
}

function setUser(user) {
	const previousUserId = state.user?.id || '';
	const nextUserId = user?.id || '';
	state.user = user;
	if (previousUserId !== nextUserId) {
		bumpWorkspaceGeneration();
	}
	if (user?.token) {
		state.token = user.token;
		localStorage.setItem('limiraToken', user.token);
	}
}

async function resumeWorkspace() {
	if (state.savedUserId && state.user?.id && state.savedUserId !== state.user.id) {
		clearWorkspaceStorage();
		resetWorkspaceState();
		await loadTaskHistory();
		await loadUploads();
		renderShell();
		return;
	}
	if (!state.taskId) {
		await loadUploads();
		return;
	}
	try {
		await refreshTask();
		await loadArtifacts();
		await loadUploads();
		await loadTaskHistory();
		if (!terminalStatuses.has(state.status)) {
			connectStream();
		}
	} catch (error) {
		addMessage(
			'error',
			`无法从后端恢复上次任务：${errorMessage(error)}`
		);
		if (isAuthoritativeRestoreRejection(error)) {
			clearRestoredTaskState();
			saveWorkspace();
		} else {
			state.restoreBlocked = true;
		}
		renderStatus();
		renderTabs();
		renderReportControls();
		await loadUploads();
		await loadTaskHistory();
	}
}

async function refreshTask() {
	if (!state.taskId) {
		return;
	}
	const context = captureAsyncContext();
	const task = await api(`/api/limira/tasks/${encodeURIComponent(state.taskId)}`);
	if (!isCurrentAsyncContext(context)) {
		return;
	}
	state.restoreBlocked = false;
	state.status = task.status || state.status;
	updateArchiveState(task);
	mergeTaskHistory(task);
	saveWorkspace();
	renderStatus();
}

async function loadTaskHistory() {
	if (!state.user || state.isLoadingHistory) {
		renderHistory();
		return;
	}
	state.isLoadingHistory = true;
	const context = captureAsyncContext({ includeTask: false });
	if (dom.historyMessage) {
		dom.historyMessage.textContent = '正在加载历史...';
	}
	try {
		const archived = state.showArchivedHistory ? 'true' : 'false';
		const data = await api(`/api/limira/tasks?limit=${MAX_HISTORY_TASKS}&archived=${archived}`);
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		state.taskHistory = Array.isArray(data.tasks) ? data.tasks : [];
		if (dom.historyMessage) {
			dom.historyMessage.textContent = state.taskHistory.length
				? ''
				: state.showArchivedHistory ? '暂无已归档历史。' : '暂无历史聊天。';
		}
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		if (dom.historyMessage) {
			dom.historyMessage.textContent = `历史加载失败：${errorMessage(error)}`;
		}
	} finally {
		state.isLoadingHistory = false;
		if (isCurrentAsyncContext(context)) {
			renderHistory();
		}
	}
}

async function loadArchivedTaskHistory() {
	if (!state.user || state.isLoadingArchivedHistory) {
		renderArchivedHistory();
		return;
	}
	state.isLoadingArchivedHistory = true;
	const context = captureAsyncContext({ includeTask: false });
	if (dom.archivedHistoryMessage) {
		dom.archivedHistoryMessage.textContent = '正在加载已归档对话...';
	}
	try {
		const data = await api(`/api/limira/tasks?limit=${MAX_HISTORY_TASKS}&archived=true`);
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		state.archivedTaskHistory = Array.isArray(data.tasks) ? data.tasks : [];
		if (dom.archivedHistoryMessage) {
			dom.archivedHistoryMessage.textContent = state.archivedTaskHistory.length
				? ''
				: '暂无已归档对话。';
		}
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		if (dom.archivedHistoryMessage) {
			dom.archivedHistoryMessage.textContent = `归档对话加载失败：${errorMessage(error)}`;
		}
	} finally {
		state.isLoadingArchivedHistory = false;
		if (isCurrentAsyncContext(context)) {
			renderArchivedHistory();
		}
	}
}

function renderArchivedHistory() {
	if (!dom.archivedHistoryPage) {
		return;
	}
	const visible = Boolean(state.user) && state.route === 'archived-chats';
	dom.archivedHistoryManageButton.classList.toggle('active', visible);
	dom.refreshArchivedHistoryButton.disabled = !state.user || state.isLoadingArchivedHistory;
	if (!visible) {
		return;
	}
	if (!state.archivedTaskHistory.length) {
		dom.archivedHistoryList.innerHTML = '<div class="empty-state compact-empty">暂无已归档对话。</div>';
		return;
	}
	dom.archivedHistoryList.innerHTML = state.archivedTaskHistory
		.map((task) => {
			const taskId = String(task.task_id || '');
			return `<article class="settings-history-item">
				<div class="history-item">
					<span class="history-title">${escapeHtml(taskHistoryTitle(task))}</span>
					<span class="history-meta">${escapeHtml(taskHistoryMeta({ ...task, history_archived: true }))}</span>
				</div>
				<div class="history-actions visible-actions">
					<button type="button" class="history-action" data-archived-restore-id="${escapeAttr(taskId)}">恢复</button>
					<button type="button" class="history-action danger" data-archived-delete-id="${escapeAttr(taskId)}">删除</button>
				</div>
			</article>`;
		})
		.join('');
}

function renderHistory() {
	if (!dom.historyList) {
		return;
	}
	dom.refreshHistoryButton.disabled = !state.user || state.isLoadingHistory;
	dom.newChatButton.disabled = !state.user;
	dom.historyArchiveToggleButton.disabled = !state.user || state.isLoadingHistory;
	dom.historyArchiveToggleButton.textContent = state.showArchivedHistory ? '返回历史' : '已归档';
	dom.historyArchiveToggleButton.classList.toggle('active', state.showArchivedHistory);
	if (!state.user) {
		dom.historyList.innerHTML = '';
		dom.historyMessage.textContent = '';
		return;
	}
	if (!state.taskHistory.length) {
		dom.historyList.innerHTML = state.showArchivedHistory
			? '<div class="empty-state compact-empty">暂无已归档历史。</div>'
			: '<div class="empty-state compact-empty">暂无历史聊天。</div>';
		return;
	}
	dom.historyList.innerHTML = state.taskHistory
		.map((task) => {
			const taskId = String(task.task_id || '');
			const active = taskId && taskId === state.taskId;
			const archived = Boolean(task.history_archived || state.showArchivedHistory);
			return `<article class="history-entry${active ? ' active' : ''}">
				<button type="button" class="history-item" data-task-id="${escapeAttr(taskId)}">
					<span class="history-title">${escapeHtml(taskHistoryTitle(task))}</span>
					<span class="history-meta">${escapeHtml(taskHistoryMeta(task))}</span>
				</button>
				<div class="history-actions">
					<button type="button" class="history-action" data-history-${archived ? 'restore' : 'archive'}-id="${escapeAttr(taskId)}">${archived ? '恢复' : '归档'}</button>
					<button type="button" class="history-action danger" data-history-delete-id="${escapeAttr(taskId)}">删除</button>
				</div>
			</article>`;
		})
		.join('');
	for (const button of dom.historyList.querySelectorAll('.history-item')) {
		button.addEventListener('click', () => void selectHistoryTask(button.dataset.taskId || ''));
	}
	for (const button of dom.historyList.querySelectorAll('[data-history-archive-id]')) {
		button.addEventListener('click', () => void archiveHistoryTask(button.dataset.historyArchiveId || ''));
	}
	for (const button of dom.historyList.querySelectorAll('[data-history-restore-id]')) {
		button.addEventListener('click', () => void restoreHistoryTask(button.dataset.historyRestoreId || ''));
	}
	for (const button of dom.historyList.querySelectorAll('[data-history-delete-id]')) {
		button.addEventListener('click', () => void deleteHistoryTask(button.dataset.historyDeleteId || ''));
	}
}

async function archiveHistoryTask(taskId) {
	const normalizedTaskId = String(taskId || '').trim();
	if (!normalizedTaskId || state.isLoadingHistory) {
		return;
	}
	try {
		await api(`/api/limira/tasks/${encodeURIComponent(normalizedTaskId)}/history/archive`, {
			method: 'POST'
		});
		state.taskHistory = state.taskHistory.filter((task) => task.task_id !== normalizedTaskId);
		renderHistory();
		if (state.route === 'archived-chats') {
			await loadArchivedTaskHistory();
		}
		await loadTaskHistory();
	} catch (error) {
		dom.historyMessage.textContent = `归档失败：${errorMessage(error)}`;
	}
}

async function restoreHistoryTask(taskId) {
	const normalizedTaskId = String(taskId || '').trim();
	if (!normalizedTaskId || state.isLoadingHistory) {
		return;
	}
	try {
		await api(`/api/limira/tasks/${encodeURIComponent(normalizedTaskId)}/history/restore`, {
			method: 'POST'
		});
		state.taskHistory = state.taskHistory.filter((task) => task.task_id !== normalizedTaskId);
		renderHistory();
		if (state.route === 'archived-chats') {
			await loadArchivedTaskHistory();
		}
		await loadTaskHistory();
	} catch (error) {
		dom.historyMessage.textContent = `恢复失败：${errorMessage(error)}`;
	}
}

async function deleteHistoryTask(taskId) {
	const normalizedTaskId = String(taskId || '').trim();
	if (!normalizedTaskId || state.isLoadingHistory) {
		return;
	}
	if (!window.confirm('删除后此聊天不会再出现在历史记录中，是否继续？')) {
		return;
	}
	try {
		await api(`/api/limira/tasks/${encodeURIComponent(normalizedTaskId)}/history`, {
			method: 'DELETE'
		});
		state.taskHistory = state.taskHistory.filter((task) => task.task_id !== normalizedTaskId);
		if (state.taskId === normalizedTaskId) {
			resetCurrentTaskView();
			dom.queryInput.value = '';
			saveWorkspace();
			renderStatus();
			renderMessages();
			renderTabs();
			renderReportControls();
		}
		renderHistory();
		if (state.route === 'archived-chats') {
			await loadArchivedTaskHistory();
		}
		await loadTaskHistory();
	} catch (error) {
		dom.historyMessage.textContent = `删除失败：${errorMessage(error)}`;
	}
}

async function restoreArchivedHistoryTask(taskId) {
	const normalizedTaskId = String(taskId || '').trim();
	if (!normalizedTaskId || state.isLoadingArchivedHistory) {
		return;
	}
	try {
		await api(`/api/limira/tasks/${encodeURIComponent(normalizedTaskId)}/history/restore`, {
			method: 'POST'
		});
		state.archivedTaskHistory = state.archivedTaskHistory.filter(
			(task) => task.task_id !== normalizedTaskId
		);
		renderArchivedHistory();
		await loadArchivedTaskHistory();
		if (!state.showArchivedHistory) {
			await loadTaskHistory();
		}
	} catch (error) {
		dom.archivedHistoryMessage.textContent = `恢复失败：${errorMessage(error)}`;
	}
}

async function deleteArchivedHistoryTask(taskId) {
	const normalizedTaskId = String(taskId || '').trim();
	if (!normalizedTaskId || state.isLoadingArchivedHistory) {
		return;
	}
	if (!window.confirm('删除后此聊天不会再出现在历史记录中，是否继续？')) {
		return;
	}
	try {
		await api(`/api/limira/tasks/${encodeURIComponent(normalizedTaskId)}/history`, {
			method: 'DELETE'
		});
		state.archivedTaskHistory = state.archivedTaskHistory.filter(
			(task) => task.task_id !== normalizedTaskId
		);
		if (state.taskId === normalizedTaskId) {
			resetCurrentTaskView();
			dom.queryInput.value = '';
			saveWorkspace();
			renderStatus();
			renderMessages();
			renderTabs();
			renderReportControls();
		}
		renderArchivedHistory();
		await loadArchivedTaskHistory();
		if (state.showArchivedHistory) {
			await loadTaskHistory();
		}
	} catch (error) {
		dom.archivedHistoryMessage.textContent = `删除失败：${errorMessage(error)}`;
	}
}


async function selectHistoryTask(taskId) {
	const normalizedTaskId = String(taskId || '').trim();
	if (!normalizedTaskId || state.isSubmitting) {
		return;
	}
	state.eventSource?.close();
	state.eventSource = null;
	bumpWorkspaceGeneration();
	const cached = state.taskHistory.find((task) => task.task_id === normalizedTaskId) || {};
	resetCurrentTaskView();
	state.taskId = normalizedTaskId;
	state.status = cached.status || 'queued';
	state.archiveStatus = cached.archive_status || 'pending';
	updateArchiveState(cached);
	state.savedUserId = state.user?.id || state.savedUserId;
	state.messages = historyMessages(cached);
	state.thinkingCollapsed = false;
	state.thinkingSteps = historyThinkingSteps(cached);
	dom.queryInput.value = cached.query || '';
	saveWorkspace();
	renderStatus();
	renderHistory();
	renderMessages();
	renderTabs();
	renderReportControls();
	try {
		await refreshTask();
		await loadArtifacts();
		await loadUploads();
		if (!terminalStatuses.has(state.status)) {
			connectStream();
		}
	} catch (error) {
		if (isAuthoritativeRestoreRejection(error)) {
			clearRestoredTaskState();
			saveWorkspace();
			renderStatus();
			renderHistory();
			renderTabs();
			renderReportControls();
		}
		addMessage('error', `无法加载历史任务：${errorMessage(error)}`);
	}
}

function startNewChat() {
	state.eventSource?.close();
	state.eventSource = null;
	bumpWorkspaceGeneration();
	resetCurrentTaskView();
	dom.queryInput.value = '';
	saveWorkspace();
	renderStatus();
	renderHistory();
	renderMessages();
	renderTabs();
	renderReportControls();
	void loadUploads();
}

function resetCurrentTaskView() {
	state.savedUserId = state.user?.id || state.savedUserId || '';
	state.taskId = '';
	state.status = 'ready';
	state.archiveStatus = 'pending';
	state.archiveDownloadUrl = '';
	state.restoreBlocked = false;
	state.isSubmitting = false;
	state.isSearching = false;
	state.activeTab = CONVERSATION_VIEW;
	state.finalReportText = '';
	state.messages = initialMessages();
	state.thinkingCollapsed = false;
	state.thinkingSteps = initialThinkingSteps();
	state.artifacts = emptyArtifacts();
	state.uploads = [];
	state.uploadResults = [];
	state.currentUpload = null;
	state.uploadMenuOpen = false;
	state.historyFilesOpen = false;
}

function mergeTaskHistory(task) {
	if (!task || typeof task !== 'object' || !task.task_id) {
		return;
	}
	const taskId = String(task.task_id);
	if (Boolean(task.history_archived) !== state.showArchivedHistory) {
		state.taskHistory = state.taskHistory.filter((item) => item.task_id !== taskId);
		renderHistory();
		return;
	}
	const next = [task, ...state.taskHistory.filter((item) => item.task_id !== taskId)];
	state.taskHistory = next.slice(0, MAX_HISTORY_TASKS);
	renderHistory();
}

function taskHistoryTitle(task) {
	const query = String(task.query || '').replace(/\s+/g, ' ').trim();
	return query || `任务 ${task.task_id || ''}` || '历史任务';
}

function taskHistoryMeta(task) {
	const parts = [statusLabel(task.status), `归档${archiveStatusLabel(task.archive_status)}`];
	if (task.history_archived || state.showArchivedHistory) {
		parts.push('已归档');
	}
	return parts.filter(Boolean).join(' · ');
}

function historyMessages(task) {
	const query = String(task.query || '').trim();
	return [
		query ? { role: 'user', content: query, time: now() } : null,
		{
			role: 'assistant',
			content: `已载入历史任务：${statusLabel(task.status || state.status)}。`,
			time: now()
		}
	].filter(Boolean);
}

function historyThinkingSteps(task) {
	const query = String(task.query || '').trim();
	const steps = [];
	if (query) {
		steps.push({
			kind: 'planning',
			title: '研究问题',
			detail: query,
			time: now(),
			status: 'done'
		});
	}
	steps.push({
		kind: 'status',
		title: `任务${statusLabel(task.status || state.status)}`,
		detail: `历史任务已载入。归档状态：${archiveStatusLabel(task.archive_status || state.archiveStatus)}。`,
		time: now(),
		status: terminalStatuses.has(String(task.status || state.status)) ? 'done' : 'active'
	});
	return steps.length ? steps : initialThinkingSteps();
}

function clearLegacyWorkspaceStorage() {
	for (const key of LEGACY_STORAGE_KEYS) {
		if (key !== STORAGE_KEY) {
			localStorage.removeItem(key);
		}
	}
}

function restoreWorkspace() {
	let saved = null;
	try {
		saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
	} catch {
		localStorage.removeItem(STORAGE_KEY);
	}
	if (!saved || typeof saved !== 'object') {
		return;
	}
	state.savedUserId = typeof saved.userId === 'string' ? saved.userId : '';
	state.taskId = typeof saved.taskId === 'string' ? saved.taskId : '';
	state.status = typeof saved.status === 'string' ? saved.status : 'ready';
	state.archiveStatus = typeof saved.archiveStatus === 'string' ? saved.archiveStatus : 'pending';
	state.archiveDownloadUrl = safeArchiveDownloadUrl(saved.archiveDownloadUrl, state.taskId);
	state.activeTab = tabs.includes(saved.activeTab)
		? saved.activeTab
		: saved.activeTab === '报告' ? CONVERSATION_VIEW : LEGACY_TAB_LABELS[saved.activeTab] || CONVERSATION_VIEW;
	state.finalReportText = typeof saved.finalReportText === 'string' ? saved.finalReportText : '';
	state.messages = Array.isArray(saved.messages) && saved.messages.length ? saved.messages : state.messages;
	state.thinkingCollapsed = Boolean(saved.thinkingCollapsed);
	state.thinkingSteps = Array.isArray(saved.thinkingSteps) && saved.thinkingSteps.length
		? saved.thinkingSteps.slice(-MAX_THINKING_STEPS)
		: state.thinkingSteps;
	state.artifacts = saved.artifacts && typeof saved.artifacts === 'object'
		? normalizeArtifacts(saved.artifacts)
		: emptyArtifacts();
	state.uploads = Array.isArray(saved.uploads) ? saved.uploads : [];
	state.uploadResults = [];
}

function saveWorkspace() {
	const payload = {
		userId: state.user?.id || state.savedUserId || '',
		taskId: state.taskId,
		status: state.status,
		archiveStatus: state.archiveStatus,
		archiveDownloadUrl: state.archiveDownloadUrl,
		activeTab: state.activeTab,
		finalReportText: state.finalReportText,
		messages: state.messages.slice(-MAX_STORED_MESSAGES),
		thinkingCollapsed: state.thinkingCollapsed,
		thinkingSteps: state.thinkingSteps.slice(-MAX_THINKING_STEPS),
		artifacts: state.artifacts,
		uploads: state.uploads
	};
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
	} catch {
		try {
			localStorage.setItem(
				STORAGE_KEY,
				JSON.stringify({
					...payload,
					messages: state.messages.slice(-30),
					artifacts: emptyArtifacts()
				})
			);
		} catch {
			// A private or quota-limited browser can still use the live page.
		}
	}
}

function clearWorkspaceStorage() {
	localStorage.removeItem(STORAGE_KEY);
}

function resetWorkspaceState() {
	bumpWorkspaceGeneration();
	state.savedUserId = state.user?.id || '';
	state.taskId = '';
	state.status = 'ready';
	state.archiveStatus = 'pending';
	state.archiveDownloadUrl = '';
	state.restoreBlocked = false;
	state.isSubmitting = false;
	state.isUploading = false;
	state.currentUpload = null;
	state.isSearching = false;
	state.isLoadingHistory = false;
	state.activeTab = CONVERSATION_VIEW;
	state.finalReportText = '';
	state.messages = initialMessages();
	state.thinkingCollapsed = false;
	state.thinkingSteps = initialThinkingSteps();
	state.artifacts = emptyArtifacts();
	state.uploads = [];
	state.cloudFiles = [];
	state.cloudStorage = null;
	state.uploadResults = [];
	state.taskHistory = [];
	state.userSettingsOpen = false;
	state.uploadMenuOpen = false;
	state.historyFilesOpen = false;
}

async function signOut() {
	try {
		await api('/api/limira/auth/signout', { method: 'POST' });
	} catch {
		// Local cleanup still matters if the server session is already gone.
	}
	state.eventSource?.close();
	state.eventSource = null;
	state.restoreBlocked = false;
	state.user = null;
	state.token = '';
	state.enterpriseMembers = [];
	state.enterpriseUsage = null;
	state.isLoadingEnterpriseAdmin = false;
	state.userSettingsOpen = false;
	resetWorkspaceState();
	clearWorkspaceStorage();
	localStorage.removeItem('limiraToken');
	localStorage.removeItem('token');
	renderShell();
}

async function submitResearch() {
	const query = dom.queryInput.value.trim();
	if (!query || state.isSubmitting) {
		return;
	}
	const documentIds = isEnterpriseAccount() ? selectedUploadDocumentIds() : [];

	state.isSubmitting = true;
	bumpWorkspaceGeneration();
	const context = captureAsyncContext({ includeTask: false });
	state.status = 'starting';
	state.taskId = '';
	state.archiveStatus = 'pending';
	state.archiveDownloadUrl = '';
	state.restoreBlocked = false;
	state.finalReportText = '';
	state.artifacts = emptyArtifacts();
	state.uploadResults = [];
	state.isSearching = false;
	state.thinkingCollapsed = false;
	state.thinkingSteps = [];
	saveWorkspace();
	addMessage('user', query);
	addThinkingStep({
		kind: 'planning',
		title: '拆解研究任务',
		detail: `围绕“${truncateText(query, 140)}”识别核心问题、证据需求和最终报告结构。`,
		status: 'active',
		meta: documentIds.length ? `已附加 ${documentIds.length} 个文件` : ''
	});
	addThinkingStep({
		kind: 'planning',
		title: '制定信息路线',
		detail: '优先查找权威机构、公开数据、研究报告和可核验网页，再进行交叉验证、实体抽取、时间线整理和报告归纳。',
		status: 'active'
	});
	renderStatus();
	renderTabs();
	renderReportControls();

	try {
		const task = await api('/api/limira/research', {
			method: 'POST',
			body: {
				query,
				document_ids: documentIds
			}
		});
		if (!state.isSubmitting || !isCurrentAsyncContext(context)) {
			return;
		}
		state.taskId = task.task_id || '';
		state.status = task.status || 'queued';
		updateArchiveState(task);
		mergeTaskHistory(task);
		state.savedUserId = state.user?.id || state.savedUserId;
		dom.queryInput.value = '';
		state.uploads = [];
		state.uploadResults = [];
		completeActiveThinkingSteps();
		addThinkingStep({
			kind: 'status',
			title: '研究任务已创建',
			detail: `任务 ${state.taskId || '已创建'} 已进入${statusLabel(state.status)}状态，正在连接实时进度并沉淀结构化成果。`,
			status: 'active'
		});
		addMessage('assistant', `研究任务 ${state.taskId || '已创建'}：${statusLabel(state.status)}。`);
		saveWorkspace();
		renderTabs();
		renderUploads();
		connectStream();
		await loadArtifacts();
		await loadUploads();
		await loadTaskHistory();
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		state.status = 'failed';
		completeActiveThinkingSteps();
		addThinkingStep({
			kind: 'error',
			title: '任务启动失败',
			detail: errorMessage(error),
			status: 'error'
		});
		addMessage('error', errorMessage(error));
	} finally {
		if (isCurrentAsyncContext(context)) {
			state.isSubmitting = false;
			renderStatus();
		}
	}
}

function connectStream() {
	if (!state.taskId) {
		return;
	}
	const context = captureAsyncContext();
	state.eventSource?.close();
	state.eventSource = new EventSource(`/api/limira/tasks/${state.taskId}/events`);
	state.eventSource.onmessage = (event) => {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		handleStreamEvent(parseJson(event.data));
	};
	state.eventSource.onerror = () => {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		if (!terminalStatuses.has(state.status)) {
			state.status = 'stream reconnecting';
			saveWorkspace();
			renderStatus();
		}
	};
}

function handleStreamEvent(payload) {
	payload = payload && typeof payload === 'object' ? payload : { message: String(payload || '') };
	const eventType = String(payload.event || payload.type || payload.data?.event || 'task_update');
	const data =
		payload.data && typeof payload.data === 'object'
			? payload.data
			: payload.payload && typeof payload.payload === 'object'
				? payload.payload
				: {};
	const nested = data.data && typeof data.data === 'object' ? data.data : {};
	const eventData = data.event === eventType && Object.keys(nested).length > 0 ? nested : data;
	const status = payload.status || data.status || nested.status || eventData.status;
	updateArchiveState(payload);
	updateArchiveState(data);
	updateArchiveState(nested);
	updateArchiveState(eventData);

	if (status) {
		state.status = String(status);
	}

	if (eventType === 'heartbeat') {
		saveWorkspace();
		renderStatus();
		return;
	}

	if (eventType === 'tool_call') {
		handleToolCall(eventData);
	} else if (eventType === 'error') {
		state.status = 'failed';
		completeActiveThinkingSteps();
		addThinkingStep({
			kind: 'error',
			title: '任务执行失败',
			detail: errorMessage(eventData.error || data.error || payload),
			status: 'error'
		});
		addMessage('error', errorMessage(eventData.error || data.error || payload));
	} else if (eventType === 'archive_generated') {
		state.archiveStatus = 'ready';
		state.archiveDownloadUrl = safeArchiveDownloadUrl(eventData.archive_url, state.taskId);
		addThinkingStep({
			kind: 'archive',
			title: '任务归档已生成',
			detail: '报告、证据和运行材料已打包并保存到云盘，可在底部“归档”入口下载。',
			status: 'done'
		});
		void loadUploads();
	} else if (eventType === 'completion_asset_warning') {
		addThinkingStep({
			kind: 'warning',
			title: '部分导出材料生成失败',
			detail: '任务主体已完成，但部分归档材料需要稍后重试或检查运行配置。',
			status: 'warning'
		});
		addMessage('error', '任务已完成，但部分导出文件生成失败，请稍后重试下载。');
	} else if (eventType === 'end_of_workflow') {
		state.status = 'completed';
		completeActiveThinkingSteps();
		addThinkingStep({
			kind: 'done',
			title: '工作流已完成',
			detail: artifactThinkingSummary(),
			status: 'done'
		});
		addMessage('assistant', '工作流已完成。');
	} else if (eventType.startsWith('start_of_')) {
		addThinkingStep(thinkingStepForStartEvent(eventType, eventData));
	} else if (artifactEvents.has(eventType)) {
		addThinkingStep(thinkingStepForArtifactEvent(eventType, eventData));
		void loadArtifacts();
	} else {
		const summary = eventData.message || eventData.summary || data.message || data.summary || payload.message || eventType;
		addThinkingStep({
			kind: 'status',
			title: eventLabel(eventType),
			detail: truncateText(stringifyCompact(summary), 260),
			status: terminalStatuses.has(String(status || state.status)) ? 'done' : 'active'
		});
	}

	if (terminalStatuses.has(state.status)) {
		state.eventSource?.close();
		state.eventSource = null;
		void loadArtifacts();
		void loadUploads();
		void loadTaskHistory();
	}

	renderStatus();
	saveWorkspace();
}

function handleToolCall(data) {
	const toolName = data.tool_name || data.name || 'tool';
	const input = data.tool_input && typeof data.tool_input === 'object' ? data.tool_input : {};
	if (toolName === 'show_text' && typeof input.text === 'string') {
		state.finalReportText = reportTextFromValue(input.text) || input.text;
		addThinkingStep({
			kind: 'report',
			title: '最终报告已生成',
			detail: '报告内容已直接写入对话，后续归档会一并打包保存。',
			status: 'done'
		});
		upsertReportMessage(state.finalReportText);
		state.activeTab = CONVERSATION_VIEW;
		saveWorkspace();
		renderMessages();
		renderTabs();
		renderReportControls();
		return;
	}

	if (typeof input.result === 'string') {
		const parsed = parseJson(input.result);
		if (parsed && typeof parsed === 'object' && parsed.success) {
			addThinkingStep({
				kind: 'tool',
				title: toolThinkingTitle(toolName, input),
				detail: toolThinkingDetail(toolName, input, parsed),
				status: 'done'
			});
			return;
		}
	}

	addThinkingStep({
		kind: 'tool',
		title: toolThinkingTitle(toolName, input),
		detail: toolThinkingDetail(toolName, input),
		status: 'active'
	});
}

function compactStartMessage(eventType, data) {
	if (eventType === 'start_of_workflow') {
		return '工作流已启动。';
	}
	if (eventType === 'start_of_agent') {
		return `智能体已启动：${data.agent_name || data.display_name || 'agent'}。`;
	}
	if (eventType === 'start_of_llm') {
		return `模型步骤已启动：${data.agent_name || 'agent'}。`;
	}
	return eventLabel(eventType);
}

function thinkingStepForStartEvent(eventType, data) {
	if (eventType === 'start_of_workflow') {
		return {
			kind: 'workflow',
			title: '启动研究工作流',
			detail: '开始组织检索、阅读、抽取、核验和报告生成步骤。',
			status: 'active'
		};
	}
	if (eventType === 'start_of_agent') {
		const name = data.agent_name || data.display_name || '研究智能体';
		return {
			kind: 'agent',
			title: `调度${name}`,
			detail: '分配当前研究阶段，准备调用检索、阅读或结构化整理能力。',
			status: 'active'
		};
	}
	if (eventType === 'start_of_llm') {
		return {
			kind: 'reasoning',
			title: '整理阶段性判断',
			detail: '模型正在阅读已有材料、规划下一步检索方向，并把信息转成可验证的研究结论。',
			status: 'active',
			meta: data.agent_name || ''
		};
	}
	return {
		kind: 'status',
		title: eventLabel(eventType),
		detail: eventThinkingDetail(data),
		status: 'active'
	};
}

function thinkingStepForArtifactEvent(eventType, data) {
	const labels = {
		evidence_collected: ['evidence', '新增证据', '有新的来源或摘录进入证据池，正在用于后续交叉验证。'],
		entity_extracted: ['entity', '识别关键实体', '从材料中抽取机构、人物、地点、政策或产业对象。'],
		relation_extracted: ['graph', '更新关系图谱', '识别实体之间的政策、贸易、投资或影响关系。'],
		timeline_event_added: ['timeline', '补充时间线', '把关键事件按时间顺序沉淀到时间线。'],
		map_feature_added: ['map', '补充地理线索', '把可定位的国家、地区、机构或项目沉淀到地图。'],
		verification_result: ['verification', '完成一轮核验', '对来源可信度、事实一致性或引用链进行检查。'],
		report_section_generated: ['report', '生成报告章节', '阶段性研究结论已写入报告结构。'],
		record_research_artifact: ['artifact', '沉淀研究成果', '新的结构化成果已保存到工作区。']
	};
	const [kind, title, fallback] = labels[eventType] || ['artifact', eventLabel(eventType), '研究成果已更新。'];
	return {
		kind,
		title,
		detail: eventThinkingDetail(data) || fallback,
		status: 'done',
		meta: artifactThinkingSummary()
	};
}

function toolThinkingTitle(toolName, input) {
	const normalized = String(toolName || '').replace(/[_-]+/g, ' ').trim();
	if (input.query) {
		return `检索：${truncateText(input.query, 72)}`;
	}
	if (input.url) {
		return `阅读来源：${truncateText(input.url, 72)}`;
	}
	return normalized ? `调用工具：${normalized}` : '调用研究工具';
}

function toolThinkingDetail(toolName, input, result = null) {
	if (result?.url) {
		return `已完成来源处理：${truncateText(result.url, 180)}。`;
	}
	if (result?.message || result?.summary) {
		return truncateText(String(result.message || result.summary), 260);
	}
	if (input.query) {
		return `围绕检索式收集公开资料，并筛选可进入证据池的来源。`;
	}
	if (input.url) {
		return '正在读取网页内容、提取可引用段落，并判断能否作为证据使用。';
	}
	const compact = stringifyCompact(input, 260);
	return compact && compact !== '{}' ? compact : `执行 ${toolName || 'tool'} 步骤。`;
}

function eventThinkingDetail(data) {
	if (!data || typeof data !== 'object') {
		return '';
	}
	const value =
		data.message ||
		data.summary ||
		data.title ||
		data.query ||
		data.url ||
		data.error ||
		data.reason ||
		'';
	return truncateText(typeof value === 'string' ? value : stringifyCompact(value), 280);
}

function artifactThinkingSummary() {
	const counts = artifactCounts();
	const parts = tabs
		.map((tab) => `${tab} ${counts[tab] || 0}`)
		.filter(Boolean);
	return parts.length ? `当前成果：${parts.join(' · ')}。` : '当前成果正在整理中。';
}

async function loadArtifacts() {
	if (!state.taskId) {
		return;
	}
	const context = captureAsyncContext();
	try {
		const data = await api(`/api/limira/tasks/${encodeURIComponent(state.taskId)}/artifacts`);
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		state.artifacts = normalizeArtifacts(data);
		upsertReportMessage(reportMarkdown());
		upsertArtifactThinkingStep();
		saveWorkspace();
		renderMessages();
		renderThinking();
		renderTabs();
		renderReportControls();
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		addMessage('error', `无法加载研究成果：${errorMessage(error)}`);
	}
}

async function loadUploads() {
	if (!state.user) {
		return;
	}
	if (!isEnterpriseAccount()) {
		state.uploads = [];
		state.cloudFiles = [];
		state.cloudStorage = null;
		state.uploadResults = [];
		state.currentUpload = null;
		renderUploads();
		renderHistoryFiles();
		renderCloudStorage();
		renderCloudDrive();
		return;
	}
	const context = captureAsyncContext();
	try {
		const historyData = await api('/api/limira/uploads/history');
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		state.cloudFiles = Array.isArray(historyData.documents) ? historyData.documents : [];
		state.cloudStorage = historyData.storage || null;
		if (state.taskId) {
			state.uploads = [];
		} else {
			state.uploads = reconcileSelectedUploads(state.uploads, state.cloudFiles);
		}
		state.uploadResults = [];
		saveWorkspace();
		renderUploads();
		renderHistoryFiles();
		renderCloudStorage();
		renderCloudDrive();
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		dom.uploadMessage.textContent = errorMessage(error);
		renderUploads();
		renderHistoryFiles();
		renderCloudStorage();
		renderCloudDrive();
	}
}

async function loadCloudStorage() {
	if (!state.user || !isEnterpriseAccount()) {
		return;
	}
	try {
		const data = await api('/api/limira/uploads/storage');
		state.cloudStorage = data.storage || state.cloudStorage;
		renderCloudStorage();
		renderCloudDrive();
	} catch (error) {
		dom.uploadMessage.textContent = errorMessage(error);
		renderUploads();
		renderCloudDrive();
	}
}

async function uploadDocument() {
	if (!isEnterpriseAccount()) {
		dom.uploadMessage.textContent = '云文件仅支持单位账号。';
		renderUploads();
		return;
	}
	const file = dom.uploadInput.files?.[0];
	if (!file || state.isUploading) {
		dom.uploadMessage.textContent = file ? '' : '请先选择文件。';
		renderUploads();
		return;
	}
	state.isUploading = true;
	state.currentUpload = {
		filename: file.name,
		byte_size: file.size,
		content_type: file.type || '文档',
		progress: 0,
		status: 'uploading',
		message: '正在上传'
	};
	dom.uploadMessage.textContent = '';
	renderUploads();
	const context = captureAsyncContext();
	const form = new FormData();
	form.append('file', file);
	if (state.taskId) {
		form.append('task_id', state.taskId);
	}
	try {
		const uploaded = await uploadFormData('/api/limira/uploads', form, (progress) => {
			if (!isCurrentAsyncContext(context) || !state.currentUpload) {
				return;
			}
			state.currentUpload.progress = progress;
			renderUploads();
		});
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		if (state.currentUpload) {
			state.currentUpload.progress = 100;
			state.currentUpload.status = 'complete';
			state.currentUpload.message = '上传完成';
			renderUploads();
		}
		if (uploaded && typeof uploaded === 'object') {
			state.uploads = mergeUploadedDocument(state.uploads, uploaded);
			saveWorkspace();
			renderUploads();
		}
		dom.uploadInput.value = '';
		dom.uploadMessage.textContent = '上传完成。';
		await loadUploads();
		if (isCurrentAsyncContext(context)) {
			state.currentUpload = null;
			renderUploads();
		}
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		if (state.currentUpload) {
			state.currentUpload.status = 'error';
			state.currentUpload.message = errorMessage(error);
		}
		dom.uploadMessage.textContent = errorMessage(error);
		renderUploads();
	} finally {
		if (isCurrentAsyncContext(context)) {
			state.isUploading = false;
		}
	}
}

function uploadFormData(path, form, onProgress) {
	return new Promise((resolve, reject) => {
		const xhr = new XMLHttpRequest();
		xhr.open('POST', path);
		xhr.withCredentials = true;
		xhr.setRequestHeader('accept', 'application/json');
		if (state.token) {
			xhr.setRequestHeader('authorization', `Bearer ${state.token}`);
		}
		xhr.upload.addEventListener('progress', (event) => {
			if (!event.lengthComputable) {
				return;
			}
			const progress = Math.max(1, Math.min(99, Math.round((event.loaded / event.total) * 100)));
			onProgress(progress);
		});
		xhr.addEventListener('load', () => {
			const contentType = xhr.getResponseHeader('content-type') || '';
			if (xhr.status >= 200 && xhr.status < 300) {
				try {
					resolve(contentType.includes('application/json') && xhr.responseText ? JSON.parse(xhr.responseText) : xhr.responseText);
				} catch {
					reject(new Error('服务返回的 JSON 不完整。请强制刷新页面后重试。'));
				}
				return;
			}
			if (xhr.status === 401) {
				state.user = null;
				renderShell();
			}
			const error = new Error(xhrResponseDetail(xhr));
			error.status = xhr.status;
			reject(error);
		});
		xhr.addEventListener('error', () => reject(new Error('上传失败，请检查网络后重试。')));
		xhr.addEventListener('abort', () => reject(new Error('上传已取消。')));
		xhr.send(form);
	});
}

function xhrResponseDetail(xhr) {
	const text = xhr.responseText || xhr.statusText || `请求失败，状态码 ${xhr.status}`;
	try {
		const json = JSON.parse(text);
		const detail = typeof json.detail === 'string' ? json.detail : JSON.stringify(json.detail || json);
		return localizedErrorDetail(detail);
	} catch {
		return text;
	}
}

async function searchUploads() {
	const query = dom.uploadSearchInput.value.trim();
	if (!query || state.isSearching) {
		return;
	}
	state.isSearching = true;
	const context = captureAsyncContext();
	try {
		const taskParam = state.taskId ? `&task_id=${encodeURIComponent(state.taskId)}` : '';
		const data = await api(`/api/limira/uploads/search?query=${encodeURIComponent(query)}${taskParam}`);
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		state.uploadResults = Array.isArray(data.documents) ? data.documents : [];
		renderUploads();
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		dom.uploadMessage.textContent = errorMessage(error);
		renderUploads();
	} finally {
		if (isCurrentAsyncContext(context)) {
			state.isSearching = false;
		}
	}
}

function isEnterpriseAdmin() {
	return (
		state.user?.account_type === 'enterprise' &&
		state.user?.organization_id &&
		state.user?.organization_role === 'admin'
	);
}

async function loadEnterpriseAdmin() {
	if (!isEnterpriseAdmin()) {
		state.enterpriseMembers = [];
		state.enterpriseUsage = null;
		renderEnterpriseAdmin();
		return;
	}
	if (state.isLoadingEnterpriseAdmin) {
		return;
	}
	state.isLoadingEnterpriseAdmin = true;
	if (dom.enterpriseMemberMessage) {
		dom.enterpriseMemberMessage.textContent = '正在加载单位账号...';
	}
	try {
		const [members, usage] = await Promise.all([
			api('/api/limira/enterprise/members'),
			api('/api/limira/enterprise/usage')
		]);
		state.enterpriseMembers = Array.isArray(members?.members) ? members.members : [];
		state.enterpriseUsage = usage?.usage || null;
		if (dom.enterpriseMemberMessage) {
			dom.enterpriseMemberMessage.textContent = '';
		}
	} catch (error) {
		if (dom.enterpriseMemberMessage) {
			dom.enterpriseMemberMessage.textContent = `单位管理加载失败：${errorMessage(error)}`;
		}
	} finally {
		state.isLoadingEnterpriseAdmin = false;
		renderEnterpriseAdmin();
	}
}

async function createEnterpriseMember() {
	if (!isEnterpriseAdmin()) {
		return;
	}
	const username = dom.enterpriseMemberUsernameInput.value.trim();
	const password = dom.enterpriseMemberPasswordInput.value;
	const name = dom.enterpriseMemberNameInput.value.trim();
	const organizationRole = dom.enterpriseMemberRoleSelect.value || 'member';
	if (!username || !password) {
		dom.enterpriseMemberMessage.textContent = '请输入用户名和初始密码。';
		return;
	}
	dom.createEnterpriseMemberButton.disabled = true;
	dom.enterpriseMemberMessage.textContent = '正在添加单位账号...';
	try {
		await api('/api/limira/enterprise/members', {
			method: 'POST',
			body: {
				username,
				password,
				name: name || username,
				organization_role: organizationRole
			}
		});
		dom.enterpriseMemberNameInput.value = '';
		dom.enterpriseMemberUsernameInput.value = '';
		dom.enterpriseMemberPasswordInput.value = '';
		dom.enterpriseMemberRoleSelect.value = 'member';
		dom.enterpriseMemberMessage.textContent = '单位账号已添加。';
		await loadEnterpriseAdmin();
	} catch (error) {
		dom.enterpriseMemberMessage.textContent = errorMessage(error);
	} finally {
		dom.createEnterpriseMemberButton.disabled = false;
	}
}

async function downloadArchive() {
	if (state.restoreBlocked) {
		dom.reportMessage.textContent = '任务暂未从后端确认，恢复后再下载归档。';
		return;
	}
	if (!state.taskId) {
		return;
	}
	const url = state.archiveDownloadUrl || defaultArchiveDownloadUrl(state.taskId);
	try {
		await downloadGeneratedArchive(url, 'archive.zip');
		state.archiveStatus = 'ready';
		state.archiveDownloadUrl = url;
		saveWorkspace();
		renderStatus();
		dom.reportMessage.textContent = '归档已下载。';
		await loadUploads();
	} catch (error) {
		dom.reportMessage.textContent = `归档下载失败：${errorMessage(error)}`;
	}
}

async function downloadGeneratedArchive(url, filename) {
	const headers = new Headers({ accept: 'application/zip' });
	if (state.token) {
		headers.set('authorization', `Bearer ${state.token}`);
	}
	const response = await fetch(url, {
		method: 'GET',
		headers,
		credentials: 'include'
	});
	if (!response.ok) {
		throw new Error(await responseDetail(response));
	}
	const blob = await response.blob();
	if (!blob.size) {
		throw new Error('empty_archive_download');
	}
	const objectUrl = URL.createObjectURL(blob);
	const link = document.createElement('a');
	link.href = objectUrl;
	link.download = filename;
	document.body.appendChild(link);
	link.click();
	link.remove();
	window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

function hasResearchSurface() {
	const counts = artifactCounts();
	return Boolean(
		state.taskId ||
		state.isSubmitting ||
		state.finalReportText ||
		Object.values(counts).some((count) => Number(count || 0) > 0)
	);
}

function hasConversationActivity() {
	return Boolean(
		state.messages.length ||
		state.taskId ||
		state.isSubmitting ||
		state.thinkingSteps.some((step) => step.kind !== 'ready')
	);
}

function isArtifactView() {
	return tabs.includes(state.activeTab);
}

function isConversationView() {
	return !isArtifactView();
}

function renderStatus() {
	dom.statusLabel.textContent = statusLabel(state.status);
	dom.taskLabel.textContent = state.taskId
		? `任务 ${state.taskId} · 归档${archiveStatusLabel(state.archiveStatus)}${
				state.restoreBlocked ? ' · 待恢复确认' : ''
			}`
		: '暂无任务';
	dom.submitResearchButton.disabled = state.isSubmitting;
	if (dom.downloadArchiveButton) {
		dom.downloadArchiveButton.disabled = state.restoreBlocked || !state.taskId;
		dom.downloadArchiveButton.textContent = `归档 ${archiveStatusLabel(state.archiveStatus)}`;
	}
}

function renderMessages() {
	const artifactView = isArtifactView();
	dom.conversationPanel?.classList.toggle('compact', artifactView);
	const sourceMessages = artifactView
		? state.messages.filter((message) => message.kind !== 'report').slice(-2)
		: state.messages.slice(-80);
	const messages = sourceMessages;
	dom.messageList.innerHTML = messages
		.map(
			(message) => `<article class="message ${escapeHtml(message.role)} ${escapeHtml(message.kind || '')}">
				<div class="message-meta"><span>${escapeHtml(roleLabel(message.role))}</span><span>${escapeHtml(message.time)}</span></div>
				<div class="message-body ${message.format === 'markdown' ? 'markdown-body compact-markdown' : ''}">${message.format === 'markdown' ? renderMarkdown(message.content) : escapeHtml(message.content)}</div>
			</article>`
		)
		.join('');
	dom.messageList.scrollTop = dom.messageList.scrollHeight;
}

function renderThinking() {
	if (!dom.thinkingPanel) {
		return;
	}
	const steps = state.thinkingSteps.length ? state.thinkingSteps : initialThinkingSteps();
	dom.thinkingPanel.classList.toggle('collapsed', state.thinkingCollapsed);
	dom.thinkingToggleButton.setAttribute('aria-expanded', state.thinkingCollapsed ? 'false' : 'true');
	dom.thinkingToggleLabel.textContent = state.thinkingCollapsed ? '展开思考过程' : '隐藏思考过程';
	dom.thinkingStepCount.textContent = `${steps.length}`;
	dom.thinkingList.innerHTML = state.thinkingCollapsed
		? ''
		: steps
				.map((step) => `<article class="thinking-step ${escapeAttr(step.kind || 'task')} ${escapeAttr(step.status || 'active')}">
					<div class="thinking-step-dot" aria-hidden="true"></div>
					<div class="thinking-step-content">
						<div class="thinking-step-title">${escapeHtml(step.title)}</div>
						${step.detail ? `<div class="thinking-step-detail">${escapeHtml(step.detail)}</div>` : ''}
						<div class="thinking-step-meta">${[step.meta, step.time].filter(Boolean).map(escapeHtml).join(' · ')}</div>
					</div>
				</article>`)
				.join('');
}

function renderTabs() {
	const surfaceVisible = hasResearchSurface();
	const conversationView = isConversationView();
	dom.workspaceContent.classList.toggle('artifact-mode', isArtifactView());
	dom.workspaceContent.classList.toggle('conversation-mode', conversationView);
	dom.inputContainer?.classList.toggle('hidden', state.route !== 'workspace' || !conversationView);
	dom.thinkingPanel?.classList.toggle('hidden', !conversationView || !hasConversationActivity());
	dom.artifactContent.classList.toggle('hidden', conversationView || !surfaceVisible);
	dom.artifactTabs.classList.toggle('hidden', !surfaceVisible);
	if (!surfaceVisible) {
		dom.artifactTabs.innerHTML = '';
		dom.downloadArchiveButton = null;
		renderArtifactContent();
		renderStatus();
		return;
	}
	const counts = artifactCounts();
	const archiveDisabled = state.restoreBlocked || !state.taskId;
	dom.artifactTabs.innerHTML = [
		isArtifactView()
			? `<button type="button" class="back-tab" data-tab="${CONVERSATION_VIEW}">${BACK_TO_CHAT_LABEL}</button>`
			: '',
		...tabs
		.map(
			(tab) =>
				`<button type="button" class="${tab === state.activeTab ? 'active' : ''}" data-tab="${tab}">${tab} ${counts[tab] || ''}</button>`
		),
		`<button id="downloadArchiveButton" type="button" class="archive-tab" data-archive-download ${archiveDisabled ? 'disabled' : ''}>归档 ${archiveStatusLabel(state.archiveStatus)}</button>`
	].join('');
	for (const button of dom.artifactTabs.querySelectorAll('[data-tab]')) {
		button.addEventListener('click', () => {
			state.activeTab = tabs.includes(button.dataset.tab) ? button.dataset.tab : CONVERSATION_VIEW;
			saveWorkspace();
			renderMessages();
			renderThinking();
			renderTabs();
			if (isArtifactView()) {
				dom.workspaceContent.scrollTo({ top: 0, behavior: 'smooth' });
			}
		});
	}
	dom.downloadArchiveButton = dom.artifactTabs.querySelector('[data-archive-download]');
	dom.downloadArchiveButton?.addEventListener('click', () => void downloadArchive());
	renderArtifactContent();
	renderStatus();
}

function renderArtifactContent() {
	if (!isArtifactView() || !hasResearchSurface()) {
		dom.artifactContent.innerHTML = '';
		return;
	}
	if (state.activeTab === '证据') {
		renderEvidence();
	} else if (state.activeTab === '实体') {
		renderEntities();
	} else if (state.activeTab === '图谱') {
		renderGraph();
	} else if (state.activeTab === '时间线') {
		renderTimeline();
	} else if (state.activeTab === '地图') {
		renderMap();
	}
}

function renderEvidence() {
	const items = state.artifacts.evidence;
	dom.artifactContent.innerHTML = items.length
		? `<div class="artifact-grid">${items.map(evidenceCard).join('')}</div>`
		: emptyState('证据条目会显示在这里。');
}

function evidenceCard(item, index) {
	const id = item.evidence_id || item.ref_id || item.id || `EVID-${String(index + 1).padStart(3, '0')}`;
	const title = item.title || item.source || id;
	const url = safeExternalUrl(item.url || item.source_url || '');
	const summary = item.summary || item.text || item.description || '';
	return `<article id="evidence-${safeDomId(id)}" class="artifact-card">
		<div class="artifact-title">${escapeHtml(title)}</div>
		<div class="artifact-meta">
			<span>${escapeHtml(id)}</span>
			${item.confidence ? `<span>置信度 ${escapeHtml(item.confidence)}</span>` : ''}
			${item.published_at ? `<span>${escapeHtml(item.published_at)}</span>` : ''}
		</div>
		<div class="artifact-body markdown-body compact-markdown">${renderMarkdown(summary)}</div>
		${url ? `<a href="${escapeAttr(url)}" class="sandbox-link" data-title="${escapeAttr(title)}" data-summary="${escapeAttr(summary)}" target="_blank" rel="noopener noreferrer">打开来源</a>` : ''}
	</article>`;
}

function openEvidenceSource({ url, title, summary }) {
	const safeUrl = safeExternalUrl(url);
	if (!safeUrl) {
		return;
	}
	if (evidencePreviewMode(safeUrl) === 'external') {
		openExternalEvidenceUrl(safeUrl);
		return;
	}
	showEvidenceIframePreview({ url: safeUrl, title, summary });
}

function evidencePreviewMode(url) {
	const safeUrl = safeExternalUrl(url);
	if (!safeUrl) {
		return 'external';
	}
	const parsed = new URL(safeUrl);
	if (isLikelyIframeDocument(parsed)) {
		return 'iframe';
	}
	return isDirectExternalEvidenceHost(parsed.hostname) ? 'external' : 'iframe';
}

function isLikelyIframeDocument(url) {
	return /\.(?:pdf|txt|csv|png|jpe?g|gif|webp)$/i.test(url.pathname);
}

function isDirectExternalEvidenceHost(hostname) {
	const normalizedHost = String(hostname || '').toLowerCase().replace(/^www\./, '');
	return DIRECT_EXTERNAL_EVIDENCE_HOSTS.some(
		(blockedHost) =>
			normalizedHost === blockedHost || normalizedHost.endsWith(`.${blockedHost}`)
	);
}

function showEvidenceIframePreview({ url, title, summary }) {
	state.sandboxPreviewId += 1;
	const previewId = state.sandboxPreviewId;
	state.sandboxPreviewLoaded = false;
	state.sandboxPreviewUrl = url;
	state.sandboxPreviewTitle = title || '网页预览';
	state.sandboxPreviewSummary = summary || '';
	clearSandboxPreviewTimer();

	dom.sandboxTitle.textContent = state.sandboxPreviewTitle;
	dom.sandboxExternalLink.href = url;
	dom.sandboxIframe.removeAttribute('srcdoc');
	dom.sandboxIframe.removeAttribute('src');
	dom.sandboxIframe.src = url;
	dom.sandboxModal.classList.remove('hidden');

	state.sandboxPreviewTimer = window.setTimeout(() => {
		if (state.sandboxPreviewId !== previewId || state.sandboxPreviewLoaded) {
			return;
		}
		redirectBlockedEvidenceSource({
			url,
			title: state.sandboxPreviewTitle,
			summary: state.sandboxPreviewSummary
		});
	}, IFRAME_PREVIEW_TIMEOUT_MS);
}

function redirectBlockedEvidenceSource({ url, title, summary }) {
	const safeUrl = safeExternalUrl(url);
	if (!safeUrl) {
		return;
	}
	clearSandboxPreviewTimer();
	state.sandboxPreviewId += 1;
	openExternalEvidenceUrl(safeUrl);
	showEvidenceSummaryFallback({
		url: safeUrl,
		title,
		summary,
		notice: '该来源可能禁止嵌入预览，已自动尝试在新标签页打开。这里保留 Limira 已保存的来源摘要。'
	});
}

function showEvidenceSummaryFallback({ url, title, summary, notice }) {
	state.sandboxPreviewUrl = safeExternalUrl(url);
	state.sandboxPreviewTitle = title || '网页预览';
	state.sandboxPreviewSummary = summary || '';
	state.sandboxPreviewLoaded = false;
	dom.sandboxTitle.textContent = state.sandboxPreviewTitle;
	dom.sandboxExternalLink.href = state.sandboxPreviewUrl;
	dom.sandboxIframe.removeAttribute('src');
	dom.sandboxIframe.srcdoc = evidencePreviewHtml({
		title: state.sandboxPreviewTitle,
		url: state.sandboxPreviewUrl,
		summary: state.sandboxPreviewSummary,
		notice
	});
	dom.sandboxModal.classList.remove('hidden');
}

function clearSandboxPreviewTimer() {
	if (!state.sandboxPreviewTimer) {
		return;
	}
	window.clearTimeout(state.sandboxPreviewTimer);
	state.sandboxPreviewTimer = null;
}

function closeSandboxPreview() {
	clearSandboxPreviewTimer();
	state.sandboxPreviewId += 1;
	state.sandboxPreviewLoaded = false;
	state.sandboxPreviewUrl = '';
	state.sandboxPreviewTitle = '';
	state.sandboxPreviewSummary = '';
	dom.sandboxModal.classList.add('hidden');
	dom.sandboxIframe.removeAttribute('src');
	dom.sandboxIframe.srcdoc = '';
}

function openExternalEvidenceUrl(url) {
	const safeUrl = safeExternalUrl(url);
	if (!safeUrl) {
		return;
	}
	const link = document.createElement('a');
	link.href = safeUrl;
	link.target = '_blank';
	link.rel = 'noopener noreferrer';
	link.referrerPolicy = 'no-referrer';
	link.style.display = 'none';
	document.body.appendChild(link);
	link.click();
	link.remove();
}

function evidencePreviewHtml({ title, url, summary, notice }) {
	const safeTitle = escapeHtml(title || '网页预览');
	const safeUrl = escapeAttr(url || '');
	const safeDisplayUrl = escapeHtml(url || '');
	const safeSummary = renderMarkdown(summary || '该来源没有可用的本地摘要。');
	const safeNotice = escapeHtml(
		notice || '部分网站禁止被第三方页面嵌入预览。这里显示 Limira 已保存的来源摘要；需要查看原网页时，请使用右上角外部打开按钮。'
	);
	return `<!doctype html>
<html lang="zh-CN">
	<head>
		<meta charset="utf-8" />
		<meta name="viewport" content="width=device-width, initial-scale=1" />
		<style>
			body{margin:0;padding:24px;font-family:Inter,Arial,sans-serif;color:#111827;background:#fff;line-height:1.65}
			h1{font-size:20px;margin:0 0 12px}
			p{margin:0 0 16px}
			.notice{border:1px solid #d8dee8;border-radius:8px;padding:14px 16px;background:#f8fafc;color:#334155}
			a{color:#2563eb;text-decoration:none;word-break:break-all}
			.summary{line-height:1.65}
			.summary h1,.summary h2,.summary h3{font-size:16px;margin:16px 0 8px}
			.summary p{margin:0 0 12px}
			.summary ul,.summary ol{padding-left:20px;margin:0 0 12px}
			.summary li{margin:4px 0}
			.summary table{border-collapse:collapse;width:100%;margin:0 0 12px}
			.summary th,.summary td{border:1px solid #d8dee8;padding:6px 8px;text-align:left}
		</style>
	</head>
	<body>
		<h1>${safeTitle}</h1>
		<p><a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${safeDisplayUrl}</a></p>
		<p class="notice">${safeNotice}</p>
		<div class="summary">${safeSummary}</div>
	</body>
</html>`;
}

function renderEntities() {
	const entities = state.artifacts.entities;
	dom.artifactContent.innerHTML = entities.length
		? `<div class="artifact-grid">${entities
				.map((item, index) => {
					const label = item.name || item.label || item.entity_id || `实体 ${index + 1}`;
					const type = item.type || item.entity_type || 'entity';
					return `<article class="artifact-card">
						<div class="artifact-title">${escapeHtml(label)}</div>
						<div class="artifact-meta"><span>${escapeHtml(type)}</span></div>
						<div class="artifact-body">${escapeHtml(item.summary || item.description || stringifyCompact(item.payload || item))}</div>
					</article>`;
				})
				.join('')}</div>`
		: emptyState('实体信息会显示在这里。');
}

function renderGraph() {
	const entities = state.artifacts.entities;
	const relations = state.artifacts.relations;
	if (!entities.length && !relations.length) {
		dom.artifactContent.innerHTML = emptyState('实体和关系会在这里形成图谱。');
		return;
	}
	const nodes = entities.map((entity, index) => ({
		id: String(entity.entity_id || entity.id || entity.name || `entity-${index + 1}`),
		label: String(entity.name || entity.label || entity.entity_id || `实体 ${index + 1}`)
	}));
	const nodeIds = new Set(nodes.map((node) => node.id));
	for (const relation of relations) {
		for (const id of [relationSource(relation), relationTarget(relation)]) {
			if (id && !nodeIds.has(id)) {
				nodeIds.add(id);
				nodes.push({ id, label: id });
			}
		}
	}
	dom.artifactContent.innerHTML = `${graphSvg(nodes, relations)}
		<div class="artifact-grid">${relations.map(relationCard).join('')}</div>`;
}

function graphSvg(nodes, relations) {
	const width = 780;
	const height = 420;
	const centerX = width / 2;
	const centerY = height / 2;
	const radius = Math.min(width, height) * 0.36;
	const positions = new Map();
	nodes.forEach((node, index) => {
		const angle = (Math.PI * 2 * index) / Math.max(nodes.length, 1) - Math.PI / 2;
		positions.set(node.id, {
			x: centerX + Math.cos(angle) * radius,
			y: centerY + Math.sin(angle) * radius
		});
	});
	const edges = relations
		.map((relation) => {
			const source = positions.get(relationSource(relation));
			const target = positions.get(relationTarget(relation));
			if (!source || !target) return '';
			return `<line x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}" stroke="#64748b" stroke-width="1.5" />`;
		})
		.join('');
	const renderedNodes = nodes
		.map((node) => {
			const pos = positions.get(node.id);
			return `<g>
				<circle cx="${pos.x}" cy="${pos.y}" r="17" fill="#0f766e" />
				<text x="${pos.x}" y="${pos.y + 31}" text-anchor="middle" font-size="11" fill="#14202b">${escapeSvg(node.label.slice(0, 28))}</text>
			</g>`;
		})
		.join('');
	return `<svg class="graph-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="研究图谱">${edges}${renderedNodes}</svg>`;
}

function relationCard(relation, index) {
	return `<article class="artifact-card">
		<div class="artifact-title">${escapeHtml(relation.type || relation.relation_type || `关系 ${index + 1}`)}</div>
		<div class="artifact-meta"><span>${escapeHtml(relationSource(relation))}</span><span>${escapeHtml(relationTarget(relation))}</span></div>
		<div class="artifact-body">${escapeHtml(relation.summary || relation.description || stringifyCompact(relation))}</div>
	</article>`;
}

function renderTimeline() {
	const items = state.artifacts.timeline_events;
	dom.artifactContent.innerHTML = items.length
		? `<div class="timeline">${items
				.map((item, index) => {
					const title = item.title || item.event_title || `时间线事件 ${index + 1}`;
					const date = item.date || item.timestamp || item.occurred_at || '';
					return `<article class="timeline-item">
						<div class="artifact-title">${escapeHtml(title)}</div>
						<div class="artifact-meta">${date ? `<span>${escapeHtml(date)}</span>` : ''}</div>
						<div class="artifact-body">${escapeHtml(item.summary || item.description || stringifyCompact(item))}</div>
					</article>`;
				})
				.join('')}</div>`
		: emptyState('时间线事件会显示在这里。');
}

function renderMap() {
	const features = mapFeatures();
	if (!features.length) {
		dom.artifactContent.innerHTML = emptyState('带 GeoJSON 坐标的地图要素会显示在这里。');
		return;
	}
	dom.artifactContent.innerHTML = `${mapSvg(features)}
		<div class="artifact-grid">${features
			.map(
				(feature) => `<article class="artifact-card">
					<div class="artifact-title">${escapeHtml(feature.title)}</div>
					<div class="artifact-meta"><span>${escapeHtml(feature.geometry.type)}</span></div>
					<div class="artifact-body">${escapeHtml(feature.summary || '')}</div>
				</article>`
			)
			.join('')}</div>`;
}

function mapSvg(features) {
	const width = 780;
	const height = 420;
	const shapes = features
		.map((feature) => geometrySvg(feature.geometry, width, height))
		.join('');
	return `<svg class="map-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="研究地图">
		<rect x="0" y="0" width="${width}" height="${height}" fill="transparent" />
		${shapes}
	</svg>`;
}

function geometrySvg(geometry, width, height) {
	const pairs = collectPairs(geometry.coordinates);
	if (!pairs.length) return '';
	if (geometry.type.includes('Polygon')) {
		const points = pairs.map((pair) => project(pair, width, height).join(',')).join(' ');
		return `<polygon points="${points}" fill="rgba(15,118,110,0.22)" stroke="#0f766e" stroke-width="2" />`;
	}
	if (geometry.type.includes('LineString')) {
		const points = pairs.map((pair) => project(pair, width, height).join(',')).join(' ');
		return `<polyline points="${points}" fill="none" stroke="#0f766e" stroke-width="3" />`;
	}
	return pairs
		.map((pair) => {
			const [x, y] = project(pair, width, height);
			return `<circle cx="${x}" cy="${y}" r="6" fill="#2563eb" stroke="#fff" stroke-width="2" />`;
		})
		.join('');
}

function project([lng, lat], width, height) {
	return [((Number(lng) + 180) / 360) * width, ((90 - Number(lat)) / 180) * height];
}

function renderReport() {
	const sections = state.artifacts.report_sections;
	const cards = sections
		.map((section, index) => reportCard(reportTitle(section, index), reportText(section), section.evidence_refs))
		.join('');
	const finalCard = !cards && state.finalReportText
		? reportCard('最终回答', reportTextFromValue(state.finalReportText), reportEvidenceRefs())
		: '';
	dom.artifactContent.innerHTML =
		cards || finalCard
			? `<div class="artifact-grid">${finalCard}${cards}</div>`
			: emptyState('报告章节或最终回答会显示在这里。');
}

function reportCard(title, text, refs = []) {
	const refChips = asArray(refs)
		.map((ref) => String(ref).replace(/^\[|\]$/g, ''))
		.filter(Boolean)
		.map(
			(ref) =>
				`<button class="ref-chip" type="button" data-ref="${escapeAttr(ref)}">${escapeHtml(ref)}</button>`
		)
		.join('');
	window.setTimeout(bindRefChips, 0);
	return `<article class="artifact-card">
		<div class="artifact-title">${escapeHtml(title)}</div>
		<div class="artifact-body markdown-body">${renderMarkdown(text)}</div>
		${refChips ? `<div class="refs">${refChips}</div>` : ''}
	</article>`;
}

function renderMarkdown(markdown) {
	const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
	const blocks = [];
	let index = 0;
	while (index < lines.length) {
		const line = lines[index];
		if (!line.trim()) {
			index += 1;
			continue;
		}
		const heading = line.match(/^(#{1,6})\s+(.+)$/);
		if (heading) {
			const level = heading[1].length;
			blocks.push(`<h${level}>${renderInlineMarkdown(heading[2].trim())}</h${level}>`);
			index += 1;
			continue;
		}
		if (/^\s*---+\s*$/.test(line)) {
			blocks.push('<hr>');
			index += 1;
			continue;
		}
		if (isMarkdownTable(lines, index)) {
			const { html, nextIndex } = renderMarkdownTable(lines, index);
			blocks.push(html);
			index = nextIndex;
			continue;
		}
		if (/^\s*[-*]\s+/.test(line)) {
			const items = [];
			while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
				items.push(lines[index].replace(/^\s*[-*]\s+/, ''));
				index += 1;
			}
			blocks.push(`<ul>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join('')}</ul>`);
			continue;
		}
		if (/^\s*\d+\.\s+/.test(line)) {
			const items = [];
			while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index])) {
				items.push(lines[index].replace(/^\s*\d+\.\s+/, ''));
				index += 1;
			}
			blocks.push(`<ol>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join('')}</ol>`);
			continue;
		}
		const paragraph = [];
		while (
			index < lines.length &&
			lines[index].trim() &&
			!lines[index].match(/^(#{1,6})\s+(.+)$/) &&
			!isMarkdownTable(lines, index) &&
			!/^\s*[-*]\s+/.test(lines[index]) &&
			!/^\s*\d+\.\s+/.test(lines[index]) &&
			!/^\s*---+\s*$/.test(lines[index])
		) {
			paragraph.push(lines[index]);
			index += 1;
		}
		blocks.push(`<p>${paragraph.map(renderInlineMarkdown).join('<br>')}</p>`);
	}
	return blocks.join('\n') || '<p></p>';
}

function isMarkdownTable(lines, index) {
	return (
		index + 1 < lines.length &&
		lines[index].includes('|') &&
		/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1])
	);
}

function renderMarkdownTable(lines, index) {
	const header = splitMarkdownTableRow(lines[index]);
	index += 2;
	const rows = [];
	while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
		rows.push(splitMarkdownTableRow(lines[index]));
		index += 1;
	}
	const html = `<table><thead><tr>${header
		.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`)
		.join('')}</tr></thead><tbody>${rows
		.map(
			(row) =>
				`<tr>${header
					.map((_, cellIndex) => `<td>${renderInlineMarkdown(row[cellIndex] || '')}</td>`)
					.join('')}</tr>`
		)
		.join('')}</tbody></table>`;
	return { html, nextIndex: index };
}

function splitMarkdownTableRow(row) {
	return row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

function renderInlineMarkdown(value) {
	let htmlText = escapeHtml(value);
	htmlText = htmlText.replace(/`([^`]+)`/g, '<code>$1</code>');
	htmlText = htmlText.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
	htmlText = htmlText.replace(/\*([^*]+)\*/g, '<em>$1</em>');
	htmlText = htmlText.replace(
		/(https?:\/\/[^\s<]+[^<.,;:!?)\]\s])/g,
		'<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
	);
	return htmlText;
}

function bindRefChips() {
	for (const chip of dom.artifactContent.querySelectorAll('.ref-chip')) {
		chip.addEventListener('click', () => {
			state.activeTab = '证据';
			renderTabs();
			const target = document.getElementById(`evidence-${safeDomId(chip.dataset.ref || '')}`);
			target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
		});
	}
}

function renderUploads() {
	const uploads = state.uploadResults.length ? state.uploadResults : state.uploads;
	const cards = [];
	if (state.currentUpload) {
		cards.push(renderUploadCard(state.currentUpload, { transient: true }));
	}
	for (const document of uploads) {
		cards.push(renderUploadCard(document));
	}
	dom.uploadList.innerHTML = cards.join('');
	dom.uploadList.classList.toggle('hidden', cards.length === 0);
	dom.uploadMessage.classList.toggle('hidden', !dom.uploadMessage.textContent.trim());
}

function renderUploadCard(document, options = {}) {
	const transient = Boolean(options.transient);
	const id = String(document.document_id || '');
	const title = document.filename || document.original_filename || id || '上传文件';
	const progress = Math.max(0, Math.min(100, Number(document.progress || 0)));
	const status = String(document.status || '').toLowerCase();
	const score = typeof document.score === 'number' ? `相关度 ${document.score.toFixed(3)}` : '';
	const statusText =
		status === 'error'
			? '上传失败'
			: status === 'complete'
				? '上传完成'
				: transient
					? `上传中 ${progress}%`
					: '已上传';
	const meta = [document.content_type || '文档', formatBytes(document.byte_size), score, statusText].filter(Boolean);
	const progressBar = transient
		? `<div class="attachment-progress" aria-label="上传进度"><span style="width: ${progress}%"></span></div>`
		: '';
	const message = document.message ? `<div class="attachment-message">${escapeHtml(document.message)}</div>` : '';
	const remove =
		!transient && id
			? `<button class="attachment-remove" type="button" data-remove-upload-id="${escapeAttr(id)}" title="从本次对话移除">×</button>`
			: '';
	return `<article class="attachment-card ${transient ? `upload-${escapeAttr(status || 'uploading')}` : ''}">
		<div class="attachment-icon" aria-hidden="true">
			<svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M14 3H7C5.89543 3 5 3.89543 5 5V19C5 20.1046 5.89543 21 7 21H17C18.1046 21 19 20.1046 19 19V8M14 3L19 8M14 3V8H19" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>
		</div>
		<div class="attachment-content">
			<div class="attachment-title">${escapeHtml(title)}</div>
			<div class="attachment-meta">${meta.map((item) => `<span>${escapeHtml(item)}</span>`).join('')}</div>
			${progressBar}
			${document.snippet ? `<div class="attachment-message">${escapeHtml(document.snippet)}</div>` : message}
		</div>
		<div class="attachment-actions">${remove}</div>
	</article>`;
}

function renderHistoryFiles() {
	if (!dom.historyFilePanel || !dom.historyFileList) {
		return;
	}
	dom.historyFilePanel.classList.toggle('hidden', !state.historyFilesOpen);
	const selectedIds = new Set(selectedUploadDocumentIds());
	dom.historyFileList.innerHTML = state.cloudFiles.length
		? state.cloudFiles
				.map((document) => {
					const id = String(document.document_id || '');
					const selected = selectedIds.has(id);
					return `<button class="history-file-option${selected ? ' selected' : ''}" type="button" data-history-document-id="${escapeAttr(id)}" ${selected ? 'disabled' : ''}>
						<span class="history-file-icon" aria-hidden="true">
							<svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M14 3H7C5.89543 3 5 3.89543 5 5V19C5 20.1046 5.89543 21 7 21H17C18.1046 21 19 20.1046 19 19V8M14 3L19 8M14 3V8H19" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>
						</span>
						<span class="history-file-text">
							<span class="history-file-title">${escapeHtml(document.filename || id || '历史文件')}</span>
							<span class="history-file-meta">${escapeHtml(document.content_type || '文档')} · ${formatBytes(document.byte_size)}${selected ? ' · 已加入' : ''}</span>
						</span>
					</button>`;
				})
				.join('')
		: '<div class="empty-state compact-empty">暂无历史文件。</div>';
}

function selectHistoryFile(documentId) {
	const document = state.cloudFiles.find((item) => String(item.document_id || '') === documentId);
	if (!document || state.taskId) {
		return;
	}
	state.uploads = mergeUploadedDocument(state.uploads, document);
	state.uploadResults = [];
	state.historyFilesOpen = false;
	saveWorkspace();
	renderUploads();
	renderHistoryFiles();
}

function removeSelectedUpload(documentId) {
	if (!documentId) {
		return;
	}
	state.uploads = state.uploads.filter(
		(document) => String(document.document_id || '') !== documentId
	);
	state.uploadResults = state.uploadResults.filter(
		(document) => String(document.document_id || '') !== documentId
	);
	saveWorkspace();
	renderUploads();
	renderHistoryFiles();
}

function selectedUploadDocumentIds() {
	return state.uploads
		.map((document) => String(document.document_id || '').trim())
		.filter(Boolean);
}

function reconcileSelectedUploads(selectedUploads, cloudFiles) {
	const cloudById = new Map(
		(Array.isArray(cloudFiles) ? cloudFiles : []).map((document) => [
			String(document.document_id || ''),
			document,
		])
	);
	return (Array.isArray(selectedUploads) ? selectedUploads : [])
		.map((document) => cloudById.get(String(document.document_id || '')) || document)
		.filter((document) => String(document.document_id || ''));
}

function mergeUploadedDocument(documents, uploaded) {
	const documentId = String(uploaded.document_id || '').trim();
	if (!documentId) {
		return Array.isArray(documents) ? documents : [];
	}
	const next = [uploaded, ...(Array.isArray(documents) ? documents : []).filter(
		(document) => String(document.document_id || '') !== documentId
	)];
	return next;
}

function renderReportControls() {
	if (dom.reportMessage) {
		dom.reportMessage.classList.toggle('hidden', !dom.reportMessage.textContent.trim());
	}
}

function renderEnterpriseAdmin() {
	if (!dom.enterpriseAdminPage) {
		return;
	}
	const admin = Boolean(state.user) && isEnterpriseAdmin();
	const pageVisible = admin && state.route === 'enterprise-admin';
	dom.enterpriseAdminManageButton.classList.toggle('hidden', !admin);
	dom.enterpriseAdminManageButton.classList.toggle('active', pageVisible);
	if (!admin) {
		dom.enterpriseMemberList.innerHTML = '';
		dom.enterpriseUsageSummary.textContent = '';
		dom.enterpriseMemberMessage.textContent = '';
		return;
	}
	dom.refreshEnterpriseAdminButton.disabled = state.isLoadingEnterpriseAdmin;
	const researchTasks = Number(state.enterpriseUsage?.totals?.research_task || 0);
	const days = Number(state.enterpriseUsage?.days || 30);
	dom.enterpriseUsageSummary.textContent = `${days} 天内研究任务 ${researchTasks} 次，按成员计入单位账单。`;
	dom.enterpriseMemberList.innerHTML = state.enterpriseMembers.length
		? state.enterpriseMembers
				.map((member) => {
					const role = member.organization_role === 'admin' ? '管理员' : '成员';
					const account = member.username || member.email || '';
					const researchCount = enterpriseMemberResearchCount(member);
					return `<article class="management-card">
						<div class="artifact-title">${escapeHtml(member.name || account || '单位账号')}</div>
						<div class="artifact-meta"><span>${escapeHtml(account)}</span><span>${role}</span><span>研究 ${researchCount} 次</span></div>
					</article>`;
				})
				.join('')
		: '<div class="empty-state compact-empty">暂无单位账号。</div>';
}

function renderCloudStorage() {
	if (!dom.cloudStoragePanel) {
		return;
	}
	const visible = Boolean(state.user) && isEnterpriseAccount();
	dom.cloudStoragePanel.classList.toggle('hidden', !visible);
	dom.cloudDriveManageButton.classList.toggle('hidden', !visible);
	if (!visible) {
		return;
	}
	const storage = state.cloudStorage || {};
	const used = Number(storage.used_bytes || 0);
	const quota = Number(storage.quota_bytes || 0);
	const remaining = Math.max(0, Number(storage.remaining_bytes || quota - used));
	const percent = quota ? Math.max(0, Math.min(100, (used / quota) * 100)) : 0;
	dom.cloudStorageSummary.textContent = `已用 ${formatBytes(used)} / ${formatBytes(quota)}，剩余 ${formatBytes(remaining)}。`;
	dom.cloudStorageMeter.style.width = `${percent}%`;
}

function renderCloudDrive() {
	if (!dom.cloudDrivePage) {
		return;
	}
	const storage = state.cloudStorage || {};
	const used = Number(storage.used_bytes || 0);
	const quota = Number(storage.quota_bytes || 0);
	const remaining = Math.max(0, Number(storage.remaining_bytes || quota - used));
	const percent = quota ? Math.max(0, Math.min(100, (used / quota) * 100)) : 0;
	dom.cloudDriveStorageSummary.textContent = isEnterpriseAccount()
		? `已用 ${formatBytes(used)} / ${formatBytes(quota)}，剩余 ${formatBytes(remaining)}。`
		: '云盘仅支持单位账号。';
	dom.cloudDriveStorageMeter.style.width = `${percent}%`;
	dom.cloudDriveFileList.innerHTML = state.cloudFiles.length
		? state.cloudFiles.map((document) => renderCloudDriveFile(document)).join('')
		: '<div class="empty-state compact-empty">暂无云文件。</div>';
}

function renderCloudDriveFile(document) {
	const id = String(document.document_id || '');
	const filename = document.filename || document.original_filename || id || '云文件';
	const contentType = document.content_type || '文档';
	const downloadUrl = document.download_url || (id ? `/api/limira/uploads/${encodeURIComponent(id)}/download` : '');
	const download = downloadUrl
		? `<a class="attachment-download" href="${escapeAttr(downloadUrl)}" title="下载文件">下载</a>`
		: '<span class="attachment-message">下载不可用</span>';
	return `<article class="cloud-drive-file">
		<div class="attachment-icon" aria-hidden="true">
			<svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M14 3H7C5.89543 3 5 3.89543 5 5V19C5 20.1046 5.89543 21 7 21H17C18.1046 21 19 20.1046 19 19V8M14 3L19 8M14 3V8H19" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>
		</div>
		<div class="attachment-content">
			<div class="attachment-title">${escapeHtml(filename)}</div>
			<div class="attachment-meta"><span>${escapeHtml(contentType)}</span><span>${formatBytes(document.byte_size)}</span></div>
		</div>
		<div class="attachment-actions">${download}</div>
	</article>`;
}

function enterpriseMemberResearchCount(member) {
	const memberUsage = Array.isArray(state.enterpriseUsage?.member_usage)
		? state.enterpriseUsage.member_usage
		: [];
	const username = String(member.username || '').trim();
	const usage = memberUsage.find((item) => String(item.username || '').trim() === username);
	return Number(usage?.totals?.research_task || 0);
}

function bumpWorkspaceGeneration() {
	state.workspaceGeneration += 1;
	return state.workspaceGeneration;
}

function captureAsyncContext(options = {}) {
	const context = {
		generation: state.workspaceGeneration,
		userId: state.user?.id || ''
	};
	if (options.includeTask !== false) {
		context.taskId = state.taskId || '';
	}
	return context;
}

function isCurrentAsyncContext(context) {
	return (
		Boolean(context) &&
		context.generation === state.workspaceGeneration &&
		context.userId === (state.user?.id || '') &&
		(!Object.hasOwn(context, 'taskId') || context.taskId === (state.taskId || ''))
	);
}

function clearRestoredTaskState() {
	state.restoreBlocked = false;
	state.taskId = '';
	state.status = 'ready';
	state.archiveStatus = 'pending';
	state.archiveDownloadUrl = '';
	state.activeTab = CONVERSATION_VIEW;
	state.finalReportText = '';
	state.artifacts = emptyArtifacts();
}

function isAuthoritativeRestoreRejection(error) {
	return error?.status === 403 || error?.status === 404;
}

function updateArchiveState(source) {
	if (!source || typeof source !== 'object') {
		return;
	}
	const archiveStatus = source.archive_status || source.archiveStatus;
	if (archiveStatus) {
		state.archiveStatus = String(archiveStatus);
	}
	const safeUrl = safeArchiveDownloadUrl(source.download_url || source.archive_download_url, state.taskId);
	if (safeUrl) {
		state.archiveDownloadUrl = safeUrl;
	} else if (state.archiveStatus === 'ready' && state.taskId) {
		state.archiveDownloadUrl = defaultArchiveDownloadUrl(state.taskId);
	} else if (archiveStatus && state.archiveStatus !== 'ready') {
		state.archiveDownloadUrl = '';
	}
}

function defaultArchiveDownloadUrl(taskId) {
	return taskId ? `/api/limira/tasks/${encodeURIComponent(taskId)}/archive.zip` : '';
}

function safeArchiveDownloadUrl(value, taskId) {
	const expected = defaultArchiveDownloadUrl(taskId);
	return String(value || '').trim() === expected ? expected : '';
}

function addMessage(role, content, options = {}) {
	state.messages = [
		...state.messages,
		{
			role,
			content: String(content),
			time: now(),
			format: options.format || '',
			kind: options.kind || ''
		}
	];
	saveWorkspace();
	renderMessages();
}

function upsertReportMessage(content) {
	const text = reportTextFromValue(content);
	if (!text) {
		return false;
	}
	const index = state.messages.findIndex((message) => message.kind === 'report');
	const message = {
		role: 'assistant',
		content: text,
		time: index >= 0 ? state.messages[index].time || now() : now(),
		format: 'markdown',
		kind: 'report'
	};
	if (
		index >= 0 &&
		state.messages[index].content === message.content &&
		state.messages[index].format === message.format
	) {
		return false;
	}
	state.messages = index >= 0
		? state.messages.map((item, itemIndex) => (itemIndex === index ? message : item))
		: [...state.messages, message];
	return true;
}

function addThinkingStep({ kind = 'task', title, detail = '', status = 'active', meta = '' }) {
	const normalizedTitle = String(title || '').trim();
	if (!normalizedTitle) {
		return;
	}
	const step = {
		kind,
		title: normalizedTitle,
		detail: String(detail || '').trim(),
		status,
		meta: String(meta || '').trim(),
		time: now()
	};
	const next = state.thinkingSteps.filter((item) => item.kind !== 'ready');
	state.thinkingSteps = [...next, step].slice(-MAX_THINKING_STEPS);
	saveWorkspace();
	renderThinking();
}

function completeActiveThinkingSteps() {
	state.thinkingSteps = state.thinkingSteps.map((step) => (
		step.status === 'active' ? { ...step, status: 'done' } : step
	));
}

function upsertArtifactThinkingStep() {
	const reportText = reportMarkdown();
	const counts = artifactCounts();
	const hasArtifacts = Boolean(reportText) || Object.values(counts).some((count) => Number(count || 0) > 0);
	if (!hasArtifacts) {
		return false;
	}
	const title = reportText ? '已恢复最终报告' : '已恢复研究成果';
	const detail = reportText
		? `最终报告已恢复到对话中。${artifactThinkingSummary()}`
		: artifactThinkingSummary();
	const step = {
		kind: 'artifact-summary',
		title,
		detail,
		status: 'done',
		meta: '',
		time: now()
	};
	const next = state.thinkingSteps.filter((item) => item.kind !== 'ready');
	const index = next.findIndex((item) => item.kind === step.kind);
	if (index >= 0) {
		if (next[index].title === step.title && next[index].detail === step.detail) {
			return false;
		}
		next[index] = { ...step, time: next[index].time || step.time };
		state.thinkingSteps = next;
		return true;
	}
	state.thinkingSteps = [...next, step].slice(-MAX_THINKING_STEPS);
	return true;
}

async function api(path, options = {}) {
	const headers = new Headers(options.headers || {});
	const isForm = options.body instanceof FormData;
	if (!isForm && options.body !== undefined) {
		headers.set('content-type', 'application/json');
	}
	headers.set('accept', 'application/json');
	if (state.token) {
		headers.set('authorization', `Bearer ${state.token}`);
	}
	const response = await fetch(path, {
		method: options.method || 'GET',
		headers,
		credentials: 'include',
		body: isForm ? options.body : options.body === undefined ? undefined : JSON.stringify(options.body)
	});
	if (!response.ok) {
		const detail = await responseDetail(response);
		if (response.status === 401) {
			state.user = null;
			renderShell();
		}
		const error = new Error(detail || `请求失败，状态码 ${response.status}`);
		error.status = response.status;
		throw error;
	}
	const contentType = response.headers.get('content-type') || '';
	if (!contentType.includes('application/json')) {
		return response.text();
	}
	const text = await response.text();
	try {
		return text ? JSON.parse(text) : null;
	} catch {
		const error = new Error('服务返回的 JSON 不完整。请强制刷新页面后重试；如果刚才是在登录，登录状态可能已经写入。');
		error.responseOk = true;
		throw error;
	}
}

async function responseDetail(response) {
	const text = await response.text();
	try {
		const json = JSON.parse(text);
		const detail = typeof json.detail === 'string' ? json.detail : JSON.stringify(json.detail || json);
		return localizedErrorDetail(detail);
	} catch {
		return text || response.statusText;
	}
}

function localizedErrorDetail(detail) {
	const messages = {
		invalid_credentials: '用户名或密码不正确。',
		not_authenticated: '请先登录。',
		admin_required: '当前账号没有管理员权限。',
		email_already_registered: '这个邮箱已经注册。',
		username_already_registered: '这个用户名已经被使用。',
		username_required: '请输入用户名。',
		invalid_username: '请输入有效用户名。',
		email_not_verified: '请先打开验证邮件完成邮箱验证。',
		invalid_email: '请输入有效邮箱。',
		password_too_long: '密码太长，请使用 72 字节以内的密码。',
		enterprise_login_required: '这是单位账号，请切换到企业登录。',
		organization_not_found: '没有找到这个单位。',
		organization_required: '请选择单位。',
		enterprise_admin_required: '当前账号没有单位管理权限。',
		enterprise_admin_already_exists: '每个单位只能保留一个管理员账号。',
		organization_already_exists: '这个单位已经存在。',
		enterprise_cloud_storage_required: '云文件仅支持单位账号。',
		enterprise_cloud_storage_quota_exceeded: '云空间已满，请清理文件或联系管理员扩容。',
		document_not_found: '没有找到这个历史文件。',
		runner_task_failed: '研究任务失败，请检查运行配置或稍后重试。',
		personal_daily_quota_exceeded: '个人方式登录每天只能创建一次研究任务。',
		invalid_organization_role: '单位角色无效。',
		google_auth_failed: 'Google 登录失败，请重试。',
		google_oauth_not_configured: 'Google 登录暂未配置。',
		invalid_google_identity: 'Google 账号验证失败。',
		google_email_not_verified: '这个 Google 账号尚未完成邮箱验证。',
		wechat_auth_failed: '微信登录失败，请重试。',
		wechat_oauth_not_configured: '微信登录暂未配置。',
		invalid_wechat_identity: '微信账号验证失败。'
	};
	return messages[detail] || detail;
}

function normalizeArtifacts(data) {
	const source = data.artifacts || data || {};
	return {
		evidence: asArray(source.evidence || source.evidence_items),
		entities: asArray(source.entities),
		relations: asArray(source.relations || source.entity_relations),
		timeline_events: asArray(source.timeline_events || source.timeline),
		map_features: asArray(source.map_features || source.features),
		report_sections: asArray(source.report_sections || source.reports)
	};
}

function emptyArtifacts() {
	return {
		evidence: [],
		entities: [],
		relations: [],
		timeline_events: [],
		map_features: [],
		report_sections: []
	};
}

function initialMessages() {
	return [];
}

function initialThinkingSteps() {
	return [
		{
			kind: 'ready',
			title: '等待研究问题',
			detail: '发送消息后，Limira 会在这里展示任务规划、检索、证据筛选、信息整合和报告生成进展。',
			time: now(),
			status: 'pending'
		}
	];
}

function artifactCounts() {
	return {
		证据: state.artifacts.evidence.length,
		实体: state.artifacts.entities.length,
		图谱: state.artifacts.entities.length + state.artifacts.relations.length,
		时间线: state.artifacts.timeline_events.length,
		地图: mapFeatures().length
	};
}

function mapFeatures() {
	const source = [...state.artifacts.map_features, ...state.artifacts.timeline_events];
	return source
		.map((item, index) => {
			const geometry = normalizeGeometry(
				item.geometry || item.geojson || item.payload?.geometry || item.payload?.geojson
			);
			if (!geometry) return null;
			return {
				title: item.title || item.event_title || item.name || `地图要素 ${index + 1}`,
				summary: item.summary || item.description || '',
				geometry
			};
		})
		.filter(Boolean);
}

function normalizeGeometry(raw) {
	const parsed = typeof raw === 'string' ? parseJson(raw) : raw;
	const candidate =
		parsed && typeof parsed === 'object' && parsed.type === 'Feature' ? parsed.geometry : parsed;
	if (!candidate || typeof candidate !== 'object') return null;
	if (
		!['Point', 'MultiPoint', 'LineString', 'MultiLineString', 'Polygon', 'MultiPolygon'].includes(
			String(candidate.type)
		)
	) {
		return null;
	}
	return collectPairs(candidate.coordinates).length ? candidate : null;
}

function collectPairs(value) {
	if (Array.isArray(value) && value.length >= 2 && isFinite(Number(value[0])) && isFinite(Number(value[1]))) {
		return [[Number(value[0]), Number(value[1])]];
	}
	if (!Array.isArray(value)) return [];
	return value.flatMap(collectPairs);
}

function reportMarkdown() {
	const sectionText = state.artifacts.report_sections
		.map((section, index) => `## ${reportTitle(section, index)}\n\n${reportText(section)}`)
		.join('\n\n');
	if (sectionText) {
		return sectionText;
	}
	return reportTextFromValue(state.finalReportText);
}

function reportEvidenceRefs() {
	const refs = new Set();
	for (const section of state.artifacts.report_sections) {
		for (const ref of asArray(section.evidence_refs)) {
			const normalized = String(ref).replace(/^\[|\]$/g, '').trim();
			if (normalized) refs.add(normalized);
		}
	}
	return [...refs];
}

function reportTitle(section, index) {
	return (
		section.title ||
		reportFieldFromValue(
			section.markdown || section.content || section.text || section.summary || section,
			REPORT_TITLE_FIELDS
		) ||
		section.report_type ||
		`报告章节 ${index + 1}`
	);
}

function reportText(section) {
	return (
		reportTextFromValue(section.markdown || section.content || section.text || section.summary || section) ||
		stringifyCompact(section)
	);
}

const REPORT_TEXT_FIELDS = ['markdown', 'content', 'text', 'summary'];
const REPORT_TITLE_FIELDS = ['title', 'report_title', 'name'];

function reportWrapper(value) {
	if (value && typeof value === 'object' && !Array.isArray(value)) {
		return value;
	}
	if (typeof value !== 'string') return null;
	const trimmed = value.trim();
	if (!trimmed.startsWith('{') || !trimmed.endsWith('}')) return null;
	const parsed = parseJson(trimmed);
	return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
}

function reportFieldFromValue(value, fields) {
	const wrapped = reportWrapper(value);
	if (!wrapped) return null;
	for (const field of fields) {
		const raw = wrapped[field];
		if (raw === undefined || raw === null || typeof raw === 'object') continue;
		const text = String(raw).trim();
		if (text) return text;
	}
	for (const field of [...REPORT_TEXT_FIELDS, 'payload', 'result']) {
		const nested = wrapped[field];
		if (nested === undefined || nested === null || nested === value) continue;
		const text = reportFieldFromValue(nested, fields);
		if (text) return text;
	}
	return null;
}

function reportTextFromValue(value) {
	const wrapped = reportWrapper(value);
	if (wrapped) {
		for (const field of REPORT_TEXT_FIELDS) {
			if (!(field in wrapped)) continue;
			const text = reportTextFromValue(wrapped[field]);
			if (text) return text;
		}
		if ('payload' in wrapped) {
			const text = reportTextFromValue(wrapped.payload);
			if (text) return text;
		}
		return '';
	}
	if (value === undefined || value === null) return '';
	return typeof value === 'string' ? value.trim() : String(value).trim();
}

function relationSource(relation) {
	return String(relation.source_entity_id || relation.source_id || relation.source || relation.from || '');
}

function relationTarget(relation) {
	return String(relation.target_entity_id || relation.target_id || relation.target || relation.to || '');
}

function asArray(value) {
	return Array.isArray(value) ? value : [];
}

function parseJson(value) {
	try {
		return JSON.parse(value);
	} catch {
		return null;
	}
}

function stringifyCompact(value, limit = 1200) {
	const text = typeof value === 'string' ? value : JSON.stringify(value);
	return text.length > limit ? `${text.slice(0, limit)}...` : text;
}

function truncateText(value, limit = 160) {
	const text = String(value || '').replace(/\s+/g, ' ').trim();
	return text.length > limit ? `${text.slice(0, Math.max(0, limit - 3))}...` : text;
}

function emptyState(text) {
	return `<div class="empty-state">${escapeHtml(text)}</div>`;
}

function formatBytes(value) {
	const bytes = Number(value || 0);
	if (bytes < 1024) return `${bytes} B`;
	if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
	return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function statusLabel(status) {
	return STATUS_LABELS[String(status || '').toLowerCase()] || String(status || '未知');
}

function archiveStatusLabel(status) {
	const labels = {
		pending: '未就绪',
		running: '生成中',
		ready: '已就绪',
		failed: '失败'
	};
	return labels[String(status || '').toLowerCase()] || statusLabel(status);
}

function roleLabel(role) {
	return ROLE_LABELS[String(role || '').toLowerCase()] || String(role || '用户');
}

function accountLabel(user) {
	if (user?.account_type === 'enterprise') {
		const organizationName = user.organization?.name || user.organization_name || '单位账号';
		const role = user.organization_role === 'admin' ? '管理员' : '成员';
		return `${organizationName} · ${role}`;
	}
	return `个人账号 · ${roleLabel(user?.role)}`;
}

function isEnterpriseAccount() {
	return state.user?.account_type === 'enterprise';
}

function eventLabel(eventType) {
	const labels = {
		start_of_workflow: '工作流开始',
		start_of_agent: '智能体开始',
		start_of_llm: '模型调用开始',
		end_of_llm: '模型调用结束',
		end_of_agent: '智能体结束',
		end_of_workflow: '工作流结束',
		tool_call: '工具调用',
		heartbeat: '心跳',
		error: '错误',
		status: '状态',
		task_update: '任务更新',
		evidence_collected: '证据已收集',
		entity_extracted: '实体已抽取',
		relation_extracted: '关系已抽取',
		timeline_event_added: '时间线已更新',
		map_feature_added: '地图要素已添加',
		verification_result: '核验结果',
		report_section_generated: '报告章节已生成'
	};
	return labels[eventType] || eventType;
}

function now() {
	return new Date().toLocaleTimeString();
}

function errorMessage(error) {
	if (error instanceof Error) return error.message;
	if (typeof error === 'string') return localizedErrorDetail(error);
	return stringifyCompact(error);
}

function safeDomId(value) {
	return String(value).replace(/[^a-zA-Z0-9_-]/g, '-');
}

function escapeHtml(value) {
	return String(value ?? '')
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;')
		.replaceAll("'", '&#039;');
}

function escapeAttr(value) {
	return escapeHtml(value).replaceAll('`', '&#096;');
}

function safeExternalUrl(value) {
	const text = String(value || '').trim();
	if (!/^https?:\/\//i.test(text)) {
		return '';
	}
	try {
		const url = new URL(text);
		return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
	} catch {
		return '';
	}
}

function escapeSvg(value) {
	return escapeHtml(value);
}
