const tabs = ['证据', '实体', '图谱', '时间线', '地图', '报告'];
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
	Report: '报告'
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
	route: window.location.hash === '#cloud-drive' ? 'cloud-drive' : 'workspace',
	isLoadingEnterpriseAdmin: false,
	userSettingsOpen: false,
	uploadMenuOpen: false,
	historyFilesOpen: false,
	savedUserId: '',
	query: '',
	taskId: '',
	status: 'ready',
	archiveStatus: 'pending',
	archiveDownloadUrl: '',
	activeTab: '证据',
	isSubmitting: false,
	isUploading: false,
	currentUpload: null,
	isSearching: false,
	isExporting: false,
	isLoadingHistory: false,
	restoreBlocked: false,
	workspaceGeneration: 0,
	latestReport: null,
	latestReportMarkdown: '',
	finalReportText: '',
	messages: initialMessages(),
	artifacts: emptyArtifacts(),
	uploads: [],
	cloudFiles: [],
	cloudStorage: null,
	uploadResults: [],
	taskHistory: [],
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
	dom.refreshHistoryButton.addEventListener('click', () => void loadTaskHistory());
	dom.researchForm.addEventListener('submit', (event) => {
		event.preventDefault();
		void submitResearch();
	});
	dom.refreshArtifactsButton.addEventListener('click', () => void loadArtifacts());
	dom.downloadArchiveButton.addEventListener('click', () => void downloadArchive());
	dom.clearStreamButton.addEventListener('click', () => {
		state.messages = [];
		saveWorkspace();
		renderMessages();
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
	dom.exportPdfButton.addEventListener('click', () => void exportPdf());
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

			dom.sandboxIframe.removeAttribute('src');
			dom.sandboxIframe.srcdoc = evidencePreviewHtml({ title, url, summary });
			dom.sandboxTitle.textContent = title;
			dom.sandboxExternalLink.href = url;
			dom.sandboxModal.classList.remove('hidden');
		}
	});

	dom.sandboxCloseButton.addEventListener('click', () => {
		dom.sandboxModal.classList.add('hidden');
		dom.sandboxIframe.removeAttribute('src');
		dom.sandboxIframe.srcdoc = '';
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
	dom.authPanel.classList.toggle('hidden', signedIn);
	dom.workspace.classList.toggle('hidden', !signedIn);
	dom.workspaceContent.classList.toggle('hidden', cloudDriveVisible);
	dom.inputContainer.classList.toggle('hidden', cloudDriveVisible);
	dom.cloudDrivePage.classList.toggle('hidden', !cloudDriveVisible);
	dom.signOutButton.classList.toggle('hidden', !signedIn);
	dom.userSettingsButton.classList.toggle('hidden', !signedIn);
	if (!signedIn) {
		state.userSettingsOpen = false;
		state.route = 'workspace';
		setUploadMenuOpen(false);
		setHistoryFilesOpen(false);
	}
	dom.userSettingsPanel.classList.toggle('hidden', !signedIn || !state.userSettingsOpen);
	renderUploadMenu();
	renderHistoryFiles();
	renderFileControls();
	renderCloudStorage();
	const displayName = state.user?.name || state.user?.username || state.user?.email || '已登录';
	const fullSessionLabel = signedIn ? `${displayName} · ${accountLabel(state.user)}` : '未登录';
	dom.sessionLabel.textContent = signedIn ? displayName : '未登录';
	dom.sessionLabel.title = fullSessionLabel;
	renderAuthMode();
	renderStatus();
	renderHistory();
	renderMessages();
	renderTabs();
	renderUploads();
	renderReportControls();
	renderEnterpriseAdmin();
	renderCloudDrive();
}

function syncRouteFromHash() {
	state.route = window.location.hash === '#cloud-drive' ? 'cloud-drive' : 'workspace';
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
		const data = await api(`/api/limira/tasks?limit=${MAX_HISTORY_TASKS}`);
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		state.taskHistory = Array.isArray(data.tasks) ? data.tasks : [];
		if (dom.historyMessage) {
			dom.historyMessage.textContent = state.taskHistory.length ? '' : '暂无历史聊天。';
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

function renderHistory() {
	if (!dom.historyList) {
		return;
	}
	dom.refreshHistoryButton.disabled = !state.user || state.isLoadingHistory;
	dom.newChatButton.disabled = !state.user;
	if (!state.user) {
		dom.historyList.innerHTML = '';
		dom.historyMessage.textContent = '';
		return;
	}
	if (!state.taskHistory.length) {
		dom.historyList.innerHTML = '<div class="empty-state compact-empty">暂无历史聊天。</div>';
		return;
	}
	dom.historyList.innerHTML = state.taskHistory
		.map((task) => {
			const taskId = String(task.task_id || '');
			const active = taskId && taskId === state.taskId;
			return `<button type="button" class="history-item${active ? ' active' : ''}" data-task-id="${escapeAttr(taskId)}">
				<span class="history-title">${escapeHtml(taskHistoryTitle(task))}</span>
				<span class="history-meta">${escapeHtml(taskHistoryMeta(task))}</span>
			</button>`;
		})
		.join('');
	for (const button of dom.historyList.querySelectorAll('.history-item')) {
		button.addEventListener('click', () => void selectHistoryTask(button.dataset.taskId || ''));
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
	state.activeTab = '证据';
	state.latestReport = null;
	state.latestReportMarkdown = '';
	state.finalReportText = '';
	state.messages = initialMessages();
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
		: LEGACY_TAB_LABELS[saved.activeTab] || '证据';
	state.latestReport = normalizeGeneratedReport(saved.latestReport);
	state.latestReportMarkdown =
		typeof saved.latestReportMarkdown === 'string' ? saved.latestReportMarkdown : '';
	state.finalReportText = typeof saved.finalReportText === 'string' ? saved.finalReportText : '';
	state.messages = Array.isArray(saved.messages) && saved.messages.length ? saved.messages : state.messages;
	state.artifacts = saved.artifacts && typeof saved.artifacts === 'object'
		? normalizeArtifacts(saved.artifacts)
		: emptyArtifacts();
	state.uploads = Array.isArray(saved.uploads) ? saved.uploads : [];
	state.uploadResults = [];
	if (!latestReportMatchesCurrentMarkdown()) {
		state.latestReport = null;
		state.latestReportMarkdown = '';
	}
}

function saveWorkspace() {
	const payload = {
		userId: state.user?.id || state.savedUserId || '',
		taskId: state.taskId,
		status: state.status,
		archiveStatus: state.archiveStatus,
		archiveDownloadUrl: state.archiveDownloadUrl,
		activeTab: state.activeTab,
		latestReport: state.latestReport,
		latestReportMarkdown: state.latestReportMarkdown,
		finalReportText: state.finalReportText,
		messages: state.messages.slice(-MAX_STORED_MESSAGES),
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
	state.isExporting = false;
	state.isLoadingHistory = false;
	state.activeTab = '证据';
	state.latestReport = null;
	state.latestReportMarkdown = '';
	state.finalReportText = '';
	state.messages = initialMessages();
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

	state.isSubmitting = true;
	bumpWorkspaceGeneration();
	const context = captureAsyncContext({ includeTask: false });
	state.status = 'starting';
	state.taskId = '';
	state.archiveStatus = 'pending';
	state.archiveDownloadUrl = '';
	state.restoreBlocked = false;
	state.latestReport = null;
	state.latestReportMarkdown = '';
	state.finalReportText = '';
	state.artifacts = emptyArtifacts();
	state.uploadResults = [];
	state.isSearching = false;
	saveWorkspace();
	addMessage('user', query);
	renderStatus();
	renderTabs();
	renderReportControls();

	try {
		const documentIds = isEnterpriseAccount() ? selectedUploadDocumentIds() : [];
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
		addMessage('assistant', `研究任务 ${state.taskId || '已创建'}：${statusLabel(state.status)}。`);
		saveWorkspace();
		connectStream();
		await loadArtifacts();
		await loadUploads();
		await loadTaskHistory();
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		state.status = 'failed';
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
		addMessage('error', errorMessage(eventData.error || data.error || payload));
	} else if (eventType === 'report_pdf_generated') {
		const report = normalizeGeneratedReport(eventData);
		if (report) {
			state.latestReport = report;
			state.latestReportMarkdown = reportMarkdown().trim();
			addMessage('assistant', '报告 PDF 已生成并保存到云盘。');
			void loadUploads();
			renderReportControls();
		}
	} else if (eventType === 'archive_generated') {
		state.archiveStatus = 'ready';
		state.archiveDownloadUrl = safeArchiveDownloadUrl(eventData.archive_url, state.taskId);
		addMessage('assistant', '任务归档已生成并保存到云盘。');
		void loadUploads();
	} else if (eventType === 'completion_asset_warning') {
		addMessage('error', '任务已完成，但部分导出文件生成失败，请稍后重试下载。');
	} else if (eventType === 'end_of_workflow') {
		state.status = 'completed';
		addMessage('assistant', '工作流已完成。');
	} else if (eventType.startsWith('start_of_')) {
		addMessage('assistant', compactStartMessage(eventType, eventData));
	} else if (artifactEvents.has(eventType)) {
		addMessage('assistant', `${eventLabel(eventType)}：研究成果已更新。`);
		void loadArtifacts();
	} else {
		const summary = eventData.message || eventData.summary || data.message || data.summary || payload.message || eventType;
		addMessage('assistant', `${eventLabel(eventType)}：${stringifyCompact(summary)}`);
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
		addMessage('assistant', '已收到最终报告，请在“报告”标签页查看。');
		state.activeTab = '报告';
		saveWorkspace();
		renderTabs();
		renderReportControls();
		return;
	}

	if (typeof input.result === 'string') {
		const parsed = parseJson(input.result);
		if (parsed && typeof parsed === 'object' && parsed.success) {
			addMessage(
				'assistant',
				`工具已完成：${toolName}${parsed.url ? ` · ${parsed.url}` : ''}`
			);
			return;
		}
	}

	const target = input.url ? ` · ${input.url}` : '';
	addMessage('assistant', `调用工具：${toolName}${target}`);
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
		await loadTaskReports(context);
		saveWorkspace();
		renderTabs();
		renderReportControls();
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		addMessage('error', `无法加载研究成果：${errorMessage(error)}`);
	}
}

async function loadTaskReports(context = captureAsyncContext()) {
	if (!state.taskId) {
		state.latestReport = null;
		state.latestReportMarkdown = '';
		return;
	}
	try {
		const data = await api(`/api/limira/tasks/${encodeURIComponent(state.taskId)}/reports`);
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		applyLatestReportFromReports(Array.isArray(data.reports) ? data.reports : []);
	} catch {
		if (!latestReportMatchesCurrentMarkdown()) {
			state.latestReport = null;
			state.latestReportMarkdown = '';
		}
	}
}

function applyLatestReportFromReports(reports) {
	const normalizedReports = (Array.isArray(reports) ? reports : [])
		.map((report) => normalizeGeneratedReport(report))
		.filter(Boolean);
	const report =
		normalizedReports.find(
			(candidate) => candidate.report_type === 'final' && reportPdfUrl(candidate)
		) || normalizedReports.find((candidate) => reportPdfUrl(candidate));
	if (!report) {
		if (!latestReportMatchesCurrentMarkdown()) {
			state.latestReport = null;
			state.latestReportMarkdown = '';
		}
		return;
	}
	state.latestReport = report;
	state.latestReportMarkdown = reportMarkdown().trim();
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
			const taskData = await api(`/api/limira/uploads?task_id=${encodeURIComponent(state.taskId)}`);
			if (!isCurrentAsyncContext(context)) {
				return;
			}
			state.uploads = Array.isArray(taskData.documents) ? taskData.documents : [];
			state.cloudStorage = taskData.storage || state.cloudStorage;
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

async function exportPdf() {
	const markdown = reportMarkdown().trim();
	if (state.restoreBlocked) {
		dom.reportMessage.textContent = '任务暂未从后端确认，恢复后再导出 PDF。';
		return;
	}
	if (!state.taskId || !markdown || state.isExporting) {
		return;
	}
	if (latestReportPdfUrl()) {
		await downloadPdf();
		return;
	}
	state.isExporting = true;
	dom.reportMessage.textContent = '正在导出...';
	const context = captureAsyncContext();
	try {
		const report = await api(`/api/limira/tasks/${encodeURIComponent(state.taskId)}/reports/pdf`, {
			method: 'POST',
			body: {
				report_id: `standalone-${Date.now()}`,
				report_type: 'final',
				markdown,
				evidence_refs: reportEvidenceRefs()
			}
		});
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		state.latestReport = normalizeGeneratedReport(report);
		state.latestReportMarkdown = markdown;
		const pdfUrl = latestReportPdfUrl();
		dom.reportMessage.textContent = pdfUrl ? 'PDF 已生成，正在下载。' : 'PDF 已生成。';
		saveWorkspace();
		renderReportControls();
		if (pdfUrl) {
			await downloadPdf();
		}
		await loadUploads();
	} catch (error) {
		if (!isCurrentAsyncContext(context)) {
			return;
		}
		dom.reportMessage.textContent = `PDF 导出失败：${errorMessage(error)}`;
	} finally {
		if (isCurrentAsyncContext(context)) {
			state.isExporting = false;
			renderReportControls();
		}
	}
}

async function downloadPdf() {
	if (state.restoreBlocked) {
		dom.reportMessage.textContent = '任务暂未从后端确认，恢复后再下载 PDF。';
		return;
	}
	if (!state.taskId || !state.latestReport?.report_id) {
		return;
	}
	const url = latestReportPdfUrl();
	if (!url) {
		state.latestReport = null;
		state.latestReportMarkdown = '';
		saveWorkspace();
		renderReportControls();
		dom.reportMessage.textContent = '报告内容已更新，请重新导出 PDF。';
		return;
	}
	try {
		await downloadGeneratedPdf(url, `${state.latestReport.report_id}.pdf`);
		dom.reportMessage.textContent = 'PDF 已下载。';
	} catch (error) {
		state.latestReport = null;
		state.latestReportMarkdown = '';
		saveWorkspace();
		renderReportControls();
		dom.reportMessage.textContent = `PDF 下载失败：${errorMessage(error)}。请重新导出 PDF。`;
	}
}

async function downloadGeneratedPdf(url, filename) {
	const headers = new Headers({ accept: 'application/pdf' });
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
		throw new Error('empty_pdf_download');
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

function renderStatus() {
	dom.statusLabel.textContent = statusLabel(state.status);
	dom.taskLabel.textContent = state.taskId
		? `任务 ${state.taskId} · 归档${archiveStatusLabel(state.archiveStatus)}${
				state.restoreBlocked ? ' · 待恢复确认' : ''
			}`
		: '暂无任务';
	dom.submitResearchButton.disabled = state.isSubmitting;
	dom.downloadArchiveButton.disabled = state.restoreBlocked || !state.taskId;
}

function renderMessages() {
	dom.messageList.innerHTML = state.messages
		.slice(-80)
		.map(
			(message) => `<article class="message ${escapeHtml(message.role)}">
				<div class="message-meta"><span>${escapeHtml(roleLabel(message.role))}</span><span>${escapeHtml(message.time)}</span></div>
				<div class="message-body">${escapeHtml(message.content)}</div>
			</article>`
		)
		.join('');
	dom.messageList.scrollTop = dom.messageList.scrollHeight;
}

function renderTabs() {
	const counts = artifactCounts();
	dom.artifactTabs.innerHTML = tabs
		.map(
			(tab) =>
				`<button type="button" class="${tab === state.activeTab ? 'active' : ''}" data-tab="${tab}">${tab} ${counts[tab] || ''}</button>`
		)
		.join('');
	for (const button of dom.artifactTabs.querySelectorAll('button')) {
		button.addEventListener('click', () => {
			state.activeTab = button.dataset.tab;
			saveWorkspace();
			renderTabs();
		});
	}
	renderArtifactContent();
}

function renderArtifactContent() {
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
	} else {
		renderReport();
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
		<div class="artifact-body">${escapeHtml(summary)}</div>
		${url ? `<a href="${escapeAttr(url)}" class="sandbox-link" data-title="${escapeAttr(title)}" data-summary="${escapeAttr(summary)}" target="_blank" rel="noopener noreferrer">打开来源</a>` : ''}
	</article>`;
}

function evidencePreviewHtml({ title, url, summary }) {
	const safeTitle = escapeHtml(title || '网页预览');
	const safeUrl = escapeAttr(url || '');
	const safeDisplayUrl = escapeHtml(url || '');
	const safeSummary = escapeHtml(summary || '该来源没有可用的本地摘要。');
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
			.summary{white-space:pre-wrap}
		</style>
	</head>
	<body>
		<h1>${safeTitle}</h1>
		<p><a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${safeDisplayUrl}</a></p>
		<p class="notice">部分网站禁止被第三方页面嵌入预览。这里显示 Limira 已保存的来源摘要；需要查看原网页时，请使用右上角外部打开按钮。</p>
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
	const download =
		!transient && id
			? `<a class="attachment-download" href="/api/limira/uploads/${encodeURIComponent(id)}/download" title="下载文件">下载</a>`
			: '';
	const remove =
		!transient && !state.taskId && id
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
		<div class="attachment-actions">${download}${remove}</div>
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
	if (!documentId || state.taskId) {
		return;
	}
	state.uploads = state.uploads.filter(
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
	const hasMarkdown = Boolean(reportMarkdown().trim());
	const hasPdf = Boolean(latestReportPdfUrl());
	dom.exportPdfButton.disabled =
		state.restoreBlocked || !state.taskId || state.isExporting || (!hasMarkdown && !hasPdf);
}

function renderEnterpriseAdmin() {
	if (!dom.enterpriseAdminPanel) {
		return;
	}
	const visible = Boolean(state.user) && isEnterpriseAdmin();
	dom.enterpriseAdminPanel.classList.toggle('hidden', !visible);
	if (!visible) {
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
	state.latestReport = null;
	state.latestReportMarkdown = '';
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

function normalizeGeneratedReport(report) {
	if (!report || typeof report !== 'object' || !state.taskId) {
		return null;
	}
	const reportId = String(report.report_id || '').trim();
	const taskId = String(report.task_id || '').trim();
	if (!reportId || taskId !== state.taskId) {
		return null;
	}
	return {
		...report,
		task_id: taskId,
		report_id: reportId,
		pdf_url: safeReportPdfUrl(report.pdf_url, taskId, reportId)
	};
}

function reportPdfUrl(report) {
	const normalized = normalizeGeneratedReport(report);
	if (!normalized) {
		return '';
	}
	return (
		normalized.pdf_url ||
		`/api/limira/tasks/${encodeURIComponent(normalized.task_id)}/reports/${encodeURIComponent(
			normalized.report_id
		)}/pdf`
	);
}

function safeReportPdfUrl(value, taskId, reportId) {
	const expected = `/api/limira/tasks/${encodeURIComponent(taskId)}/reports/${encodeURIComponent(reportId)}/pdf`;
	return String(value || '').trim() === expected ? expected : '';
}

function latestReportMatchesCurrentMarkdown() {
	if (!reportPdfUrl(state.latestReport)) {
		return false;
	}
	const currentMarkdown = reportMarkdown().trim();
	return Boolean(currentMarkdown && state.latestReportMarkdown === currentMarkdown);
}

function latestReportPdfUrl() {
	return latestReportMatchesCurrentMarkdown() ? reportPdfUrl(state.latestReport) : '';
}

function addMessage(role, content) {
	state.messages = [...state.messages, { role, content: String(content), time: now() }];
	saveWorkspace();
	renderMessages();
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
	return [
		{
			role: 'assistant',
			content:
				'请输入研究问题。系统会在这里流式显示进展，并把结构化成果填入右侧工作区。',
			time: now()
		}
	];
}

function artifactCounts() {
	return {
		证据: state.artifacts.evidence.length,
		实体: state.artifacts.entities.length,
		图谱: state.artifacts.entities.length + state.artifacts.relations.length,
		时间线: state.artifacts.timeline_events.length,
		地图: mapFeatures().length,
		报告: state.artifacts.report_sections.length + (state.finalReportText ? 1 : 0)
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
