// MyVocal real-time Speech to Text — browser page for server.mjs.
//
// The page never sees the API key. It asks its own backend (same origin) for a session and a
// single-use ticket, then streams ~100 ms PCM frames straight to MyVocal's public socket.
// On stop it waits for the audio worklet to hand over the tail, sends everything, finishes with the
// real capturedSamples and shows the server's outcome. If the socket fails it releases the
// microphone and finishes through the backend instead; it never reports success it did not receive.

const SUPPORTED_RATES = [8000, 16000, 22050, 24000, 44100, 48000];
const TERMINAL = new Set(['COMPLETED', 'PARTIAL', 'FAILED']);
const MAX_BUFFERED_BYTES = 512 * 1024;   // socket send buffer we let build up before pausing sends
const MAX_QUEUED_SECONDS = 30;           // audio we hold while the network is slow, before giving up
const READY_TIMEOUT_MS = 10_000;
const TAIL_TIMEOUT_MS = 2_000;
const DRAIN_TIMEOUT_MS = 20_000;
const COMPLETE_TIMEOUT_MS = 45_000;
const ACK_TIMEOUT_MS = 10_000;           // a sent frame not acknowledged by then means the connection stalled
const RECOVERY_KEY = 'myvocal-stt-unfinished';

const $ = (id) => document.getElementById(id);
const ui = {
  start: $('start'), stop: $('stop'), status: $('status'), lines: $('lines'), partial: $('partial'),
  details: $('details'), recovery: $('recovery'), recoveryText: $('recoveryText'), retry: $('retry'),
};

let run = null;

function setStatus(text) { ui.status.textContent = text; }
function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

function withTimeout(promise, ms, code) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(code)), ms);
  })]).finally(() => clearTimeout(timer));
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.errorCode || 'HTTP_' + response.status);
    error.retryable = Boolean(data.retryable) || response.status >= 500;
    throw error;
  }
  return data;
}

function base64(buffer) {
  const bytes = new Uint8Array(buffer);
  let text = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(text);
}

function renderLines(lines) {
  ui.lines.replaceChildren(...[...lines.values()].map((line) => {
    const p = document.createElement('p');
    p.textContent = line.revisedText || line.text;
    return p;
  }));
}

function renderDetails() {
  if (!run) return;
  ui.details.textContent = `session ${run.sessionId || '-'} · ${run.rate || '-'} Hz · captured ${run.captured}`
    + ` · epoch ${run.epoch} · sent in epoch ${run.sentOffset} · acknowledged ${run.acked}`
    + (run.notices.length ? '\nnotices: ' + run.notices.join(', ') : '');
}

async function start() {
  ui.start.disabled = true;
  ui.recovery.hidden = true;
  ui.lines.replaceChildren();
  ui.partial.textContent = '';
  run = {
    sessionId: null, ws: null, stream: null, context: null, node: null, rate: 0,
    captured: 0, epoch: 1, sentOffset: 0, acked: 0, pending: [], pendingSamples: 0, inflight: [],
    rotating: false, finishing: false, closing: false, failed: null, notices: [], lines: new Map(),
    ready: null, completed: null, tail: null,
  };
  const current = run;
  try {
    setStatus('Opening the microphone…');
    current.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    if (current !== run) return releaseCapture(current);
    current.context = new AudioContext();
    if (!SUPPORTED_RATES.includes(current.context.sampleRate)) {
      await current.context.close();
      current.context = new AudioContext({ sampleRate: 16000 });
    }
    current.rate = current.context.sampleRate;
    await current.context.audioWorklet.addModule('/capture-worklet.js');

    setStatus('Creating the session…');
    const session = await api('POST', '/api/sessions', {
      inputEncoding: 'pcm_s16le_' + current.rate, requestId: crypto.randomUUID(),
    });
    current.sessionId = session.sessionId;
    const ticket = await api('POST', '/api/sessions/' + encodeURIComponent(current.sessionId) + '/ticket', {});
    const url = new URL(ticket.streamUrl);
    url.searchParams.set('ticket', ticket.ticket);

    current.ready = deferred();
    current.completed = deferred();
    current.ws = new WebSocket(url);
    current.ws.onmessage = (event) => onEvent(current, JSON.parse(event.data));
    current.ws.onclose = () => fail(current, 'SOCKET_CLOSED');
    current.ws.onerror = () => fail(current, 'SOCKET_ERROR');
    await withTimeout(current.ready.promise, READY_TIMEOUT_MS, 'READY_TIMEOUT');
    if (current.failed) return;

    const source = current.context.createMediaStreamSource(current.stream);
    current.node = new AudioWorkletNode(current.context, 'myvocal-capture', { numberOfOutputs: 1 });
    current.tail = deferred();
    current.node.port.onmessage = (event) => onCapture(current, event.data);
    source.connect(current.node);
    current.node.connect(current.context.destination); // silent output keeps the worklet running
    // A socket can stay "open" while nothing gets through (for example the network went away).
    current.watchdog = setInterval(() => {
      const oldest = current.inflight[0];
      if (oldest && !current.rotating && Date.now() - oldest.sentAt > ACK_TIMEOUT_MS) fail(current, 'NO_ACKNOWLEDGEMENT');
    }, 1000);
    setStatus('Recording — speak now');
    ui.stop.disabled = false;
  } catch (error) {
    fail(current, error.message || 'START_FAILED');
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, no) => { resolve = ok; reject = no; });
  promise.catch(() => {});
  return { promise, resolve, reject };
}

function onCapture(current, message) {
  if (message.type === 'frame') {
    current.captured += message.samples;
    current.pending.push({ pcm: message.pcm, samples: message.samples });
    current.pendingSamples += message.samples;
    if (current.pendingSamples > MAX_QUEUED_SECONDS * current.rate) {
      fail(current, 'SEND_BUFFER_FULL');
      return;
    }
    pump(current);
  } else if (message.type === 'stopped') {
    current.tail.resolve(message.totalSamples);
  }
}

/** Sends queued frames while the socket accepts audio and its send buffer stays bounded. */
function pump(current) {
  const ws = current.ws;
  if (!ws || ws.readyState !== WebSocket.OPEN || current.rotating || current.failed) return;
  while (current.pending.length && ws.bufferedAmount < MAX_BUFFERED_BYTES) {
    const frame = current.pending.shift();
    current.pendingSamples -= frame.samples;
    ws.send(JSON.stringify({ eventType: 'audio.append', epoch: current.epoch, payload: {
      audioBase64: base64(frame.pcm), sampleOffset: current.sentOffset, capturedSamples: String(current.captured),
    } }));
    current.inflight.push({ start: current.sentOffset, pcm: frame.pcm, samples: frame.samples, sentAt: Date.now() });
    current.sentOffset += frame.samples;
  }
  if (current.pending.length) setTimeout(() => pump(current), 50);
  renderDetails();
}

function onEvent(current, event) {
  const p = event.payload || {};
  switch (event.eventType) {
    case 'session.ready':
      current.epoch = p.epoch || current.epoch;
      current.ready.resolve(p);
      break;
    case 'usage.updated':
      onUsage(current, p);
      break;
    case 'transcript.delta':
      ui.partial.textContent = p.text || '';
      break;
    case 'transcript.final':
      current.lines.set(p.id, { text: p.text, revisedText: p.revisedText });
      ui.partial.textContent = '';
      renderLines(current.lines);
      break;
    case 'transcript.revision':
      if (current.lines.has(p.id)) current.lines.get(p.id).revisedText = p.revisedText;
      renderLines(current.lines);
      break;
    case 'session.notice':
      current.notices.push(p.code);
      renderDetails();
      break;
    case 'session.completed':
      current.completed.resolve(p);
      break;
    case 'session.error':
      fail(current, p.errorCode || 'SESSION_ERROR');
      break;
    default:
      break;
  }
}

function onUsage(current, p) {
  if (p.epoch === current.epoch && p.sentSamples != null) {
    current.acked = Math.max(current.acked, Number(p.sentSamples));
    current.inflight = current.inflight.filter((frame) => frame.start + frame.samples > current.acked);
  }
  if (p.rotate && !current.rotating) {
    // The server wants a new segment. The frame that got this answer was not stored: close the epoch,
    // open the next one and send the unacknowledged frames again from offset 0.
    current.rotating = true;
    const captured = String(current.captured);
    current.ws.send(JSON.stringify({ eventType: 'session.pause', epoch: current.epoch, payload: { capturedSamples: captured } }));
    current.ws.send(JSON.stringify({ eventType: 'session.resume', epoch: current.epoch, payload: { capturedSamples: captured } }));
  } else if (current.rotating && p.epoch > current.epoch && p.accepting) {
    const resend = current.inflight.map(({ pcm, samples }) => ({ pcm, samples }));
    current.pending.unshift(...resend);
    current.pendingSamples += resend.reduce((sum, frame) => sum + frame.samples, 0);
    current.epoch = p.epoch;
    current.sentOffset = 0;
    current.acked = 0;
    current.inflight = [];
    current.rotating = false;
    pump(current);
  } else if (p.accepting === false && !p.rotate && !current.rotating && !current.finishing) {
    fail(current, p.errorCode || 'NOT_ACCEPTING');
  }
  renderDetails();
}

async function stop() {
  const current = run;
  if (!current || current.finishing || current.failed) return;
  current.finishing = true;
  ui.stop.disabled = true;
  setStatus('Stopping — sending the last audio…');
  try {
    current.node.port.postMessage({ type: 'stop' });
    // The worklet posts the partial last frame before 'stopped', so captured now includes the tail.
    await withTimeout(current.tail.promise, TAIL_TIMEOUT_MS, 'TAIL_NOT_CONFIRMED');
    releaseCapture(current);
    await withTimeout(drained(current), DRAIN_TIMEOUT_MS, 'DRAIN_TIMEOUT');
    setStatus('Finishing — waiting for the final result…');
    current.ws.send(JSON.stringify({ eventType: 'session.finish', epoch: current.epoch,
      payload: { capturedSamples: String(current.captured) } }));
    const view = await withTimeout(current.completed.promise, COMPLETE_TIMEOUT_MS, 'FINISH_TIMEOUT');
    current.closing = true;
    current.ws.close();
    showOutcome(current, view);
  } catch (error) {
    fail(current, error.message || 'STOP_FAILED');
  }
}

async function drained(current) {
  while (current.pending.length || current.inflight.length || current.rotating) {
    if (current.failed || current.ws.readyState !== WebSocket.OPEN) throw new Error(current.failed || 'SOCKET_CLOSED');
    await sleep(50);
  }
}

function releaseCapture(current) {
  clearInterval(current.watchdog);
  if (current.stream) current.stream.getTracks().forEach((track) => track.stop());
  if (current.node) current.node.port.onmessage = null;
  if (current.context && current.context.state !== 'closed') current.context.close().catch(() => {});
  current.stream = null;
}

function showOutcome(current, view) {
  const status = view && view.status;
  (view.lines || []).forEach((line) => current.lines.set(line.id, { text: line.text, revisedText: line.revisedText }));
  renderLines(current.lines);
  ui.partial.textContent = '';
  (view.notices || []).forEach((notice) => { if (!current.notices.includes(notice.code)) current.notices.push(notice.code); });
  setStatus(TERMINAL.has(status) ? 'Session ' + status : 'Session status: ' + (status || 'unknown'));
  renderDetails();
  localStorage.removeItem(RECOVERY_KEY);
  ui.start.disabled = false;
}

/** Any failure: release the microphone, then settle the session through the backend. */
function fail(current, code) {
  if (current !== run || current.failed || current.closing) return;
  current.failed = code;
  current.closing = true;
  ui.stop.disabled = true;
  [current.ready, current.completed, current.tail].forEach((wait) => wait && wait.reject(new Error(code)));
  releaseCapture(current);
  if (current.ws && current.ws.readyState <= WebSocket.OPEN) current.ws.close();
  if (!current.sessionId) {
    setStatus('Could not start: ' + code);
    ui.start.disabled = false;
    return;
  }
  closeOut({ sessionId: current.sessionId, capturedSamples: String(current.captured), reason: code });
}

/** Bounded, idempotent finish through the backend; a repeated finish returns the same task. */
async function closeOut(unfinished) {
  setStatus(`Stopped (${unfinished.reason}). Finishing session through the backend…`);
  const path = '/api/sessions/' + encodeURIComponent(unfinished.sessionId);
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const view = await api('POST', path + '/finish', { capturedSamples: unfinished.capturedSamples });
      return showOutcome(run, view);
    } catch (error) {
      lastError = error;
      if (error.retryable === false) break;
      await sleep(2000 * (attempt + 1));
    }
  }
  try {
    const view = await api('GET', path);
    if (TERMINAL.has(view.status)) return showOutcome(run, view);
  } catch (ignored) {
    // reported below
  }
  localStorage.setItem(RECOVERY_KEY, JSON.stringify({ ...unfinished, savedAt: new Date().toISOString() }));
  showRecovery(unfinished, lastError ? lastError.message : 'UNKNOWN');
}

function showRecovery(unfinished, code) {
  setStatus('Not finished yet');
  ui.recoveryText.textContent = `Session ${unfinished.sessionId} has not been finished (${code}). Its audio is not `
    + 'reported as complete. Retry when the network is back; finishing again never charges twice.';
  ui.recovery.hidden = false;
  ui.start.disabled = false;
}

function savedSession() {
  try {
    return JSON.parse(localStorage.getItem(RECOVERY_KEY) || 'null');
  } catch {
    return null;
  }
}

function recoveryRun(saved) {
  return { sessionId: saved.sessionId, rate: null, captured: saved.capturedSamples, epoch: '-', sentOffset: '-',
    acked: '-', lines: new Map(), notices: [] };
}

ui.start.addEventListener('click', start);
ui.stop.addEventListener('click', stop);
ui.retry.addEventListener('click', () => {
  const saved = savedSession();
  if (!saved) return;
  ui.recovery.hidden = true;
  ui.start.disabled = true;
  run = recoveryRun(saved);
  closeOut({ ...saved, reason: 'RETRY' });
});

const pendingCloseOut = savedSession();
if (pendingCloseOut) {
  run = recoveryRun(pendingCloseOut);
  showRecovery(pendingCloseOut, 'PAGE_RELOADED');
}
