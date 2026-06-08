const tabs = ['证据', '实体', '图谱', '时间线', '地图', '报告'];
const terminalStatuses = new Set(['completed', 'failed', 'cancelled']);
const artifactEvents = new Set([
	'evidence_collected',
	'entity_extracted',
	'relation_extracted',
	'timeline_event_added',
	'map_feature_added',
	'verification_result',
	'report_section_generated'
]);

const STORAGE_KEY = 'limraStandaloneWorkspace:v2';
const LEGACY_STORAGE_KEYS = ['limraStandaloneWorkspace:v1'];
const MAX_STORED_MESSAGES = 100;
const MAX_STORED_EVENTS = 100;
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
const SCENARIO_TEXT = {
	sanctions_export_controls: {
		title: '制裁与出口管制',
		description: '跟踪影响企业、行业或供应链的制裁、出口管制、实体清单和许可变化。',
		focus: [
			'官方制裁、出口管制、实体清单和许可通知',
			'受影响公司、中间商、司法辖区和供应链关系',
			'生效日期、执法节点和合规期限',
			'冲突或不明确说法的来源支持置信度分级'
		],
		defaultQuery: '分析近期影响半导体供应链的制裁与出口管制变化。'
	},
	geopolitical_risk_assessment: {
		title: '地缘政治风险评估',
		description: '评估可能影响市场、供应链或运营资产的地缘政治风险。',
		focus: [
			'近期官方公告、事件、制裁和监管动作',
			'国家与非国家行为体、联盟、关键瓶颈和暴露资产',
			'风险升级、缓和和缓释措施的时间线',
			'可信坐标或地图为空的明确理由'
		],
		defaultQuery: '评估近期红海航运风险对能源与制造业供应链的影响。'
	},
	critical_minerals_competition: {
		title: '关键矿产竞争',
		description: '梳理关键矿产项目、包销协议、加工能力、政策动作和战略关键瓶颈。',
		focus: [
			'可核验位置的矿山、炼厂、加工和运输资产',
			'政府政策、投资审查、补贴和贸易限制',
			'公司、国有资本、包销协议和供应链依赖',
			'项目与关键瓶颈的证据支持时间线和地图'
		],
		defaultQuery: '分析近期国际锂和镍供应链竞争，包括项目、政策变化和关键瓶颈。'
	}
};

const state = {
	authMode: 'signin',
	token: localStorage.getItem('limraToken') || localStorage.getItem('token') || '',
	user: null,
	scenarios: [],
	selectedScenario: '',
	savedUserId: '',
	query: '',
	taskId: '',
	status: 'ready',
	archiveStatus: 'pending',
	archiveDownloadUrl: '',
	activeTab: '证据',
	isSubmitting: false,
	isUploading: false,
	isSearching: false,
	isExporting: false,
	restoreBlocked: false,
	latestReport: null,
	finalReportText: '',
	messages: initialMessages(),
	events: [],
	artifacts: emptyArtifacts(),
	uploads: [],
	uploadResults: [],
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
	dom.signinModeButton.addEventListener('click', () => setAuthMode('signin'));
	dom.signupModeButton.addEventListener('click', () => setAuthMode('signup'));
	dom.signOutButton.addEventListener('click', () => void signOut());
	dom.scenarioSelect.addEventListener('change', () => {
		state.selectedScenario = dom.scenarioSelect.value;
		saveWorkspace();
		renderScenario();
	});
	dom.useScenarioButton.addEventListener('click', useScenarioQuery);
	dom.researchForm.addEventListener('submit', (event) => {
		event.preventDefault();
		void submitResearch();
	});
	dom.refreshArtifactsButton.addEventListener('click', () => void loadArtifacts());
	dom.downloadArchiveButton.addEventListener('click', downloadArchive);
	dom.clearStreamButton.addEventListener('click', () => {
		state.messages = [];
		saveWorkspace();
		renderMessages();
	});
	dom.refreshUploadsButton.addEventListener('click', () => void loadUploads());
	dom.uploadButton.addEventListener('click', () => void uploadDocument());
	dom.uploadSearchButton.addEventListener('click', () => void searchUploads());
	dom.uploadSearchInput.addEventListener('keydown', (event) => {
		if (event.key === 'Enter') {
			event.preventDefault();
			void searchUploads();
		}
	});
	dom.exportPdfButton.addEventListener('click', () => void exportPdf());
	dom.downloadPdfButton.addEventListener('click', downloadPdf);
}

async function boot() {
	clearLegacyWorkspaceStorage();
	restoreWorkspace();
	try {
		await loadSession();
		await loadScenarios();
		await resumeWorkspace();
	} catch {
		state.user = null;
	}
	renderShell();
}

function renderShell() {
	const signedIn = Boolean(state.user);
	dom.authPanel.classList.toggle('hidden', signedIn);
	dom.workspace.classList.toggle('hidden', !signedIn);
	dom.signOutButton.classList.toggle('hidden', !signedIn);
	dom.sessionLabel.textContent = signedIn
		? `${state.user.name || state.user.email || '已登录'} · ${roleLabel(state.user.role)}`
		: '未登录';
	renderAuthMode();
	renderStatus();
	renderScenarios();
	renderMessages();
	renderTabs();
	renderUploads();
	renderReportControls();
	renderEvents();
}

function renderAuthMode() {
	dom.signinModeButton.classList.toggle('active', state.authMode === 'signin');
	dom.signupModeButton.classList.toggle('active', state.authMode === 'signup');
	dom.nameInput.parentElement.classList.toggle('hidden', state.authMode !== 'signup');
	dom.authSubmitButton.textContent = state.authMode === 'signup' ? '注册' : '登录';
	dom.passwordInput.autocomplete =
		state.authMode === 'signup' ? 'new-password' : 'current-password';
}

function setAuthMode(mode) {
	state.authMode = mode;
	dom.authMessage.textContent = '';
	renderAuthMode();
}

async function authenticate() {
	const email = dom.emailInput.value.trim();
	const password = dom.passwordInput.value;
	const name = dom.nameInput.value.trim();
	const path = state.authMode === 'signup' ? '/api/v1/auths/signup' : '/api/v1/auths/signin';
	const payload =
		state.authMode === 'signup'
			? { name: name || email, email, password, profile_image_url: '/user.png' }
			: { email, password };
	dom.authMessage.textContent = '处理中...';
	try {
		const user = await api(path, { method: 'POST', body: payload });
		setUser(user);
		dom.authMessage.textContent = '';
		await loadScenarios();
		await loadUploads();
		renderShell();
	} catch (error) {
		if (error?.responseOk) {
			try {
				await loadSession();
				dom.authMessage.textContent = '';
				await loadScenarios();
				await loadUploads();
				renderShell();
				return;
			} catch {
				// Fall through to the controlled parse error below.
			}
		}
		dom.authMessage.textContent = errorMessage(error);
	}
}

async function loadSession() {
	const user = await api('/api/v1/auths/');
	setUser(user);
}

function setUser(user) {
	state.user = user;
	if (user?.token) {
		state.token = user.token;
		localStorage.setItem('limraToken', user.token);
		localStorage.setItem('token', user.token);
	}
}

async function resumeWorkspace() {
	if (state.savedUserId && state.user?.id && state.savedUserId !== state.user.id) {
		clearWorkspaceStorage();
		resetWorkspaceState();
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
	}
}

async function refreshTask() {
	if (!state.taskId) {
		return;
	}
	const task = await api(`/api/limra/tasks/${encodeURIComponent(state.taskId)}`);
	state.restoreBlocked = false;
	state.status = task.status || state.status;
	updateArchiveState(task);
	saveWorkspace();
	renderStatus();
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
	state.selectedScenario = typeof saved.selectedScenario === 'string' ? saved.selectedScenario : '';
	state.savedUserId = typeof saved.userId === 'string' ? saved.userId : '';
	state.taskId = typeof saved.taskId === 'string' ? saved.taskId : '';
	state.status = typeof saved.status === 'string' ? saved.status : 'ready';
	state.archiveStatus = typeof saved.archiveStatus === 'string' ? saved.archiveStatus : 'pending';
	state.archiveDownloadUrl = safeArchiveDownloadUrl(saved.archiveDownloadUrl, state.taskId);
	state.activeTab = tabs.includes(saved.activeTab)
		? saved.activeTab
		: LEGACY_TAB_LABELS[saved.activeTab] || '证据';
	state.latestReport = normalizeGeneratedReport(saved.latestReport);
	state.finalReportText = typeof saved.finalReportText === 'string' ? saved.finalReportText : '';
	state.messages = Array.isArray(saved.messages) && saved.messages.length ? saved.messages : state.messages;
	state.events = Array.isArray(saved.events) ? saved.events : [];
	state.artifacts = saved.artifacts && typeof saved.artifacts === 'object'
		? normalizeArtifacts(saved.artifacts)
		: emptyArtifacts();
}

function saveWorkspace() {
	const payload = {
		userId: state.user?.id || state.savedUserId || '',
		selectedScenario: state.selectedScenario,
		taskId: state.taskId,
		status: state.status,
		archiveStatus: state.archiveStatus,
		archiveDownloadUrl: state.archiveDownloadUrl,
		activeTab: state.activeTab,
		latestReport: state.latestReport,
		finalReportText: state.finalReportText,
		messages: state.messages.slice(-MAX_STORED_MESSAGES),
		events: state.events.slice(-MAX_STORED_EVENTS),
		artifacts: state.artifacts
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
					events: state.events.slice(-30),
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
	state.savedUserId = state.user?.id || '';
	state.taskId = '';
	state.status = 'ready';
	state.archiveStatus = 'pending';
	state.archiveDownloadUrl = '';
	state.restoreBlocked = false;
	state.activeTab = '证据';
	state.latestReport = null;
	state.finalReportText = '';
	state.messages = initialMessages();
	state.events = [];
	state.artifacts = emptyArtifacts();
	state.uploadResults = [];
}

async function signOut() {
	try {
		await api('/api/v1/auths/signout', { method: 'POST' });
	} catch {
		// Local cleanup still matters if the server session is already gone.
	}
	state.eventSource?.close();
	state.eventSource = null;
	state.restoreBlocked = false;
	state.user = null;
	state.token = '';
	clearWorkspaceStorage();
	localStorage.removeItem('limraToken');
	localStorage.removeItem('token');
	renderShell();
}

async function loadScenarios() {
	const data = await api('/api/limra/scenarios');
	state.scenarios = Array.isArray(data.scenarios) ? data.scenarios : [];
	if (!state.selectedScenario && state.scenarios[0]) {
		state.selectedScenario = state.scenarios[0].id;
	}
	renderScenarios();
}

function renderScenarios() {
	dom.scenarioSelect.innerHTML = state.scenarios
		.map(
			(scenario) =>
				`<option value="${escapeHtml(scenario.id)}"${scenario.id === state.selectedScenario ? ' selected' : ''}>${escapeHtml(scenarioTitle(scenario))}</option>`
		)
		.join('');
	renderScenario();
}

function renderScenario() {
	const scenario = selectedScenario();
	if (!scenario) {
		dom.scenarioDetails.textContent = '尚未加载场景信息。';
		return;
	}
	const focus = scenarioFocus(scenario).join(' · ');
	dom.scenarioDetails.innerHTML = `<strong>${escapeHtml(scenarioDescription(scenario))}</strong>${
		focus ? `<br>${escapeHtml(focus)}` : ''
	}`;
}

function selectedScenario() {
	return state.scenarios.find((scenario) => scenario.id === state.selectedScenario) || state.scenarios[0];
}

function useScenarioQuery() {
	const scenario = selectedScenario();
	if (scenario) {
		dom.queryInput.value = scenarioDefaultQuery(scenario);
	}
}

async function submitResearch() {
	const query = dom.queryInput.value.trim();
	if (!query || state.isSubmitting) {
		return;
	}

	state.isSubmitting = true;
	state.status = 'starting';
	state.taskId = '';
	state.archiveStatus = 'pending';
	state.archiveDownloadUrl = '';
	state.restoreBlocked = false;
	state.latestReport = null;
	state.finalReportText = '';
	state.artifacts = emptyArtifacts();
	saveWorkspace();
	addMessage('user', query);
	renderStatus();
	renderTabs();
	renderReportControls();

	try {
		const task = await api('/api/limra/research', {
			method: 'POST',
			body: {
				query,
				scenario: state.selectedScenario || undefined
			}
		});
		state.taskId = task.task_id || '';
		state.status = task.status || 'queued';
		updateArchiveState(task);
		state.savedUserId = state.user?.id || state.savedUserId;
		dom.queryInput.value = '';
		addMessage('assistant', `研究任务 ${state.taskId || '已创建'}：${statusLabel(state.status)}。`);
		saveWorkspace();
		connectStream();
		await loadArtifacts();
		await loadUploads();
	} catch (error) {
		state.status = 'failed';
		addMessage('error', errorMessage(error));
	} finally {
		state.isSubmitting = false;
		renderStatus();
	}
}

function connectStream() {
	if (!state.taskId) {
		return;
	}
	state.eventSource?.close();
	state.eventSource = new EventSource(`/api/limra/tasks/${state.taskId}/events`);
	state.eventSource.onmessage = (event) => handleStreamEvent(parseJson(event.data));
	state.eventSource.onerror = () => {
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
	const status = payload.status || data.status || nested.status;
	updateArchiveState(payload);
	updateArchiveState(data);
	updateArchiveState(nested);

	recordEvent(eventType, payload);

	if (status) {
		state.status = String(status);
	}

	if (eventType === 'heartbeat') {
		saveWorkspace();
		renderStatus();
		renderEvents();
		return;
	}

	if (eventType === 'tool_call') {
		handleToolCall(data);
	} else if (eventType === 'error') {
		state.status = 'failed';
		addMessage('error', errorMessage(data.error || payload));
	} else if (eventType === 'end_of_workflow') {
		state.status = 'completed';
		addMessage('assistant', '工作流已完成。');
	} else if (eventType.startsWith('start_of_')) {
		addMessage('assistant', compactStartMessage(eventType, data));
	} else if (artifactEvents.has(eventType)) {
		addMessage('assistant', `${eventLabel(eventType)}：研究成果已更新。`);
		void loadArtifacts();
	} else {
		const summary = data.message || data.summary || payload.message || eventType;
		addMessage('assistant', `${eventLabel(eventType)}：${stringifyCompact(summary)}`);
	}

	if (terminalStatuses.has(state.status)) {
		state.eventSource?.close();
		state.eventSource = null;
		void loadArtifacts();
		void loadUploads();
	}

	renderStatus();
	renderEvents();
	saveWorkspace();
}

function handleToolCall(data) {
	const toolName = data.tool_name || data.name || 'tool';
	const input = data.tool_input && typeof data.tool_input === 'object' ? data.tool_input : {};
	if (toolName === 'show_text' && typeof input.text === 'string') {
		state.finalReportText = input.text;
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
	try {
		const data = await api(`/api/limra/tasks/${encodeURIComponent(state.taskId)}/artifacts`);
		state.artifacts = normalizeArtifacts(data);
		saveWorkspace();
		renderTabs();
		renderReportControls();
	} catch (error) {
		addMessage('error', `无法加载研究成果：${errorMessage(error)}`);
	}
}

async function loadUploads() {
	if (!state.user) {
		return;
	}
	try {
		const taskParam = state.taskId ? `?task_id=${encodeURIComponent(state.taskId)}` : '';
		const data = await api(`/api/limra/uploads${taskParam}`);
		state.uploads = Array.isArray(data.documents) ? data.documents : [];
		renderUploads();
	} catch (error) {
		dom.uploadMessage.textContent = errorMessage(error);
	}
}

async function uploadDocument() {
	const file = dom.uploadInput.files?.[0];
	if (!file || state.isUploading) {
		dom.uploadMessage.textContent = file ? '' : '请先选择文件。';
		return;
	}
	state.isUploading = true;
	dom.uploadMessage.textContent = '正在上传...';
	const form = new FormData();
	form.append('file', file);
	if (state.taskId) {
		form.append('task_id', state.taskId);
	}
	try {
		await api('/api/limra/uploads', { method: 'POST', body: form });
		dom.uploadInput.value = '';
		dom.uploadMessage.textContent = '上传完成。';
		await loadUploads();
	} catch (error) {
		dom.uploadMessage.textContent = errorMessage(error);
	} finally {
		state.isUploading = false;
	}
}

async function searchUploads() {
	const query = dom.uploadSearchInput.value.trim();
	if (!query || state.isSearching) {
		return;
	}
	state.isSearching = true;
	try {
		const taskParam = state.taskId ? `&task_id=${encodeURIComponent(state.taskId)}` : '';
		const data = await api(`/api/limra/uploads/search?query=${encodeURIComponent(query)}${taskParam}`);
		state.uploadResults = Array.isArray(data.documents) ? data.documents : [];
		renderUploads();
	} catch (error) {
		dom.uploadMessage.textContent = errorMessage(error);
	} finally {
		state.isSearching = false;
	}
}

async function exportPdf() {
	const markdown = reportMarkdown();
	if (state.restoreBlocked) {
		dom.reportMessage.textContent = '任务暂未从后端确认，恢复后再导出 PDF。';
		return;
	}
	if (!state.taskId || !markdown.trim() || state.isExporting) {
		return;
	}
	state.isExporting = true;
	dom.reportMessage.textContent = '正在导出...';
	try {
		const report = await api(`/api/limra/tasks/${encodeURIComponent(state.taskId)}/reports/pdf`, {
			method: 'POST',
			body: {
				report_id: `standalone-${Date.now()}`,
				report_type: 'final',
				markdown,
				evidence_refs: reportEvidenceRefs()
			}
		});
		state.latestReport = normalizeGeneratedReport(report);
		dom.reportMessage.textContent = 'PDF 已生成。';
		saveWorkspace();
		renderReportControls();
	} catch (error) {
		dom.reportMessage.textContent = `PDF 导出失败：${errorMessage(error)}`;
	} finally {
		state.isExporting = false;
		renderReportControls();
	}
}

function downloadPdf() {
	if (state.restoreBlocked) {
		dom.reportMessage.textContent = '任务暂未从后端确认，恢复后再下载 PDF。';
		return;
	}
	if (!state.taskId || !state.latestReport?.report_id) {
		return;
	}
	const url = reportPdfUrl(state.latestReport);
	if (!url) {
		dom.reportMessage.textContent = '当前任务没有可下载的 PDF。';
		return;
	}
	window.location.href = url;
}

function downloadArchive() {
	if (state.restoreBlocked) {
		dom.reportMessage.textContent = '任务暂未从后端确认，恢复后再下载归档。';
		return;
	}
	if (state.archiveStatus === 'ready' && state.archiveDownloadUrl) {
		window.location.href = state.archiveDownloadUrl;
		return;
	}
	dom.reportMessage.textContent =
		state.archiveStatus === 'failed' ? '归档生成失败，暂时无法下载。' : '归档尚未生成完成。';
}

function renderStatus() {
	dom.statusLabel.textContent = statusLabel(state.status);
	dom.taskLabel.textContent = state.taskId
		? `任务 ${state.taskId} · 归档${archiveStatusLabel(state.archiveStatus)}${
				state.restoreBlocked ? ' · 待恢复确认' : ''
			}`
		: '暂无任务';
	dom.submitResearchButton.disabled = state.isSubmitting;
	dom.downloadArchiveButton.disabled =
		state.restoreBlocked || !(state.archiveStatus === 'ready' && state.archiveDownloadUrl);
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
		${url ? `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">打开来源</a>` : ''}
	</article>`;
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
	const finalCard = state.finalReportText
		? reportCard('最终回答', state.finalReportText, reportEvidenceRefs())
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
	dom.uploadList.innerHTML = uploads.length
		? uploads
				.map((document) => {
					const id = document.document_id;
					const title = document.filename || document.original_filename || id;
					const score = typeof document.score === 'number' ? `相关度 ${document.score.toFixed(3)}` : '';
					return `<article class="upload-card">
						<div class="artifact-title">${escapeHtml(title)}</div>
						<div class="artifact-meta"><span>${escapeHtml(document.content_type || '文档')}</span><span>${formatBytes(document.byte_size)}</span>${score ? `<span>${score}</span>` : ''}</div>
						${document.snippet ? `<div class="artifact-body">${escapeHtml(document.snippet)}</div>` : ''}
						<a href="/api/limra/uploads/${encodeURIComponent(id)}/download">下载</a>
					</article>`;
				})
				.join('')
		: '<div class="empty-state">暂无上传文档。</div>';
}

function renderReportControls() {
	const hasMarkdown = Boolean(reportMarkdown().trim());
	dom.exportPdfButton.disabled = state.restoreBlocked || !state.taskId || !hasMarkdown || state.isExporting;
	dom.downloadPdfButton.disabled = state.restoreBlocked || !reportPdfUrl(state.latestReport);
}

function clearRestoredTaskState() {
	state.restoreBlocked = false;
	state.taskId = '';
	state.status = 'ready';
	state.archiveStatus = 'pending';
	state.archiveDownloadUrl = '';
	state.latestReport = null;
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
	return taskId ? `/api/limra/tasks/${encodeURIComponent(taskId)}/archive.zip` : '';
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
		`/api/limra/tasks/${encodeURIComponent(normalized.task_id)}/reports/${encodeURIComponent(
			normalized.report_id
		)}/pdf`
	);
}

function safeReportPdfUrl(value, taskId, reportId) {
	const expected = `/api/limra/tasks/${encodeURIComponent(taskId)}/reports/${encodeURIComponent(reportId)}/pdf`;
	return String(value || '').trim() === expected ? expected : '';
}

function renderEvents() {
	dom.eventLog.innerHTML = state.events
		.slice(-35)
		.reverse()
		.map(
			(entry) => `<article class="event-card">
				<strong>${escapeHtml(eventLabel(entry.event))}</strong><br />
				${escapeHtml(entry.summary)}
			</article>`
		)
		.join('');
}

function addMessage(role, content) {
	state.messages = [...state.messages, { role, content: String(content), time: now() }];
	saveWorkspace();
	renderMessages();
}

function recordEvent(event, payload) {
	const summary = event === 'heartbeat' ? '心跳' : stringifyCompact(payload, 320);
	state.events = [...state.events.slice(-99), { event, summary }];
	saveWorkspace();
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
		return typeof json.detail === 'string' ? json.detail : JSON.stringify(json.detail || json);
	} catch {
		return text || response.statusText;
	}
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
	return [state.finalReportText, sectionText].filter(Boolean).join('\n\n');
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
	return section.title || section.report_type || `报告章节 ${index + 1}`;
}

function reportText(section) {
	return section.markdown || section.text || section.summary || stringifyCompact(section);
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

function scenarioText(scenario) {
	return SCENARIO_TEXT[scenario?.id] || {};
}

function scenarioTitle(scenario) {
	return scenarioText(scenario).title || scenario?.title || '';
}

function scenarioDescription(scenario) {
	return scenarioText(scenario).description || scenario?.description || '';
}

function scenarioFocus(scenario) {
	return scenarioText(scenario).focus || (Array.isArray(scenario?.focus_areas) ? scenario.focus_areas : []);
}

function scenarioDefaultQuery(scenario) {
	return scenarioText(scenario).defaultQuery || scenario?.default_query || '';
}

function now() {
	return new Date().toLocaleTimeString();
}

function errorMessage(error) {
	if (error instanceof Error) return error.message;
	if (typeof error === 'string') return error;
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
