<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import 'maplibre-gl/dist/maplibre-gl.css';

	type ArtifactTab = 'Evidence' | 'Entities' | 'Graph' | 'Timeline' | 'Map' | 'Report';

	type LimraScenario = {
		id: string;
		title: string;
		description: string;
		default_query: string;
		focus_areas: string[];
	};

	type ChatMessage = {
		role: 'user' | 'assistant' | 'system';
		content: string;
		time: string;
	};

	type ArtifactState = {
		evidence: Record<string, any>[];
		entities: Record<string, any>[];
		relations: Record<string, any>[];
		timeline_events: Record<string, any>[];
		map_features: Record<string, any>[];
		report_sections: Record<string, any>[];
	};

	type GeoJSONFeature = {
		type: 'Feature';
		geometry: Record<string, any>;
		properties: Record<string, any>;
	};

	type GeneratedReport = {
		report_id: string;
		task_id: string;
		report_type: string;
		evidence_refs: string[];
		pdf_url: string;
		pdf_size_bytes?: number;
		pdf_sha256?: string;
	};

	type UploadedDocument = {
		document_id: string;
		task_id?: string | null;
		filename: string;
		content_type?: string | null;
		byte_size: number;
		language?: string | null;
		extracted_text_chars: number;
		download_url?: string;
	};

	type UploadedDocumentSearchResult = UploadedDocument & {
		score: number;
		snippet: string;
		matched_terms: string[];
	};

	const artifactTabs: ArtifactTab[] = ['Evidence', 'Entities', 'Graph', 'Timeline', 'Map', 'Report'];
	const terminalStatuses = new Set(['completed', 'failed', 'cancelled']);
	const supportedMapGeometryTypes = new Set([
		'Point',
		'MultiPoint',
		'LineString',
		'MultiLineString',
		'Polygon',
		'MultiPolygon'
	]);

	const emptyArtifacts = (): ArtifactState => ({
		evidence: [],
		entities: [],
		relations: [],
		timeline_events: [],
		map_features: [],
		report_sections: []
	});

	let query = '';
	let taskId = '';
	let status = 'ready';
	let isSubmitting = false;
	let errorMessage = '';
	let activeTab: ArtifactTab = 'Evidence';
	let artifacts = emptyArtifacts();
	let eventSource: EventSource | null = null;
	let graphContainer: HTMLDivElement;
	let mapContainer: HTMLDivElement;
	let graphInstance: any = null;
	let mapInstance: any = null;
	let scenarios: LimraScenario[] = [];
	let selectedScenario = '';
	let latestGeneratedReport: GeneratedReport | null = null;
	let isExportingReport = false;
	let reportMessage = '';
	let uploadedDocuments: UploadedDocument[] = [];
	let selectedUploadFile: File | null = null;
	let uploadInput: HTMLInputElement;
	let isUploadingDocument = false;
	let uploadMessage = '';
	let uploadSearchQuery = '';
	let uploadSearchResults: UploadedDocumentSearchResult[] = [];
	let isSearchingUploads = false;
	let uploadSearchMessage = '';

	let messages: ChatMessage[] = [
		{
			role: 'assistant',
			content: 'Enter an OSINT research question. limra will stream progress here and collect structured artifacts in the drawer.',
			time: new Date().toLocaleTimeString()
		}
	];

	$: evidenceCount = artifacts.evidence.length;
	$: entityCount = artifacts.entities.length;
	$: relationCount = artifacts.relations.length;
	$: timelineCount = artifacts.timeline_events.length;
	$: reportSectionCount = artifacts.report_sections.length;
	$: graphHasData = entityCount > 0 || relationCount > 0;
	$: mapFeatureCollection = buildMapFeatureCollection(artifacts);
	$: mapHasData = mapFeatureCollection.features.length > 0;
	$: selectedScenarioDetail =
		scenarios.find((scenario) => scenario.id === selectedScenario) ?? scenarios[0];

	const appendMessage = (role: ChatMessage['role'], content: string) => {
		messages = [
			...messages,
			{
				role,
				content,
				time: new Date().toLocaleTimeString()
			}
		];
	};

	const apiJson = async (path: string, init?: RequestInit) => {
		const response = await fetch(path, {
			...init,
			headers: {
				'Content-Type': 'application/json',
				...(init?.headers ?? {})
			}
		});

		if (!response.ok) {
			const text = await response.text();
			throw new Error(text || `Request failed with ${response.status}`);
		}

		return response.json();
	};

	const loadScenarios = async () => {
		try {
			const data = await apiJson('/api/limra/scenarios');
			scenarios = Array.isArray(data.scenarios) ? data.scenarios : [];
			if (!selectedScenario && scenarios[0]) {
				selectedScenario = scenarios[0].id;
			}
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Unable to load scenarios.';
		}
	};

	const loadUploadedDocuments = async (id = taskId) => {
		try {
			const data = await apiJson(
				id ? `/api/limra/uploads?task_id=${encodeURIComponent(id)}` : '/api/limra/uploads'
			);
			uploadedDocuments = Array.isArray(data.documents) ? data.documents : [];
		} catch (error) {
			uploadMessage = error instanceof Error ? error.message : 'Unable to load uploaded documents.';
		}
	};

	const selectUploadFile = (event: Event) => {
		const input = event.currentTarget as HTMLInputElement;
		selectedUploadFile = input.files?.[0] ?? null;
		uploadMessage = selectedUploadFile ? selectedUploadFile.name : '';
	};

	const uploadDocument = async () => {
		if (!selectedUploadFile || isUploadingDocument) {
			return;
		}

		isUploadingDocument = true;
		uploadMessage = '';

		const formData = new FormData();
		formData.append('file', selectedUploadFile);
		if (taskId) {
			formData.append('task_id', taskId);
		}

		try {
			const response = await fetch('/api/limra/uploads', {
				method: 'POST',
				body: formData
			});
			if (!response.ok) {
				const text = await response.text();
				throw new Error(text || `Upload failed with ${response.status}`);
			}

			selectedUploadFile = null;
			if (uploadInput) {
				uploadInput.value = '';
			}
			uploadMessage = 'Upload ready.';
			await loadUploadedDocuments(taskId);
		} catch (error) {
			uploadMessage = error instanceof Error ? error.message : 'Unable to upload document.';
		} finally {
			isUploadingDocument = false;
		}
	};

	const searchUploadedDocuments = async () => {
		const trimmed = uploadSearchQuery.trim();
		if (!trimmed || isSearchingUploads) {
			return;
		}

		isSearchingUploads = true;
		uploadSearchMessage = '';

		try {
			const taskFilter = taskId ? `&task_id=${encodeURIComponent(taskId)}` : '';
			const data = await apiJson(
				`/api/limra/uploads/search?query=${encodeURIComponent(trimmed)}${taskFilter}`
			);
			uploadSearchResults = Array.isArray(data.documents) ? data.documents : [];
			uploadSearchMessage = uploadSearchResults.length
				? `${uploadSearchResults.length} matching uploads`
				: 'No matching uploads.';
		} catch (error) {
			uploadSearchMessage = error instanceof Error ? error.message : 'Unable to search uploads.';
		} finally {
			isSearchingUploads = false;
		}
	};

	const uploadedDocumentDownloadUrl = (uploadedDocument: UploadedDocument) =>
		uploadedDocument.download_url?.startsWith('/api/limra/uploads/')
			? uploadedDocument.download_url
			: `/api/limra/uploads/${uploadedDocument.document_id}/download`;

	const useScenarioQuery = () => {
		if (selectedScenarioDetail?.default_query) {
			query = selectedScenarioDetail.default_query;
		}
	};

	const submitResearch = async () => {
		const trimmed = query.trim();
		if (!trimmed || isSubmitting) {
			return;
		}

		isSubmitting = true;
		errorMessage = '';
		status = 'starting';
		query = '';
		appendMessage('user', trimmed);

		try {
			const task = await apiJson('/api/limra/research', {
				method: 'POST',
				body: JSON.stringify({
					query: trimmed,
					scenario: selectedScenario || undefined
				})
			});

			taskId = task.task_id ?? task.id ?? '';
			status = task.status ?? 'queued';
			latestGeneratedReport = null;
			reportMessage = '';
			appendMessage('assistant', `Research task ${taskId || 'created'} is ${status}.`);

			if (taskId) {
				connectTaskStream(taskId);
				await loadArtifacts(taskId);
				await loadUploadedDocuments(taskId);
			}
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Unable to start research.';
			status = 'error';
			appendMessage('system', errorMessage);
		} finally {
			isSubmitting = false;
		}
	};

	const connectTaskStream = (id: string) => {
		eventSource?.close();
		eventSource = new EventSource(`/api/limra/tasks/${id}/events`);

		eventSource.onmessage = (event) => {
			const taskEvent = parsePayload(event.data);
			const eventPayload = taskEvent.payload && typeof taskEvent.payload === 'object' ? taskEvent.payload : {};
			const eventType = taskEvent.event ?? taskEvent.type ?? 'task_update';
			const nextStatus = taskEvent.status ?? eventPayload.status;
			const content =
				taskEvent.message ??
				taskEvent.summary ??
				taskEvent.status ??
				eventPayload.message ??
				eventPayload.summary ??
				eventPayload.status ??
				JSON.stringify(Object.keys(eventPayload).length ? eventPayload : taskEvent);

			status = nextStatus ?? status;
			appendMessage('assistant', `${eventType}: ${content}`);

			if (isArtifactEvent(eventType)) {
				void loadArtifacts(id);
			}
			if (isTerminalStatus(nextStatus)) {
				eventSource?.close();
				eventSource = null;
				void loadArtifacts(id);
				void refreshTask(id);
			}
		};

		eventSource.onerror = () => {
			status = status === 'ready' ? status : 'stream reconnecting';
		};
	};

	const isArtifactEvent = (eventType: string) =>
		eventType.includes('evidence') ||
		eventType.includes('entity') ||
		eventType.includes('timeline') ||
		eventType.includes('report');

	const isTerminalStatus = (value: unknown) => typeof value === 'string' && terminalStatuses.has(value);

	const refreshTask = async (id = taskId) => {
		if (!id) {
			return;
		}

		try {
			const task = await apiJson(`/api/limra/tasks/${id}`);
			status = task.status ?? status;
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Unable to refresh task.';
		}
	};

	const loadArtifacts = async (id = taskId) => {
		if (!id) {
			return;
		}

		try {
			const data = await apiJson(`/api/limra/tasks/${id}/artifacts`);
			artifacts = normalizeArtifacts(data);
			await tick();
			void renderGraph();
			void renderMap();
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Unable to load artifacts.';
		}
	};

	const downloadArchive = () => {
		if (!taskId) {
			return;
		}

		window.location.href = `/api/limra/tasks/${taskId}/archive.zip`;
	};

	const exportReportPdf = async () => {
		if (!taskId || artifacts.report_sections.length === 0 || isExportingReport) {
			return;
		}

		isExportingReport = true;
		reportMessage = '';

		try {
			const report = await apiJson(`/api/limra/tasks/${taskId}/reports/pdf`, {
				method: 'POST',
				body: JSON.stringify({
					report_id: `ui-${Date.now()}`,
					report_type: 'final',
					markdown: buildReportMarkdown(),
					evidence_refs: reportEvidenceRefs()
				})
			});

			latestGeneratedReport = report;
			reportMessage = 'PDF ready.';
		} catch (error) {
			reportMessage = error instanceof Error ? error.message : 'Unable to export PDF.';
		} finally {
			isExportingReport = false;
		}
	};

	const downloadGeneratedReportPdf = () => {
		if (!taskId || !latestGeneratedReport?.report_id) {
			return;
		}

		window.location.href =
			latestGeneratedReport.pdf_url ??
			`/api/limra/tasks/${taskId}/reports/${latestGeneratedReport.report_id}/pdf`;
	};

	const parsePayload = (raw: string) => {
		try {
			return JSON.parse(raw);
		} catch {
			return { message: raw };
		}
	};

	const normalizeArtifacts = (data: Record<string, any>): ArtifactState => {
		const source = data.artifacts ?? data;
		return {
			evidence: asArray(source.evidence ?? source.evidence_items),
			entities: asArray(source.entities),
			relations: asArray(source.relations ?? source.entity_relations),
			timeline_events: asArray(source.timeline_events ?? source.timeline),
			map_features: asArray(source.map_features ?? source.features),
			report_sections: asArray(source.report_sections ?? source.reports)
		};
	};

	const asArray = (value: unknown): Record<string, any>[] => {
		if (Array.isArray(value)) {
			return value as Record<string, any>[];
		}
		return [];
	};

	const evidenceId = (item: Record<string, any>, index: number) =>
		String(item.evidence_id ?? item.ref_id ?? item.id ?? `EVID-${String(index + 1).padStart(3, '0')}`);

	const entityLabel = (item: Record<string, any>, index: number) =>
		String(item.name ?? item.label ?? item.entity_id ?? `Entity ${index + 1}`);

	const entityId = (item: Record<string, any>, index: number) =>
		String(item.entity_id ?? item.id ?? item.name ?? `entity-${index + 1}`);

	const relationEndpoint = (item: Record<string, any>, side: 'source' | 'target') =>
		String(
			item[`${side}_entity_id`] ??
				item[`${side}_id`] ??
				item[side] ??
				item[side === 'source' ? 'from' : 'to'] ??
				''
		);

	const reportSectionTitle = (section: Record<string, any>, index: number) =>
		String(section.title ?? section.report_type ?? `Report section ${index + 1}`);

	const reportSectionText = (section: Record<string, any>) =>
		String(section.markdown ?? section.text ?? section.summary ?? 'No report text available.');

	const reportEvidenceRefs = () => {
		const refs = new Set<string>();
		for (const section of artifacts.report_sections) {
			for (const ref of asArray(section.evidence_refs)) {
				const normalized = String(ref).trim();
				if (normalized) {
					refs.add(normalized);
				}
			}
		}
		return [...refs];
	};

	const buildReportMarkdown = () =>
		artifacts.report_sections
			.map((section, index) => {
				const refs = asArray(section.evidence_refs)
					.map((ref) => String(ref).trim())
					.filter(Boolean);
				const refLine = refs.length ? `\n\nEvidence refs: ${refs.map((ref) => `[${ref}]`).join(' ')}` : '';
				return `## ${reportSectionTitle(section, index)}\n\n${reportSectionText(section)}${refLine}`;
			})
			.join('\n\n');

	const selectTab = async (tab: ArtifactTab) => {
		activeTab = tab;
		await tick();
		if (tab === 'Graph') {
			await renderGraph();
		}
		if (tab === 'Map') {
			await renderMap();
		}
	};

	const scrollToEvidence = async (ref: string) => {
		activeTab = 'Evidence';
		await tick();
		document.getElementById(`evidence-${safeDomId(ref)}`)?.scrollIntoView({
			behavior: 'smooth',
			block: 'center'
		});
	};

	const safeDomId = (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, '-');

	const renderGraph = async () => {
		if (activeTab !== 'Graph' || !graphContainer || !graphHasData) {
			return;
		}

		const { default: cytoscape } = await import('cytoscape');
		graphInstance?.destroy();

		const nodes = artifacts.entities.map((entity, index) => ({
			data: {
				id: entityId(entity, index),
				label: entityLabel(entity, index),
				type: entity.type ?? entity.entity_type ?? 'entity'
			}
		}));

		const edges = artifacts.relations
			.map((relation, index) => {
				const source = relationEndpoint(relation, 'source');
				const target = relationEndpoint(relation, 'target');
				if (!source || !target) {
					return null;
				}

				return {
					data: {
						id: String(relation.relation_id ?? relation.id ?? `relation-${index + 1}`),
						source,
						target,
						label: relation.type ?? relation.relation_type ?? 'mentions'
					}
				};
			})
			.filter(Boolean);

		graphInstance = cytoscape({
			container: graphContainer,
			elements: [...nodes, ...edges],
			layout: {
				name: nodes.length > 1 ? 'cose' : 'grid',
				animate: false
			},
			style: [
				{
					selector: 'node',
					style: {
						'background-color': '#0f766e',
						color: '#111827',
						label: 'data(label)',
						'font-size': 11,
						'text-valign': 'bottom',
						'text-margin-y': 7,
						width: 24,
						height: 24
					}
				},
				{
					selector: 'edge',
					style: {
						width: 1.4,
						'line-color': '#64748b',
						'target-arrow-color': '#64748b',
						'target-arrow-shape': 'triangle',
						'curve-style': 'bezier',
						label: 'data(label)',
						'font-size': 9,
						color: '#475569'
					}
				}
			]
		});
	};

	const buildMapFeatureCollection = (state: ArtifactState) => {
		const features = [...state.map_features, ...state.timeline_events]
			.map((item, index) => {
				const geometry = normalizeMapGeometry(
					item.geometry ?? item.payload?.geometry ?? item.geojson ?? item.payload?.geojson
				);
				if (!geometry) {
					return null;
				}

				return {
					type: 'Feature',
					geometry,
					properties: {
						title: item.title ?? item.event_title ?? item.name ?? `Feature ${index + 1}`,
						risk_level: item.risk_level ?? item.risk ?? 'unknown'
					}
				} satisfies GeoJSONFeature;
			})
			.filter(Boolean) as GeoJSONFeature[];

		return {
			type: 'FeatureCollection',
			features
		};
	};

	const normalizeMapGeometry = (rawGeometry: unknown) => {
		const parsed = typeof rawGeometry === 'string' ? parsePayload(rawGeometry) : rawGeometry;
		const candidate =
			parsed && typeof parsed === 'object' && (parsed as Record<string, any>).type === 'Feature'
				? (parsed as Record<string, any>).geometry
				: parsed;
		if (!candidate || typeof candidate !== 'object') {
			return null;
		}

		const geometry = candidate as Record<string, any>;
		if (!supportedMapGeometryTypes.has(String(geometry.type))) {
			return null;
		}
		if (collectCoordinatePairs(geometry.coordinates).length === 0) {
			return null;
		}
		return geometry;
	};

	const collectCoordinatePairs = (coordinates: unknown): [number, number][] => {
		if (isLngLatPair(coordinates)) {
			return [[Number(coordinates[0]), Number(coordinates[1])]];
		}
		if (!Array.isArray(coordinates)) {
			return [];
		}
		return coordinates.flatMap((value) => collectCoordinatePairs(value));
	};

	const isLngLatPair = (value: unknown): value is [number | string, number | string] =>
		Array.isArray(value) &&
		value.length >= 2 &&
		Number.isFinite(Number(value[0])) &&
		Number.isFinite(Number(value[1]));

	const featureCenter = (feature: GeoJSONFeature) => {
		const coordinates = collectCoordinatePairs(feature.geometry?.coordinates);
		if (coordinates.length > 0) {
			const sums = coordinates.reduce(
				([lng, lat], pair) => [lng + pair[0], lat + pair[1]],
				[0, 0]
			);
			return [sums[0] / coordinates.length, sums[1] / coordinates.length];
		}
		return [0, 20];
	};

	const renderMap = async () => {
		if (activeTab !== 'Map' || !mapContainer || !mapHasData) {
			return;
		}

		const maplibregl = await import('maplibre-gl');
		const maplibre = (maplibregl.default ?? maplibregl) as any;
		mapInstance?.remove();

		const firstFeature = mapFeatureCollection.features[0] as GeoJSONFeature;

		mapInstance = new maplibre.Map({
			container: mapContainer,
			center: featureCenter(firstFeature),
			zoom: 2,
			attributionControl: false,
			style: {
				version: 8,
				sources: {
					limra: {
						type: 'geojson',
						data: mapFeatureCollection
					}
				},
				layers: [
					{
						id: 'limra-polygons',
						type: 'fill',
						source: 'limra',
						filter: [
							'any',
							['==', ['geometry-type'], 'Polygon'],
							['==', ['geometry-type'], 'MultiPolygon']
						],
						paint: {
							'fill-color': '#0f766e',
							'fill-opacity': 0.22
						}
					},
					{
						id: 'limra-lines',
						type: 'line',
						source: 'limra',
						filter: [
							'any',
							['==', ['geometry-type'], 'LineString'],
							['==', ['geometry-type'], 'MultiLineString'],
							['==', ['geometry-type'], 'Polygon'],
							['==', ['geometry-type'], 'MultiPolygon']
						],
						paint: {
							'line-color': '#0f766e',
							'line-width': 3,
							'line-opacity': 0.82
						}
					},
					{
						id: 'limra-points',
						type: 'circle',
						source: 'limra',
						filter: [
							'any',
							['==', ['geometry-type'], 'Point'],
							['==', ['geometry-type'], 'MultiPoint']
						],
						paint: {
							'circle-radius': 7,
							'circle-color': '#2563eb',
							'circle-stroke-width': 2,
							'circle-stroke-color': '#ffffff'
						}
					}
				]
			}
		});

		mapInstance.addControl(new maplibre.NavigationControl({ showCompass: false }), 'top-right');
	};

	onMount(() => {
		void loadScenarios();
		void loadUploadedDocuments();
	});

	onDestroy(() => {
		eventSource?.close();
		graphInstance?.destroy();
		mapInstance?.remove();
	});
</script>

<svelte:head>
	<title>limra research</title>
</svelte:head>

<div class="limra-research-page">
	<section class="chat-pane" aria-label="limra research chat">
		<header class="workspace-header">
			<div>
				<p class="eyebrow">limra OSINT</p>
				<h1>Research workspace</h1>
			</div>
			<div class="task-state" aria-live="polite">
				<span>{status}</span>
				{#if taskId}
					<code>{taskId}</code>
				{/if}
			</div>
		</header>

		<div class="message-stream" aria-live="polite">
			{#each messages as message}
				<article class={`message-row ${message.role === 'user' ? 'from-user' : ''}`}>
					<div class="message-meta">
						<span>{message.role}</span>
						<time>{message.time}</time>
					</div>
					<p>{message.content}</p>
				</article>
			{/each}
		</div>

		{#if errorMessage}
			<p class="error-message">{errorMessage}</p>
		{/if}

		<form class="query-form" on:submit|preventDefault={submitResearch}>
			<div class="scenario-field">
				<label for="limra-scenario">Scenario</label>
				<div class="scenario-select-row">
					<select id="limra-scenario" bind:value={selectedScenario}>
						{#if scenarios.length === 0}
							<option value="">General OSINT</option>
						{/if}
						{#each scenarios as scenario}
							<option value={scenario.id}>{scenario.title}</option>
						{/each}
					</select>
					<button type="button" class="secondary-button" disabled={!selectedScenarioDetail} on:click={useScenarioQuery}>
						Use scenario query
					</button>
				</div>
				{#if selectedScenarioDetail}
					<p class="scenario-summary">{selectedScenarioDetail.description}</p>
				{/if}
			</div>
			<label for="limra-query">Research query</label>
			<textarea
				id="limra-query"
				bind:value={query}
				rows="4"
				placeholder={selectedScenarioDetail?.default_query ??
					'Track recent export control changes affecting semiconductor supply chains'}
				on:keydown={(event) => {
					if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
						void submitResearch();
					}
				}}
			/>
			<div class="form-actions">
				<button type="button" class="secondary-button" disabled={!taskId} on:click={() => loadArtifacts()}>
					Refresh artifacts
				</button>
				<button type="button" class="secondary-button" disabled={!taskId} on:click={downloadArchive}>
					Download archive
				</button>
				<button type="submit" class="primary-button" disabled={isSubmitting || !query.trim()}>
					{isSubmitting ? 'Starting...' : 'Run research'}
				</button>
			</div>
		</form>

		<section class="upload-tools" aria-label="Uploaded documents">
			<div class="upload-heading">
				<div>
					<label for="limra-upload">Upload document</label>
					<p>Attach PDF or text sources to this workspace.</p>
				</div>
				<button type="button" class="secondary-button" on:click={() => loadUploadedDocuments()}>
					Refresh uploads
				</button>
			</div>
			<div class="upload-row">
				<input
					id="limra-upload"
					bind:this={uploadInput}
					type="file"
					accept=".pdf,.txt,application/pdf,text/plain"
					on:change={selectUploadFile}
				/>
				<button
					type="button"
					class="secondary-button"
					disabled={!selectedUploadFile || isUploadingDocument}
					on:click={uploadDocument}
				>
					{isUploadingDocument ? 'Uploading...' : 'Upload'}
				</button>
			</div>
			{#if uploadMessage}
				<p class="upload-message">{uploadMessage}</p>
			{/if}
			<div class="upload-search-row">
				<label for="limra-upload-search">Search uploads</label>
				<input
					id="limra-upload-search"
					type="search"
					bind:value={uploadSearchQuery}
					placeholder="Search extracted text"
					on:keydown={(event) => {
						if (event.key === 'Enter') {
							event.preventDefault();
							void searchUploadedDocuments();
						}
					}}
				/>
				<button
					type="button"
					class="secondary-button"
					disabled={!uploadSearchQuery.trim() || isSearchingUploads}
					on:click={searchUploadedDocuments}
				>
					{isSearchingUploads ? 'Searching...' : 'Search'}
				</button>
			</div>
			{#if uploadSearchMessage}
				<p class="upload-message">{uploadSearchMessage}</p>
			{/if}
			{#if uploadSearchResults.length > 0}
				<ul class="upload-search-list">
					{#each uploadSearchResults as result}
						<li>
							<div>
								<strong>{result.filename}</strong>
								<span>{result.snippet}</span>
							</div>
							<small>{result.matched_terms.join(', ')} · {result.score}</small>
						</li>
					{/each}
				</ul>
			{/if}
			{#if uploadedDocuments.length > 0}
				<ul class="upload-list">
					{#each uploadedDocuments as uploadedDocument}
						<li>
							<div>
								<strong>{uploadedDocument.filename}</strong>
								<span>{uploadedDocument.extracted_text_chars} extracted chars</span>
							</div>
							<a href={uploadedDocumentDownloadUrl(uploadedDocument)}>Download</a>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	</section>

	<aside class="artifact-drawer" aria-label="Research artifacts">
		<div class="drawer-header">
			<div>
				<p class="eyebrow">Artifact ledger</p>
				<h2>Evidence workspace</h2>
			</div>
			<div class="counts">
				<span>{evidenceCount} evidence</span>
				<span>{entityCount} entities</span>
			</div>
		</div>

		<div class="tabs" role="tablist" aria-label="Artifact panels">
			{#each artifactTabs as tab}
				<button
					type="button"
					role="tab"
					aria-selected={activeTab === tab}
					class:active={activeTab === tab}
					on:click={() => selectTab(tab)}
				>
					{tab}
				</button>
			{/each}
		</div>

		<div class="drawer-body">
			{#if activeTab === 'Evidence'}
				{#if artifacts.evidence.length === 0}
					<div class="empty-state">
						<strong>No evidence collected yet.</strong>
						<span>Evidence records will appear here as the research stream validates sources.</span>
					</div>
				{:else}
					<div class="artifact-list">
						{#each artifacts.evidence as item, index}
							<article id={`evidence-${safeDomId(evidenceId(item, index))}`} class="artifact-item">
								<div class="item-heading">
									<button type="button" class="ref-button" on:click={() => scrollToEvidence(evidenceId(item, index))}>
										[{evidenceId(item, index)}]
									</button>
									<span>{item.publisher ?? item.language ?? 'source'}</span>
								</div>
								<h3>{item.title ?? item.source_title ?? 'Untitled evidence'}</h3>
								<p>{item.summary ?? item.original_text ?? 'No summary available.'}</p>
								{#if item.source_url ?? item.url}
									<a href={item.source_url ?? item.url} target="_blank" rel="noreferrer">Open source</a>
								{/if}
							</article>
						{/each}
					</div>
				{/if}
			{:else if activeTab === 'Entities'}
				{#if artifacts.entities.length === 0}
					<div class="empty-state">
						<strong>No entities extracted yet.</strong>
						<span>Countries, agencies, companies, people, policies, projects, locations, and events will appear here.</span>
					</div>
				{:else}
					<div class="entity-grid">
						{#each artifacts.entities as item, index}
							<article class="entity-chip">
								<strong>{entityLabel(item, index)}</strong>
								<span>{item.type ?? item.entity_type ?? 'entity'}</span>
							</article>
						{/each}
					</div>
				{/if}
			{:else if activeTab === 'Graph'}
				{#if !graphHasData}
					<div class="empty-state">
						<strong>Graph is empty.</strong>
						<span>Cytoscape.js will render entity and relation artifacts after extraction.</span>
					</div>
				{:else}
					<div class="graph-summary">
						<span>{entityCount} nodes</span>
						<span>{relationCount} relations</span>
					</div>
					<div bind:this={graphContainer} class="graph-canvas" aria-label="Entity relation graph"></div>
				{/if}
			{:else if activeTab === 'Timeline'}
				{#if artifacts.timeline_events.length === 0}
					<div class="empty-state">
						<strong>No timeline events yet.</strong>
						<span>Time-bound events with evidence references will populate this panel.</span>
					</div>
				{:else}
					<ol class="timeline-list">
						{#each artifacts.timeline_events as item}
							<li>
								<time>{item.time ?? item.event_time ?? 'time unknown'}</time>
								<strong>{item.title ?? item.event_title ?? 'Timeline event'}</strong>
								<span>{item.risk_level ?? item.confidence ?? 'unrated'}</span>
							</li>
						{/each}
					</ol>
				{/if}
			{:else if activeTab === 'Map'}
				{#if !mapHasData}
					<div class="empty-state">
						<strong>Map has no geometry yet.</strong>
						<span>MapLibre GL JS will render timeline and map features once geometry artifacts exist.</span>
					</div>
				{:else}
					<div bind:this={mapContainer} class="map-canvas" aria-label="Artifact map"></div>
				{/if}
			{:else if activeTab === 'Report'}
				<div class="report-toolbar">
					<div class="report-summary">
						<strong>PDF export</strong>
						{#if latestGeneratedReport}
							<span>{latestGeneratedReport.report_id}</span>
						{:else}
							<span>{reportSectionCount} sections</span>
						{/if}
					</div>
					<div class="report-actions">
						<button
							type="button"
							class="secondary-button"
							disabled={!taskId || isExportingReport || artifacts.report_sections.length === 0}
							on:click={exportReportPdf}
						>
							{isExportingReport ? 'Exporting...' : 'Export PDF'}
						</button>
						<button
							type="button"
							class="secondary-button"
							disabled={!latestGeneratedReport?.report_id}
							on:click={downloadGeneratedReportPdf}
						>
							Download PDF
						</button>
					</div>
				</div>
				{#if reportMessage}
					<p class="report-message">{reportMessage}</p>
				{/if}
				{#if artifacts.report_sections.length === 0}
					<div class="empty-state">
						<strong>No report sections yet.</strong>
						<span>Draft analysis sections and clickable evidence references will appear here before export.</span>
					</div>
				{:else}
					<div class="artifact-list">
						{#each artifacts.report_sections as section, index}
							<article class="artifact-item">
								<h3>{reportSectionTitle(section, index)}</h3>
								<p>{reportSectionText(section)}</p>
								<div class="ref-row">
									{#each asArray(section.evidence_refs) as ref}
										<button type="button" class="ref-button" on:click={() => scrollToEvidence(String(ref))}>
											[{String(ref)}]
										</button>
									{/each}
								</div>
							</article>
						{/each}
					</div>
				{/if}
			{/if}
		</div>
	</aside>
</div>

<style>
	.limra-research-page {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(340px, 430px);
		gap: 1rem;
		height: calc(100vh - 1rem);
		padding: 0.75rem;
		background: #f8fafc;
		color: #111827;
	}

	:global(.dark) .limra-research-page {
		background: #0f1115;
		color: #f8fafc;
	}

	.chat-pane,
	.artifact-drawer {
		min-width: 0;
		border: 1px solid #d9e2ec;
		background: #ffffff;
		display: flex;
		flex-direction: column;
	}

	:global(.dark) .chat-pane,
	:global(.dark) .artifact-drawer {
		border-color: #2f3642;
		background: #151922;
	}

	.workspace-header,
	.drawer-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 1rem;
		border-bottom: 1px solid #e2e8f0;
	}

	:global(.dark) .workspace-header,
	:global(.dark) .drawer-header {
		border-color: #293241;
	}

	h1,
	h2,
	h3,
	p {
		margin: 0;
	}

	h1 {
		font-size: 1.25rem;
		font-weight: 700;
	}

	h2 {
		font-size: 1rem;
		font-weight: 700;
	}

	h3 {
		font-size: 0.9rem;
		font-weight: 700;
	}

	.eyebrow {
		color: #0f766e;
		font-size: 0.75rem;
		font-weight: 700;
		letter-spacing: 0;
		text-transform: uppercase;
	}

	.task-state,
	.counts {
		display: flex;
		align-items: flex-end;
		flex-direction: column;
		gap: 0.2rem;
		color: #64748b;
		font-size: 0.78rem;
		text-align: right;
	}

	.task-state code {
		max-width: 14rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.message-stream {
		flex: 1;
		overflow-y: auto;
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.message-row {
		border: 1px solid #e2e8f0;
		padding: 0.75rem;
		background: #f8fafc;
	}

	.message-row.from-user {
		border-color: #bfdbfe;
		background: #eff6ff;
	}

	:global(.dark) .message-row {
		border-color: #2f3642;
		background: #111827;
	}

	:global(.dark) .message-row.from-user {
		border-color: #1d4ed8;
		background: #172554;
	}

	.message-meta {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		margin-bottom: 0.35rem;
		color: #64748b;
		font-size: 0.72rem;
		text-transform: uppercase;
	}

	.query-form {
		border-top: 1px solid #e2e8f0;
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.65rem;
	}

	:global(.dark) .query-form {
		border-color: #293241;
	}

	label {
		font-size: 0.82rem;
		font-weight: 700;
	}

	textarea {
		min-height: 7rem;
		resize: vertical;
		border: 1px solid #cbd5e1;
		padding: 0.75rem;
		background: #ffffff;
		color: inherit;
	}

	:global(.dark) textarea {
		border-color: #334155;
		background: #0f172a;
	}

	.scenario-field {
		display: grid;
		gap: 0.45rem;
	}

	.scenario-select-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	select {
		flex: 1;
		min-width: 14rem;
		min-height: 2.25rem;
		border: 1px solid #cbd5e1;
		padding: 0 0.65rem;
		background: #ffffff;
		color: inherit;
		font: inherit;
		font-size: 0.85rem;
	}

	:global(.dark) select {
		border-color: #334155;
		background: #0f172a;
	}

	.scenario-summary {
		color: #64748b;
		font-size: 0.82rem;
		line-height: 1.4;
	}

	.form-actions {
		display: flex;
		justify-content: flex-end;
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	.upload-tools {
		border-top: 1px solid #e2e8f0;
		padding: 0.85rem 1rem 1rem;
		display: grid;
		gap: 0.65rem;
	}

	:global(.dark) .upload-tools {
		border-color: #293241;
	}

	.upload-heading,
	.upload-row,
	.upload-search-row,
	.upload-list li,
	.upload-search-list li {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
	}

	.upload-heading p,
	.upload-message,
	.upload-list span,
	.upload-search-list span,
	.upload-search-list small {
		color: #64748b;
		font-size: 0.78rem;
		line-height: 1.35;
	}

	.upload-row,
	.upload-search-row {
		flex-wrap: wrap;
		justify-content: flex-start;
	}

	input[type='file'],
	input[type='search'] {
		min-width: 16rem;
		max-width: 100%;
		font: inherit;
		font-size: 0.82rem;
	}

	input[type='search'] {
		min-height: 2.25rem;
		border: 1px solid #cbd5e1;
		padding: 0 0.65rem;
		background: #ffffff;
		color: inherit;
	}

	:global(.dark) input[type='search'] {
		border-color: #334155;
		background: #0f172a;
	}

	.upload-list,
	.upload-search-list {
		display: grid;
		gap: 0.4rem;
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.upload-list li,
	.upload-search-list li {
		border: 1px solid #e2e8f0;
		padding: 0.55rem 0.65rem;
		background: #f8fafc;
	}

	:global(.dark) .upload-list li,
	:global(.dark) .upload-search-list li {
		border-color: #2f3642;
		background: #111827;
	}

	.upload-list div,
	.upload-search-list div {
		min-width: 0;
		display: grid;
		gap: 0.15rem;
	}

	.upload-list strong,
	.upload-search-list strong {
		overflow-wrap: anywhere;
		font-size: 0.84rem;
	}

	button {
		min-height: 2.25rem;
		border: 1px solid transparent;
		padding: 0 0.75rem;
		font-size: 0.85rem;
		font-weight: 700;
	}

	button:disabled {
		cursor: not-allowed;
		opacity: 0.45;
	}

	.primary-button {
		background: #0f766e;
		color: #ffffff;
	}

	.secondary-button {
		border-color: #cbd5e1;
		background: transparent;
		color: inherit;
	}

	.error-message {
		margin: 0 1rem;
		border: 1px solid #fecaca;
		background: #fef2f2;
		color: #991b1b;
		padding: 0.65rem;
		font-size: 0.85rem;
	}

	.tabs {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		border-bottom: 1px solid #e2e8f0;
	}

	:global(.dark) .tabs {
		border-color: #293241;
	}

	.tabs button {
		border: 0;
		border-right: 1px solid #e2e8f0;
		background: transparent;
		color: #64748b;
	}

	.tabs button.active {
		background: #ecfdf5;
		color: #065f46;
	}

	:global(.dark) .tabs button {
		border-color: #293241;
	}

	:global(.dark) .tabs button.active {
		background: #052e2b;
		color: #99f6e4;
	}

	.drawer-body {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 0.9rem;
	}

	.empty-state {
		display: flex;
		min-height: 12rem;
		align-items: center;
		justify-content: center;
		flex-direction: column;
		gap: 0.45rem;
		border: 1px dashed #cbd5e1;
		color: #64748b;
		padding: 1rem;
		text-align: center;
	}

	.artifact-list,
	.timeline-list,
	.entity-grid {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.artifact-item,
	.entity-chip,
	.timeline-list li {
		border: 1px solid #e2e8f0;
		padding: 0.75rem;
		background: #ffffff;
	}

	:global(.dark) .artifact-item,
	:global(.dark) .entity-chip,
	:global(.dark) .timeline-list li {
		border-color: #2f3642;
		background: #111827;
	}

	.item-heading,
	.ref-row,
	.graph-summary {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.45rem;
		color: #64748b;
		font-size: 0.78rem;
	}

	.artifact-item p {
		margin-top: 0.45rem;
		color: #475569;
		font-size: 0.88rem;
		line-height: 1.45;
	}

	:global(.dark) .artifact-item p {
		color: #cbd5e1;
	}

	.artifact-item a {
		display: inline-flex;
		margin-top: 0.55rem;
		color: #2563eb;
		font-size: 0.84rem;
		font-weight: 700;
	}

	.ref-button {
		min-height: 1.7rem;
		border-color: #bfdbfe;
		background: #eff6ff;
		color: #1d4ed8;
		padding: 0 0.45rem;
		font-size: 0.75rem;
	}

	.entity-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.entity-chip {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.entity-chip span,
	.timeline-list span,
	.timeline-list time {
		color: #64748b;
		font-size: 0.78rem;
	}

	.timeline-list {
		list-style: none;
		margin: 0;
		padding: 0;
	}

	.timeline-list li {
		display: grid;
		gap: 0.3rem;
	}

	.graph-canvas,
	.map-canvas {
		min-height: 26rem;
		border: 1px solid #cbd5e1;
		background: #eef2f7;
	}

	.graph-summary {
		justify-content: space-between;
		margin-bottom: 0.5rem;
	}

	.report-toolbar,
	.report-actions,
	.report-summary {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
	}

	.report-toolbar {
		justify-content: space-between;
		margin-bottom: 0.75rem;
	}

	.report-summary span,
	.report-message {
		color: #64748b;
		font-size: 0.82rem;
	}

	.report-message {
		margin: 0 0 0.75rem;
	}

	@media (max-width: 900px) {
		.limra-research-page {
			grid-template-columns: 1fr;
			height: auto;
		}

		.chat-pane,
		.artifact-drawer {
			min-height: 36rem;
		}

		.artifact-drawer {
			max-height: 80vh;
		}
	}

	@media (max-width: 560px) {
		.workspace-header,
		.drawer-header,
		.scenario-select-row,
		.report-toolbar,
		.report-actions,
		.form-actions {
			align-items: stretch;
			flex-direction: column;
		}

		.task-state,
		.counts {
			align-items: flex-start;
			text-align: left;
		}

		.tabs {
			grid-template-columns: repeat(2, 1fr);
		}

		.entity-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
