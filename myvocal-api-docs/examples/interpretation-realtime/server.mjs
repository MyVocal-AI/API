import { createReadStream } from "node:fs";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import WebSocket from "ws";
import { api, findTerminal, serverKey, wsUrl } from "./api.mjs";

async function stream(round, file) {
  const socket = new WebSocket(wsUrl(round.streamUrl), { headers: { accessKey: serverKey() } });
  const counts = { original: 0, translated: 0, audioBytes: 0, acceptedSamples: 0 };
  let ready, terminal;
  const canSend = new Promise((resolve, reject) => { ready = { resolve, reject }; });
  const completed = new Promise((resolve, reject) => { terminal = { resolve, reject }; });
  let timeout = setTimeout(() => socket.terminate(), 25000);
  completed.catch(() => {});
  socket.on("error", () => socket.terminate());
  socket.on("close", () => {
    const interrupted = new Error("RTT_TRANSPORT_CLOSED");
    ready.reject(interrupted);
    terminal.reject(interrupted);
  });
  socket.on("message", raw => {
    const event = JSON.parse(raw);
    switch (event.type) {
      case "ready": ready.resolve(); break;
      case "final": counts.original++; break; // event.text is the original caption.
      case "translated": counts.translated++; break; // event.text is the translated caption.
      case "audio": counts.audioBytes += Buffer.from(event.data, "base64").length; break;
      case "input_ack": counts.acceptedSamples = event.acceptedSamples; break;
      case "done": terminal.resolve(event); break;
    }
  });
  try {
    await canSend;
    clearTimeout(timeout);
    let sequence = 0;
    for await (const chunk of createReadStream(file, { highWaterMark: 3200 })) {
      if (socket.readyState !== WebSocket.OPEN) break;
      if (chunk.length % 2 !== 0 || (sequence === 0 && chunk.subarray(0, 4).toString() === "RIFF")) {
        throw new Error("RTT_SAMPLE_PCM_INVALID");
      }
      socket.send(JSON.stringify({ type: "audio", sequence: sequence++, data: chunk.toString("base64") }));
      await delay(chunk.length / 32);
    }
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "finish" }));
    timeout = setTimeout(() => socket.terminate(), 65000);
    return { result: await completed, counts };
  } finally {
    clearTimeout(timeout);
    socket.close();
  }
}

async function main() {
  const [file, targetLanguage = "es"] = process.argv.slice(2);
  if (!file) throw new Error("RTT_SAMPLE_PCM_REQUIRED");
  if (file === "--status") {
    if (!/^rts_[a-f0-9]{32}$/.test(targetLanguage)) throw new Error("RTT_SAMPLE_SESSION_INVALID");
    console.log(JSON.stringify(await api("GET", "/sessions/" + targetLanguage)));
    return;
  }
  const session = await api("POST", "/sessions", {}, randomUUID());
  const round = await api("POST", "/sessions/" + session.sessionId + "/rounds",
    { targetLanguage, audioEnabled: true }, randomUUID());
  let result;
  try {
    try {
      result = await stream(round, file);
    } catch (transport) {
      // A missing WS response can conceal a completed, billed round.
      try {
        result = { result: await findTerminal(session.sessionId, round.roundId), transport: "RTT_TRANSPORT_CLOSED" };
      } catch {
        console.error(JSON.stringify({ code: "RTT_TERMINAL_UNKNOWN", queryRequired: true, sessionId: session.sessionId, roundId: round.roundId }));
        throw new Error("RTT_TERMINAL_UNKNOWN");
      }
    }
    console.log(JSON.stringify(result));
  } finally {
    await api("POST", "/sessions/" + session.sessionId + "/end");
  }
  if ((result.result.state ?? result.result.round?.state) !== "SETTLED") process.exitCode = 1;
}

main().catch(error => {
  console.error(/^RTT_[A-Z0-9_]+$/.test(error.message) ? error.message : "RTT_SAMPLE_FAILED");
  process.exitCode = 1;
});
