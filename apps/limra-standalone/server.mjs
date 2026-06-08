import { createServer } from 'node:http';
import { createReadStream, statSync } from 'node:fs';
import { extname, join, normalize, resolve } from 'node:path';
import { Readable } from 'node:stream';
import { fileURLToPath } from 'node:url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const publicRoot = resolve(__dirname, 'public');
const indexPath = join(publicRoot, 'index.html');

const host = process.env.LIMRA_STANDALONE_HOST || '0.0.0.0';
const port = Number(process.env.LIMRA_STANDALONE_PORT || 5173);
const backendUrl = (process.env.LIMRA_BACKEND_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');

const mimeTypes = new Map([
	['.html', 'text/html; charset=utf-8'],
	['.css', 'text/css; charset=utf-8'],
	['.js', 'text/javascript; charset=utf-8'],
	['.json', 'application/json; charset=utf-8'],
	['.svg', 'image/svg+xml'],
	['.png', 'image/png'],
	['.ico', 'image/x-icon']
]);

const server = createServer(async (req, res) => {
	try {
		const requestUrl = new URL(req.url || '/', `http://${req.headers.host || `${host}:${port}`}`);
		if (isLimraApiPath(requestUrl.pathname)) {
			await proxyApi(req, res, requestUrl);
			return;
		}
		if (requestUrl.pathname.startsWith('/api/') || requestUrl.pathname.startsWith('/mirothinker/')) {
			rejectPrivateApi(res);
			return;
		}
		serveStatic(requestUrl.pathname, res);
	} catch (error) {
		console.error(error);
		if (!res.headersSent) {
			res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' });
		}
		res.end('Standalone frontend error');
	}
});

server.listen(port, host, () => {
	console.log(`limra standalone frontend listening on http://${host}:${port}`);
	console.log(`proxying /api/limra/* to ${backendUrl}`);
});

function isLimraApiPath(pathname) {
	return pathname === '/api/limra' || pathname.startsWith('/api/limra/');
}

function rejectPrivateApi(res) {
	res.writeHead(404, {
		'content-type': 'application/json; charset=utf-8',
		'cache-control': 'no-store'
	});
	res.end(JSON.stringify({ detail: 'not_found' }));
}

async function proxyApi(req, res, requestUrl) {
	const target = `${backendUrl}${requestUrl.pathname}${requestUrl.search}`;
	const headers = { ...req.headers };
	delete headers.host;
	delete headers.connection;
	delete headers['content-length'];

	const body = await requestBody(req);
	const proxyResponse = await fetch(target, {
		method: req.method,
		headers,
		body,
		redirect: 'manual'
	});

	for (const [key, value] of proxyResponse.headers) {
		const lower = key.toLowerCase();
		if (lower === 'transfer-encoding' || lower === 'content-encoding' || lower === 'content-length') {
			continue;
		}
		res.setHeader(key, value);
	}
	res.writeHead(proxyResponse.status);

	if (proxyResponse.body) {
		Readable.fromWeb(proxyResponse.body).pipe(res);
	} else {
		res.end();
	}
}

function requestBody(req) {
	if (req.method === 'GET' || req.method === 'HEAD') {
		return undefined;
	}
	return new Promise((resolveBody, rejectBody) => {
		const chunks = [];
		req.on('data', (chunk) => chunks.push(chunk));
		req.on('end', () => resolveBody(Buffer.concat(chunks)));
		req.on('error', rejectBody);
	});
}

function serveStatic(pathname, res) {
	const decoded = decodeURIComponent(pathname);
	const requestedPath =
		decoded === '/' || decoded === '/limra'
			? indexPath
			: join(publicRoot, normalize(decoded).replace(/^(\.\.(\/|\\|$))+/, ''));
	if (!requestedPath.startsWith(publicRoot)) {
		res.writeHead(403);
		res.end();
		return;
	}

	let filePath = requestedPath;
	try {
		const stats = statSync(filePath);
		if (stats.isDirectory()) {
			filePath = indexPath;
		}
	} catch {
		filePath = indexPath;
	}

	const extension = extname(filePath);
	res.writeHead(200, {
		'content-type': mimeTypes.get(extension) || 'application/octet-stream',
		'cache-control': extension === '.html' ? 'no-store' : 'no-cache'
	});
	createReadStream(filePath).pipe(res);
}
