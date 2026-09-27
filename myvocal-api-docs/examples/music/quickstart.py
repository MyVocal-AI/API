#!/usr/bin/env python3
"""MyVocal Text-to-Music quickstart (Python, standard library only).

Flow: capabilities -> create project -> poll arrangement -> quote -> generate
      -> poll song -> playback URL -> download MP3

Environment:
    MYVOCAL_API_KEY           required
    MYVOCAL_API_BASE_URL      optional, defaults to https://api.myvocal.ai
    MYVOCAL_OUTPUT_DIR        optional, defaults to the current directory
    MYVOCAL_MAX_WAIT_SECONDS  optional, defaults to 900
    MYVOCAL_DURATION_SEC      optional, defaults to 90
    MYVOCAL_REQUEST_TIMEOUT   optional per-request timeout in seconds, defaults to 30

Recovery: the idempotency key and the exact request body of each create/generate
call are written to the state file before the request is sent, so a lost response
or a restart resumes the same operation. A project that already reached READY skips
quote/generate entirely; an in-flight generation is replayed with its original key
and quote instead of being re-priced. The state file never contains the API key,
a signed URL or customer content.

Exit codes: 0 = a media file was produced; 1 = API/domain error; 2 = bounded wait
expired (resource ids are printed).

Safety: running this against the production host performs REAL, BILLABLE work and
consumes Characters. Use the local stub for development.
"""

import http.client
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
EXIT_OK, EXIT_API_ERROR, EXIT_TIMEOUT = 0, 1, 2


class ApiError(RuntimeError):
    """A non-success MyVocal response, carrying the documented error fields."""

    def __init__(self, code, message, details=None, http_status=None):
        super().__init__("MyVocal error code=%s message=%s" % (code, message))
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status


class TimeoutFailure(RuntimeError):
    pass


def as_int(value):
    """Characters arrive as JSON strings; parse them before any arithmetic."""
    return None if value is None else int(value)


def is_json(content_type):
    return "json" in (content_type or "").lower()


class Config:
    def __init__(self):
        self.base_url = os.environ.get("MYVOCAL_API_BASE_URL", "https://api.myvocal.ai").rstrip("/")
        self.api_key = os.environ.get("MYVOCAL_API_KEY", "")
        self.output_dir = os.environ.get("MYVOCAL_OUTPUT_DIR", ".")
        self.max_wait = float(os.environ.get("MYVOCAL_MAX_WAIT_SECONDS", "900"))
        self.duration_sec = int(os.environ.get("MYVOCAL_DURATION_SEC", "90"))
        self.vocal_language = os.environ.get("MYVOCAL_VOCAL_LANGUAGE", "")
        self.request_timeout = float(os.environ.get("MYVOCAL_REQUEST_TIMEOUT", "30"))
        if not self.api_key:
            sys.exit("MYVOCAL_API_KEY is required")
        os.makedirs(self.output_dir, exist_ok=True)

    @property
    def state_path(self):
        return os.path.join(self.output_dir, "music_state.json")


def new_idempotency_key():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


class Client:
    def __init__(self, config):
        self.config = config

    def _send(self, request, what):
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as failure:
            return failure.code, dict(failure.headers), failure.read()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as failure:
            # A dropped/reset connection is a normal transport failure, not a crash.
            raise ApiError(-1, "%s failed: %s (re-run to resume the same operation)" % (what, failure),
                           None, None)

    def call(self, method, path, body=None, idempotency_key=None):
        """Success requires BOTH a 2xx status and JSON code == 1."""
        headers = {"accessKey": self.config.api_key, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        request = urllib.request.Request(self.config.base_url + path, data=data,
                                         headers=headers, method=method)
        status, response_headers, raw = self._send(request, "request %s %s" % (method, path))
        content_type = response_headers.get("Content-Type", "")

        if not (200 <= status < 300):
            # A non-2xx status is a failure even when the body carries code == 1.
            raise ApiError(-1, "HTTP %s for %s %s" % (status, method, path), None, status)
        if not is_json(content_type):
            raise ApiError(-1, "expected JSON but received %s (HTTP %s)" % (content_type, status),
                           None, status)
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("code") != 1:
            raise ApiError(payload.get("code"), payload.get("message"), payload.get("data"), status)
        return payload.get("data") or {}

    def download_audio(self, path):
        """The download endpoint streams audio on success and JSON on failure.

        An HTML/JSON error page, an empty body or a non-MPEG payload is reported as a
        failure instead of being written to an .mp3 file.
        """
        request = urllib.request.Request(self.config.base_url + path,
                                         headers={"accessKey": self.config.api_key,
                                                  "Accept": "audio/mpeg"},
                                         method="GET")
        status, response_headers, payload = self._send(request, "audio download")
        content_type = (response_headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (200 <= status < 300):
            raise ApiError(-1, "HTTP %s for the audio download" % status, None, status)
        if is_json(content_type):
            # Documented: a rejected download returns the normal JSON error envelope.
            error = json.loads(payload.decode("utf-8"))
            raise ApiError(error.get("code"), error.get("message"), error.get("data"), status)
        if content_type.startswith("text/"):
            raise ApiError(-1, "refusing to store a %s response as audio" % content_type)
        if not content_type.startswith("audio/"):
            raise ApiError(-1, "unexpected content type %s for the audio download" % content_type)
        if not payload:
            raise ApiError(-1, "audio body was empty")
        head = payload[:16]
        looks_like_mpeg = head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
        if not looks_like_mpeg:
            raise ApiError(-1, "payload does not start with an MPEG audio signature")
        return content_type, payload


def jittered_delay(next_poll_after_ms):
    """`nextPollAfterMs` is currently null, so a bounded jittered wait is used."""
    if next_poll_after_ms is not None:
        return max(0.5, float(next_poll_after_ms) / 1000.0)
    return random.uniform(2.0, 5.0)


def deadline_reached(deadline):
    return time.monotonic() >= deadline


def load_state(config):
    if os.path.exists(config.state_path):
        with open(config.state_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_state(config, state):
    """Persist ids, the original request body and idempotency keys — nothing sensitive."""
    with open(config.state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def poll_detail(client, project_id, accept, deadline, what):
    while True:
        detail = client.call("GET", "%s/projects/%s" % (MUSIC_PATH, project_id))
        state = accept(detail)
        if state:
            print("  projectStatus -> %s" % state)
            return detail
        if deadline_reached(deadline):
            raise TimeoutFailure(
                "%s did not finish within MYVOCAL_MAX_WAIT_SECONDS; keep projectId=%s and poll again "
                "later (the work continues server-side)" % (what, project_id))
        time.sleep(jittered_delay(None))


def log_billing(quote):
    quoted = as_int(quote.get("quotedCharacters"))
    total = as_int((quote.get("balances") or {}).get("total"))
    print("  quotedCharacters = %d" % quoted)
    if total is not None:
        print("  balance total    = %d" % total)
    print("  affordable       = %s (shortfall %s)" % (quote.get("affordable"), as_int(quote.get("shortfall"))))
    if total is not None and quoted is not None:
        # Exact 64-bit comparison: a float would lose precision above 2^53.
        print("  balance >= quote : %s (exact integer comparison)" % (total >= quoted))
        if total < quoted:
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
    print("  accessState=%s plan=%s ratePerMinute=%s" % (
        capabilities.get("accessState"), capabilities.get("planKey"), capabilities.get("ratePerMinute")))
    if capabilities.get("accessState") != "ENABLED":
        sys.exit("account accessState is %s; Text-to-Music is not available" % capabilities.get("accessState"))

    languages = capabilities.get("supportedVocalLanguages") or []
    vocal_language = config.vocal_language or (languages[0]["code"] if languages else "en")
    durations = capabilities.get("supportedDurationsSec") or []
    duration = config.duration_sec if config.duration_sec in durations else (durations[0] if durations else 90)

    if not state.get("projectId"):
        print("[2/7] create project")
        request_body = {
            "description": "An upbeat summer pop song about a road trip along the coast.",
            "genre": "POP",
            "styleNotes": "Bright synths, driving drums, warm bass.",
            "moods": ["UPLIFTING", "ENERGETIC"],
            "vocalLanguage": vocal_language,
            "durationSec": duration,
            "lyricsMode": "AUTO",
            "vocalStyle": "BRIGHT_ENERGETIC",
        }
        # Persist the key and the request binding BEFORE sending, so a lost response can
        # be resumed with the same identity instead of creating a second project.
        state["createKey"] = state.get("createKey") or new_idempotency_key()
        state["createBody"] = request_body
        save_state(config, state)
        created = client.call("POST", MUSIC_PATH + "/projects", request_body, state["createKey"])
        state["projectId"] = created["projectId"]
        save_state(config, state)
        print("  projectId=%s status=%s" % (state["projectId"], created.get("projectStatus")))
    elif state.get("createKey") and not state.get("createReplayed"):
        replayed = client.call("POST", MUSIC_PATH + "/projects", state.get("createBody"), state["createKey"])
        if replayed.get("projectId") != state["projectId"]:
            sys.exit("server returned a different projectId for the same idempotency key")
        state["createReplayed"] = True
        save_state(config, state)
        print("  create replayed with the saved key (no second project)")

    project_id = state["projectId"]

    print("[3/7] poll until the arrangement is ready")
    detail = poll_detail(
        client, project_id,
        lambda value: value.get("projectStatus")
        if value.get("projectStatus") in ("ARRANGEMENT_READY", "GENERATING", "READY")
        else None,
        deadline, "the arrangement")
    arrangement_version = detail.get("arrangementVersion")

    if detail.get("projectStatus") == "READY":
        # The song already exists: re-quoting now would produce a new quote that cannot be
        # used with the saved generation key, so go straight to the finished asset.
        print("[4/7]-[6/7] song is already READY; skipping quote and generation")
    elif state.get("quoteId") and state.get("generateKey"):
        print("[4/7]-[6/7] reusing the saved quoteId and generate key (no re-pricing)")
        # Re-sending the same key and body is the documented replay of an accepted
        # generation; it must not create a second job or a second reservation.
        replay = client.call("POST", "%s/projects/%s/generations" % (MUSIC_PATH, project_id),
                             state.get("generateBody"), state["generateKey"])
        state["jobId"] = replay.get("jobId") or state.get("jobId")
        save_state(config, state)
        print("  generation replayed; jobId=%s" % state.get("jobId"))
    else:
        print("[4/7] quote")
        quote = client.call("POST", "%s/projects/%s/quotes" % (MUSIC_PATH, project_id),
                            {"arrangementVersion": arrangement_version})
        log_billing(quote)
        state["quoteId"] = quote.get("quoteId")
        state["arrangementVersion"] = arrangement_version
        save_state(config, state)

        print("[5/7] generate")
        request_body = {"quoteId": state["quoteId"]}
        state["generateKey"] = state.get("generateKey") or new_idempotency_key()
        state["generateBody"] = request_body
        save_state(config, state)
        generation = client.call("POST", "%s/projects/%s/generations" % (MUSIC_PATH, project_id),
                                 request_body, state["generateKey"])
        state["jobId"] = generation.get("jobId")
        save_state(config, state)
        print("  jobId=%s reservedCharacters=%s" % (state["jobId"], generation.get("reservedCharacters")))

    print("[6/7] poll until the song is ready")
    ready = poll_detail(
        client, project_id,
        lambda value: value.get("projectStatus") if value.get("projectStatus") in ("READY", "DELETED") else None,
        deadline, "the song")
    if ready.get("projectStatus") != "READY":
        print("FAILED: the project state is %s; no audio was produced." % ready.get("projectStatus"),
              file=sys.stderr)
        return EXIT_API_ERROR
    asset = ready.get("readyAsset") or {}
    print("  assetId=%s durationMillis=%s" % (asset.get("assetId"), asset.get("durationMillis")))

    print("[7/7] playback URL and download")
    playback = client.call("GET", "%s/projects/%s/playback-url" % (MUSIC_PATH, project_id))
    print("  playback url expires at %s" % playback.get("expiresAt"))

    mp3_path = os.path.join(config.output_dir, "music_output.mp3")
    content_type, payload = client.download_audio("%s/projects/%s/download" % (MUSIC_PATH, project_id))
    with open(mp3_path, "wb") as handle:
        handle.write(payload)
    print("  wrote %s (%d bytes, %s)" % (mp3_path, len(payload), content_type))
    print("OK: Text-to-Music quickstart completed")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as error:
        print("FAILED: %s" % error, file=sys.stderr)
        if error.code == 401:
            print("The accessKey was rejected; check MYVOCAL_API_KEY.", file=sys.stderr)
        sys.exit(EXIT_API_ERROR)
    except TimeoutFailure as error:
        print("TIMEOUT: %s" % error, file=sys.stderr)
        sys.exit(EXIT_TIMEOUT)
