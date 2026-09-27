#!/usr/bin/env python3
"""MyVocal Text-to-Music quickstart (Python, standard library only).

Runs the full documented flow:
    capabilities -> create project -> poll arrangement -> quote -> generate
    -> poll song -> playback URL -> download MP3

Environment:
    MYVOCAL_API_KEY          required, your public API key
    MYVOCAL_API_BASE_URL     optional, defaults to https://api.myvocal.ai
    MYVOCAL_OUTPUT_DIR       optional, defaults to the current directory
    MYVOCAL_MAX_WAIT_SECONDS optional, defaults to 900
    MYVOCAL_DURATION_SEC     optional, defaults to 90
    MYVOCAL_VOCAL_LANGUAGE   optional, defaults to the first supported code

Safety:
    Running this against the production host performs REAL, BILLABLE work and
    consumes Characters. Development should use the local stub described in
    README.md.
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

MUSIC_PATH = "/sound_clone/api/v1/music"


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
        self.output_dir = os.environ.get("MYVOCAL_OUTPUT_DIR", ".")
        self.max_wait = float(os.environ.get("MYVOCAL_MAX_WAIT_SECONDS", "900"))
        self.duration_sec = int(os.environ.get("MYVOCAL_DURATION_SEC", "90"))
        self.vocal_language = os.environ.get("MYVOCAL_VOCAL_LANGUAGE", "")
        if not self.api_key:
            sys.exit("MYVOCAL_API_KEY is required")
        os.makedirs(self.output_dir, exist_ok=True)

    @property
    def state_path(self):
        return os.path.join(self.output_dir, "music_state.json")


def new_idempotency_key():
    """16-64 printable ASCII characters, unique per operation."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def as_int(value):
    """Characters arrive as JSON strings; parse them before any arithmetic."""
    if value is None:
        return None
    return int(value)


class Client:
    def __init__(self, config):
        self.config = config

    def call(self, method, path, body=None, idempotency_key=None):
        """One request. Checks the HTTP status *and* the JSON business code."""
        url = self.config.base_url + path
        data = None
        headers = {"accessKey": self.config.api_key, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                http_status = response.status
                content_type = response.headers.get("Content-Type", "")
                raw = response.read()
        except urllib.error.HTTPError as failure:  # transport-level failure
            http_status = failure.code
            content_type = failure.headers.get("Content-Type", "")
            raw = failure.read()

        if not _is_json(content_type):
            raise ApiError(
                -1,
                "expected JSON but received %s (HTTP %s)" % (content_type or "no content type", http_status),
                http_status=http_status,
            )
        payload = json.loads(raw.decode("utf-8"))
        code = payload.get("code")
        if code != 1:
            # HTTP 200 with code 401 is the documented authentication failure.
            raise ApiError(code, payload.get("message"), payload.get("data"), http_status)
        return payload.get("data") or {}


def _is_json(content_type):
    return "json" in (content_type or "").lower()


def jittered_delay(next_poll_after_ms):
    """Honour a server hint when present; otherwise use a bounded jittered wait."""
    if next_poll_after_ms is not None:
        return max(0.5, float(next_poll_after_ms) / 1000.0)
    return random.uniform(2.0, 5.0)  # a deterministic stub also accepts this spread


def poll(description, fetch, done, deadline):
    """Bounded polling loop shared by the arrangement and the song stages."""
    while True:
        value = fetch()
        state = done(value)
        if state:
            print("  %s -> %s" % (description, state))
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(
                "%s did not finish within MYVOCAL_MAX_WAIT_SECONDS; keep the returned ids "
                "and poll again later (the work continues server-side)" % description
            )
        time.sleep(jittered_delay(None))


def load_state(config):
    if os.path.exists(config.state_path):
        with open(config.state_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_state(config, state):
    """Persist only ids and idempotency keys — never keys, URLs or bodies."""
    with open(config.state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def log_billing(quote):
    """Long values are strings: convert, then compare numerically."""
    quoted = as_int(quote.get("quotedCharacters"))
    total = as_int((quote.get("balances") or {}).get("total"))
    shortfall = as_int(quote.get("shortfall"))
    print("  quotedCharacters = %d" % quoted)
    if total is not None:
        print("  balance total    = %d" % total)
    print("  affordable       = %s (shortfall %s)" % (quote.get("affordable"), shortfall))
    if total is not None and quoted is not None:
        # Exact 64-bit comparison: a float would lose precision above 2^53.
        print("  balance >= quote : %s (exact integer comparison)" % (total >= quoted))
    if total is not None and quoted is not None and total < quoted:
        print("  NOTE: balance is below the quote; generation would fail with code 47005")


def main():
    config = Config()
    client = Client(config)
    deadline = time.monotonic() + config.max_wait
    state = load_state(config)

    if state.get("projectId"):
        print("[resume] continuing project %s from the saved state file" % state["projectId"])

    print("[1/7] capabilities")
    capabilities = client.call("GET", MUSIC_PATH + "/capabilities")
    print("  plan=%s ratePerMinute=%s" % (capabilities.get("planKey"), capabilities.get("ratePerMinute")))

    languages = capabilities.get("supportedVocalLanguages") or []
    vocal_language = config.vocal_language or (languages[0]["code"] if languages else "en")
    durations = capabilities.get("supportedDurationsSec") or []
    duration = config.duration_sec if config.duration_sec in durations else (durations[0] if durations else 90)

    if not state.get("projectId"):
        print("[2/7] create project")
        state["createKey"] = state.get("createKey") or new_idempotency_key()
        brief = {
            "description": "An upbeat summer pop song about a road trip along the coast.",
            "genre": "POP",
            "styleNotes": "Bright synths, driving drums, warm bass.",
            "moods": ["UPLIFTING", "ENERGETIC"],
            "vocalLanguage": vocal_language,
            "durationSec": duration,
            "lyricsMode": "AUTO",
            "vocalStyle": "BRIGHT_ENERGETIC",
        }
        created = client.call("POST", MUSIC_PATH + "/projects", brief, state["createKey"])
        state["projectId"] = created["projectId"]
        save_state(config, state)
        print("  projectId=%s status=%s" % (state["projectId"], created.get("projectStatus")))

    project_id = state["projectId"]

    print("[3/7] poll until the arrangement is ready")
    detail = poll(
        "projectStatus",
        lambda: client.call("GET", "%s/projects/%s" % (MUSIC_PATH, project_id)),
        # On a resume the arrangement may already be done, so accept the later stages too.
        lambda value: value.get("projectStatus")
        if value.get("projectStatus") in ("ARRANGEMENT_READY", "GENERATING", "READY")
        else None,
        deadline,
    )
    arrangement_version = detail.get("arrangementVersion")

    print("[4/7] quote")
    quote = client.call("POST", "%s/projects/%s/quotes" % (MUSIC_PATH, project_id),
                        {"arrangementVersion": arrangement_version})
    log_billing(quote)
    state["quoteId"] = quote.get("quoteId")
    save_state(config, state)

    print("[5/7] generate")
    state["generateKey"] = state.get("generateKey") or new_idempotency_key()
    generation = client.call("POST", "%s/projects/%s/generations" % (MUSIC_PATH, project_id),
                             {"quoteId": state["quoteId"]}, state["generateKey"])
    state["jobId"] = generation.get("jobId")
    save_state(config, state)
    print("  jobId=%s reservedCharacters=%s" % (state["jobId"], generation.get("reservedCharacters")))

    print("[6/7] poll until the song is ready")
    ready = poll(
        "projectStatus",
        lambda: client.call("GET", "%s/projects/%s" % (MUSIC_PATH, project_id)),
        lambda value: value.get("projectStatus")
        if value.get("projectStatus") in ("READY", "DELETED")
        else None,
        deadline,
    )
    asset = ready.get("readyAsset") or {}
    print("  assetId=%s durationMillis=%s" % (asset.get("assetId"), asset.get("durationMillis")))

    print("[7/7] playback URL and download")
    playback = client.call("GET", "%s/projects/%s/playback-url" % (MUSIC_PATH, project_id))
    print("  playback url expires at %s" % playback.get("expiresAt"))

    mp3_path = os.path.join(config.output_dir, "music_output.mp3")
    download_audio(client, "%s/projects/%s/download" % (MUSIC_PATH, project_id), mp3_path)
    print("wrote %s" % mp3_path)
    print("OK: Text-to-Music quickstart completed")


def download_audio(client, path, destination):
    """Download endpoint: success is audio bytes, failure is a JSON envelope."""
    url = client.config.base_url + path
    request = urllib.request.Request(
        url, headers={"accessKey": client.config.api_key, "Accept": "audio/mpeg"}, method="GET")
    try:
        with urllib.request.urlopen(request) as response:
            http_status = response.status
            content_type = response.headers.get("Content-Type", "")
            raw = response.read()
    except urllib.error.HTTPError as failure:
        http_status = failure.code
        content_type = failure.headers.get("Content-Type", "")
        raw = failure.read()

    if _is_json(content_type):
        payload = json.loads(raw.decode("utf-8"))
        raise ApiError(payload.get("code"), payload.get("message"), payload.get("data"), http_status)
    if not content_type.lower().startswith("audio/"):
        raise ApiError(-1, "refusing to save non-audio response (%s)" % content_type, None, http_status)
    if not raw:
        raise ApiError(-1, "empty audio body", None, http_status)
    with open(destination, "wb") as handle:
        handle.write(raw)


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
