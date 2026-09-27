#!/usr/bin/env python3
"""MyVocal Interpretation quickstart (Python, standard library only).

Runs the full documented flow:
    capabilities -> create project -> upload session -> sign/upload/complete parts
    -> poll probe -> quote -> generate -> poll targets -> playback -> export -> download

Environment:
    MYVOCAL_API_KEY           required, your public API key
    MYVOCAL_API_BASE_URL      optional, defaults to https://api.myvocal.ai
    MYVOCAL_MEDIA_FILE        optional, defaults to ../_stub/fixtures/sample_source.wav
    MYVOCAL_TARGET_LANGUAGES  optional, comma separated, defaults to "es,fr"
    MYVOCAL_EXPORT_FORMAT     optional, defaults to "wav"
    MYVOCAL_OUTPUT_DIR        optional, defaults to the current directory
    MYVOCAL_MAX_WAIT_SECONDS  optional, defaults to 1800

Safety:
    Running this against the production host performs REAL, BILLABLE work and
    consumes Characters. Development should use the local stub described in
    README.md. The presigned part URLs point at object storage: the accessKey is
    never sent to them.
"""

import json
import os
import random
import secrets
import string
import sys
import time
import urllib.error
import urllib.request

INTERP = "/sound_clone/api/v1/interpretation"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MEDIA = os.path.join(HERE, "..", "_stub", "fixtures", "sample_source.wav")


class ApiError(RuntimeError):
    """A non-success MyVocal response, carrying the documented error fields."""

    def __init__(self, code, message, details=None, http_status=None):
        super().__init__("MyVocal error code=%s message=%s" % (code, message))
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status


class Config:
    def __init__(self):
        self.base_url = os.environ.get("MYVOCAL_API_BASE_URL", "https://api.myvocal.ai").rstrip("/")
        self.api_key = os.environ.get("MYVOCAL_API_KEY", "")
        self.media_file = os.environ.get("MYVOCAL_MEDIA_FILE", DEFAULT_MEDIA)
        self.target_languages = [code.strip() for code in
                                 os.environ.get("MYVOCAL_TARGET_LANGUAGES", "es,fr").split(",") if code.strip()]
        self.export_format = os.environ.get("MYVOCAL_EXPORT_FORMAT", "wav")
        self.output_dir = os.environ.get("MYVOCAL_OUTPUT_DIR", ".")
        self.max_wait = float(os.environ.get("MYVOCAL_MAX_WAIT_SECONDS", "1800"))
        if not self.api_key:
            sys.exit("MYVOCAL_API_KEY is required")
        if not os.path.exists(self.media_file):
            sys.exit("MYVOCAL_MEDIA_FILE does not exist: %s" % self.media_file)
        os.makedirs(self.output_dir, exist_ok=True)

    @property
    def state_path(self):
        return os.path.join(self.output_dir, "interpretation_state.json")


def new_idempotency_key():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def as_int(value):
    """Characters arrive as JSON strings; parse them before any arithmetic."""
    return None if value is None else int(value)


def is_json(content_type):
    return "json" in (content_type or "").lower()


class Client:
    def __init__(self, config):
        self.config = config

    def call(self, method, path, body=None, idempotency_key=None):
        """One API request. Checks the HTTP status *and* the JSON business code."""
        headers = {"accessKey": self.config.api_key, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        request = urllib.request.Request(self.config.base_url + path, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                http_status = response.status
                content_type = response.headers.get("Content-Type", "")
                raw = response.read()
        except urllib.error.HTTPError as failure:
            http_status = failure.code
            content_type = failure.headers.get("Content-Type", "")
            raw = failure.read()

        if not is_json(content_type):
            raise ApiError(-1, "expected JSON but received %s (HTTP %s)" % (content_type, http_status),
                           http_status=http_status)
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("code") != 1:
            raise ApiError(payload.get("code"), payload.get("message"), payload.get("data"), http_status)
        return payload.get("data") or {}

    def upload_part(self, url, payload, required_headers):
        """PUT one part straight to object storage. The accessKey is NOT sent."""
        headers = dict(required_headers or {})
        headers.setdefault("Content-Length", str(len(payload)))
        request = urllib.request.Request(url, data=payload, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(request) as response:
                etag = response.headers.get("ETag")
        except urllib.error.HTTPError as failure:
            raise ApiError(-1, "part upload failed with HTTP %s" % failure.code, None, failure.code)
        if not etag:
            raise ApiError(-1, "storage did not return an ETag for the part")
        return etag

    def fetch_bytes(self, url):
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request) as response:
            return response.headers.get("Content-Type", ""), response.read()


def jittered_delay(next_poll_after_ms):
    """`nextPollAfterMs` is currently null, so a bounded jittered wait is used."""
    if next_poll_after_ms is not None:
        return max(0.5, float(next_poll_after_ms) / 1000.0)
    return random.uniform(2.0, 5.0)


def load_state(config):
    if os.path.exists(config.state_path):
        with open(config.state_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_state(config, state):
    """Persist ids and idempotency keys only — never keys, URLs or media."""
    with open(config.state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def main():
    config = Config()
    client = Client(config)
    deadline = time.monotonic() + config.max_wait
    state = load_state(config)
    if state.get("projectId"):
        print("[resume] continuing project %s from the saved state file" % state["projectId"])

    print("[1/9] capabilities")
    capabilities = client.call("GET", INTERP + "/capabilities")
    available = [entry["code"] for entry in capabilities.get("languages") or []]
    export_formats = capabilities.get("exportFormats") or []
    print("  rate=%s per language, exportFormats=%s" % (
        (capabilities.get("account") or {}).get("charactersPerMinutePerLanguage"), export_formats))
    if capabilities.get("languageCatalogState") != "CONFIGURED":
        sys.exit("the language catalog is %s; quoting is unavailable" % capabilities.get("languageCatalogState"))
    targets = [code for code in config.target_languages if code in available] or available[:1]
    if not targets:
        sys.exit("no target language from capabilities.languages is selectable")
    if config.export_format not in export_formats:
        sys.exit("MYVOCAL_EXPORT_FORMAT=%s is not one of %s" % (config.export_format, export_formats))

    if not state.get("projectId"):
        print("[2/9] create project")
        state["createKey"] = state.get("createKey") or new_idempotency_key()
        created = client.call("POST", INTERP + "/projects",
                              {"name": "Quickstart dubbing", "targetLanguages": targets},
                              state["createKey"])
        state["projectId"] = created["projectId"]
        save_state(config, state)
        print("  projectId=%s state=%s settingsVersion=%s" % (
            state["projectId"], created.get("state"), created.get("settingsVersion")))

    project_id = state["projectId"]

    print("[3/9] upload the source file")
    size = os.path.getsize(config.media_file)
    filename = os.path.basename(config.media_file)
    if not state.get("uploadId"):
        session = client.call("POST", "%s/projects/%s/uploads" % (INTERP, project_id),
                              {"filename": filename, "size": size, "contentType": "audio/wav"})
        state["uploadId"] = session["uploadId"]
        state["partSizeBytes"] = session["partSizeBytes"]
        state["totalParts"] = session["totalParts"]
        save_state(config, state)
        print("  uploadId=%s partSizeBytes=%s totalParts=%s" % (
            state["uploadId"], state["partSizeBytes"], state["totalParts"]))

    upload_id = state["uploadId"]
    part_size = state["partSizeBytes"]
    total_parts = state["totalParts"]

    print("[4/9] sign, upload and collect ETags for every part")
    with open(config.media_file, "rb") as handle:
        media = handle.read()
    completed_parts = []
    for part_number in range(1, total_parts + 1):
        chunk = media[(part_number - 1) * part_size:part_number * part_size]
        signed = client.call("POST", "%s/uploads/%s/parts" % (INTERP, upload_id),
                             {"partNumber": part_number})
        etag = client.upload_part(signed["url"], chunk, signed.get("requiredHeaders"))
        completed_parts.append({"partNumber": part_number, "etag": etag})
        print("  part %d/%d -> %s" % (part_number, total_parts, etag[:12]))

    print("[5/9] complete the upload and wait for the probe")
    if not state.get("completed"):
        client.call("POST", "%s/uploads/%s/complete" % (INTERP, upload_id),
                    {"parts": completed_parts})
        state["completed"] = True
        save_state(config, state)
    final_state = None
    while True:
        status = client.call("GET", "%s/uploads/%s" % (INTERP, upload_id))
        final_state = status.get("state")
        if final_state == "READY":
            print("  upload state -> READY (media=%s)" % (status.get("media") or {}).get("inputFormat"))
            break
        if final_state in ("FAILED", "EXPIRED", "ABORTED"):
            raise ApiError(status.get("errorCode") or -1, "upload ended in state %s" % final_state)
        if time.monotonic() > deadline:
            raise TimeoutError("upload %s did not reach READY; keep the id and poll again later" % upload_id)
        print("  upload state -> %s" % final_state)
        time.sleep(jittered_delay(None))

    print("[6/9] quote")
    quote = client.call("POST", "%s/projects/%s/quotes" % (INTERP, project_id),
                        {"settingsVersion": None, "targetLanguages": targets})
    if quote.get("state") == "ALL_TARGETS_EXIST":
        print("  state=ALL_TARGETS_EXIST quoteId=%s totalCharacters=%s" % (
            quote.get("quoteId"), quote.get("totalCharacters")))
        print("  every requested language already exists; nothing to generate or pay for")
    else:
        # Interpretation quotes expose availableCharacters; Music quotes expose balances.total.
        available = quote.get("availableCharacters")
        if available is None:
            available = (quote.get("balances") or {}).get("total")
        affordable = (as_int(available) >= as_int(quote.get("totalCharacters") or "0")
                      if available is not None else "unknown")
        print("  perTargetCharacters=%s totalCharacters=%s affordable=%s" % (
            quote.get("perTargetCharacters"), quote.get("totalCharacters"), affordable))
        state["quoteId"] = quote["quoteId"]
        save_state(config, state)

    print("[7/9] generate")
    if state.get("quoteId") and not state.get("acceptanceId"):
        state["generateKey"] = state.get("generateKey") or new_idempotency_key()
        generation = client.call("POST", "%s/projects/%s/generations" % (INTERP, project_id),
                                 {"quoteId": state["quoteId"]}, state["generateKey"])
        state["acceptanceId"] = generation.get("acceptanceId")
        save_state(config, state)
        print("  acceptanceId=%s reservedCharacters=%s" % (
            state["acceptanceId"], generation.get("reservedCharacters")))

    print("[8/9] poll targets until each language is finished")
    detail = None
    while True:
        detail = client.call("GET", "%s/projects/%s" % (INTERP, project_id))
        targets_state = detail.get("targets") or []
        if targets_state and all(t.get("state") in ("READY", "FAILED_RELEASED") for t in targets_state):
            break
        if detail.get("summaryState") in ("READY", "FAILED"):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(
                "project %s is still processing; keep the projectId and poll again later "
                "(the work continues server-side)" % project_id)
        print("  summaryState -> %s" % detail.get("summaryState"))
        time.sleep(jittered_delay(None))

    for target in detail.get("targets") or []:
        print("  %s -> %s (%s)" % (target.get("language"), target.get("state"), target.get("billingState")))
        if target.get("action") == "RETRY":
            plan = client.call(
                "GET", "%s/projects/%s/targets/%s/retry-plan" % (INTERP, project_id, target["targetId"]))
            print("    retry plan: characters=%s nextReservationCycle=%s retryable=%s" % (
                plan.get("characters"), plan.get("nextReservationCycle"), plan.get("retryable")))

    ready = [t for t in (detail.get("targets") or []) if t.get("state") == "READY"]
    if not ready:
        print("no target finished ready; nothing to play or export")
        print("OK: Interpretation quickstart completed (no ready target)")
        return

    print("[9/9] playback, export and download")
    playback = client.call("GET", "%s/assets/%s/playback" % (INTERP, ready[0]["outputAssetId"]))
    print("  playback url expires at %s" % playback.get("expiresAt"))

    export = client.call("POST", "%s/targets/%s/exports" % (INTERP, ready[0]["targetId"]),
                         {"format": config.export_format})
    export_id = export["exportId"]
    print("  exportId=%s state=%s" % (export_id, export.get("state")))

    destination = os.path.join(config.output_dir, "interpretation_output." + config.export_format)
    while True:
        download = client.call("GET", "%s/exports/%s/download" % (INTERP, export_id))
        export_state = download.get("state")
        if export_state == "READY":
            try:
                content_type, payload = client.fetch_bytes(download["url"])
            except urllib.error.HTTPError as failure:
                # The product URL is temporary; ask the endpoint for a fresh one.
                print("  download URL rejected (HTTP %s); requesting a fresh URL" % failure.code)
                time.sleep(1.0)
                continue
            if not payload:
                raise ApiError(-1, "export produced an empty body")
            with open(destination, "wb") as handle:
                handle.write(payload)
            print("  wrote %s (%d bytes, %s)" % (destination, len(payload), content_type))
            break
        if export_state in ("PROCESSING", "RETRY"):
            print("  export state -> %s (continuing to poll the same exportId)" % export_state)
        else:
            raise ApiError(download.get("errorCode") or -1, "export ended in state %s" % export_state)
        if time.monotonic() > deadline:
            raise TimeoutError("export %s did not become READY; keep the exportId and poll again" % export_id)
        time.sleep(jittered_delay(None))

    print("OK: Interpretation quickstart completed")


if __name__ == "__main__":
    try:
        main()
    except ApiError as error:
        print("FAILED: %s" % error, file=sys.stderr)
        if error.code == 401:
            print("The accessKey was rejected; check MYVOCAL_API_KEY.", file=sys.stderr)
        sys.exit(1)
    except TimeoutError as error:
        print("TIMEOUT: %s" % error, file=sys.stderr)
        sys.exit(2)
