import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { api, apiOrigin, wsUrl } from "./api.mjs";

apiOrigin();
const port = Number(process.env.RTT_SAMPLE_PORT || 18806);
const origin = "http://127.0.0.1:" + port;
const routes = {
  "/browser.mjs": new URL("./browser.mjs", import.meta.url),
  "/capture-worklet.mjs": new URL("./capture-worklet.mjs", import.meta.url)
};
const uuid = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const sessionId = /^rts_[a-f0-9]{32}$/;

async function body(request) {
  let value = "";
  for await (const part of request) {
    value += part;
    if (value.length > 4096) throw new Error("RTT_SAMPLE_INPUT_INVALID");
  }
  return JSON.parse(value);
}

function reply(response, status, data) {
  response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify(data));
}

createServer(async (request, response) => {
  try {
    if (request.method === "GET" && request.url === "/") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end('<!doctype html><html><body></body></html>');
      return;
    }
    if (request.method === "GET" && Object.hasOwn(routes, request.url)) {
      response.writeHead(200, { "Content-Type": "text/javascript; charset=utf-8" });
      response.end(await readFile(routes[request.url]));
      return;
    }
    // Local sample only. In an app, bind each session and ticket request to the app's own signed-in user.
    if (request.method !== "POST" || request.headers.origin !== origin ||
        request.headers["content-type"] !== "application/json") {
      reply(response, 403, { code: "RTT_SAMPLE_ORIGIN_INVALID" });
      return;
    }
    const input = await body(request);
    if (request.url === "/sample/round") {
      if (!uuid.test(input.sessionKey) || !uuid.test(input.roundKey)) throw new Error("RTT_SAMPLE_INPUT_INVALID");
      const session = await api("POST", "/sessions", {}, input.sessionKey);
      const round = await api("POST", "/sessions/" + session.sessionId + "/rounds", input.options, input.roundKey);
      const ticket = await api("POST", "/sessions/" + session.sessionId + "/rounds/" + round.roundId + "/tickets");
      reply(response, 200, { sessionId: session.sessionId, roundId: round.roundId,
        url: wsUrl(ticket.streamUrl), ticket: ticket.ticket });
      return;
    }
    if (!sessionId.test(input.sessionId)) throw new Error("RTT_SAMPLE_INPUT_INVALID");
    const path = "/sessions/" + input.sessionId;
    if (request.url === "/sample/status") reply(response, 200, await api("GET", path));
    else if (request.url === "/sample/end") reply(response, 200, await api("POST", path + "/end"));
    else reply(response, 404, { code: "RTT_SAMPLE_NOT_FOUND" });
  } catch (error) {
    reply(response, 502, { code: /^RTT_[A-Z0-9_]+$/.test(error.message) ? error.message : "RTT_SAMPLE_FAILED" });
  }
}).listen(port, "127.0.0.1");
