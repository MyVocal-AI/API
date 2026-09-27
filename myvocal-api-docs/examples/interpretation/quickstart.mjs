#!/usr/bin/env node
/**
 * MyVocal Interpretation quickstart (Node.js >= 18, no dependencies).
 *
 * Flow: capabilities -> create project -> upload session -> sign/upload/complete parts
 *       -> poll probe -> quote -> generate -> poll targets -> playback -> export -> download
 *
 * Environment:
 *   MYVOCAL_API_KEY           required
 *   MYVOCAL_API_BASE_URL      optional, defaults to https://api.myvocal.ai
 *   MYVOCAL_MEDIA_FILE        optional, defaults to ../_stub/fixtures/sample_source.wav
 *   MYVOCAL_TARGET_LANGUAGES  optional, comma separated, defaults to "es,fr"
 *   MYVOCAL_EXPORT_FORMAT     optional, defaults to "wav"
 *   MYVOCAL_OUTPUT_DIR        optional, defaults to the current directory
 *   MYVOCAL_MAX_WAIT_SECONDS  optional, defaults to 1800
 *
 * Safety: running this against the production host performs REAL, BILLABLE work.
 * The presigned part URLs point at object storage: the accessKey is never sent to them.
 */

import { randomBytes, randomInt } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const INTERP = "/sound_clone/api/v1/interpretation";
const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_MEDIA = join(HERE, "..", "_stub", "fixtures", "sample_source.wav");

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
    this.mediaFile = process.env.MYVOCAL_MEDIA_FILE ?? DEFAULT_MEDIA;
    this.targetLanguages = (process.env.MYVOCAL_TARGET_LANGUAGES ?? "es,fr")
      .split(",").map((code) => code.trim()).filter(Boolean);
    this.exportFormat = process.env.MYVOCAL_EXPORT_FORMAT ?? "wav";
    this.outputDir = process.env.MYVOCAL_OUTPUT_DIR ?? ".";
    this.maxWaitSeconds = Number(process.env.MYVOCAL_MAX_WAIT_SECONDS ?? "1800");
    if (!this.apiKey) exitWith("MYVOCAL_API_KEY is required", 1);
    if (!existsSync(this.mediaFile)) exitWith(`MYVOCAL_MEDIA_FILE does not exist: ${this.mediaFile}`, 1);
    mkdirSync(this.outputDir, { recursive: true });
  }

  get statePath() {
    return join(this.outputDir, "interpretation_state.json");
  }
}

function exitWith(message, code) {
  console.error(message);
  process.exit(code);
}

/** 16-64 printable ASCII characters, unique per operation. */
const newIdempotencyKey = () => randomBytes(24).toString("hex").slice(0, 32);

/** Characters arrive as strings; parse with BigInt before comparing. */
const asBigInt = (value) => (value === null || value === undefined ? null : BigInt(value));

const isJson = (contentType) => (contentType ?? "").toLowerCase().includes("json");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class Client {
  constructor(config) {
    this.config = config;
  }

  /** One API request. Checks the HTTP status *and* the JSON business code. */
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
    if (payload.code !== 1) throw new ApiError(payload.code, payload.message, payload.data, response.status);
    return payload.data ?? {};
  }

  /** PUT one part straight to object storage. The accessKey is NOT sent. */
  async uploadPart(url, payload, requiredHeaders) {
    const headers = { ...(requiredHeaders ?? {}) };
    headers["Content-Length"] ??= String(payload.length);
    const response = await fetch(url, { method: "PUT", headers, body: payload });
    if (!response.ok) throw new ApiError(-1, `part upload failed with HTTP ${response.status}`, null, response.status);
    const etag = response.headers.get("etag");
    if (!etag) throw new ApiError(-1, "storage did not return an ETag for the part");
    return etag;
  }

  async fetchBytes(url) {
    const response = await fetch(url);
    if (!response.ok) throw new ApiError(-1, `download failed with HTTP ${response.status}`, null, response.status);
    return { contentType: response.headers.get("content-type") ?? "", payload: Buffer.from(await response.arrayBuffer()) };
  }
}

/** `nextPollAfterMs` is currently null, so a bounded jittered wait is used. */
function jitteredDelay(nextPollAfterMs) {
  if (nextPollAfterMs !== null && nextPollAfterMs !== undefined) {
    return Math.max(500, Number(nextPollAfterMs));
  }
  return 2000 + randomInt(0, 3000);
}

const loadState = (config) => (existsSync(config.statePath) ? JSON.parse(readFileSync(config.statePath, "utf8")) : {});
/** Persist ids and idempotency keys only — never keys, URLs or media. */
const saveState = (config, state) => writeFileSync(config.statePath, JSON.stringify(state, null, 2));

async function main() {
  const config = new Config();
  const client = new Client(config);
  const deadline = Date.now() + config.maxWaitSeconds * 1000;
  const state = loadState(config);
  if (state.projectId) console.log(`[resume] continuing project ${state.projectId} from the saved state file`);

  console.log("[1/9] capabilities");
  const capabilities = await client.call("GET", `${INTERP}/capabilities`);
  const available = (capabilities.languages ?? []).map((entry) => entry.code);
  const exportFormats = capabilities.exportFormats ?? [];
  console.log(`  rate=${capabilities.account?.charactersPerMinutePerLanguage} per language, exportFormats=${exportFormats}`);
  if (capabilities.languageCatalogState !== "CONFIGURED") {
    exitWith(`the language catalog is ${capabilities.languageCatalogState}; quoting is unavailable`, 1);
  }
  const targets = config.targetLanguages.filter((code) => available.includes(code));
  const selected = targets.length ? targets : available.slice(0, 1);
  if (!selected.length) exitWith("no target language from capabilities.languages is selectable", 1);
  if (!exportFormats.includes(config.exportFormat)) {
    exitWith(`MYVOCAL_EXPORT_FORMAT=${config.exportFormat} is not one of ${exportFormats}`, 1);
  }

  if (!state.projectId) {
    console.log("[2/9] create project");
    state.createKey ??= newIdempotencyKey();
    const created = await client.call("POST", `${INTERP}/projects`,
      { name: "Quickstart dubbing", targetLanguages: selected }, state.createKey);
    state.projectId = created.projectId;
    saveState(config, state);
    console.log(`  projectId=${state.projectId} state=${created.state} settingsVersion=${created.settingsVersion}`);
  }

  const projectId = state.projectId;

  console.log("[3/9] upload the source file");
  const media = readFileSync(config.mediaFile);
  const size = statSync(config.mediaFile).size;
  if (!state.uploadId) {
    const session = await client.call("POST", `${INTERP}/projects/${projectId}/uploads`,
      { filename: basename(config.mediaFile), size, contentType: "audio/wav" });
    state.uploadId = session.uploadId;
    state.partSizeBytes = session.partSizeBytes;
    state.totalParts = session.totalParts;
    saveState(config, state);
    console.log(`  uploadId=${state.uploadId} partSizeBytes=${state.partSizeBytes} totalParts=${state.totalParts}`);
  }

  console.log("[4/9] sign, upload and collect ETags for every part");
  const completedParts = [];
  for (let partNumber = 1; partNumber <= state.totalParts; partNumber += 1) {
    const chunk = media.subarray((partNumber - 1) * state.partSizeBytes, partNumber * state.partSizeBytes);
    const signed = await client.call("POST", `${INTERP}/uploads/${state.uploadId}/parts`, { partNumber });
    const etag = await client.uploadPart(signed.url, chunk, signed.requiredHeaders);
    completedParts.push({ partNumber, etag });
    console.log(`  part ${partNumber}/${state.totalParts} -> ${etag.slice(0, 12)}`);
  }

  console.log("[5/9] complete the upload and wait for the probe");
  if (!state.completed) {
    await client.call("POST", `${INTERP}/uploads/${state.uploadId}/complete`, { parts: completedParts });
    state.completed = true;
    saveState(config, state);
  }
  for (;;) {
    const status = await client.call("GET", `${INTERP}/uploads/${state.uploadId}`);
    if (status.state === "READY") {
      console.log(`  upload state -> READY (media=${status.media?.inputFormat})`);
      break;
    }
    if (["FAILED", "EXPIRED", "ABORTED"].includes(status.state)) {
      throw new ApiError(status.errorCode ?? -1, `upload ended in state ${status.state}`);
    }
    if (Date.now() > deadline) {
      throw new Error(`upload ${state.uploadId} did not reach READY; keep the id and poll again later`);
    }
    console.log(`  upload state -> ${status.state}`);
    await sleep(jitteredDelay(null));
  }

  console.log("[6/9] quote");
  const quote = await client.call("POST", `${INTERP}/projects/${projectId}/quotes`,
    { settingsVersion: null, targetLanguages: selected });
  if (quote.state === "ALL_TARGETS_EXIST") {
    console.log(`  state=ALL_TARGETS_EXIST quoteId=${quote.quoteId} totalCharacters=${quote.totalCharacters}`);
    console.log("  every requested language already exists; nothing to generate or pay for");
  } else {
    // Interpretation quotes expose availableCharacters; Music quotes expose balances.total.
    const available = quote.availableCharacters ?? quote.balances?.total ?? null;
    const affordable = available === null ? "unknown" : asBigInt(available) >= asBigInt(quote.totalCharacters ?? "0");
    console.log(`  perTargetCharacters=${quote.perTargetCharacters} totalCharacters=${quote.totalCharacters} affordable=${affordable}`);
    state.quoteId = quote.quoteId;
    saveState(config, state);
  }

  console.log("[7/9] generate");
  if (state.quoteId && !state.acceptanceId) {
    state.generateKey ??= newIdempotencyKey();
    const generation = await client.call("POST", `${INTERP}/projects/${projectId}/generations`,
      { quoteId: state.quoteId }, state.generateKey);
    state.acceptanceId = generation.acceptanceId;
    saveState(config, state);
    console.log(`  acceptanceId=${state.acceptanceId} reservedCharacters=${generation.reservedCharacters}`);
  }

  console.log("[8/9] poll targets until each language is finished");
  let detail;
  for (;;) {
    detail = await client.call("GET", `${INTERP}/projects/${projectId}`);
    const list = detail.targets ?? [];
    if (list.length && list.every((t) => ["READY", "FAILED_RELEASED"].includes(t.state))) break;
    if (["READY", "FAILED"].includes(detail.summaryState)) break;
    if (Date.now() > deadline) {
      throw new Error(`project ${projectId} is still processing; keep the projectId and poll again later`);
    }
    console.log(`  summaryState -> ${detail.summaryState}`);
    await sleep(jitteredDelay(null));
  }

  for (const target of detail.targets ?? []) {
    console.log(`  ${target.language} -> ${target.state} (${target.billingState})`);
    if (target.action === "RETRY") {
      const plan = await client.call("GET", `${INTERP}/projects/${projectId}/targets/${target.targetId}/retry-plan`);
      console.log(`    retry plan: characters=${plan.characters} nextReservationCycle=${plan.nextReservationCycle} retryable=${plan.retryable}`);
    }
  }

  const ready = (detail.targets ?? []).filter((target) => target.state === "READY");
  if (!ready.length) {
    console.log("no target finished ready; nothing to play or export");
    console.log("OK: Interpretation quickstart completed (no ready target)");
    return;
  }

  console.log("[9/9] playback, export and download");
  const playback = await client.call("GET", `${INTERP}/assets/${ready[0].outputAssetId}/playback`);
  console.log(`  playback url expires at ${playback.expiresAt}`);

  const exportResponse = await client.call("POST", `${INTERP}/targets/${ready[0].targetId}/exports`,
    { format: config.exportFormat });
  const exportId = exportResponse.exportId;
  console.log(`  exportId=${exportId} state=${exportResponse.state}`);

  const destination = join(config.outputDir, `interpretation_output.${config.exportFormat}`);
  for (;;) {
    const download = await client.call("GET", `${INTERP}/exports/${exportId}/download`);
    if (download.state === "READY") {
      try {
        const { contentType, payload } = await client.fetchBytes(download.url);
        if (!payload.length) throw new ApiError(-1, "export produced an empty body");
        writeFileSync(destination, payload);
        console.log(`  wrote ${destination} (${payload.length} bytes, ${contentType})`);
        break;
      } catch (failure) {
        // The product URL is temporary; ask the endpoint for a fresh one.
        console.log(`  download URL rejected (${failure.message}); requesting a fresh URL`);
        await sleep(1000);
        continue;
      }
    }
    if (["PROCESSING", "RETRY"].includes(download.state)) {
      console.log(`  export state -> ${download.state} (continuing to poll the same exportId)`);
    } else {
      throw new ApiError(download.errorCode ?? -1, `export ended in state ${download.state}`);
    }
    if (Date.now() > deadline) throw new Error(`export ${exportId} did not become READY; keep the exportId`);
    await sleep(jitteredDelay(null));
  }

  console.log("OK: Interpretation quickstart completed");
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
