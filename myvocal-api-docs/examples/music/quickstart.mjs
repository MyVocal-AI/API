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
 *   MYVOCAL_REQUEST_TIMEOUT   optional per-request timeout in seconds, defaults to 30
 *
 * Recovery: the idempotency key and the exact request body of each create/generate
 * call are written to the state file before the request is sent. A saved create key
 * stays bound to its original request body, so changed settings are reported instead
 * of being sent under an old key. A project that already reached READY skips
 * quote/generate; an in-flight generation is replayed with its original key and quote
 * instead of being re-priced.
 *
 * Exit codes: 0 = a media file was produced; 1 = API/domain error; 2 = bounded wait
 * expired. Characters are parsed with BigInt so values above 2^53 stay exact.
 */

import { randomBytes, randomInt } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const MUSIC_PATH = "/sound_clone/api/v1/music";

// MPEG audio frame tables (MPEG 1 / MPEG 2 / MPEG 2.5). A real MP3 is accepted only when the
// payload actually starts with a decodable frame (possibly after an ID3v2 tag), so a WAV file or
// arbitrary bytes that merely contain a 0xFF byte are not mistaken for MP3.
const MPEG_BITRATES = {
  "3,3": [32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
  "3,2": [32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
  "3,1": [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
  "2,3": [32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
  "2,2": [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
  "2,1": [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
};
const MPEG_SAMPLE_RATES = { 3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000] };

export function mpegFrameLength(payload, offset) {
  if (offset + 4 > payload.length) return null;
  const b0 = payload[offset];
  const b1 = payload[offset + 1];
  const b2 = payload[offset + 2];
  const b3 = payload[offset + 3];
  if (b0 !== 0xff || (b1 & 0xe0) !== 0xe0) return null;
  const version = (b1 >> 3) & 0x03; // 0 = MPEG 2.5, 1 = reserved, 2 = MPEG 2, 3 = MPEG 1
  const layer = (b1 >> 1) & 0x03; // 0 = reserved, 1 = Layer III, 2 = Layer II, 3 = Layer I
  if (version === 1 || layer === 0) return null;
  const bitrateIndex = (b2 >> 4) & 0x0f;
  const sampleIndex = (b2 >> 2) & 0x03;
  if (bitrateIndex === 0 || bitrateIndex === 15 || sampleIndex === 3 || (b3 & 0x03) === 2) return null;
  const bitrate = MPEG_BITRATES[`${version === 3 ? 3 : 2},${layer}`][bitrateIndex - 1];
  const sampleRate = MPEG_SAMPLE_RATES[version][sampleIndex];
  const padding = (b2 >> 1) & 0x01;
  if (layer === 3) return (Math.floor(12 * bitrate * 1000 / sampleRate) + padding) * 4;
  const coefficient = layer === 1 && version !== 3 ? 72 : 144;
  return Math.floor(coefficient * bitrate * 1000 / sampleRate) + padding;
}

function startsWithMpegFrame(payload, offset) {
  const length = mpegFrameLength(payload, offset);
  if (length === null || offset + length > payload.length) return false;
  const following = offset + length;
  if (following === payload.length) return true;
  return mpegFrameLength(payload, following) !== null;
}

function id3v2Length(header) {
  if (header.length < 10 || ![2, 3, 4].includes(header[3])) return null;
  const size = ((header[6] & 0x7f) << 21) | ((header[7] & 0x7f) << 14)
    | ((header[8] & 0x7f) << 7) | (header[9] & 0x7f);
  return 10 + size + ((header[5] & 0x10) ? 10 : 0);
}

export function looksLikeMpeg(payload) {
  if (startsWithMpegFrame(payload, 0)) return true;
  if (payload.length >= 10) {
    const magic = payload.subarray(0, 3).toString("latin1");
    if (magic === "ID3" || (payload[0] === 0 && payload[1] === 0 && payload[2] === 0)) {
      const tagLength = id3v2Length(payload.subarray(0, 10));
      if (tagLength !== null && startsWithMpegFrame(payload, tagLength)) return true;
    }
  }
  return false;
}

class ApiError extends Error {
  constructor(code, message, details, httpStatus) {
    super(`MyVocal error code=${code} message=${message}`);
    this.code = code;
    this.message = message;
    this.details = details ?? null;
    this.httpStatus = httpStatus;
  }
}

class TimeoutFailure extends Error {}

class Config {
  constructor() {
    this.baseUrl = (process.env.MYVOCAL_API_BASE_URL ?? "https://api.myvocal.ai").replace(/\/+$/, "");
    this.apiKey = process.env.MYVOCAL_API_KEY ?? "";
    this.outputDir = process.env.MYVOCAL_OUTPUT_DIR ?? ".";
    this.maxWaitSeconds = Number(process.env.MYVOCAL_MAX_WAIT_SECONDS ?? "900");
    this.durationSec = Number(process.env.MYVOCAL_DURATION_SEC ?? "90");
    this.requestTimeoutMs = Number(process.env.MYVOCAL_REQUEST_TIMEOUT ?? "30") * 1000;
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
const newIdempotencyKey = () => randomBytes(24).toString("hex").slice(0, 32);

/** Characters arrive as strings; parse with BigInt so values above 2^53 stay exact. */
const asBigInt = (value) => (value === null || value === undefined ? null : BigInt(value));

const isJson = (contentType) => (contentType ?? "").toLowerCase().includes("json");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class Client {
  constructor(config) {
    this.config = config;
  }

  /** One request with a finite timeout. Success needs BOTH a 2xx status and code == 1. */
  async #send(url, options) {
    try {
      return await fetch(url, { ...options, signal: AbortSignal.timeout(this.config.requestTimeoutMs) });
    } catch (failure) {
      throw new ApiError(-1, `request failed: ${failure.message}`, null, null);
    }
  }

  async call(method, path, body, idempotencyKey) {
    const headers = { accessKey: this.config.apiKey, Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

    const response = await this.#send(this.config.baseUrl + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const contentType = response.headers.get("content-type");
    const raw = await response.text();

    if (!(response.status >= 200 && response.status < 300)) {
      // A non-2xx status is a failure even when the body carries code == 1.
      throw new ApiError(-1, `HTTP ${response.status} for ${method} ${path}`, null, response.status);
    }
    if (!isJson(contentType)) {
      throw new ApiError(-1, `expected JSON but received ${contentType ?? "no content type"} (HTTP ${response.status})`, null, response.status);
    }
    const payload = JSON.parse(raw);
    if (payload.code !== 1) throw new ApiError(payload.code, payload.message, payload.data, response.status);
    return payload.data ?? {};
  }

  /**
   * The download endpoint streams audio on success and JSON on failure. An HTML/JSON
   * error page, an empty body or a non-MPEG payload is reported as a failure instead
   * of being written to an .mp3 file.
   */
  async downloadAudio(path) {
    const response = await this.#send(this.config.baseUrl + path, {
      method: "GET",
      headers: { accessKey: this.config.apiKey, Accept: "audio/mpeg" },
    });
    const contentType = (response.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
    const payload = Buffer.from(await response.arrayBuffer());

    if (!(response.status >= 200 && response.status < 300)) {
      throw new ApiError(-1, `HTTP ${response.status} for the audio download`, null, response.status);
    }
    if (isJson(contentType)) {
      const error = JSON.parse(payload.toString("utf8"));
      throw new ApiError(error.code, error.message, error.data, response.status);
    }
    if (contentType.startsWith("text/")) {
      throw new ApiError(-1, `refusing to store a ${contentType} response as audio`);
    }
    if (!contentType.startsWith("audio/")) {
      throw new ApiError(-1, `unexpected content type ${contentType} for the audio download`);
    }
    if (payload.length === 0) throw new ApiError(-1, "audio body was empty");
    if (!looksLikeMpeg(payload)) throw new ApiError(-1, "payload does not contain a valid MPEG audio frame");
    return { contentType, payload };
  }
}

/** `nextPollAfterMs` is currently null, so a bounded jittered wait is used. */
function jitteredDelay(nextPollAfterMs) {
  if (nextPollAfterMs !== null && nextPollAfterMs !== undefined) return Math.max(500, Number(nextPollAfterMs));
  return 2000 + randomInt(0, 3000);
}

const loadState = (config) => (existsSync(config.statePath) ? JSON.parse(readFileSync(config.statePath, "utf8")) : {});
const saveState = (config, state) => writeFileSync(config.statePath, JSON.stringify(state, null, 2));

async function pollDetail(client, projectId, accept, deadline, what) {
  for (;;) {
    const detail = await client.call("GET", `${MUSIC_PATH}/projects/${projectId}`);
    const state = accept(detail);
    if (state) {
      console.log(`  projectStatus -> ${state}`);
      return detail;
    }
    if (Date.now() > deadline) {
      throw new TimeoutFailure(`${what} did not finish within MYVOCAL_MAX_WAIT_SECONDS; keep projectId=${projectId} and poll again later`);
    }
    await sleep(jitteredDelay(null));
  }
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
    if (total < quoted) console.log("  NOTE: balance is below the quote; generation would fail with code 47005");
  }
}

async function main() {
  const config = new Config();
  const client = new Client(config);
  const deadline = Date.now() + config.maxWaitSeconds * 1000;
  const state = loadState(config);

  if (state.projectId) console.log(`[resume] continuing project ${state.projectId} from the saved state file`);

  console.log("[1/7] capabilities");
  const capabilities = await client.call("GET", `${MUSIC_PATH}/capabilities`);
  console.log(`  accessState=${capabilities.accessState} plan=${capabilities.planKey} ratePerMinute=${capabilities.ratePerMinute}`);
  if (capabilities.accessState !== "ENABLED") {
    console.error(`account accessState is ${capabilities.accessState}; Text-to-Music is not available`);
    process.exit(1);
  }

  const languages = capabilities.supportedVocalLanguages ?? [];
  const vocalLanguage = languages[0]?.code ?? "en";
  const durations = capabilities.supportedDurationsSec ?? [];
  const duration = durations.includes(config.durationSec) ? config.durationSec : durations[0] ?? 90;

  if (!state.projectId) {
    console.log("[2/7] create project");
    const requestBody = {
      description: "An upbeat summer pop song about a road trip along the coast.",
      genre: "POP",
      styleNotes: "Bright synths, driving drums, warm bass.",
      moods: ["UPLIFTING", "ENERGETIC"],
      vocalLanguage,
      durationSec: duration,
      lyricsMode: "AUTO",
      vocalStyle: "BRIGHT_ENERGETIC",
    };
    // Persist the key and the request binding BEFORE sending.
    let body = requestBody;
    if (state.createKey !== undefined && state.createBody !== undefined) {
      // The key is bound to the body it was sent with: a lost response is replayed with that
      // original body, never with settings changed since.
      if (JSON.stringify(state.createBody) !== JSON.stringify(requestBody)) {
        console.error(`the saved create request used durationSec=${state.createBody.durationSec}/vocalLanguage=${state.createBody.vocalLanguage} but this run would send durationSec=${requestBody.durationSec}/vocalLanguage=${requestBody.vocalLanguage}. Refusing to send the current settings with the saved Idempotency-Key; re-run with the original settings, or use a fresh MYVOCAL_OUTPUT_DIR for a different request (the saved state is kept).`);
        process.exit(1);
      }
      body = state.createBody;
    } else if (state.createKey !== undefined) {
      console.error("a create Idempotency-Key is saved without its original request body; it cannot be replayed safely, so the saved state is kept unchanged");
      process.exit(1);
    } else {
      state.createKey = newIdempotencyKey();
      state.createBody = requestBody;
      saveState(config, state);
    }
    const created = await client.call("POST", `${MUSIC_PATH}/projects`, body, state.createKey);
    state.projectId = created.projectId;
    saveState(config, state);
    console.log(`  projectId=${state.projectId} status=${created.projectStatus}`);
  } else if (state.createKey && !state.createReplayed) {
    const replayed = await client.call("POST", `${MUSIC_PATH}/projects`, state.createBody, state.createKey);
    if (replayed.projectId !== state.projectId) {
      console.error("server returned a different projectId for the same idempotency key");
      process.exit(1);
    }
    state.createReplayed = true;
    saveState(config, state);
    console.log("  create replayed with the saved key (no second project)");
  }

  const projectId = state.projectId;

  console.log("[3/7] poll until the arrangement is ready");
  const detail = await pollDetail(client, projectId,
    (value) => (["ARRANGEMENT_READY", "GENERATING", "READY"].includes(value.projectStatus) ? value.projectStatus : null),
    deadline, "the arrangement");

  if (detail.projectStatus === "READY") {
    console.log("[4/7]-[6/7] song is already READY; skipping quote and generation");
  } else if (state.quoteId && state.generateKey) {
    console.log("[4/7]-[6/7] reusing the saved quoteId and generate key (no re-pricing)");
    const replay = await client.call("POST", `${MUSIC_PATH}/projects/${projectId}/generations`, state.generateBody, state.generateKey);
    state.jobId = replay.jobId ?? state.jobId;
    saveState(config, state);
    console.log(`  generation replayed; jobId=${state.jobId}`);
  } else {
    console.log("[4/7] quote");
    const quote = await client.call("POST", `${MUSIC_PATH}/projects/${projectId}/quotes`, { arrangementVersion: detail.arrangementVersion });
    logBilling(quote);
    state.quoteId = quote.quoteId;
    state.arrangementVersion = detail.arrangementVersion;
    saveState(config, state);

    console.log("[5/7] generate");
    const requestBody = { quoteId: state.quoteId };
    state.generateKey ??= newIdempotencyKey();
    state.generateBody = requestBody;
    saveState(config, state);
    const generation = await client.call("POST", `${MUSIC_PATH}/projects/${projectId}/generations`, requestBody, state.generateKey);
    state.jobId = generation.jobId;
    saveState(config, state);
    console.log(`  jobId=${state.jobId} reservedCharacters=${generation.reservedCharacters}`);
  }

  console.log("[6/7] poll until the song is ready");
  const ready = await pollDetail(client, projectId,
    (value) => (["READY", "DELETED"].includes(value.projectStatus) ? value.projectStatus : null),
    deadline, "the song");
  if (ready.projectStatus !== "READY") {
    console.error(`FAILED: the project state is ${ready.projectStatus}; no audio was produced.`);
    process.exit(1);
  }
  console.log(`  assetId=${ready.readyAsset?.assetId} durationMillis=${ready.readyAsset?.durationMillis}`);

  console.log("[7/7] playback URL and download");
  const playback = await client.call("GET", `${MUSIC_PATH}/projects/${projectId}/playback-url`);
  console.log(`  playback url expires at ${playback.expiresAt}`);

  const mp3Path = join(config.outputDir, "music_output.mp3");
  const { contentType, payload } = await client.downloadAudio(`${MUSIC_PATH}/projects/${projectId}/download`);
  writeFileSync(mp3Path, payload);
  console.log(`  wrote ${mp3Path} (${payload.length} bytes, ${contentType})`);
  console.log("OK: Text-to-Music quickstart completed");
}

const entryUrl = process.argv[1] ? pathToFileURL(process.argv[1]).href : "";
if (import.meta.url === entryUrl) {
  main().catch((error) => {
    if (error instanceof ApiError) {
      console.error(`FAILED: ${error.message}`);
      if (error.code === 401) console.error("The accessKey was rejected; check MYVOCAL_API_KEY.");
      process.exit(1);
    }
    if (error instanceof TimeoutFailure) {
      console.error(`TIMEOUT: ${error.message}`);
      process.exit(2);
    }
    console.error(`FAILED: ${error.message}`);
    process.exit(1);
  });
}
