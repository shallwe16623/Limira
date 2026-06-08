import { expect, test } from '@playwright/test';

const limraBaseUrl = process.env.LIMRA_WEB_BASE_URL ?? 'http://127.0.0.1:5173';
const smokeAuthToken = 'limra-smoke-token';
const privateRunnerUrlFragments = ['RUNNER_SERVICE_TOKEN', '/mirothinker/', 'limra-runner:8091', 'localhost:8091'];
const streamedArtifactEvents = ['relation_extracted', 'map_feature_added', 'verification_result'] as const;

const smokeBackendConfig = {
	name: 'limra',
	version: 'smoke',
	default_locale: 'en-US',
	features: {
		enable_websocket: false,
		enable_direct_connections: false
	}
};

const smokeSessionUser = {
	id: 'user-smoke',
	name: 'Smoke User',
	email: 'smoke@example.test',
	role: 'user',
	profile_image_url: '',
	token: smokeAuthToken,
	permissions: {}
};

const emptyArtifacts = {
	artifacts: {
		evidence: [],
		entities: [],
		relations: [],
		timeline_events: [],
		map_features: [],
		report_sections: []
	}
};

const populatedArtifacts = {
	artifacts: {
		evidence: [
			{
				evidence_id: 'EVID-001',
				title: 'Critical minerals corridor filing',
				summary: 'Public filing links a processing zone, rail corridor, and export review.',
				source_url: 'https://sources.example.test/critical-minerals-corridor'
			}
		],
		entities: [
			{ entity_id: 'entity-ministry', name: 'Ministry of Trade', type: 'agency' },
			{ entity_id: 'entity-refinery', name: 'Lithium Refinery Alpha', type: 'facility' }
		],
		relations: [
			{
				relation_id: 'rel-001',
				source_entity_id: 'entity-ministry',
				target_entity_id: 'entity-refinery',
				relation_type: 'licenses'
			}
		],
		timeline_events: [
			{
				title: 'Export license review opened',
				time: '2026-05-12',
				risk_level: 'medium',
				geometry: { type: 'Point', coordinates: [101.7, 3.1] }
			}
		],
		map_features: [
			{
				title: 'Supply corridor',
				geometry: {
					type: 'LineString',
					coordinates: [
						[100.9, 2.9],
						[101.7, 3.1],
						[102.4, 3.6]
					]
				}
			},
			{
				title: 'Processing zone',
				geojson: {
					type: 'Feature',
					geometry: {
						type: 'Polygon',
						coordinates: [
							[
								[101.1, 2.8],
								[101.9, 2.8],
								[101.9, 3.4],
								[101.1, 3.4],
								[101.1, 2.8]
							]
						]
					},
					properties: { risk_level: 'medium' }
				}
			}
		],
		report_sections: [
			{
				title: 'Assessment',
				markdown: 'The corridor creates export-control monitoring exposure for lithium shipments.',
				evidence_refs: ['EVID-001']
			}
		]
	}
};

declare global {
	interface Window {
		__limraFakeEventSource?: {
			instances: Array<{
				url: string;
				emit: (data: unknown) => void;
				close: () => void;
			}>;
		};
	}
}

test('limra research stream refreshes artifact tabs and map geometry without private Runner URLs', async ({
	page
}) => {
	const requestedUrls: string[] = [];
	let artifactLoadCount = 0;

	page.on('request', (request) => {
		requestedUrls.push(request.url());
	});

	await page.addInitScript(() => {
		class FakeEventSource {
			static instances: FakeEventSource[] = [];
			url: string;
			readyState = 1;
			onmessage: ((event: MessageEvent) => void) | null = null;
			onerror: (() => void) | null = null;

			constructor(url: string) {
				this.url = url;
				FakeEventSource.instances.push(this);
			}

			close() {
				this.readyState = 2;
			}

			emit(data: unknown) {
				this.onmessage?.({
					data: JSON.stringify(data),
					origin: window.location.origin,
					lastEventId: '',
					source: null,
					ports: []
				} as MessageEvent);
			}
		}

		Object.defineProperty(window, 'EventSource', { value: FakeEventSource });
		Object.defineProperty(window, '__limraFakeEventSource', { value: FakeEventSource });
		localStorage.setItem('token', 'limra-smoke-token');
		localStorage.setItem('locale', 'en-US');
		localStorage.setItem(
			'settings',
			JSON.stringify({
				ui: {},
				toolServers: [],
				terminalServers: []
			})
		);
		(localStorage as Storage & { token: string }).token = 'limra-smoke-token';
	});

	await page.route('**/api/config', async (route) => {
		await route.fulfill({ json: smokeBackendConfig });
	});
	await page.route('**/api/v1/auths/', async (route) => {
		await route.fulfill({ json: smokeSessionUser });
	});
	await page.route('**/api/v1/auths/update/timezone', async (route) => {
		await route.fulfill({ json: { ok: true } });
	});
	await page.route('**/api/v1/users/user/settings', async (route) => {
		await route.fulfill({
			json: {
				ui: {},
				toolServers: [],
				terminalServers: []
			}
		});
	});
	await page.route('**/api/models**', async (route) => {
		await route.fulfill({ json: { data: [] } });
	});
	await page.route('**/api/v1/configs/banners', async (route) => {
		await route.fulfill({ json: [] });
	});
	await page.route('**/api/v1/tools/', async (route) => {
		await route.fulfill({ json: [] });
	});
	await page.route('**/api/v1/terminals/', async (route) => {
		await route.fulfill({ json: [] });
	});
	await page.route('**/api/limra/scenarios', async (route) => {
		await route.fulfill({
			json: {
				scenarios: [
					{
						id: 'critical-minerals-competition',
						title: 'Critical minerals competition',
						description: 'Track supply-chain, licensing, and geopolitical risks.',
						default_query: 'Assess critical minerals competition around lithium processing.',
						focus_areas: ['evidence', 'entities', 'map']
					}
				]
			}
		});
	});
	await page.route('**/api/limra/uploads**', async (route) => {
		await route.fulfill({ json: { documents: [] } });
	});
	await page.route('**/api/limra/research', async (route) => {
		await route.fulfill({
			status: 201,
			json: { task_id: 'task-smoke', status: 'running' }
		});
	});
	await page.route('**/api/limra/tasks/task-smoke', async (route) => {
		await route.fulfill({ json: { task_id: 'task-smoke', status: 'running' } });
	});
	await page.route('**/api/limra/tasks/task-smoke/artifacts', async (route) => {
		artifactLoadCount += 1;
		await route.fulfill({
			json: artifactLoadCount === 1 ? emptyArtifacts : populatedArtifacts
		});
	});

	await page.goto(new URL('/limra', limraBaseUrl).toString());
	await page.getByLabel('Research query').fill('Assess critical minerals competition.');
	await page.getByRole('button', { name: 'Run research' }).click();

	await expect.poll(async () => page.evaluate(() => window.__limraFakeEventSource?.instances.length ?? 0)).toBe(1);

	for (const eventType of streamedArtifactEvents) {
		await page.evaluate((currentEventType) => {
			const sources = window.__limraFakeEventSource?.instances ?? [];
			const source = sources[sources.length - 1];
			if (!source) {
				throw new Error('Fake EventSource was not created.');
			}
			source.emit({
				event: currentEventType,
				payload: { message: `${currentEventType} ready` }
			});
		}, eventType);
	}

	await expect.poll(() => artifactLoadCount).toBeGreaterThanOrEqual(1 + streamedArtifactEvents.length);

	await page.getByRole('tab', { name: 'Graph' }).click();
	await expect(page.getByText('2 nodes')).toBeVisible();
	await expect(page.getByText('1 relations')).toBeVisible();

	await page.getByRole('tab', { name: 'Map' }).click();
	await expect(page.locator('.map-canvas')).toBeVisible();
	await expect(page.getByText('Map has no geometry yet.')).toHaveCount(0);

	await page.getByRole('tab', { name: 'Report' }).click();
	await expect(page.getByText('Assessment')).toBeVisible();
	await expect(page.getByRole('button', { name: '[EVID-001]' })).toBeVisible();

	for (const forbidden of privateRunnerUrlFragments) {
		expect(requestedUrls.some((url) => url.includes(forbidden))).toBe(false);
	}
	expect(requestedUrls.some((url) => url.includes('/api/limra/research'))).toBe(true);
	expect(requestedUrls.some((url) => url.includes('/api/limra/tasks/task-smoke/events'))).toBe(true);
	expect(requestedUrls.some((url) => url.includes('/api/limra/tasks/task-smoke/artifacts'))).toBe(true);
});
