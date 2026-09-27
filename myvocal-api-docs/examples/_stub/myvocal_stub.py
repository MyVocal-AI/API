#!/usr/bin/env python3
"""Deterministic local stub of the MyVocal public API for running the examples.

Development/validation aid only: it reproduces the documented response shapes so the
example clients can be exercised end to end without a real key, without spending
Characters and without calling any provider.

Response shapes follow the real DTOs (InterpretationApiModels, the Music service
maps): language entries use `languageKey`/`displayName`/`sourceSupport`,
`account.accessState` is ENABLED/DISABLED (Music) or ENABLED/FREE_LOCKED/
ENTERPRISE_UNRESOLVED/SYNCING/UNRESOLVED (Interpretation), `balanceState` is
AVAILABLE/LEDGER_UNAVAILABLE, quote `targets` is the `newTargets` alias and
`renewalAttemptLimit` is 1.

Idempotency follows the real rules rather than being permissive:

  Music
    - create replay returns the project detail (a replay is not the first response)
    - a generation key is bound to projectId:quoteId; the same key with a different
      body is 47008, and a consumed quote presented with a different key is 47007
  Interpretation
    - same key + same body replays; same key + different body is 47111
    - an already-accepted quote presented with a different key reuses the existing
      acceptance and does not reserve again

Behaviour is selected by the API key value, so the example clients stay realistic:

    stub-happy          happy path (default for any other value)
    stub-401            HTTP 200 with {"code":401,...} on every call
    stub-bignum         Characters values above 2^53
    stub-timeout        the arrangement never becomes ready
    stub-500            HTTP 500 carrying a code=1 body (HTTP status must be checked)
    stub-partial        Interpretation: one target ready, one failed_released
    stub-all-failed     Interpretation: every target failed_released
    stub-export-retry   Interpretation: the export returns RETRY before READY
    stub-expired-url    Interpretation: the first download URL has expired
    stub-forbidden      Interpretation: every download URL returns 403
    stub-html-media     Interpretation: the download URL returns an HTML error page
    stub-drop           the first create/generation response is dropped (response lost)

Usage:
    python3 myvocal_stub.py --port 8765
"""

import argparse
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

MUSIC = "/sound_clone/api/v1/music"
INTERP = "/sound_clone/api/v1/interpretation"

# Characters values chosen so a double cannot tell them apart while exact 64-bit
# integers can: both round to the same IEEE-754 double.
BIG_QUOTE = 9007199254740993
BIG_BALANCE = 9007199254740992


class Store:
    """In-memory per-API-key state, guarded by one lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_key = {}

    def session(self, key):
        with self.lock:
            return self.by_key.setdefault(key, {
                "projects": {}, "uploads": {}, "jobs": {}, "exports": {},
                "idem": {}, "quotes": {}, "seq": 0, "reservations": [],
                "acceptances": {}, "dropped": set(),
            })

    def next_id(self, session, prefix):
        with self.lock:
            session["seq"] += 1
            return "%s_%06d" % (prefix, session["seq"])


STORE = Store()


def scenario_of(api_key):
    key = (api_key or "").strip()
    return key if key.startswith("stub-") else "stub-happy"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("STUB_VERBOSE"):
            print("[stub] " + (fmt % args))

    # ------------------------------------------------------------------ plumbing

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, payload, content_type, extra_headers=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _success(self, data):
        self._send_json({"code": 1, "message": "success", "data": data})

    def _error(self, code, message, action="CONTACT_SUPPORT", retryable=False, status=200):
        self._send_json({
            "code": code,
            "message": message,
            "data": {
                "errorCode": str(code), "messageKey": "stub.%s" % code,
                "retryable": retryable, "action": action, "requestId": "stub-request",
            },
        }, status)

    def _drop_connection(self):
        """Accept the request, then drop it without a response (lost response)."""
        try:
            self.connection.close()
        except Exception:
            pass

    def _fixture(self, name):
        path = os.path.join(FIXTURES, name)
        if not os.path.exists(path):
            raise FileNotFoundError(
                "%s is missing; run 'python3 myvocal_stub.py --make-fixtures'" % path)
        with open(path, "rb") as handle:
            return handle.read()

    # ------------------------------------------------------------------ routing

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path == "/_stats":
            # Test-only introspection: lets a validation run assert that recovery did not
            # create a second project or a second reservation.
            key = self.headers.get("accessKey") or "stub-happy"
            session = STORE.session(key)
            self._send_json({
                "projects": len(session["projects"]),
                "uploads": len(session["uploads"]),
                "jobs": len(session["jobs"]),
                "exports": len(session["exports"]),
                "quotes": len(session["quotes"]),
                "reservations": len(session["reservations"]),
                "reservationCharacters": sum(r.get("characters", 0) for r in session["reservations"]),
            })
            return

        if path.startswith("/_storage/"):
            self._storage(method, path)
            return

        body = self._read_body()
        api_key = self.headers.get("accessKey")
        scenario = scenario_of(api_key)

        if scenario == "stub-401":
            self._send_json({"code": 401, "message": "Key verification failed"})
            return
        if scenario == "stub-500":
            # Deliberately contradictory: HTTP 500 carrying a business code of 1.
            self._send_json({"code": 1, "message": "success", "data": {"projectId": "impossible"}}, 500)
            return

        session = STORE.session(api_key)
        try:
            if path.startswith(MUSIC):
                self._music(method, path[len(MUSIC):], body, session, scenario)
            elif path.startswith(INTERP):
                self._interpretation(method, path[len(INTERP):], body, session, scenario, query)
            else:
                self._send_json({"code": -1, "message": "stub: no route", "data": None}, 404)
        except Exception as failure:
            self._send_json({"code": -1, "message": "stub failure: %s" % failure, "data": None}, 500)

    def _storage(self, method, path):
        """Stands in for the presigned object-storage URL (no accessKey is sent here).

        The scenario is carried in the URL path so a single stub process can serve
        every scenario without being restarted.
        """
        if self.headers.get("accessKey"):
            self._send_json({"code": -1, "message": "stub: accessKey must not be sent to storage"}, 400)
            return
        parts = path.split("/")
        scenario = parts[2] if len(parts) > 2 and parts[2] else "stub-happy"
        rest = "/".join(parts[3:])
        if method == "PUT":
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b""
            self.send_response(200)
            self.send_header("ETag", '"%s"' % hashlib.md5(payload).hexdigest())
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "GET":
            if scenario == "stub-hang":
                time.sleep(120)  # makes the client's own request timeout the deciding factor
                return
            if scenario == "stub-forbidden" or rest.startswith("expired/"):
                self._send_json({"code": -1, "message": "stub: URL has expired"}, 403)
                return
            if scenario == "stub-html-media":
                # HTTP 200 with an HTML error page: must never be stored as media.
                self._send_bytes(b"<html><body>Not found</body></html>", "text/html")
                return
            if "wav" in rest:
                self._send_bytes(self._fixture("sample_source.wav"), "audio/wav")
                return
            self._send_bytes(self._fixture("sample.mp3"), "audio/mpeg")
            return
        self._send_json({"code": -1, "message": "stub: unsupported storage method"}, 405)

    # ------------------------------------------------------------------ music

    def _music(self, method, rest, body, session, scenario):
        if rest == "/capabilities" and method == "GET":
            self._success({
                "accessState": "ENABLED",
                "planKey": "PRO",
                "ratePerMinute": 1920,
                "rateVersion": "stub-music-v1",
                "balances": {"monthly": "10500", "additional": "0", "total": "10500"},
                "quoteTtlSeconds": 600,
                "supportedDurationsSec": [60, 90, 120, 180],
                "supportedVocalLanguages": [{"code": "en", "name": "English"}],
                "genres": ["POP", "ROCK", "HIP_HOP", "ELECTRONIC", "RNB_SOUL", "INDIE", "JAZZ",
                           "CINEMATIC", "FOLK_ACOUSTIC", "LATIN", "COUNTRY", "CLASSICAL"],
                "moods": ["UPLIFTING", "ENERGETIC", "ROMANTIC", "DREAMY", "MELANCHOLIC", "DARK",
                          "CALM", "HOPEFUL", "DRAMATIC", "PLAYFUL", "NOSTALGIC", "INTENSE"],
                "vocalStyles": ["WARM_INTIMATE", "BRIGHT_ENERGETIC", "POWERFUL_ANTHEMIC", "SOFT_BREATHY"],
                "lyricsModes": ["AUTO", "CUSTOM"],
            })
            return

        if rest == "/projects" and method == "POST":
            key = self.headers.get("Idempotency-Key")
            if not key:
                self._error(47001, "Idempotency-Key is required")
                return
            if body is None or not body.get("description"):
                self._error(47001, "description is required")
                return
            canonical = {"op": "CREATE_PROJECT", "brief": body}
            seen = session["idem"].get(key)
            if seen:
                if seen["canonical"] != canonical:
                    self._error(47008, "Idempotency key reused with a different body")
                    return
                # A replay re-reads the current resource, not the first response.
                self._success(self._music_detail(session, seen["resourceId"]))
                return
            project_id = STORE.next_id(session, "mup")
            job_id = STORE.next_id(session, "muj")
            session["projects"][project_id] = {
                "brief": body, "status": "ARRANGEMENT_GENERATING", "jobId": job_id,
                "arrangementVersion": 1, "polls": 0, "quoteId": None, "songPolls": 0,
            }
            session["jobs"][job_id] = {"jobId": job_id, "projectId": project_id,
                                       "jobType": "ARRANGEMENT", "status": "QUEUED"}
            session["idem"][key] = {"canonical": canonical, "resourceId": project_id}
            if self._maybe_drop(session, scenario, "create"):
                self._drop_connection()
                return
            self._success({
                "projectId": project_id, "projectStatus": "ARRANGEMENT_GENERATING",
                "job": {"jobId": job_id, "jobType": "ARRANGEMENT", "status": "QUEUED"},
                "requestId": "stub-request",
            })
            return

        if rest == "/projects" and method == "GET":
            self._success({"page": 1, "pageSize": 20, "total": len(session["projects"]),
                           "totalPage": 1, "list": [], "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/projects/([^/]+)", rest)
        if match and method == "GET":
            if match.group(1) not in session["projects"]:
                self._error(47009, "Project is not available")
                return
            self._success(self._music_detail(session, match.group(1)))
            return

        match = re.fullmatch(r"/jobs/([^/]+)", rest)
        if match and method == "GET":
            job = session["jobs"].get(match.group(1))
            if not job:
                self._error(47009, "Project is not available")
                return
            project = session["projects"][job["projectId"]]
            status = "READY" if project["status"] in ("ARRANGEMENT_READY", "READY") else "GENERATING"
            self._success({"jobId": job["jobId"], "projectId": job["projectId"],
                           "jobType": job["jobType"], "status": status,
                           "displayStage": "QUEUED" if status != "GENERATING" else "CREATING_MUSIC_AND_VOCALS",
                           "terminal": status == "READY", "failure": None, "attemptCount": None,
                           "createdAt": "2027-01-15T15:50:02", "updatedAt": "2027-01-15T15:56:40",
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/projects/([^/]+)/quotes", rest)
        if match and method == "POST":
            project = session["projects"].get(match.group(1))
            if not project:
                self._error(47009, "Project is not available")
                return
            if scenario == "stub-bignum":
                quoted, shortfall, remainder = BIG_QUOTE, "1", "0"
                balances = {"monthly": str(BIG_BALANCE), "additional": "0", "total": str(BIG_BALANCE)}
            else:
                quoted, shortfall, remainder = 2880, "0", "7620"
                balances = {"monthly": "10500", "additional": "0", "total": "10500"}
            quote_id = STORE.next_id(session, "muq")
            session["quotes"][quote_id] = {"quoteId": quote_id, "projectId": match.group(1),
                                           "quotedCharacters": quoted, "consumed": False}
            project["quoteId"] = quote_id
            self._success({
                "quoteId": quote_id, "projectId": match.group(1), "arrangementVersion": 1,
                "planKey": "PRO", "ratePerMinute": 1920, "rateVersion": "stub-music-v1",
                "durationSec": 90, "quotedCharacters": str(quoted),
                "expiresAt": "2027-01-15T16:00:00", "affordable": True, "shortfall": shortfall,
                "remainingAfterGeneration": remainder, "balances": balances,
                "requestId": "stub-request",
            })
            return

        match = re.fullmatch(r"/projects/([^/]+)/generations", rest)
        if match and method == "POST":
            project_id = match.group(1)
            project = session["projects"].get(project_id)
            if not project:
                self._error(47009, "Project is not available")
                return
            key = self.headers.get("Idempotency-Key")
            if not key:
                self._error(47001, "Idempotency-Key is required")
                return
            quote_id = (body or {}).get("quoteId")
            if not quote_id:
                self._error(47001, "quoteId is required")
                return
            # The key is bound to the exact request it was accepted for.
            canonical = {"op": "GENERATE", "projectId": project_id, "quoteId": quote_id}
            seen = session["idem"].get(key)
            if seen:
                if seen["canonical"] != canonical:
                    self._error(47008, "Idempotency key reused with a different body")
                    return
                job_id = seen["resourceId"]
                self._success(self._music_job_read_model(session, job_id))
                return
            quote = session["quotes"].get(quote_id)
            if quote is None or quote["projectId"] != project_id:
                self._error(47007, "Quote is no longer valid")
                return
            if quote["consumed"]:
                # The same quote with a different key is a conflict, like the real service.
                self._error(47007, "Quote is no longer valid")
                return
            job_id = STORE.next_id(session, "muj")
            quote["consumed"] = True
            project["status"] = "GENERATING"
            project["songPolls"] = 0
            session["jobs"][job_id] = {"jobId": job_id, "projectId": project_id,
                                       "jobType": "SONG", "status": "QUEUED"}
            session["idem"][key] = {"canonical": canonical, "resourceId": job_id}
            session["reservations"].append({"op": "MUSIC_GENERATE", "projectId": project_id,
                                            "jobId": job_id, "characters": quote["quotedCharacters"]})
            if self._maybe_drop(session, scenario, "generate"):
                self._drop_connection()
                return
            self._success({"projectId": project_id, "jobId": job_id, "jobType": "SONG",
                           "status": "QUEUED",
                           "reservedCharacters": str(quote["quotedCharacters"]),
                           "createdAt": "2027-01-15T15:52:11", "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/projects/([^/]+)/playback-url", rest)
        if match and method == "GET":
            self._success({"assetId": "ma_stub",
                           "url": "%s/_storage/%s/media" % (self._base(), scenario_of(self._api_key())),
                           "expiresAt": "2027-01-15T16:10:00", "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/projects/([^/]+)/download", rest)
        if match and method == "GET":
            project = session["projects"].get(match.group(1))
            if not project or project["status"] != "READY":
                self._error(47015, "Download is temporarily unavailable", "RETRY_LATER", True)
                return
            self._send_bytes(self._fixture("sample.mp3"), "audio/mpeg",
                             {"Content-Disposition": 'attachment; filename="MyVocal-Stub.mp3"'})
            return

        self._send_json({"code": -1, "message": "stub: no music route for %s" % rest, "data": None}, 404)

    def _maybe_drop(self, session, scenario, stage):
        # Keys like stub-drop-music / stub-drop-interp isolate the one-shot drop per product.
        if not scenario.startswith("stub-drop"):
            return False
        if any(k.startswith(stage + ":") for k in session["dropped"]):
            return False
        session["dropped"].add("%s:once" % stage)
        return True

    def _music_job_read_model(self, session, job_id):
        job = session["jobs"][job_id]
        project = session["projects"][job["projectId"]]
        status = "READY" if project["status"] == "READY" else "GENERATING"
        return {"jobId": job_id, "projectId": job["projectId"], "jobType": job["jobType"],
                "status": status,
                "displayStage": "FINALIZING_LIBRARY_ITEM" if status == "READY" else "CREATING_MUSIC_AND_VOCALS",
                "terminal": status == "READY", "failure": None, "attemptCount": None,
                "createdAt": "2027-01-15T15:52:11", "updatedAt": "2027-01-15T15:56:40",
                "requestId": "stub-request"}

    def _music_detail(self, session, project_id):
        project = session["projects"][project_id]
        if scenario_of(self._api_key()) != "stub-timeout":
            project["polls"] += 1
            if project["status"] == "ARRANGEMENT_GENERATING" and project["polls"] >= 1:
                project["status"] = "ARRANGEMENT_READY"
            elif project["status"] == "GENERATING":
                project["songPolls"] += 1
                if project["songPolls"] >= 2:
                    project["status"] = "READY"
        ready_asset = None
        if project["status"] == "READY":
            ready_asset = {"assetId": "ma_stub", "durationMillis": "90000",
                           "sampleRateHz": 44100, "mimeType": "audio/mpeg"}
        return {
            "projectId": project_id, "parentProjectId": None, "name": "Stub Song",
            "projectStatus": project["status"], "brief": project["brief"],
            "arrangementVersion": project["arrangementVersion"],
            "arrangement": {"durationSec": 90, "sections": []},
            "activeQuote": None,
            "activeJob": {"jobId": project["jobId"], "jobType": "SONG",
                          "status": "READY" if project["status"] == "READY" else "GENERATING",
                          "displayStage": "FINALIZING_LIBRARY_ITEM",
                          "terminal": project["status"] == "READY"},
            "readyAsset": ready_asset, "lastFailure": None,
            "createdAt": "2027-01-15T15:50:02", "updatedAt": "2027-01-15T15:56:40",
            "requestId": "stub-request",
        }

    def _api_key(self):
        return self.headers.get("accessKey")

    # ------------------------------------------------------------------ interpretation

    def _interpretation(self, method, rest, body, session, scenario, query):
        if rest == "/capabilities" and method == "GET":
            self._success({
                "productName": "Interpretation",
                "rateVersion": "stub-interp-v1",
                "languageCatalogVersion": "1",
                "languageCatalogState": "CONFIGURED",
                "formatCatalogVersion": "1",
                # Real field names: languageKey / displayName / sourceSupport.
                "languages": [{"languageKey": "es", "displayName": "Spanish", "sourceSupport": "UNVERIFIED"},
                              {"languageKey": "fr", "displayName": "French", "sourceSupport": "UNVERIFIED"}],
                "audioFormats": ["mp3", "wav", "m4a", "aac", "flac", "aiff", "ogg", "oga", "opus", "weba"],
                "videoFormats": ["mp4", "mov", "m4v", "mkv", "avi", "webm", "wmv", "mpeg", "mpg", "3gpp"],
                "exportFormats": ["mp3", "wav", "flac", "mp4"],
                "maxSourceBytes": "3221225472",
                "planRates": [{"planKey": "PRO", "charactersPerMinutePerLanguage": 4000}],
                "quoteTtlSeconds": 600,
                "account": {"planKey": "PRO", "accessState": "ENABLED", "entitlementVersion": "1",
                            "charactersPerMinutePerLanguage": 4000, "availableCharacters": "30000",
                            "balanceState": "AVAILABLE"},
                "stageAvailability": {"project": "AVAILABLE", "upload": "AVAILABLE", "export": "AVAILABLE"},
            })
            return

        if rest == "/projects" and method == "POST":
            key = self.headers.get("Idempotency-Key")
            if not key:
                self._error(47101, "Idempotency-Key is required")
                return
            if body is None or not body.get("name"):
                self._error(47101, "name is required")
                return
            canonical = {"op": "CREATE_PROJECT", "body": body}
            seen = session["idem"].get(key)
            if seen:
                if seen["canonical"] != canonical:
                    self._error(47111, "Idempotency key reused with a different body")
                    return
                project = session["projects"][seen["resourceId"]]
                self._success({"projectId": seen["resourceId"], "state": project["state"],
                               "settingsVersion": project["settingsVersion"],
                               "requestId": "stub-request"})
                return
            project_id = STORE.next_id(session, "ip")
            session["projects"][project_id] = {
                "name": body["name"], "state": "DRAFT", "settingsVersion": 1,
                "sourceLanguage": body.get("sourceLanguage"),
                "targetLanguages": body.get("targetLanguages") or [],
                "keyterms": [], "cloningStrength": None, "durationMs": "0",
                "sourceState": "DRAFT", "sourceReady": False, "uploadId": None,
                "targets": {}, "generationPolls": 0,
            }
            session["idem"][key] = {"canonical": canonical, "resourceId": project_id}
            if self._maybe_drop(session, scenario, "create"):
                self._drop_connection()
                return
            self._success({"projectId": project_id, "state": "DRAFT", "settingsVersion": 1,
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/projects/([^/]+)/draft", rest)
        if match and method == "PATCH":
            project = session["projects"].get(match.group(1))
            if not project:
                self._error(47102, "Project is not available")
                return
            body = body or {}
            if body.get("sourceLanguage") is not None:
                text = str(body["sourceLanguage"]).strip()
                project["sourceLanguage"] = None if text in ("", "AUTO") else text
            if body.get("keyterms") is not None:
                project["keyterms"] = list(body["keyterms"])
            if body.get("cloningStrength") is not None:
                project["cloningStrength"] = body["cloningStrength"]
            if body.get("targetLanguages") is not None:
                project["targetLanguages"] = list(body["targetLanguages"])
            project["settingsVersion"] += 1
            self._success(self._draft(session, match.group(1)))
            return

        match = re.fullmatch(r"/projects/([^/]+)", rest)
        if match and method == "GET":
            if match.group(1) not in session["projects"]:
                self._error(47102, "Project is not available")
                return
            self._success(self._detail(session, match.group(1)))
            return

        match = re.fullmatch(r"/projects/([^/]+)/uploads", rest)
        if match and method == "POST":
            project_id = match.group(1)
            project = session["projects"].get(project_id)
            if not project:
                self._error(47102, "Project is not available")
                return
            body = body or {}
            if not body.get("filename"):
                self._error(47101, "filename is required")
                return
            size = body.get("size")
            if not isinstance(size, int) or size <= 0:
                self._error(47125, "File must not be empty and must not exceed the limit")
                return
            upload_id = STORE.next_id(session, "iu")
            part_size = 16 * 1024 * 1024
            total_parts = max(1, (size + part_size - 1) // part_size)
            session["uploads"][upload_id] = {"uploadId": upload_id, "projectId": project_id,
                                             "declaredSize": size, "partSizeBytes": part_size,
                                             "totalParts": total_parts, "state": "CREATED",
                                             "parts": {}, "actualSize": None}
            project["uploadId"] = upload_id
            self._success({"uploadId": upload_id, "projectId": project_id, "state": "CREATED",
                           "declaredSize": str(size), "partSizeBytes": part_size,
                           "totalParts": total_parts, "expiresAt": "2027-01-16T15:52:11",
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/uploads/([^/]+)/parts", rest)
        if match and method == "POST":
            upload = session["uploads"].get(match.group(1))
            if not upload:
                self._error(47124, "Upload session is not available")
                return
            # Real service (InterpretationUploadService.signPart): signing a part of an
            # upload that is no longer CREATED/UPLOADING is UPLOAD_PART_INVALID (47126).
            if upload["state"] not in ("CREATED", "UPLOADING"):
                self._error(47126, "One or more uploaded parts do not match this session")
                return
            upload["state"] = "UPLOADING"
            part_number = (body or {}).get("partNumber")
            if not isinstance(part_number, int) or not 1 <= part_number <= upload["totalParts"]:
                self._error(47126, "Part does not match the session")
                return
            self._success({"uploadId": upload["uploadId"], "partNumber": part_number,
                           "method": "PUT",
                           "url": "%s/_storage/%s/%s/%d" % (self._base(), scenario_of(self._api_key()),
                                                               upload["uploadId"], part_number),
                           "requiredHeaders": {}, "expiresAt": "2027-01-15T16:07:11",
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/uploads/([^/]+)/complete", rest)
        if match and method == "POST":
            upload = session["uploads"].get(match.group(1))
            if not upload:
                self._error(47124, "Upload session is not available")
                return
            if upload["state"] == "READY":
                # Real complete/claim return the terminal response when the upload is already
                # READY (InterpretationUploadService.complete -> claim().terminal), so a lost
                # complete response is recoverable by replaying it.
                self._success(self._upload_ready_response(upload))
                return
            parts = (body or {}).get("parts")
            if not isinstance(parts, list) or len(parts) != upload["totalParts"]:
                self._error(47126, "Part does not match the session")
                return
            upload["state"] = "READY"
            upload["actualSize"] = upload["declaredSize"]
            project = session["projects"][upload["projectId"]]
            project["sourceReady"] = True
            project["sourceState"] = "FROZEN"
            project["durationMs"] = "90000"
            project["state"] = "PROCESSING"
            self._success(self._upload_ready_response(upload))
            return

        match = re.fullmatch(r"/uploads/([^/]+)", rest)
        if match and method == "GET":
            upload = session["uploads"].get(match.group(1))
            if not upload:
                self._error(47124, "Upload session is not available")
                return
            self._success({"uploadId": upload["uploadId"], "projectId": upload["projectId"],
                           "state": upload["state"], "declaredSize": str(upload["declaredSize"]),
                           "actualSize": None if upload["actualSize"] is None else str(upload["actualSize"]),
                           "uploadedBytes": str(sum(upload["parts"].values())),
                           "partSizeBytes": upload["partSizeBytes"], "totalParts": upload["totalParts"],
                           "uploadedParts": [{"partNumber": n, "etag": e, "size": str(s)}
                                             for n, (e, s) in sorted(upload["parts"].items())],
                           "errorCode": None,
                           "sourceAssetId": "ia_stub" if upload["state"] == "READY" else None,
                           "sourceVersion": 1, "durationMs": "90000",
                           "media": self._media() if upload["state"] == "READY" else None,
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/projects/([^/]+)/quotes", rest)
        if match and method == "POST":
            project = session["projects"].get(match.group(1))
            if not project:
                self._error(47102, "Project is not available")
                return
            if not project["sourceReady"]:
                self._error(47103, "Source is not ready")
                return
            requested = (body or {}).get("targetLanguages")
            if not requested:
                self._error(47101, "targetLanguages must not be empty")
                return
            existing = [code for code in requested if code in project["targets"]]
            new = [code for code in requested if code not in project["targets"]]
            # `targets` is the newTargets alias, never a union with existingTargets.
            new_dtos = [{"language": code, "characters": "6000"} for code in new]
            if not new:
                self._success({"quoteId": None, "projectId": match.group(1), "settingsVersion": 1,
                               "sourceDurationMs": "90000", "planKey": "PRO",
                               "rateVersion": "stub-interp-v1", "charactersPerMinutePerLanguage": 4000,
                               "perTargetCharacters": None, "requestedLanguages": requested,
                               "existingTargets": [{"language": code, "characters": "6000",
                                                    "targetId": project["targets"][code]["targetId"],
                                                    "state": project["targets"][code]["state"],
                                                    "action": "NONE"} for code in existing],
                               "newTargets": [], "targets": [], "totalCharacters": "0",
                               "availableCharacters": "30000", "estimatedRemainingCharacters": "30000",
                               "balanceState": "AVAILABLE", "state": "ALL_TARGETS_EXIST",
                               "expiresAt": None, "requestId": "stub-request"})
                return
            quote_id = STORE.next_id(session, "iq")
            session["quotes"][quote_id] = {"quoteId": quote_id, "new": new,
                                           "acceptedAcceptanceId": None}
            project["quoteId"] = quote_id
            project["quoteLanguages"] = new
            self._success({"quoteId": quote_id, "projectId": match.group(1), "settingsVersion": 1,
                           "sourceDurationMs": "90000", "planKey": "PRO",
                           "rateVersion": "stub-interp-v1", "charactersPerMinutePerLanguage": 4000,
                           "perTargetCharacters": "6000", "requestedLanguages": requested,
                           "existingTargets": [], "newTargets": new_dtos, "targets": new_dtos,
                           "totalCharacters": str(6000 * len(new)), "availableCharacters": "30000",
                           "estimatedRemainingCharacters": str(30000 - 6000 * len(new)),
                           "balanceState": "AVAILABLE", "state": "ACTIVE",
                           "expiresAt": "2027-01-15T16:05:00", "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/projects/([^/]+)/targets/([^/]+)/retry-plan", rest)
        if match and method == "GET":
            project = session["projects"].get(match.group(1))
            target_ids = [t["targetId"] for t in project["targets"].values()] if project else []
            if not project or match.group(2) not in target_ids:
                self._error(47117, "Target is not available")
                return
            language = next(code for code, t in project["targets"].items() if t["targetId"] == match.group(2))
            self._success({"targetId": match.group(2), "language": language, "generationId": "ia_stub",
                           "nextReservationCycle": 2, "characters": "6000",
                           "rateVersion": "stub-interp-v1", "retryable": True,
                           "reason": "PREVIOUS_ATTEMPT_RELEASED", "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/projects/([^/]+)/generations", rest)
        if match and method == "POST":
            project_id = match.group(1)
            project = session["projects"].get(project_id)
            if not project:
                self._error(47102, "Project is not available")
                return
            key = self.headers.get("Idempotency-Key")
            if not key:
                self._error(47101, "Idempotency-Key is required")
                return
            quote_id = (body or {}).get("quoteId")
            if not quote_id:
                self._error(47101, "quoteId is required")
                return
            canonical = {"op": "GENERATE", "projectId": project_id, "quoteId": quote_id}
            seen = session["idem"].get(key)
            if seen:
                if seen["canonical"] != canonical:
                    self._error(47111, "Idempotency key reused with a different body")
                    return
                self._success(self._acceptance(session, seen["resourceId"]))
                return
            quote = session["quotes"].get(quote_id)
            if quote is None:
                self._error(47109, "Quote is no longer valid")
                return
            if quote["acceptedAcceptanceId"]:
                # Same quote presented with a different key: reuse, never charge twice.
                self._success(self._acceptance(session, quote["acceptedAcceptanceId"]))
                return
            acceptance = STORE.next_id(session, "ia")
            targets = []
            for code in quote["new"]:
                target_id = STORE.next_id(session, "it")
                job_id = STORE.next_id(session, "ij")
                project["targets"][code] = {"targetId": target_id, "state": "QUEUED",
                                            "jobId": job_id, "language": code}
                session["jobs"][job_id] = {"jobId": job_id, "projectId": project_id,
                                           "targetId": target_id, "language": code, "state": "QUEUED"}
                targets.append({"targetId": target_id, "language": code, "generationId": acceptance,
                                "state": "QUEUED", "jobId": job_id, "reservationCycle": 1,
                                "reservedCharacters": "6000"})
            quote["acceptedAcceptanceId"] = acceptance
            project["generationPolls"] = 0
            session["acceptances"][acceptance] = project_id
            session["idem"][key] = {"canonical": canonical, "resourceId": acceptance}
            session["reservations"].append({"op": "INTERPRETATION_GENERATE", "projectId": project_id,
                                            "acceptanceId": acceptance,
                                            "languages": list(quote["new"]),
                                            "characters": 6000 * len(quote["new"])})
            if self._maybe_drop(session, scenario, "generate"):
                self._drop_connection()
                return
            self._success({"acceptanceId": acceptance, "generationId": acceptance,
                           "projectId": project_id, "quoteId": quote_id, "state": "ACCEPTED",
                           "targets": targets, "existingTargets": [],
                           "newTargets": [{"language": code, "characters": "6000"} for code in quote["new"]],
                           "reservedCharacters": str(6000 * len(targets)), "reservationCycle": 1,
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/jobs/([^/]+)", rest)
        if match and method == "GET":
            job = session["jobs"].get(match.group(1))
            if not job:
                self._error(47117, "Target is not available")
                return
            self._success({"jobId": job["jobId"], "projectId": job["projectId"],
                           "targetId": job.get("targetId"), "stage": "FETCH_AUDIO",
                           "state": job["state"], "nextPollAfterMs": None,
                           "errorCode": job.get("errorCode"), "billingState": "RESERVED",
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/assets/([^/]+)/playback", rest)
        if match and method == "GET":
            self._success({"assetId": match.group(1), "state": "READY", "mimeType": "audio/mpeg",
                           "url": "%s/_storage/%s/media" % (self._base(), scenario_of(self._api_key())),
                           "expiresAt": "2027-01-15T16:10:00",
                           "assetVersion": "1", "rangeSupported": True, "renewalAttemptLimit": 1,
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/targets/([^/]+)/exports", rest)
        if match and method == "POST":
            export_id = STORE.next_id(session, "ie")
            session["exports"][export_id] = {"exportId": export_id, "targetId": match.group(1),
                                             "format": (body or {}).get("format") or "wav", "polls": 0}
            self._success({"exportId": export_id, "targetId": match.group(1),
                           "format": (body or {}).get("format") or "wav", "state": "PROCESSING",
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/exports/([^/]+)/download", rest)
        if match and method == "GET":
            record = session["exports"].get(match.group(1))
            if not record:
                self._error(47117, "Target is not available")
                return
            record["polls"] += 1
            state = "READY"
            if scenario == "stub-export-retry" and record["polls"] == 1:
                state = "PROCESSING"
            elif scenario == "stub-export-retry" and record["polls"] == 2:
                state = "RETRY"
            if state != "READY":
                self._success({"exportId": record["exportId"], "format": record["format"],
                               "state": state, "mimeType": None, "url": None, "expiresAt": None,
                               "assetVersion": None, "rangeSupported": False,
                               "renewalAttemptLimit": 1, "requestId": "stub-request"})
                return
            expired = scenario == "stub-expired-url" and record["polls"] == 1
            if expired:
                url = "%s/_storage/%s/expired/%s" % (self._base(), scenario, record["exportId"])
            else:
                url = "%s/_storage/%s/wav/%s" % (self._base(), scenario, record["exportId"])
            self._success({"exportId": record["exportId"], "format": record["format"], "state": "READY",
                           "mimeType": "audio/wav", "url": url,
                           "expiresAt": "2027-01-15T16:20:00", "assetVersion": "1",
                           "rangeSupported": True, "renewalAttemptLimit": 1,
                           "requestId": "stub-request"})
            return

        self._send_json({"code": -1, "message": "stub: no interpretation route for %s" % rest,
                         "data": None}, 404)

    def _acceptance(self, session, acceptance_id):
        project_id = session["acceptances"][acceptance_id]
        project = session["projects"][project_id]
        targets = [{"targetId": t["targetId"], "language": code, "generationId": acceptance_id,
                    "state": t["state"], "jobId": t["jobId"], "reservationCycle": 1,
                    "reservedCharacters": "6000"} for code, t in project["targets"].items()]
        return {"acceptanceId": acceptance_id, "generationId": acceptance_id, "projectId": project_id,
                "quoteId": project.get("quoteId"), "state": "ACCEPTED", "targets": targets,
                "existingTargets": [], "newTargets": [],
                "reservedCharacters": str(6000 * len(targets)), "reservationCycle": 1,
                "requestId": "stub-request"}

    def _draft(self, session, project_id):
        project = session["projects"][project_id]
        return {"projectId": project_id, "settingsVersion": project["settingsVersion"],
                "state": project["state"], "sourceLanguage": project["sourceLanguage"],
                "keyterms": project["keyterms"], "cloningStrength": project["cloningStrength"],
                "targetLanguages": project["targetLanguages"], "sourceDurationMs": project["durationMs"],
                "sourceState": project["sourceState"], "requestId": "stub-request"}

    def _media(self):
        return {"inputFormat": "wav", "containerName": "wav", "audioStreamCount": 1,
                "video": False, "width": None, "height": None, "rotationDegrees": None}

    def _upload_ready_response(self, upload):
        """The terminal CompleteUploadResponse for an upload that is READY."""
        return {"uploadId": upload["uploadId"], "projectId": upload["projectId"],
                "state": "READY", "errorCode": None, "sourceAssetId": "ia_stub",
                "sourceVersion": 1, "durationMs": "90000",
                "media": self._media(), "requestId": "stub-request"}

    def _detail(self, session, project_id):
        project = session["projects"][project_id]
        scenario = scenario_of(self._api_key())
        if project["targets"] and project["generationPolls"] < 3:
            project["generationPolls"] += 1
            for index, code in enumerate(list(project["targets"].keys())):
                target = project["targets"][code]
                failed = (scenario == "stub-all-failed"
                          or (scenario == "stub-partial" and index == 1))
                if failed:
                    target["state"] = "FAILED_RELEASED"
                    session["jobs"][target["jobId"]]["state"] = "RELEASED"
                    session["jobs"][target["jobId"]]["errorCode"] = "47118"
                else:
                    target["state"] = "READY"
                    session["jobs"][target["jobId"]]["state"] = "READY"
        targets = [{"targetId": t["targetId"], "language": code, "state": t["state"],
                    "characters": "6000",
                    "billingState": "SETTLED" if t["state"] == "READY" else "RELEASED",
                    "action": "RETRY" if t["state"] == "FAILED_RELEASED" else "NONE",
                    "reservationCycle": 1,
                    "outputAssetId": "ia_out_%s" % code if t["state"] == "READY" else None}
                   for code, t in project["targets"].items()]
        states = [t["state"] for t in targets]
        if not states:
            summary = "DRAFT"
        elif all(s == "READY" for s in states):
            summary = "READY"
        elif all(s == "FAILED_RELEASED" for s in states):
            summary = "FAILED"
        elif any(s == "READY" for s in states):
            summary = "PARTIAL_READY"
        else:
            summary = "PROCESSING"
        project["state"] = summary if states else "DRAFT"
        source = None
        if project["sourceReady"]:
            source = {"version": 1, "state": "FROZEN", "sourceLanguage": project["sourceLanguage"],
                      "durationMs": project["durationMs"], "sourceAssetId": "ia_stub",
                      "cloningStrength": project["cloningStrength"], "keyterms": project["keyterms"],
                      "media": self._media(),
                      "availableExportFormats": ["mp3", "wav", "flac", "mp4"]}
        return {"projectId": project_id, "name": project["name"], "state": project["state"],
                "summaryState": summary, "sourceLanguage": project["sourceLanguage"],
                "sourceDurationMs": project["durationMs"], "settingsVersion": project["settingsVersion"],
                "keyterms": project["keyterms"], "cloningStrength": project["cloningStrength"],
                "draftTargetLanguages": project["targetLanguages"], "source": source,
                "targets": targets,
                "jobs": [{"jobId": j["jobId"], "projectId": j["projectId"], "targetId": j.get("targetId"),
                          "stage": "FETCH_AUDIO", "state": j["state"], "nextPollAfterMs": None,
                          "errorCode": j.get("errorCode"), "billingState": "RESERVED"}
                         for j in session["jobs"].values() if j.get("projectId") == project_id],
                "createdAt": "2027-01-15T15:50:02", "updatedAt": "2027-01-15T15:57:12",
                "requestId": "stub-request"}

    def _base(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]


def make_fixtures(duration_seconds=1, sample_rate=8000):
    """Write a small deterministic WAV (and an MP3 transcode when ffmpeg exists)."""
    import struct
    import subprocess

    os.makedirs(FIXTURES, exist_ok=True)
    frames = duration_seconds * sample_rate
    data = struct.pack("<%dh" % frames, *([0] * frames))
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    wav_path = os.path.join(FIXTURES, "sample_source.wav")
    with open(wav_path, "wb") as handle:
        handle.write(header + data)

    mp3_path = os.path.join(FIXTURES, "sample.mp3")
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=%d:cl=mono" % sample_rate, "-t", str(duration_seconds),
                        mp3_path], check=True)
    except Exception:
        with open(mp3_path, "wb") as handle:
            handle.write(header + data)
    return wav_path, mp3_path


def main():
    parser = argparse.ArgumentParser(description="MyVocal public API local stub")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--storage-scenario", default="")
    parser.add_argument("--make-fixtures", action="store_true")
    args = parser.parse_args()

    if args.make_fixtures:
        print("fixtures: %s" % (make_fixtures(),))
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.storage_scenario = args.storage_scenario
    print("stub listening on http://127.0.0.1:%d" % args.port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
