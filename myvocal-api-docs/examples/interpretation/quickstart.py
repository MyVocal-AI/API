#!/usr/bin/env python3
"""MyVocal Interpretation quickstart (Python, standard library only).

Flow: capabilities -> create project -> upload session -> sign/upload/complete parts
      -> poll the probe -> quote -> generate -> poll targets -> playback -> export -> download

Environment:
    MYVOCAL_API_KEY           required, your public API key
    MYVOCAL_API_BASE_URL      optional, defaults to https://api.myvocal.ai
    MYVOCAL_MEDIA_FILE        optional, defaults to ../_stub/fixtures/sample_source.wav
    MYVOCAL_TARGET_LANGUAGES  optional, comma separated, defaults to "es,fr"
    MYVOCAL_EXPORT_FORMAT     optional, defaults to "wav"
    MYVOCAL_OUTPUT_DIR        optional, defaults to the current directory
    MYVOCAL_MAX_WAIT_SECONDS  optional, defaults to 1800
    MYVOCAL_REQUEST_TIMEOUT   optional absolute per-request budget in seconds, defaults to 30

Recovery: identifiers and idempotency keys are written to the state file *before*
each create/generate request is sent, so a lost response or a restart resumes the
same operation instead of starting a second one. The saved create key stays bound to
its original request body, and the upload session is bound to a non-sensitive
length+SHA-256 fingerprint of the source file: if the settings or the file changed,
the client stops with an explanation instead of sending new settings under an old key
or assembling two different files. The state file never contains the API key, a
signed URL or customer media.

Exit codes: 0 = a media file was produced; 1 = API/domain error; 2 = bounded wait
expired (resource ids are printed); 3 = the run finished without usable media.

Safety: running this against the production host performs REAL, BILLABLE work and
reserves Characters per target language. The presigned part URLs point at object
storage; the accessKey is never sent to them.
"""

import hashlib
import http.client
import json
import os
import random
import secrets
import socket
import string
import sys
import threading
import time
import urllib.error
import urllib.request

INTERP = "/sound_clone/api/v1/interpretation"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MEDIA = os.path.join(HERE, "..", "_stub", "fixtures", "sample_source.wav")

EXIT_OK, EXIT_API_ERROR, EXIT_TIMEOUT, EXIT_NO_MEDIA = 0, 1, 2, 3

# Minimum bytes of each container needed to recognise the format.
MEDIA_SIGNATURES = {
    "wav": [b"RIFF"],
    "mp3": [b"ID3"],
    "flac": [b"fLaC"],
    "mp4": [b"ftyp"],
}
# Content types accepted for each export format (never HTML/JSON).
EXPECTED_CONTENT_TYPES = {
    "wav": ("audio/wav", "audio/x-wav", "audio/wave"),
    "mp3": ("audio/mpeg", "audio/mp3"),
    "flac": ("audio/flac", "audio/x-flac"),
    "mp4": ("video/mp4", "audio/mp4"),
}
# Generic binary is accepted because the payload signature is verified separately:
# object storage can serve valid media as application/octet-stream.
GENERIC_CONTENT_TYPES = ("application/octet-stream", "binary/octet-stream")


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


class RequestTimeout(RuntimeError):
    """A request exceeded its absolute time budget (MYVOCAL_REQUEST_TIMEOUT)."""


def as_int(value):
    """Characters arrive as JSON strings; parse them before any arithmetic."""
    return None if value is None else int(value)


def is_json(content_type):
    return "json" in (content_type or "").lower()


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
        self.request_timeout = float(os.environ.get("MYVOCAL_REQUEST_TIMEOUT", "30"))
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


class Client:
    def __init__(self, config, deadline):
        self.config = config
        self.deadline = deadline

    # ------------------------------------------------------------------ transport

    def _send(self, request, what):
        """One HTTP request under one absolute time budget. Network failures stay failures.

        The budget covers the whole exchange: connecting, the status line and headers, and the
        body. A socket timeout alone only bounds one receive, so a peer that keeps trickling
        header or body bytes could hold the call open forever: the live socket is shut down at
        the deadline (_RequestDeadline) and the body is read against the same deadline
        (_read_bounded). Header lookup stays on HTTPMessage, which is case-insensitive for
        Content-Type/ETag.
        """
        timeout = self.config.request_timeout
        deadline = time.monotonic() + timeout
        tracking = _RequestDeadline(timeout)
        tracking.start()
        try:
            try:
                response = _open_bounded(request, timeout, tracking)
            except urllib.error.HTTPError as failure:
                try:
                    return failure.code, failure.headers, _read_bounded(failure, deadline, timeout,
                                                                        what, tracking)
                finally:
                    failure.close()
            try:
                return response.status, response.headers, _read_bounded(
                    response, deadline, timeout, what, tracking)
            finally:
                response.close()
        except RequestTimeout as expired:
            raise ApiError(-1, str(expired), None, None)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as failure:
            if time.monotonic() >= deadline:
                # The deadline shut the request socket down, so this is a timeout, not a drop.
                raise ApiError(-1, "%s timed out after %.0fs" % (what, timeout), None, None)
            # A dropped/reset connection is a normal transport failure, not a crash.
            raise ApiError(-1, "%s failed: %s (re-run to resume the same operation)" % (what, failure),
                           None, None)
        finally:
            tracking.stop()

    def call(self, method, path, body=None, idempotency_key=None):
        """One API request. Success requires BOTH a 2xx status and JSON code == 1."""
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
            # A non-2xx status is a failure even if the body carries code == 1.
            raise ApiError(-1, "HTTP %s for %s %s" % (status, method, path), None, status)
        if not is_json(content_type):
            raise ApiError(-1, "expected JSON but received %s (HTTP %s)" % (content_type, status),
                           None, status)
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("code") != 1:
            # HTTP 200 with code 401 is the documented authentication failure.
            raise ApiError(payload.get("code"), payload.get("message"), payload.get("data"), status)
        return payload.get("data") or {}

    def upload_part(self, url, payload, required_headers):
        """PUT one part straight to object storage. The accessKey is NOT sent."""
        headers = dict(required_headers or {})
        headers.setdefault("Content-Length", str(len(payload)))
        request = urllib.request.Request(url, data=payload, headers=headers, method="PUT")
        status, response_headers, _ = self._send(request, "part upload")
        if not (200 <= status < 300):
            raise ApiError(-1, "part upload failed with HTTP %s" % status, None, status)
        lower = {name.lower(): value for name, value in response_headers.items()}
        if not lower.get("etag"):
            raise ApiError(-1, "storage did not return an ETag for the part")
        return lower["etag"]

    def fetch_media(self, url, export_format):
        """Fetch media bytes and refuse anything that is not the requested media.

        HTML error pages, JSON envelopes, empty bodies and wrong-signature payloads
        are reported as failures instead of being written as a media file.
        """
        request = urllib.request.Request(url, method="GET")
        status, response_headers, payload = self._send(request, "media download")
        content_type = (response_headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (200 <= status < 300):
            raise _MediaUnavailable("HTTP %s" % status)
        if is_json(content_type) or content_type.startswith("text/"):
            raise ApiError(-1, "refusing to store a %s response as media" % (content_type or "unknown"))
        allowed = EXPECTED_CONTENT_TYPES.get(export_format, ())
        if allowed and content_type not in allowed and content_type not in GENERIC_CONTENT_TYPES:
            # A specific-but-wrong type is a real mismatch; a generic binary type is accepted
            # only because the container signature below must still match the export format.
            raise ApiError(-1, "unexpected content type %s for %s" % (content_type, export_format))
        if not payload:
            raise ApiError(-1, "media body was empty")
        if not _looks_like_media(payload, export_format):
            raise ApiError(-1, "payload does not start with a %s signature" % export_format)
        return content_type, payload


class _MediaUnavailable(Exception):
    """The temporary media URL is no longer usable and may be renewed."""


def _looks_like_media(payload, export_format):
    signatures = MEDIA_SIGNATURES.get(export_format)
    if not signatures:
        return True
    head = payload[:16]
    if export_format == "mp3":
        return head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if export_format == "mp4":
        return len(head) >= 8 and head[4:8] == b"ftyp"
    return any(head.startswith(signature) for signature in signatures)


def source_fingerprint(path):
    """Non-sensitive content binding: streamed byte length + SHA-256 (never the media)."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"size": size, "sha256": digest.hexdigest()}


def _socket_for(reader):
    """Best-effort access to the socket behind a urllib response or an HTTPError reader."""
    for candidate in (reader, getattr(reader, "fp", None),
                      getattr(getattr(reader, "fp", None), "fp", None)):
        if candidate is None:
            continue
        for holder in (getattr(candidate, "raw", None), candidate):
            sock = getattr(holder, "_sock", None)
            if sock is not None:
                return sock
    return None


class _RequestDeadline:
    """Shuts one request's live socket down when its absolute budget expires.

    urllib applies `timeout` only as a per-receive socket timeout. A peer that keeps trickling
    bytes while the status line or the headers are still incomplete never trips it, so the
    call can block far past the budget. This keeps a reference to the request socket and
    closes it at the deadline, which makes the blocked read raise and lets the caller report a
    timeout and release the connection instead of waiting forever.
    """

    def __init__(self, timeout):
        self._lock = threading.Lock()
        self._socket = None
        self._expired = False
        self._timer = threading.Timer(timeout, self._expire)

    @property
    def expired(self):
        return self._expired

    def start(self):
        self._timer.daemon = True
        self._timer.start()

    def stop(self):
        self._timer.cancel()

    def track(self, sock):
        """Register the connected socket; close it at once if the budget already expired."""
        with self._lock:
            expired = self._expired
            if not expired:
                self._socket = sock
        if expired:
            _close_socket(sock)

    def _expire(self):
        with self._lock:
            self._expired = True
            sock, self._socket = self._socket, None
        _close_socket(sock)


def _close_socket(sock):
    """Best-effort shutdown/close of the in-flight socket from the deadline timer."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _tracked_connection(base, tracking):
    """An HTTP(S) connection that registers its final socket for deadline enforcement.

    Tracking happens after `connect()` completes, so an HTTPS socket is the wrapped TLS
    socket and a proxy tunnel is already established.
    """

    class TrackedConnection(base):
        def connect(self):
            super().connect()
            tracking.track(self.sock)

    return TrackedConnection


class _DeadlineHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, tracking):
        super().__init__()
        self._tracking = tracking

    def http_open(self, request):
        return self.do_open(_tracked_connection(http.client.HTTPConnection, self._tracking), request)


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, tracking, context=None):
        super().__init__(context=context)
        self._tracking = tracking

    def https_open(self, request):
        return self.do_open(_tracked_connection(http.client.HTTPSConnection, self._tracking), request,
                            context=self._context)


def _open_bounded(request, timeout, tracking):
    """urlopen under a budget that also covers the response status line and headers."""
    opener = urllib.request.build_opener(_DeadlineHTTPHandler(tracking),
                                         _DeadlineHTTPSHandler(tracking))
    return opener.open(request, timeout=timeout)


def _read_bounded(reader, deadline, timeout, what, tracking=None):
    """Read a whole body under one absolute deadline.

    The remaining budget is re-checked before every chunk and re-applied as the socket
    timeout, and only single-receive reads (`read1`) are used: a plain `read(n)` would keep
    blocking until n bytes even while the peer drips one byte at a time. When the deadline
    timer already shut the socket down, a body that ends at EOF must still be reported as a
    timeout instead of a truncated success.
    """
    read_some = getattr(reader, "read1", None) or getattr(getattr(reader, "fp", None), "read1", None)
    if read_some is None:
        read_some = reader.read
    chunks = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestTimeout("%s timed out after %.0fs" % (what, timeout))
        sock = _socket_for(reader)
        if sock is not None:
            try:
                sock.settimeout(max(0.05, remaining))
            except (OSError, ValueError):
                pass
        try:
            chunk = read_some(65536)
        except (TimeoutError, OSError) as failure:
            detail = str(failure) or failure.__class__.__name__
            lowered = detail.lower()
            if "timeout" in lowered or "timed out" in lowered:
                raise RequestTimeout("%s timed out after %.0fs" % (what, timeout))
            raise RequestTimeout("%s failed while reading the response after %.0fs (%s)"
                                 % (what, timeout, detail))
        if not chunk:
            break
        chunks.append(chunk)
    if tracking is not None and tracking.expired:
        raise RequestTimeout("%s timed out after %.0fs" % (what, timeout))
    return b"".join(chunks)


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
    """Persist ids, the original request binding and idempotency keys — nothing sensitive."""
    with open(config.state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def main():
    config = Config()
    deadline = time.monotonic() + config.max_wait
    client = Client(config, deadline)
    state = load_state(config)
    if state.get("projectId"):
        print("[resume] continuing project %s from the saved state file" % state["projectId"])

    print("[1/9] capabilities")
    capabilities = client.call("GET", INTERP + "/capabilities")
    # Real DTO field names: languageKey / displayName / sourceSupport.
    available = [entry["languageKey"] for entry in capabilities.get("languages") or []]
    export_formats = capabilities.get("exportFormats") or []
    account = capabilities.get("account") or {}
    print("  accessState=%s balanceState=%s rate=%s per language, exportFormats=%s" % (
        account.get("accessState"), account.get("balanceState"),
        account.get("charactersPerMinutePerLanguage"), export_formats))
    if capabilities.get("languageCatalogState") != "CONFIGURED":
        sys.exit("the language catalog is %s; quoting is unavailable" % capabilities.get("languageCatalogState"))
    if account.get("accessState") != "ENABLED":
        sys.exit("account accessState is %s; generation is not available" % account.get("accessState"))
    selected = [code for code in config.target_languages if code in available] or available[:1]
    if not selected:
        sys.exit("no target language from capabilities.languages is selectable")
    if config.export_format not in export_formats:
        sys.exit("MYVOCAL_EXPORT_FORMAT=%s is not one of %s" % (config.export_format, export_formats))

    if state.get("projectId") and state.get("targetLanguages") not in (None, selected):
        sys.exit("the saved state was created for %s but this run selected %s; use a fresh "
                 "MYVOCAL_OUTPUT_DIR or the same languages" % (state.get("targetLanguages"), selected))

    if not state.get("projectId"):
        print("[2/9] create project")
        # Persist the key and the request binding BEFORE sending, so a lost response
        # can be resumed with the same identity instead of creating a second project.
        request_body = {"name": "Quickstart dubbing", "targetLanguages": selected}
        saved_key, saved_body = state.get("createKey"), state.get("createBody")
        if saved_key and saved_body:
            # The saved key is bound to the body it was sent with. A response that never
            # arrived must be replayed with that original body, never with new settings.
            if saved_body != request_body:
                sys.exit("the saved create request used targetLanguages=%s but this run selected "
                         "%s. Refusing to send the current settings with the saved Idempotency-Key; "
                         "re-run with the same MYVOCAL_TARGET_LANGUAGES, or use a fresh "
                         "MYVOCAL_OUTPUT_DIR for a different request (the saved state is kept)."
                         % (saved_body.get("targetLanguages"), selected))
            request_body = saved_body
        elif saved_key:
            sys.exit("a create Idempotency-Key is saved without its original request body; it "
                     "cannot be replayed safely, so the saved state is kept unchanged")
        else:
            state["createKey"] = new_idempotency_key()
            state["createBody"] = request_body
            state["targetLanguages"] = selected
            save_state(config, state)
        created = client.call("POST", INTERP + "/projects", request_body, state["createKey"])
        state["projectId"] = created["projectId"]
        save_state(config, state)
        print("  projectId=%s state=%s settingsVersion=%s" % (
            state["projectId"], created.get("state"), created.get("settingsVersion")))
    elif state.get("createKey") and not state.get("completed"):
        # A previous attempt may have been accepted without a response reaching us.
        # Re-sending the SAME key and body is the documented replay, not a new project.
        replayed = client.call("POST", INTERP + "/projects", state.get("createBody"),
                               state["createKey"])
        if replayed.get("projectId") != state["projectId"]:
            sys.exit("server returned a different projectId for the same idempotency key")

    project_id = state["projectId"]

    print("[3/9] upload the source file")
    if not state.get("uploadId"):
        fingerprint = source_fingerprint(config.media_file)
        session = client.call("POST", "%s/projects/%s/uploads" % (INTERP, project_id),
                              {"filename": os.path.basename(config.media_file),
                               "size": fingerprint["size"], "contentType": "audio/wav"})
        state["uploadId"] = session["uploadId"]
        state["partSizeBytes"] = session["partSizeBytes"]
        state["totalParts"] = session["totalParts"]
        # Bind the session to the exact bytes (length + SHA-256, never the media itself) so a
        # resume cannot silently assemble parts of two different files.
        state["sourceBytes"] = fingerprint["size"]
        state["sourceSha256"] = fingerprint["sha256"]
        save_state(config, state)
        print("  uploadId=%s partSizeBytes=%s totalParts=%s" % (
            state["uploadId"], state["partSizeBytes"], state["totalParts"]))

    upload_id = state["uploadId"]

    print("[4/9] sign, upload and collect ETags for every part")
    if state.get("uploadState") == "READY" or state.get("completed"):
        # Signing is only allowed while the session is CREATED/UPLOADING: skip it.
        print("  upload is already complete; skipping part signing and upload")
    else:
        with open(config.media_file, "rb") as handle:
            media = handle.read()
        media_sha256 = hashlib.sha256(media).hexdigest()
        if state.get("sourceSha256") != media_sha256 or state.get("sourceBytes") != len(media):
            raise ApiError(-1, "the media file changed since this upload session was created "
                               "(session %s bytes sha256=%s, file now %s bytes sha256=%s); the partial "
                               "upload and its ids are kept — use a fresh MYVOCAL_OUTPUT_DIR to "
                               "upload a different file"
                           % (state.get("sourceBytes"), state.get("sourceSha256"), len(media), media_sha256))
        completed_parts = state.get("completedParts") or []
        signed_numbers = {part["partNumber"] for part in completed_parts}
        for part_number in range(1, state["totalParts"] + 1):
            if part_number in signed_numbers:
                continue
            chunk = media[(part_number - 1) * state["partSizeBytes"]:part_number * state["partSizeBytes"]]
            signed = client.call("POST", "%s/uploads/%s/parts" % (INTERP, upload_id),
                                 {"partNumber": part_number})
            etag = client.upload_part(signed["url"], chunk, signed.get("requiredHeaders"))
            completed_parts.append({"partNumber": part_number, "etag": etag})
            state["completedParts"] = completed_parts
            save_state(config, state)
            print("  part %d/%d -> %s" % (part_number, state["totalParts"], etag[:12]))

    print("[5/9] complete the upload and wait for the probe")
    if not state.get("completed"):
        client.call("POST", "%s/uploads/%s/complete" % (INTERP, upload_id),
                    {"parts": state.get("completedParts") or []})
        state["completed"] = True
        save_state(config, state)
    while True:
        status = client.call("GET", "%s/uploads/%s" % (INTERP, upload_id))
        upload_state = status.get("state")
        state["uploadState"] = upload_state
        save_state(config, state)
        if upload_state == "READY":
            print("  upload state -> READY (media=%s)" % (status.get("media") or {}).get("inputFormat"))
            break
        if upload_state in ("FAILED", "EXPIRED", "ABORTED"):
            raise ApiError(status.get("errorCode") or -1, "upload ended in state %s" % upload_state)
        if deadline_reached(deadline):
            raise TimeoutFailure("upload %s did not reach READY; keep the id and poll again later" % upload_id)
        print("  upload state -> %s" % upload_state)
        time.sleep(jittered_delay(None))

    print("[6/9] quote")
    if state.get("quoteId"):
        print("  reusing the saved quoteId=%s (the accepted quote must not be re-priced)" % state["quoteId"])
    else:
        quote = client.call("POST", "%s/projects/%s/quotes" % (INTERP, project_id),
                            {"settingsVersion": None, "targetLanguages": selected})
        if quote.get("state") == "ALL_TARGETS_EXIST":
            print("  state=ALL_TARGETS_EXIST quoteId=%s totalCharacters=%s" % (
                quote.get("quoteId"), quote.get("totalCharacters")))
            print("  every requested language already exists; nothing to generate or pay for")
            state["allTargetsExist"] = True
            save_state(config, state)
        else:
            available_characters = quote.get("availableCharacters")
            affordable = (as_int(available_characters) >= as_int(quote.get("totalCharacters") or "0")
                          if available_characters is not None else "unknown")
            print("  perTargetCharacters=%s totalCharacters=%s affordable=%s" % (
                quote.get("perTargetCharacters"), quote.get("totalCharacters"), affordable))
            state["quoteId"] = quote["quoteId"]
            save_state(config, state)

    print("[7/9] generate")
    if state.get("quoteId") and not state.get("acceptanceId"):
        request_body = {"quoteId": state["quoteId"]}
        state["generateKey"] = state.get("generateKey") or new_idempotency_key()
        state["generateBody"] = request_body
        save_state(config, state)
        generation = client.call("POST", "%s/projects/%s/generations" % (INTERP, project_id),
                                 request_body, state["generateKey"])
        state["acceptanceId"] = generation.get("acceptanceId")
        save_state(config, state)
        print("  acceptanceId=%s reservedCharacters=%s" % (
            state["acceptanceId"], generation.get("reservedCharacters")))
    elif state.get("acceptanceId"):
        print("  reusing the saved acceptanceId=%s (a paid generation is never resubmitted)" % state["acceptanceId"])
        client.call("POST", "%s/projects/%s/generations" % (INTERP, project_id),
                    state.get("generateBody"), state["generateKey"])

    print("[8/9] poll targets until each language is finished")
    detail = None
    while True:
        detail = client.call("GET", "%s/projects/%s" % (INTERP, project_id))
        targets_state = detail.get("targets") or []
        if targets_state and all(t.get("state") in ("READY", "FAILED_RELEASED") for t in targets_state):
            break
        if detail.get("summaryState") in ("READY", "FAILED"):
            break
        if deadline_reached(deadline):
            raise TimeoutFailure(
                "project %s is still processing; keep the projectId and poll again later "
                "(the work continues server-side)" % project_id)
        print("  summaryState -> %s" % detail.get("summaryState"))
        time.sleep(jittered_delay(None))

    ready_targets, failed_targets = [], []
    for target in detail.get("targets") or []:
        print("  %s -> %s (%s)" % (target.get("language"), target.get("state"), target.get("billingState")))
        if target.get("state") == "READY":
            ready_targets.append(target)
        else:
            failed_targets.append(target)
            if target.get("action") == "RETRY":
                plan = client.call("GET", "%s/projects/%s/targets/%s/retry-plan" % (
                    INTERP, project_id, target["targetId"]))
                print("    retry plan: characters=%s nextReservationCycle=%s retryable=%s" % (
                    plan.get("characters"), plan.get("nextReservationCycle"), plan.get("retryable")))

    if not ready_targets:
        print("FAILED: no target language produced a result (%d failed). Nothing to download."
              % len(failed_targets), file=sys.stderr)
        print("Recoverable: projectId=%s" % project_id, file=sys.stderr)
        return EXIT_NO_MEDIA
    if failed_targets:
        print("PARTIAL: %d ready, %d failed (failed targets keep their own retry plan)"
              % (len(ready_targets), len(failed_targets)))
    else:
        print("ALL READY: %d target(s)" % len(ready_targets))

    print("[9/9] playback, export and download")
    playback = client.call("GET", "%s/assets/%s/playback" % (INTERP, ready_targets[0]["outputAssetId"]))
    print("  playback url expires at %s" % playback.get("expiresAt"))

    export_id = state.get("exportId")
    if not export_id:
        export = client.call("POST", "%s/targets/%s/exports" % (INTERP, ready_targets[0]["targetId"]),
                             {"format": config.export_format})
        export_id = export["exportId"]
        state["exportId"] = export_id
        save_state(config, state)
    print("  exportId=%s" % export_id)

    destination = os.path.join(config.output_dir, "interpretation_output." + config.export_format)
    renewals = 0
    renewal_limit = playback.get("renewalAttemptLimit")
    renewal_limit = 1 if renewal_limit is None else int(renewal_limit)
    while True:
        download = client.call("GET", "%s/exports/%s/download" % (INTERP, export_id))
        export_state = download.get("state")
        if export_state == "READY":
            try:
                content_type, payload = client.fetch_media(download["url"], config.export_format)
            except _MediaUnavailable as unavailable:
                renewals += 1
                if renewals > renewal_limit:
                    print("FAILED: the download URL stayed unusable after %d renewal(s) (%s); "
                          "exportId=%s stays recoverable" % (renewals, unavailable, export_id),
                          file=sys.stderr)
                    return EXIT_API_ERROR
                if deadline_reached(deadline):
                    raise TimeoutFailure("deadline reached while renewing the download URL; exportId=%s" % export_id)
                print("  download URL not usable (%s); renewing (%d/%d)" % (unavailable, renewals, renewal_limit))
                time.sleep(1.0)
                continue
            with open(destination, "wb") as handle:
                handle.write(payload)
            print("  wrote %s (%d bytes, %s)" % (destination, len(payload), content_type))
            break
        if export_state in ("PROCESSING", "RETRY"):
            print("  export state -> %s (continuing to poll the same exportId)" % export_state)
        else:
            raise ApiError(download.get("errorCode") or -1, "export ended in state %s" % export_state)
        if deadline_reached(deadline):
            raise TimeoutFailure("export %s did not become READY; keep the exportId and poll again" % export_id)
        time.sleep(jittered_delay(None))

    print("OK: Interpretation quickstart completed")
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
