#!/usr/bin/env node
/**
 * MyVocal Text-to-Music quickstart (Node.js >= 18, no dependencies).
 *
 * Flow: capabilities -> create project -> poll arrangement -> quote -> generate
 *       -> poll song -> playback URL -> download MP3
 *
 * Environment:
 *   MYVOCAL_API_KEY           required
 *   MYVOCAL_API_BASE_URL      optional, defaults to https://api.myvocal.ai
 *   MYVOCAL_OUTPUT_DIR        optional, defaults to the current directory
 *   MYVOCAL_MAX_WAIT_SECONDS  optional, defaults to 900
 *   MYVOCAL_DURATION_SEC      optional, defaults to 90
 *
 * Safety: running this against the production host performs REAL, BILLABLE work.
 * Use the local stub described in README.md for development.
 *
 * Characters are serialized as JSON strings and can exceed Number.MAX_SAFE_INTEGER,
 * so every amount is converted with BigInt before it is compared.
 */

import { randomBytes, randomInt } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const MUSIC_PATH = "/sound_clone/api/v1/music";

class ApiError extends Error {
  constructor(code, message, details, httpStatus) {
    super(`MyVocal error code=${code} message=${message}`);
    this.code = code;
    this.message = message;
    this.details = details ?? null;
    this.httpStatus = httpStatus;
  }
}

class Config {
  constructor() {
    this.baseUrl = (process.env.MYVOCAL_API_BASE_URL ?? "https://api.myvocal.ai").replace(/\/+$/, "");
    this.apiKey = process.env.MYVOCAL_API_KEY ?? "";
    this.outputDir = process.env.MYVOCAL_OUTPUT_DIR ?? ".";
    this.maxWaitSeconds = Number(process.env.MYVOCAL_MAX_WAIT_SECONDS ?? "900");
    this.durationSec = Number(process.env.MYVOCAL_DURATION_SEC ?? "90");
    if (!this.apiKey) {
      console.error("MYVOCAL_API_KEY is required");
      process.exit(1);
    }
    mkdirSync(this.outputDir, { recursive: true });
  }

  get statePath() {
    return join(this.outputDir, "music_state.json");
  }
}

/** 16-64 printable ASCII characters, unique per operation. */
function newIdempotencyKey() {
  return randomBytes(24).toString("hex").slice(0, 32);
}

/** Characters arrive as strings: parse with BigInt before comparing. */
function asBigInt(value) {
  if (value === null || value === undefined) return null;
  return BigInt(value);
}

function isJson(contentType) {
  return (contentType ?? "").toLowerCase().includes("json");
}

class Client {
  constructor(config) {
    this.config = config;
  }

  /** One request. Checks the HTTP status *and* the JSON business code. */
  async call(method, path, body, idempotencyKey) {
    const headers = { accessKey: this.config.apiKey, Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

    const response = await fetch(this.config.baseUrl + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const contentType = response.headers.get("content-type");
    const raw = await response.text();

    if (!isJson(contentType)) {
      throw new ApiError(-1, `expected JSON but received ${contentType ?? "no content type"} (HTTP ${response.status})`, null, response.status);
    }
    const payload = JSON.parse(raw);
    if (payload.code !== 1) {
      // HTTP 200 with code 401 is the documented authentication failure.
      throw new ApiError(payload.code, payload.message, payload.data, response.status);
    }
    return payload.data ?? {};
  }
}

/** Honour a server hint when present; otherwise use a bounded jittered wait. */
function jitteredDelay(nextPollAfterMs) {
  if (nextPollAfterMs !== null && nextPollAfterMs !== undefined) {
    return Math.max(500, Number(nextPollAfterMs));
  }
  return 2000 + randomInt(0, 3000);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Bounded polling loop shared by the arrangement and the song stages. */
async function poll(description, fetchValue, done, deadline) {
  for (;;) {
    const value = await fetchValue();
    const state = done(value);
    if (state) {
      console.log(`  ${description} -> ${state}`);
      return value;
    }
    if (Date.now() > deadline) {
      throw new Error(
        `${description} did not finish within MYVOCAL_MAX_WAIT_SECONDS; keep the returned ids and poll again later`,
      );
    }
    await sleep(jitteredDelay(null));
  }
}

function loadState(config) {
  return existsSync(config.statePath) ? JSON.parse(readFileSync(config.statePath, "utf8")) : {};
}

/** Persist only ids and idempotency keys — never keys, URLs or bodies. */
function saveState(config, state) {
  writeFileSync(config.statePath, JSON.stringify(state, null, 2));
}

function logBilling(quote) {
  const quoted = asBigInt(quote.quotedCharacters);
  const total = asBigInt(quote.balances?.total ?? null);
  console.log(`  quotedCharacters = ${quoted}`);
  if (total !== null) console.log(`  balance total    = ${total}`);
  console.log(`  affordable       = ${quote.affordable} (shortfall ${asBigInt(quote.shortfall)})`);
  if (total !== null && quoted !== null) {
    // Exact 64-bit comparison: Number would lose precision above 2^53.
    console.log(`  balance >= quote : ${total >= quoted} (exact BigInt comparison)`);
  }
  if (total !== null && quoted !== null && total < quoted) {
    console.log("  NOTE: balance is below the quote; generation would fail with code 47005");
  }
}

/** Download endpoint: success is audio bytes, failure is a JSON envelope. */
async function downloadAudio(client, path, destination) {
  const response = await fetch(client.config.baseUrl + path, {
    method: "GET",
    headers: { accessKey: client.config.apiKey, Accept: "audio/mpeg" },
  });
  const contentType = response.headers.get("content-type") ?? "";
  const buffer = Buffer.from(await response.arrayBuffer());

  if (isJson(contentType)) {
    const payload = JSON.parse(buffer.toString("utf8"));
    throw new ApiError(payload.code, payload.message, payload.data, response.status);
  }
  if (!contentType.toLowerCase().startsWith("audio/")) {
    throw new ApiError(-1, `refusing to save non-audio response (${contentType})`, null, response.status);
  }
  if (buffer.length === 0) throw new ApiError(-1, "empty audio body", null, response.status);
  writeFileSync(destination, buffer);
}

async function main() {
  const config = new Config();
  const client = new Client(config);
  const deadline = Date.now() + config.maxWaitSeconds * 1000;
  const state = loadState(config);

  if (state.projectId) console.log(`[resume] continuing project ${state.projectId} from the saved state file`);

  console.log("[1/7] capabilities");
  const capabilities = await client.call("GET", `${MUSIC_PATH}/capabilities`);
  console.log(`  plan=${capabilities.planKey} ratePerMinute=${capabilities.ratePerMinute}`);

  const languages = capabilities.supportedVocalLanguages ?? [];
  const vocalLanguage = languages[0]?.code ?? "en";
  const durations = capabilities.supportedDurationsSec ?? [];
  const duration = durations.includes(config.durationSec) ? config.durationSec : durations[0] ?? 90;

  if (!state.projectId) {
    console.log("[2/7] create project");
    state.createKey ??= newIdempotencyKey();
    const brief = {
      description: "An upbeat summer pop song about a road trip along the coast.",
      genre: "POP",
      styleNotes: "Bright synths, driving drums, warm bass.",
      moods: ["UPLIFTING", "ENERGETIC"],
      vocalLanguage,
      durationSec: duration,
      lyricsMode: "AUTO",
      vocalStyle: "BRIGHT_ENERGETIC",
    };
    const created = await client.call("POST", `${MUSIC_PATH}/projects`, brief, state.createKey);
    state.projectId = created.projectId;
    saveState(config, state);
    console.log(`  projectId=${state.projectId} status=${created.projectStatus}`);
  }

  const projectId = state.projectId;

  console.log("[3/7] poll until the arrangement is ready");
  const detail = await poll(
    "projectStatus",
    () => client.call("GET", `${MUSIC_PATH}/projects/${projectId}`),
    // On a resume the arrangement may already be done, so accept the later stages too.
    (value) => (["ARRANGEMENT_READY", "GENERATING", "READY"].includes(value.projectStatus) ? value.projectStatus : null),
    deadline,
  );

  console.log("[4/7] quote");
  const quote = await client.call("POST", `${MUSIC_PATH}/projects/${projectId}/quotes`, {
    arrangementVersion: detail.arrangementVersion,
  });
  logBilling(quote);
  state.quoteId = quote.quoteId;
  saveState(config, state);

  console.log("[5/7] generate");
  state.generateKey ??= newIdempotencyKey();
  const generation = await client.call(
    "POST",
    `${MUSIC_PATH}/projects/${projectId}/generations`,
    { quoteId: state.quoteId },
    state.generateKey,
  );
  state.jobId = generation.jobId;
  saveState(config, state);
  console.log(`  jobId=${state.jobId} reservedCharacters=${generation.reservedCharacters}`);

  console.log("[6/7] poll until the song is ready");
  const ready = await poll(
    "projectStatus",
    () => client.call("GET", `${MUSIC_PATH}/projects/${projectId}`),
    (value) => (["READY", "DELETED"].includes(value.projectStatus) ? value.projectStatus : null),
    deadline,
  );
  console.log(`  assetId=${ready.readyAsset?.assetId} durationMillis=${ready.readyAsset?.durationMillis}`);

  console.log("[7/7] playback URL and download");
  const playback = await client.call("GET", `${MUSIC_PATH}/projects/${projectId}/playback-url`);
  console.log(`  playback url expires at ${playback.expiresAt}`);

  const mp3Path = join(config.outputDir, "music_output.mp3");
  await downloadAudio(client, `${MUSIC_PATH}/projects/${projectId}/download`, mp3Path);
  console.log(`wrote ${mp3Path}`);
  console.log("OK: Text-to-Music quickstart completed");
}

main().catch((error) => {
  if (error instanceof ApiError) {
    console.error(`FAILED: ${error.message}`);
    if (error.code === 401) console.error("The accessKey was rejected; check MYVOCAL_API_KEY.");
    process.exit(1);
  }
  console.error(`TIMEOUT: ${error.message}`);
  process.exit(2);
});
