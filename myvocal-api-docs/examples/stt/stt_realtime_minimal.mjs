#!/usr/bin/env node
/**
 * MyVocal real-time Speech to Text — minimal Node.js client.
 *
 * Requires Node 18+ for global `fetch`, and Node 22+ for the global `WebSocket`
 * handler with a custom `headers` option (verified on Node v22). `node:crypto`
 * supplies the request id. Set the environment first:
 *
 *   MYVOCAL_API_BASE=http://127.0.0.1:8080        # local or https://api.myvocal.ai
 *   MYVOCAL_ACCESS_KEY=stt_live_...
 *   MYVOCAL_AUDIO=./sample-16k-mono-s16le.pcm
 *
 * The client creates a session, streams the raw PCM16 mono file, finishes, waits
 * for the terminal event, then reads the shared History view. It talks only to
 * MyVocal; no provider SDK is required.
 */
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

const BASE = (process.env.MYVOCAL_API_BASE || '').replace(/\/$/, '');
const KEY = process.env.MYVOCAL_ACCESS_KEY;
const AUDIO = process.env.MYVOCAL_AUDIO;
const RT = '/sound_clone/api/v1/stt/realtime';
if (!BASE || !KEY || !AUDIO) {
  console.error('set MYVOCAL_API_BASE, MYVOCAL_ACCESS_KEY and MYVOCAL_AUDIO');
  process.exit(2);
}

/** Builds the socket URL from the HTTP base and the returned path, for http or https. */
function socketUrl(base, path) {
  const url = new URL(base + path);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

async function call(method, path, body) {
  const response = await fetch(BASE + path, {
    method,
    headers: Object.assign({ accessKey: KEY, 'Content-Type': 'application/json' },
      body ? { 'Idempotency-Key': randomUUID() } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json();
  if (payload.code !== 1) throw new Error(`MyVocal error ${payload.code}: ${payload.message}`);
  return payload.data;
}

async function main() {
  const session = await call('POST', RT + '/sessions',
    { languageHint: 'en', options: { inputEncoding: 'pcm_s16le_16000' } });
  console.log('session', session.sessionId, 'transcription', session.transcriptionId);

  const socket = new WebSocket(socketUrl(BASE, session.streamUrl), { headers: { accessKey: KEY } });
  const events = [];
  socket.addEventListener('message', (message) => events.push(JSON.parse(message.data)));
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve);
    socket.addEventListener('error', reject);
  });

  const raw = readFileSync(AUDIO);
  const frameBytes = 32000; // one second of 16 kHz PCM16
  let offset = 0;
  for (let start = 0; start < raw.length; start += frameBytes) {
    const chunk = raw.subarray(start, start + frameBytes);
    socket.send(JSON.stringify({
      eventType: 'audio.append',
      epoch: 1,
      payload: {
        audioBase64: chunk.toString('base64'),
        sampleOffset: offset,
        capturedSamples: offset + chunk.length / 2,
      },
    }));
    offset += chunk.length / 2;
  }
  socket.send(JSON.stringify({ eventType: 'session.finish', epoch: 1, payload: { capturedSamples: offset } }));

  // Wait for the terminal event; the server does not close the socket for us.
  await new Promise((resolve, reject) => {
    const deadline = Date.now() + 30_000;
    const timer = setInterval(() => {
      if (events.some((event) => event.eventType === 'session.completed')) { clearInterval(timer); resolve(); }
      else if (events.some((event) => event.eventType === 'session.error')) {
        clearInterval(timer); reject(new Error('stream error ' + events.find((e) => e.eventType === 'session.error').payload.errorCode));
      } else if (Date.now() > deadline) { clearInterval(timer); reject(new Error('no terminal event')); }
    }, 50);
  });
  for (const event of events) {
    if (event.eventType === 'transcript.final') console.log('final:', event.payload.text);
    if (event.eventType === 'session.notice') console.log('notice:', event.payload.code);
  }
  socket.close();

  const view = await call('GET', RT + '/sessions/' + encodeURIComponent(session.sessionId));
  console.log('status', view.status, 'billable', view.usage.billableCharacters, 'Characters');
}

main().catch((error) => { console.error(error.message); process.exit(1); });
