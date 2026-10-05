#!/usr/bin/env node
/**
 * MyVocal real-time Speech to Text — minimal Node.js client.
 *
 * Requires Node 22+ for global `fetch` and `WebSocket`. The socket authenticates
 * with a one-use ticket; the native WebSocket does not accept custom headers. `node:crypto`
 * supplies the request id. Set the environment first:
 *
 *   MYVOCAL_API_BASE=http://127.0.0.1:8080        # local or https://api.myvocal.ai
 *   MYVOCAL_ACCESS_KEY=stt_live_...
 *   MYVOCAL_AUDIO=./sample-16k-mono-s16le.pcm
 *
 * The client streams the raw PCM16 mono file the way a live source would: 100 ms frames, each sent
 * when its audio would have been captured, and never more than a bounded amount waiting in the
 * socket's send buffer. It follows a rotation request, finishes, waits (bounded) for the terminal
 * event and reads the session. If the socket fails, it finishes over REST instead; the result may be
 * PARTIAL. It talks only to MyVocal; no provider SDK is required.
 */
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

const BASE = (process.env.MYVOCAL_API_BASE || '').replace(/\/$/, '');
const KEY = process.env.MYVOCAL_ACCESS_KEY;
const AUDIO = process.env.MYVOCAL_AUDIO;
const RT = '/sound_clone/api/v1/stt/realtime';
const RATE = 16_000;
const FRAME_SAMPLES = RATE / 10;            // 100 ms
const MAX_BUFFERED_BYTES = 256 * 1024;      // pause sending while this much is still unsent locally
const COMPLETE_TIMEOUT_MS = 60_000;
if (!BASE || !KEY || !AUDIO) {
  console.error('set MYVOCAL_API_BASE, MYVOCAL_ACCESS_KEY and MYVOCAL_AUDIO');
  process.exit(2);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Builds the socket URL from the HTTP base and the returned path, for http or https. */
function socketUrl(base, path) {
  const url = new URL(base + path);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

async function call(method, path, body, idempotencyKey) {
  const response = await fetch(BASE + path, {
    method,
    headers: Object.assign({ accessKey: KEY, 'Content-Type': 'application/json' },
      idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}),
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(70_000),
  });
  const payload = await response.json();
  if (payload.code !== 1) throw new Error(`MyVocal error ${payload.code}: ${payload.data?.errorCode || payload.message}`);
  return payload.data;
}

async function main() {
  const raw = readFileSync(AUDIO);
  const total = Math.floor(raw.length / 2);
  // Keep this key and body for a retry of the same create.
  const session = await call('POST', RT + '/sessions',
    { languageHint: 'en', options: { inputEncoding: 'pcm_s16le_' + RATE } }, randomUUID());
  const id = encodeURIComponent(session.sessionId);
  console.log('session', session.sessionId, 'transcription', session.transcriptionId);

  const ticket = await call('POST', RT + '/sessions/' + id + '/tickets');
  const stream = new URL(socketUrl(BASE, ticket.streamUrl));
  stream.searchParams.set('ticket', ticket.ticket);
  const socket = new WebSocket(stream);

  const state = { epoch: 1, epochStart: 0, sent: 0, acked: 0, rotating: false, failure: null, completed: null };
  socket.addEventListener('message', (message) => {
    const event = JSON.parse(message.data);
    const p = event.payload || {};
    if (event.eventType === 'session.ready') state.epoch = p.epoch || state.epoch;
    if (event.eventType === 'transcript.final') console.log('final:', p.text);
    if (event.eventType === 'transcript.revision') console.log('revised:', p.revisedText);
    if (event.eventType === 'session.notice') console.log('notice:', p.code);
    if (event.eventType === 'session.completed') state.completed = p;
    if (event.eventType === 'session.error') state.failure = p.errorCode;
    if (event.eventType !== 'usage.updated') return;
    if (p.epoch === state.epoch && p.sentSamples != null) state.acked = Math.max(state.acked, Number(p.sentSamples));
    if (p.rotate && !state.rotating) {
      // The frame that got this answer was not stored: open a new epoch and continue after the last
      // acknowledged sample, starting again at sampleOffset 0.
      state.rotating = true;
      const captured = String(state.epochStart + state.sent);
      socket.send(JSON.stringify({ eventType: 'session.pause', epoch: state.epoch, payload: { capturedSamples: captured } }));
      socket.send(JSON.stringify({ eventType: 'session.resume', epoch: state.epoch, payload: { capturedSamples: captured } }));
    } else if (state.rotating && p.epoch > state.epoch && p.accepting) {
      state.epochStart += state.acked;
      Object.assign(state, { epoch: p.epoch, sent: 0, acked: 0, rotating: false });
    }
  });
  socket.addEventListener('close', () => { if (!state.completed) state.failure = state.failure || 'SOCKET_CLOSED'; });
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', () => reject(new Error('socket did not open')), { once: true });
  });

  const started = Date.now();
  let captured = 0;
  try {
    while (state.epochStart + state.sent < total) {
      if (state.failure) throw new Error(state.failure);
      const position = state.epochStart + state.sent;
      const end = Math.min(total, position + FRAME_SAMPLES);
      // Real-time pace: a frame leaves once its last sample would have been captured.
      const due = started + (end * 1000) / RATE - Date.now();
      if (state.rotating || socket.bufferedAmount > MAX_BUFFERED_BYTES || due > 0) {
        await sleep(state.rotating || socket.bufferedAmount > MAX_BUFFERED_BYTES ? 20 : Math.min(due, 100));
        continue;
      }
      captured = Math.max(captured, end);
      socket.send(JSON.stringify({ eventType: 'audio.append', epoch: state.epoch, payload: {
        audioBase64: raw.subarray(position * 2, end * 2).toString('base64'),
        sampleOffset: state.sent,
        capturedSamples: String(captured),
      } }));
      state.sent += end - position;
    }
    socket.send(JSON.stringify({ eventType: 'session.finish', epoch: state.epoch, payload: { capturedSamples: String(total) } }));
    const deadline = Date.now() + COMPLETE_TIMEOUT_MS;
    while (!state.completed) {
      if (state.failure) throw new Error(state.failure);
      if (Date.now() > deadline) throw new Error('FINISH_TIMEOUT');
      await sleep(50);
    }
    console.log('status', state.completed.status);
  } catch (error) {
    // A disconnect is not a finish. Settle what the server can confirm; repeating finish never
    // charges twice and returns the same task.
    console.error('stream stopped:', error.message, '- finishing over REST');
    const view = await call('POST', RT + '/sessions/' + id + '/finish', { capturedSamples: String(captured) });
    console.log('status', view.status);
    process.exitCode = 1;
  } finally {
    socket.close();
  }

  const view = await call('GET', RT + '/sessions/' + id);
  console.log('status', view.status, 'billable', view.usage.billableCharacters, 'Characters');
}

main().catch((error) => { console.error(error.message); process.exit(1); });
