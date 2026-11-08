const base = "/sound_clone/api/v1/interpretation/realtime";
const origin = process.env.MYVOCAL_RTT_ORIGIN;
const key = process.env.MYVOCAL_ACCESS_KEY;

export function apiOrigin() {
  const url = new URL(origin);
  if (!key || (url.protocol !== "https:" && !(url.protocol === "http:" && url.hostname === "127.0.0.1"))) {
    throw new Error("RTT_SAMPLE_CONFIGURATION");
  }
  return url.origin;
}

export async function api(method, route, body, idempotencyKey) {
  const headers = { accessKey: key };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(apiOrigin() + base + route, {
    method, headers, redirect: "error", signal: AbortSignal.timeout(10000),
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const envelope = await response.json();
  if (!response.ok || envelope.code !== 1) {
    throw new Error(envelope.data?.code ?? (envelope.code === 401 ? "RTT_UNAUTHENTICATED" : "RTT_HTTP_ERROR"));
  }
  return envelope.data;
}

export function wsUrl(path) {
  const origin = apiOrigin();
  const url = new URL(path, origin);
  if (url.origin !== origin || !url.pathname.startsWith(base + "/rounds/") || url.search || url.hash) {
    throw new Error("RTT_STREAM_PATH_INVALID");
  }
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}

export function serverKey() { return key; }

export async function findTerminal(sessionId, roundId) {
  for (let attempt = 0; attempt < 12; attempt++) {
    const session = await api("GET", "/sessions/" + sessionId);
    const round = session.rounds.find(entry => entry.roundId === roundId);
    if (round?.state === "SETTLED" || round?.state === "RELEASED") return { round, usage: session.usage };
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error("RTT_TERMINAL_UNKNOWN");
}
