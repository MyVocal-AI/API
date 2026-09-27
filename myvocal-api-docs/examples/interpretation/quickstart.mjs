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
 *   MYVOCAL_REQUEST_TIMEOUT   optional absolute per-request budget in seconds, defaults to 30
 *
 * Recovery: identifiers, part ETags and idempotency keys are written to the state
 * file before each create/generate request is sent, so a lost response or a restart
 * resumes the same operation. A completed upload is never re-signed or re-uploaded,
 * and an accepted generation is never re-priced or resubmitted. The saved create key
 * stays bound to its original request body, and the upload session is bound to a
 * non-sensitive length+SHA-256 fingerprint of the source file: if the settings or the
 * file changed, the client stops with an explanation instead of sending new settings
 * under an old key or assembling two different files.
 *
 * Exit codes: 0 = media produced; 1 = API/domain error; 2 = bounded wait expired;
 * 3 = finished without usable media. The accessKey is never sent to storage URLs.
 */

import { createHash, randomBytes, randomInt } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const INTERP = "/sound_clone/api/v1/interpretation";
const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_MEDIA = join(HERE, "..", "_stub", "fixtures", "sample_source.wav");

const EXIT_OK = 0, EXIT_API_ERROR = 1, EXIT_TIMEOUT = 2, EXIT_NO_MEDIA = 3;

/** Content types accepted per export format, and the signature each container starts with. */
const EXPECTED_CONTENT_TYPES = {
  wav: ["audio/wav", "audio/x-wav", "audio/wave"],
  mp3: ["audio/mpeg", "audio/mp3"],
  flac: ["audio/flac", "audio/x-flac"],
  mp4: ["video/mp4", "audio/mp4"],
};
/** Generic binary is accepted because the container signature is still verified below. */
const GENERIC_CONTENT_TYPES = ["application/octet-stream", "binary/octet-stream"];

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

class MediaUnavailable extends Error {}

const asBigInt = (value) => (value === null || value === undefined ? null : BigInt(value));
const isJson = (contentType) => (contentType ?? "").toLowerCase().includes("json");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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
    this.requestTimeoutMs = Number(process.env.MYVOCAL_REQUEST_TIMEOUT ?? "30") * 1000;
    if (!this.apiKey) fail("MYVOCAL_API_KEY is required", EXIT_API_ERROR);
    if (!existsSync(this.mediaFile)) fail(`MYVOCAL_MEDIA_FILE does not exist: ${this.mediaFile}`, EXIT_API_ERROR);
    mkdirSync(this.outputDir, { recursive: true });
  }

  get statePath() {
    return join(this.outputDir, "interpretation_state.json");
  }
}

function fail(message, code) {
  console.error(message);
  process.exit(code);
}

const newIdempotencyKey = () => randomBytes(24).toString("hex").slice(0, 32);

class Client {
  constructor(config) {
    this.config = config;
  }

  async #send(url, options, what) {
    try {
      return await fetch(url, { ...options, signal: AbortSignal.timeout(this.config.requestTimeoutMs) });
    } catch (failure) {
      throw new ApiError(-1, `${what} failed: ${failure.message}`, null, null);
    }
  }

  /** Success needs BOTH a 2xx status and JSON code == 1. */
  async call(method, path, body, idempotencyKey) {
    const headers = { accessKey: this.config.apiKey, Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

    const response = await this.#send(this.config.baseUrl + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    }, `request ${method} ${path}`);
    const contentType = response.headers.get("content-type");
    const raw = await response.text();

    if (!(response.status >= 200 && response.status < 300)) {
      throw new ApiError(-1, `HTTP ${response.status} for ${method} ${path}`, null, response.status);
    }
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
    const response = await this.#send(url, { method: "PUT", headers, body: payload }, "part upload");
    if (!(response.status >= 200 && response.status < 300)) {
      throw new ApiError(-1, `part upload failed with HTTP ${response.status}`, null, response.status);
    }
    const etag = response.headers.get("etag");
    if (!etag) throw new ApiError(-1, "storage did not return an ETag for the part");
    return etag;
  }

  /**
   * Fetch media bytes and refuse anything that is not the requested media. HTML error
   * pages, JSON envelopes, empty bodies and wrong-signature payloads are failures.
   */
  async fetchMedia(url, exportFormat) {
    const response = await this.#send(url, { method: "GET" }, "media download");
    const contentType = (response.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
    const payload = Buffer.from(await response.arrayBuffer());

    if (!(response.status >= 200 && response.status < 300)) throw new MediaUnavailable(`HTTP ${response.status}`);
    if (isJson(contentType) || contentType.startsWith("text/")) {
      throw new ApiError(-1, `refusing to store a ${contentType || "unknown"} response as media`);
    }
    const allowed = EXPECTED_CONTENT_TYPES[exportFormat] ?? [];
    if (allowed.length && !allowed.includes(contentType) && !GENERIC_CONTENT_TYPES.includes(contentType)) {
      // A specific-but-wrong type is a real mismatch; a generic binary type is accepted only
      // because the container signature below must still match the export format.
      throw new ApiError(-1, `unexpected content type ${contentType} for ${exportFormat}`);
    }
    if (payload.length === 0) throw new ApiError(-1, "media body was empty");
    if (!looksLikeMedia(payload, exportFormat)) {
      throw new ApiError(-1, `payload does not start with a ${exportFormat} signature`);
    }
    return { contentType, payload };
  }
}

function looksLikeMedia(payload, exportFormat) {
  const head = payload.subarray(0, 16);
  switch (exportFormat) {
    case "wav":
      return head.subarray(0, 4).toString("latin1") === "RIFF";
    case "mp3":
      return head.subarray(0, 3).toString("latin1") === "ID3"
        || (head.length > 1 && head[0] === 0xff && (head[1] & 0xe0) === 0xe0);
    case "flac":
      return head.subarray(0, 4).toString("latin1") === "fLaC";
    case "mp4":
      return head.length >= 8 && head.subarray(4, 8).toString("latin1") === "ftyp";
    default:
      return true;
  }
}

/** `nextPollAfterMs` is currently null, so a bounded jittered wait is used. */
function jitteredDelay(nextPollAfterMs) {
  if (nextPollAfterMs !== null && nextPollAfterMs !== undefined) return Math.max(500, Number(nextPollAfterMs));
  return 2000 + randomInt(0, 3000);
}

const loadState = (config) => (existsSync(config.statePath) ? JSON.parse(readFileSync(config.statePath, "utf8")) : {});
const saveState = (config, state) => writeFileSync(config.statePath, JSON.stringify(state, null, 2));

async function main() {
  const config = new Config();
  const client = new Client(config);
  const deadline = Date.now() + config.maxWaitSeconds * 1000;
  const state = loadState(config);
  if (state.projectId) console.log(`[resume] continuing project ${state.projectId} from the saved state file`);

  console.log("[1/9] capabilities");
  const capabilities = await client.call("GET", `${INTERP}/capabilities`);
  // Real DTO field names: languageKey / displayName / sourceSupport.
  const available = (capabilities.languages ?? []).map((entry) => entry.languageKey);
  const exportFormats = capabilities.exportFormats ?? [];
  const account = capabilities.account ?? {};
  console.log(`  accessState=${account.accessState} balanceState=${account.balanceState} rate=${account.charactersPerMinutePerLanguage} per language, exportFormats=${exportFormats}`);
  if (capabilities.languageCatalogState !== "CONFIGURED") fail(`the language catalog is ${capabilities.languageCatalogState}; quoting is unavailable`, EXIT_API_ERROR);
  if (account.accessState !== "ENABLED") fail(`account accessState is ${account.accessState}; generation is not available`, EXIT_API_ERROR);
  const selected = config.targetLanguages.filter((code) => available.includes(code));
  const targets = selected.length ? selected : available.slice(0, 1);
  if (!targets.length) fail("no target language from capabilities.languages is selectable", EXIT_API_ERROR);
  if (!exportFormats.includes(config.exportFormat)) {
    fail(`MYVOCAL_EXPORT_FORMAT=${config.exportFormat} is not one of ${exportFormats}`, EXIT_API_ERROR);
  }
  if (state.projectId && state.targetLanguages && JSON.stringify(state.targetLanguages) !== JSON.stringify(targets)) {
    fail(`the saved state was created for ${state.targetLanguages} but this run selected ${targets}; use a fresh MYVOCAL_OUTPUT_DIR or the same languages`, EXIT_API_ERROR);
  }

  if (!state.projectId) {
    console.log("[2/9] create project");
    // Persist the key and the request binding BEFORE sending.
    const requestBody = { name: "Quickstart dubbing", targetLanguages: targets };
    let body = requestBody;
    if (state.createKey !== undefined && state.createBody !== undefined) {
      // The key is bound to the body it was sent with: a lost response is replayed with that
      // original body, never with settings changed since.
      if (JSON.stringify(state.createBody) !== JSON.stringify(requestBody)) {
        fail(`the saved create request used targetLanguages=${JSON.stringify(state.createBody.targetLanguages)} but this run selected ${JSON.stringify(targets)}. Refusing to send the current settings with the saved Idempotency-Key; re-run with the same MYVOCAL_TARGET_LANGUAGES, or use a fresh MYVOCAL_OUTPUT_DIR for a different request (the saved state is kept).`, EXIT_API_ERROR);
      }
      body = state.createBody;
    } else if (state.createKey !== undefined) {
      fail("a create Idempotency-Key is saved without its original request body; it cannot be replayed safely, so the saved state is kept unchanged", EXIT_API_ERROR);
    } else {
      state.createKey = newIdempotencyKey();
      state.createBody = requestBody;
      state.targetLanguages = targets;
      saveState(config, state);
    }
    const created = await client.call("POST", `${INTERP}/projects`, body, state.createKey);
    state.projectId = created.projectId;
    saveState(config, state);
    console.log(`  projectId=${state.projectId} state=${created.state} settingsVersion=${created.settingsVersion}`);
  } else if (state.createKey && !state.completed) {
    // A previous attempt may have been accepted without a response reaching us: resend
    // the SAME key and body (the documented replay), never a new project.
    const replayed = await client.call("POST", `${INTERP}/projects`, state.createBody, state.createKey);
    if (replayed.projectId !== state.projectId) fail("server returned a different projectId for the same idempotency key", EXIT_API_ERROR);
  }

  const projectId = state.projectId;

  console.log("[3/9] upload the source file");
  const media = readFileSync(config.mediaFile);
  const sourceSha256 = createHash("sha256").update(media).digest("hex");
  if (!state.uploadId) {
    const session = await client.call("POST", `${INTERP}/projects/${projectId}/uploads`,
      { filename: basename(config.mediaFile), size: media.length, contentType: "audio/wav" });
    state.uploadId = session.uploadId;
    state.partSizeBytes = session.partSizeBytes;
    state.totalParts = session.totalParts;
    // Bind the session to the exact bytes (length + SHA-256, never the media itself) so a
    // resume cannot silently assemble parts of two different files.
    state.sourceBytes = media.length;
    state.sourceSha256 = sourceSha256;
    saveState(config, state);
    console.log(`  uploadId=${state.uploadId} partSizeBytes=${state.partSizeBytes} totalParts=${state.totalParts}`);
  }

  console.log("[4/9] sign, upload and collect ETags for every part");
  if (state.uploadState === "READY" || state.completed) {
    // Signing is only allowed while the session is CREATED/UPLOADING: skip it.
    console.log("  upload is already complete; skipping part signing and upload");
  } else {
    if (state.sourceSha256 !== sourceSha256 || state.sourceBytes !== media.length) {
      throw new ApiError(-1, `the media file changed since this upload session was created (session ${state.sourceBytes} bytes sha256=${state.sourceSha256 ?? "unknown"}, file now ${media.length} bytes sha256=${sourceSha256}); the partial upload and its ids are kept — use a fresh MYVOCAL_OUTPUT_DIR to upload a different file`);
    }
    const completedParts = state.completedParts ?? [];
    const signedNumbers = new Set(completedParts.map((part) => part.partNumber));
    for (let partNumber = 1; partNumber <= state.totalParts; partNumber += 1) {
      if (signedNumbers.has(partNumber)) continue;
      const chunk = media.subarray((partNumber - 1) * state.partSizeBytes, partNumber * state.partSizeBytes);
      const signed = await client.call("POST", `${INTERP}/uploads/${state.uploadId}/parts`, { partNumber });
      const etag = await client.uploadPart(signed.url, chunk, signed.requiredHeaders);
      completedParts.push({ partNumber, etag });
      state.completedParts = completedParts;
      saveState(config, state);
      console.log(`  part ${partNumber}/${state.totalParts} -> ${etag.slice(0, 12)}`);
    }
  }

  console.log("[5/9] complete the upload and wait for the probe");
  if (!state.completed) {
    await client.call("POST", `${INTERP}/uploads/${state.uploadId}/complete`, { parts: state.completedParts ?? [] });
    state.completed = true;
    saveState(config, state);
  }
  for (;;) {
    const status = await client.call("GET", `${INTERP}/uploads/${state.uploadId}`);
    state.uploadState = status.state;
    saveState(config, state);
    if (status.state === "READY") {
      console.log(`  upload state -> READY (media=${status.media?.inputFormat})`);
      break;
    }
    if (["FAILED", "EXPIRED", "ABORTED"].includes(status.state)) {
      throw new ApiError(status.errorCode ?? -1, `upload ended in state ${status.state}`);
    }
    if (Date.now() > deadline) {
      throw new TimeoutFailure(`upload ${state.uploadId} did not reach READY; keep the id and poll again later`);
    }
    console.log(`  upload state -> ${status.state}`);
    await sleep(jitteredDelay(null));
  }

  console.log("[6/9] quote");
  if (state.quoteId) {
    console.log(`  reusing the saved quoteId=${state.quoteId} (an accepted quote must not be re-priced)`);
  } else {
    const quote = await client.call("POST", `${INTERP}/projects/${projectId}/quotes`,
      { settingsVersion: null, targetLanguages: targets });
    if (quote.state === "ALL_TARGETS_EXIST") {
      console.log(`  state=ALL_TARGETS_EXIST quoteId=${quote.quoteId} totalCharacters=${quote.totalCharacters}`);
      console.log("  every requested language already exists; nothing to generate or pay for");
      state.allTargetsExist = true;
      saveState(config, state);
    } else {
      const availableCharacters = quote.availableCharacters ?? null;
      const affordable = availableCharacters === null ? "unknown" : asBigInt(availableCharacters) >= asBigInt(quote.totalCharacters ?? "0");
      console.log(`  perTargetCharacters=${quote.perTargetCharacters} totalCharacters=${quote.totalCharacters} affordable=${affordable}`);
      state.quoteId = quote.quoteId;
      saveState(config, state);
    }
  }

  console.log("[7/9] generate");
  if (state.quoteId && !state.acceptanceId) {
    const requestBody = { quoteId: state.quoteId };
    state.generateKey ??= newIdempotencyKey();
    state.generateBody = requestBody;
    saveState(config, state);
    const generation = await client.call("POST", `${INTERP}/projects/${projectId}/generations`, requestBody, state.generateKey);
    state.acceptanceId = generation.acceptanceId;
    saveState(config, state);
    console.log(`  acceptanceId=${state.acceptanceId} reservedCharacters=${generation.reservedCharacters}`);
  } else if (state.acceptanceId) {
    console.log(`  reusing the saved acceptanceId=${state.acceptanceId} (a paid generation is never resubmitted)`);
    await client.call("POST", `${INTERP}/projects/${projectId}/generations`, state.generateBody, state.generateKey);
  }

  console.log("[8/9] poll targets until each language is finished");
  let detail;
  for (;;) {
    detail = await client.call("GET", `${INTERP}/projects/${projectId}`);
    const list = detail.targets ?? [];
    if (list.length && list.every((t) => ["READY", "FAILED_RELEASED"].includes(t.state))) break;
    if (["READY", "FAILED"].includes(detail.summaryState)) break;
    if (Date.now() > deadline) {
      throw new TimeoutFailure(`project ${projectId} is still processing; keep the projectId and poll again later`);
    }
    console.log(`  summaryState -> ${detail.summaryState}`);
    await sleep(jitteredDelay(null));
  }

  const readyTargets = [];
  const failedTargets = [];
  for (const target of detail.targets ?? []) {
    console.log(`  ${target.language} -> ${target.state} (${target.billingState})`);
    if (target.state === "READY") {
      readyTargets.push(target);
    } else {
      failedTargets.push(target);
      if (target.action === "RETRY") {
        const plan = await client.call("GET", `${INTERP}/projects/${projectId}/targets/${target.targetId}/retry-plan`);
        console.log(`    retry plan: characters=${plan.characters} nextReservationCycle=${plan.nextReservationCycle} retryable=${plan.retryable}`);
      }
    }
  }

  if (!readyTargets.length) {
    console.error(`FAILED: no target language produced a result (${failedTargets.length} failed). Nothing to download.`);
    console.error(`Recoverable: projectId=${projectId}`);
    return EXIT_NO_MEDIA;
  }
  if (failedTargets.length) {
    console.log(`PARTIAL: ${readyTargets.length} ready, ${failedTargets.length} failed (failed targets keep their own retry plan)`);
  } else {
    console.log(`ALL READY: ${readyTargets.length} target(s)`);
  }

  console.log("[9/9] playback, export and download");
  const playback = await client.call("GET", `${INTERP}/assets/${readyTargets[0].outputAssetId}/playback`);
  console.log(`  playback url expires at ${playback.expiresAt}`);

  if (!state.exportId) {
    const exportResponse = await client.call("POST", `${INTERP}/targets/${readyTargets[0].targetId}/exports`,
      { format: config.exportFormat });
    state.exportId = exportResponse.exportId;
    saveState(config, state);
  }
  console.log(`  exportId=${state.exportId}`);

  const destination = join(config.outputDir, `interpretation_output.${config.exportFormat}`);
  const renewalLimit = playback.renewalAttemptLimit === null || playback.renewalAttemptLimit === undefined
    ? 1 : Number(playback.renewalAttemptLimit);
  let renewals = 0;
  for (;;) {
    const download = await client.call("GET", `${INTERP}/exports/${state.exportId}/download`);
    if (download.state === "READY") {
      try {
        const { contentType, payload } = await client.fetchMedia(download.url, config.exportFormat);
        writeFileSync(destination, payload);
        console.log(`  wrote ${destination} (${payload.length} bytes, ${contentType})`);
        break;
      } catch (failure) {
        if (!(failure instanceof MediaUnavailable)) throw failure; // a real failure, not a renewal
        renewals += 1;
        if (renewals > renewalLimit) {
          console.error(`FAILED: the download URL stayed unusable after ${renewals} renewal(s) (${failure.message}); exportId=${state.exportId} stays recoverable`);
          return EXIT_API_ERROR;
        }
        if (Date.now() > deadline) {
          throw new TimeoutFailure(`deadline reached while renewing the download URL; exportId=${state.exportId}`);
        }
        console.log(`  download URL not usable (${failure.message}); renewing (${renewals}/${renewalLimit})`);
        await sleep(1000);
        continue;
      }
    }
    if (["PROCESSING", "RETRY"].includes(download.state)) {
      console.log(`  export state -> ${download.state} (continuing to poll the same exportId)`);
    } else {
      throw new ApiError(download.errorCode ?? -1, `export ended in state ${download.state}`);
    }
    if (Date.now() > deadline) throw new TimeoutFailure(`export ${state.exportId} did not become READY; keep the exportId`);
    await sleep(jitteredDelay(null));
  }

  console.log("OK: Interpretation quickstart completed");
  return EXIT_OK;
}

main().then((code) => process.exit(code)).catch((error) => {
  if (error instanceof ApiError) {
    console.error(`FAILED: ${error.message}`);
    if (error.code === 401) console.error("The accessKey was rejected; check MYVOCAL_API_KEY.");
    process.exit(EXIT_API_ERROR);
  }
  if (error instanceof TimeoutFailure) {
    console.error(`TIMEOUT: ${error.message}`);
    process.exit(EXIT_TIMEOUT);
  }
  console.error(`FAILED: ${error.message}`);
  process.exit(EXIT_API_ERROR);
});
