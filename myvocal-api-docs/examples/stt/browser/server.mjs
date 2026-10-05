#!/usr/bin/env node
/**
 * MyVocal real-time Speech to Text — same-origin backend for the browser example.
 *
 * Node.js 22+, no dependencies. The API key stays in this process (MYVOCAL_ACCESS_KEY). The page is
 * served from the same origin and can call only these fixed routes:
 *
 *   POST /api/sessions               create a session for the page's real input encoding
 *   POST /api/sessions/{id}/ticket   one single-use socket ticket (the page connects to MyVocal itself)
 *   GET  /api/sessions/{id}          read the session
 *   POST /api/sessions/{id}/finish   finish with the page's capturedSamples (idempotent)
 *
 * Only sessions created by this process are accepted, so it is not a general proxy for the account.
 * No CORS headers are sent: other origins cannot read these responses, and a JSON content type plus
 * a same-origin check keep other sites from triggering the routes.
 *
 *   MYVOCAL_ACCESS_KEY=...                         required
 *   MYVOCAL_API_BASE=https://api.myvocal.ai     optional (default shown)
 *   PORT=8787 HOST=127.0.0.1                    optional
 */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const KEY = process.env.MYVOCAL_ACCESS_KEY;
const API_BASE = (process.env.MYVOCAL_API_BASE || 'https://api.myvocal.ai').replace(/\/$/, '');
const HOST = process.env.HOST || '127.0.0.1';
const PORT = Number(process.env.PORT || 8787);
const RT = '/sound_clone/api/v1/stt/realtime';
const HERE = dirname(fileURLToPath(import.meta.url));
if (!KEY) {
  console.error('set MYVOCAL_ACCESS_KEY');
  process.exit(2);
}

const ENCODINGS = new Set(['pcm_s16le_8000', 'pcm_s16le_16000', 'pcm_s16le_22050', 'pcm_s16le_24000',
  'pcm_s16le_44100', 'pcm_s16le_48000']);
const SESSION_ID = /^rts_[A-Za-z0-9]{1,64}$/;
const REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const DECIMAL = /^(0|[1-9][0-9]{0,15})$/;
const LANGUAGE = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$/;
const owned = new Set();
const OWNED_LIMIT = 1000;
const STATIC = {
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/app.js': ['app.js', 'text/javascript; charset=utf-8'],
  '/capture-worklet.js': ['capture-worklet.js', 'text/javascript; charset=utf-8'],
};

/** Calls one MyVocal route with the key and returns the HTTP status and the envelope. */
async function myvocal(method, path, body, idempotencyKey, timeoutMs) {
  const headers = { accessKey: KEY };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
  const response = await fetch(API_BASE + RT + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  return { status: response.status, envelope: await response.json() };
}

function send(res, status, value) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(value));
}

/** Passes the MyVocal result on: `data` on success, the MyVocal error code otherwise. */
function relay(res, { status, envelope }) {
  if (envelope && envelope.code === 1) return send(res, 200, envelope.data);
  const error = (envelope && envelope.data) || {};
  send(res, status >= 400 ? status : 502, { errorCode: error.errorCode || 'UPSTREAM_ERROR',
    retryable: Boolean(error.retryable) });
}

/** The small JSON body, or null when it is too large or not a JSON object. */
async function readJson(req) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 4096) return null;
    chunks.push(chunk);
  }
  try {
    const value = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {};
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function sameOrigin(req) {
  const origin = req.headers.origin;
  return !origin || origin === 'http://' + req.headers.host || origin === 'https://' + req.headers.host;
}

/** Turns MyVocal's relative stream path into the public socket URL the browser connects to. */
function socketUrl(path) {
  const url = new URL(API_BASE + path);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

async function route(req, res) {
  const url = new URL(req.url, 'http://local');
  if (req.method === 'GET' && STATIC[url.pathname]) {
    const [file, type] = STATIC[url.pathname];
    res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store',
      'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' "
        + new URL(socketUrl('/')).origin });
    return res.end(await readFile(join(HERE, file)));
  }
  if (!url.pathname.startsWith('/api/')) return send(res, 404, { errorCode: 'NOT_FOUND' });
  if (!sameOrigin(req)) return send(res, 403, { errorCode: 'FORBIDDEN' });
  if (req.method === 'POST' && !String(req.headers['content-type'] || '').startsWith('application/json')) {
    return send(res, 415, { errorCode: 'INPUT_INVALID' });
  }

  if (req.method === 'POST' && url.pathname === '/api/sessions') {
    const body = await readJson(req);
    if (!body || !ENCODINGS.has(body.inputEncoding) || !REQUEST_ID.test(String(body.requestId || ''))
        || (body.languageHint != null && !LANGUAGE.test(String(body.languageHint)))) {
      return send(res, 400, { errorCode: 'INPUT_INVALID' });
    }
    const options = { inputEncoding: body.inputEncoding };
    const created = await myvocal('POST', '/sessions',
      { languageHint: body.languageHint || undefined, options }, body.requestId, 15_000);
    if (created.envelope && created.envelope.code === 1) {
      if (owned.size >= OWNED_LIMIT) owned.delete(owned.values().next().value);
      owned.add(created.envelope.data.sessionId);
    }
    return relay(res, created);
  }

  const match = /^\/api\/sessions\/([^/]+)(\/ticket|\/finish)?$/.exec(url.pathname);
  if (!match || !SESSION_ID.test(match[1]) || !owned.has(match[1])) return send(res, 404, { errorCode: 'NOT_FOUND' });
  const id = encodeURIComponent(match[1]);
  if (req.method === 'GET' && !match[2]) return relay(res, await myvocal('GET', '/sessions/' + id, undefined, null, 15_000));
  if (req.method === 'POST' && match[2] === '/ticket') {
    const issued = await myvocal('POST', '/sessions/' + id + '/tickets', undefined, null, 15_000);
    if (!issued.envelope || issued.envelope.code !== 1) return relay(res, issued);
    const ticket = issued.envelope.data;
    return send(res, 200, { streamUrl: socketUrl(ticket.streamUrl), ticket: ticket.ticket,
      expiresInSeconds: ticket.expiresInSeconds });
  }
  if (req.method === 'POST' && match[2] === '/finish') {
    const body = await readJson(req);
    if (!body) return send(res, 400, { errorCode: 'INPUT_INVALID' });
    const captured = body.capturedSamples == null ? undefined : String(body.capturedSamples);
    if (captured !== undefined && !DECIMAL.test(captured)) return send(res, 400, { errorCode: 'INPUT_INVALID' });
    // Finish waits for the tail and any requested supplements, so it gets a longer bound.
    return relay(res, await myvocal('POST', '/sessions/' + id + '/finish',
      captured === undefined ? {} : { capturedSamples: captured }, null, 60_000));
  }
  return send(res, 405, { errorCode: 'METHOD_NOT_ALLOWED' });
}

createServer((req, res) => {
  route(req, res).catch((error) => {
    const timeout = error && (error.name === 'TimeoutError' || error.name === 'AbortError');
    send(res, timeout ? 504 : 502, { errorCode: timeout ? 'UPSTREAM_TIMEOUT' : 'UPSTREAM_UNREACHABLE', retryable: true });
  });
}).listen(PORT, HOST, () => {
  console.log(`open http://${HOST}:${PORT}/  (MyVocal API ${API_BASE})`);
});
