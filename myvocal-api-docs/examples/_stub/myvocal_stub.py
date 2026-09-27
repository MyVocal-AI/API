#!/usr/bin/env python3
"""Deterministic local stub of the MyVocal public API for running the examples.

It is a development and validation aid: it reproduces the documented request and
response shapes of the Text-to-Music and Interpretation endpoints so the example
clients can be exercised end to end without consuming Characters, without a real
API key and without calling any provider.

The behaviour is selected by the API key value, so the example scripts stay
realistic and only read the documented environment variables:

    stub-happy         normal happy path (default for any other value)
    stub-401           every call returns HTTP 200 with {"code":401,...}
    stub-bignum        Characters values above 2^53 (exactness check)
    stub-timeout       the arrangement never becomes ready
    stub-partial       Interpretation: one target ready, one failed_released
    stub-export-retry  Interpretation: the export returns RETRY before READY
    stub-expired-url   Interpretation: the first download URL has expired
    stub-replay        Music: a create replay returns the project detail

Usage:
    python3 myvocal_stub.py --port 8765
"""

import argparse
import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

MUSIC = "/sound_clone/api/v1/music"
INTERP = "/sound_clone/api/v1/interpretation"

# Characters values chosen so that a double cannot tell them apart, while exact
# 64-bit integers can: 9007199254740993 and 9007199254740992 both round to the same
# IEEE-754 double, so a float-based client would consider them equal.
BIG_QUOTE = 9007199254740993
BIG_BALANCE = 9007199254740992
BIG_TOTAL = 9007199254740992


class Store:
    """In-memory per-API-key state, guarded by one lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_key = {}

    def session(self, key):
        with self.lock:
            return self.by_key.setdefault(key, {
                "projects": {},
                "uploads": {},
                "jobs": {},
                "exports": {},
                "seen_keys": {},
                "polls": {},
                "seq": 0,
            })

    def next_id(self, session, prefix):
        with self.lock:
            session["seq"] += 1
            return "%s_%06d" % (prefix, session["seq"])


STORE = Store()


def scenario_of(api_key):
    return (api_key or "").strip() or "stub-happy"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):
        if os.environ.get("STUB_VERBOSE"):
            print("[stub] " + (fmt % args))

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def _api_key(self):
        return self.headers.get("accessKey")

    def _send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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

    def _error(self, code, message, action="CONTACT_SUPPORT", retryable=False):
        self._send_json({
            "code": code,
            "message": message,
            "data": {
                "errorCode": str(code),
                "messageKey": "stub.%s" % code,
                "retryable": retryable,
                "action": action,
                "requestId": "stub-request",
            },
        })

    def _reject_if_unauthenticated(self):
        if scenario_of(self._api_key()) == "stub-401":
            # Documented authentication failure: HTTP 200, code 401, no data.
            self._send_json({"code": 401, "message": "Key verification failed"})
            return True
        return False

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

        # Object storage is a separate endpoint: it must not consume the JSON body path.
        if path.startswith("/_storage/"):
            self._storage(method, path)
            return

        body = self._read_body()
        if self._reject_if_unauthenticated():
            return

        session = STORE.session(self._api_key())
        scenario = scenario_of(self._api_key())

        try:
            if path.startswith(MUSIC):
                self._music(method, path[len(MUSIC):], body, session, scenario)
            elif path.startswith(INTERP):
                self._interpretation(method, path[len(INTERP):], body, session, scenario, query)
            else:
                self._send_json({"code": -1, "message": "stub: no route", "data": None}, 404)
        except Exception as failure:  # a stub must never crash the example run
            self._send_json({"code": -1, "message": "stub failure: %s" % failure, "data": None}, 500)

    def _storage(self, method, path):
        """Stands in for the presigned object-storage URL (no accessKey is sent here)."""
        if self.headers.get("accessKey"):
            self._send_json({"code": -1, "message": "stub: accessKey must not be sent to storage"}, 400)
            return
        if method == "PUT":
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b""
            etag = hashlib.md5(payload).hexdigest()
            self.send_response(200)
            self.send_header("ETag", '"%s"' % etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "GET":
            if path.startswith("/_storage/expired/"):
                self._send_json({"code": -1, "message": "stub: URL has expired"}, 403)
                return
            if "/wav/" in path:
                self._send_bytes(self._fixture("sample_source.wav"), "audio/wav")
                return
            self._send_bytes(self._fixture("sample.mp3"), "audio/mpeg")
            return
        self._send_json({"code": -1, "message": "stub: unsupported storage method"}, 405)

    # ------------------------------------------------------------------ music

    def _music(self, method, rest, body, session, scenario):
        if rest == "/capabilities" and method == "GET":
            self._success({
                "accessState": "AVAILABLE",
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
            if key in session["seen_keys"]:
                if session["seen_keys"][key] != json.dumps(body, sort_keys=True):
                    self._error(47008, "Idempotency key reused with a different body")
                    return
                project_id = session["seen_keys"][key + ":project"]
                # A replay re-reads the current resource instead of replaying the first response.
                self._success(self._music_detail(session, project_id))
                return
            project_id = STORE.next_id(session, "mup")
            job_id = STORE.next_id(session, "muj")
            session["projects"][project_id] = {
                "brief": body, "status": "ARRANGEMENT_GENERATING", "jobId": job_id,
                "arrangementVersion": 1, "polls": 0, "quoteId": None, "songPolls": 0,
            }
            session["seen_keys"][key] = json.dumps(body, sort_keys=True)
            session["seen_keys"][key + ":project"] = project_id
            session["jobs"][job_id] = {"jobId": job_id, "projectId": project_id,
                                       "jobType": "ARRANGEMENT", "status": "QUEUED"}
            self._success({
                "projectId": project_id,
                "projectStatus": "ARRANGEMENT_GENERATING",
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
            project_id = match.group(1)
            if project_id not in session["projects"]:
                self._error(47009, "Project is not available")
                return
            self._success(self._music_detail(session, project_id))
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
            project_id = match.group(1)
            project = session["projects"].get(project_id)
            if not project:
                self._error(47009, "Project is not available")
                return
            if scenario == "stub-bignum":
                rate, quoted, shortfall, remainder = 1920, BIG_QUOTE, "1", "0"
                balances = {"monthly": str(BIG_BALANCE), "additional": "0", "total": str(BIG_TOTAL)}
            else:
                rate = 1920
                quoted = 2880
                shortfall = "0"
                remainder = "7620"
                balances = {"monthly": "10500", "additional": "0", "total": "10500"}
            quote_id = STORE.next_id(session, "muq")
            project["quoteId"] = quote_id
            self._success({
                "quoteId": quote_id, "projectId": project_id, "arrangementVersion": 1,
                "planKey": "PRO", "ratePerMinute": rate, "rateVersion": "stub-music-v1",
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
            if body is None or not body.get("quoteId"):
                self._error(47001, "quoteId is required")
                return
            job_id = STORE.next_id(session, "muj")
            project["status"] = "GENERATING"
            project["songPolls"] = 0
            session["jobs"][job_id] = {"jobId": job_id, "projectId": project_id,
                                       "jobType": "SONG", "status": "QUEUED"}
            self._success({"projectId": project_id, "jobId": job_id, "jobType": "SONG",
                           "status": "QUEUED",
                           "reservedCharacters": "2880" if scenario != "stub-bignum" else str(BIG_QUOTE),
                           "createdAt": "2027-01-15T15:52:11", "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/projects/([^/]+)/playback-url", rest)
        if match and method == "GET":
            self._success({"assetId": "ma_stub", "url": self._base() + "/_storage/fixture",
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
            "projectId": project_id,
            "parentProjectId": None,
            "name": "Stub Song",
            "projectStatus": project["status"],
            "brief": project["brief"],
            "arrangementVersion": project["arrangementVersion"],
            "arrangement": {"durationSec": 90, "sections": []},
            "activeQuote": None,
            "activeJob": {"jobId": project["jobId"], "jobType": "SONG",
                          "status": "READY" if project["status"] == "READY" else "GENERATING",
                          "displayStage": "FINALIZING_LIBRARY_ITEM",
                          "terminal": project["status"] == "READY"},
            "readyAsset": ready_asset,
            "lastFailure": None,
            "createdAt": "2027-01-15T15:50:02",
            "updatedAt": "2027-01-15T15:56:40",
            "requestId": "stub-request",
        }

    # ------------------------------------------------------------------ interpretation

    def _interpretation(self, method, rest, body, session, scenario, query):
        if rest == "/capabilities" and method == "GET":
            self._success({
                "productName": "Interpretation",
                "rateVersion": "stub-interp-v1",
                "languageCatalogVersion": "1",
                "languageCatalogState": "CONFIGURED",
                "formatCatalogVersion": "1",
                "languages": [{"code": "es", "name": "Spanish", "sourceSupport": "SUPPORTED"},
                              {"code": "fr", "name": "French", "sourceSupport": "SUPPORTED"}],
                "audioFormats": ["mp3", "wav", "m4a", "aac", "flac", "aiff", "ogg", "oga", "opus", "weba"],
                "videoFormats": ["mp4", "mov", "m4v", "mkv", "avi", "webm", "wmv", "mpeg", "mpg", "3gpp"],
                "exportFormats": ["mp3", "wav", "flac", "mp4"],
                "maxSourceBytes": "3221225472",
                "planRates": [{"planKey": "PRO", "charactersPerMinutePerLanguage": 4000}],
                "quoteTtlSeconds": 600,
                "account": {"planKey": "PRO", "accessState": "AVAILABLE", "entitlementVersion": "1",
                            "charactersPerMinutePerLanguage": 4000, "availableCharacters": "30000",
                            "balanceState": "OK"},
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
            if key in session["seen_keys"]:
                if session["seen_keys"][key] != json.dumps(body, sort_keys=True):
                    self._error(47111, "Idempotency key reused with a different body")
                    return
                project_id = session["seen_keys"][key + ":project"]
                project = session["projects"][project_id]
                self._success({"projectId": project_id, "state": project["state"],
                               "settingsVersion": project["settingsVersion"],
                               "requestId": "stub-request"})
                return
            project_id = STORE.next_id(session, "ip")
            session["projects"][project_id] = {
                "name": body["name"], "state": "DRAFT", "settingsVersion": 1,
                "sourceLanguage": body.get("sourceLanguage"),
                "targetLanguages": body.get("targetLanguages") or [],
                "keyterms": [], "cloningStrength": None, "durationMs": "0",
                "sourceState": "DRAFT", "sourceReady": False,
                "uploadId": None, "targets": {}, "generationPolls": 0,
            }
            session["seen_keys"][key] = json.dumps(body, sort_keys=True)
            session["seen_keys"][key + ":project"] = project_id
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
            project = session["projects"].get(match.group(1))
            if not project:
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
            part_number = (body or {}).get("partNumber")
            if not isinstance(part_number, int) or not 1 <= part_number <= upload["totalParts"]:
                self._error(47126, "Part does not match the session")
                return
            self._success({"uploadId": upload["uploadId"], "partNumber": part_number,
                           "method": "PUT",
                           "url": "%s/_storage/%s/%d" % (self._base(), upload["uploadId"], part_number),
                           "requiredHeaders": {}, "expiresAt": "2027-01-15T16:07:11",
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/uploads/([^/]+)/complete", rest)
        if match and method == "POST":
            upload = session["uploads"].get(match.group(1))
            if not upload:
                self._error(47124, "Upload session is not available")
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
            self._success({"uploadId": upload["uploadId"], "projectId": upload["projectId"],
                           "state": "READY", "errorCode": None, "sourceAssetId": "ia_stub",
                           "sourceVersion": 1, "durationMs": "90000",
                           "media": self._media(), "requestId": "stub-request"})
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
                           "errorCode": None, "sourceAssetId": "ia_stub" if upload["state"] == "READY" else None,
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
                               "balanceState": "OK", "state": "ALL_TARGETS_EXIST", "expiresAt": None,
                               "requestId": "stub-request"})
                return
            quote_id = STORE.next_id(session, "iq")
            project["quoteId"] = quote_id
            project["quoteLanguages"] = new
            self._success({"quoteId": quote_id, "projectId": match.group(1), "settingsVersion": 1,
                           "sourceDurationMs": "90000", "planKey": "PRO",
                           "rateVersion": "stub-interp-v1", "charactersPerMinutePerLanguage": 4000,
                           "perTargetCharacters": "6000", "requestedLanguages": requested,
                           "existingTargets": [], "newTargets": [{"language": code, "characters": "6000"}
                                                                 for code in new],
                           "targets": [{"language": code, "characters": "6000"} for code in new],
                           "totalCharacters": str(6000 * len(new)), "availableCharacters": "30000",
                           "estimatedRemainingCharacters": str(30000 - 6000 * len(new)),
                           "balanceState": "OK", "state": "ACTIVE", "expiresAt": "2027-01-15T16:05:00",
                           "requestId": "stub-request"})
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
            project = session["projects"].get(match.group(1))
            if not project:
                self._error(47102, "Project is not available")
                return
            if not (body or {}).get("quoteId"):
                self._error(47101, "quoteId is required")
                return
            acceptance = STORE.next_id(session, "ia")
            targets = []
            for code in project.get("quoteLanguages", []):
                target_id = STORE.next_id(session, "it")
                job_id = STORE.next_id(session, "ij")
                project["targets"][code] = {"targetId": target_id, "state": "QUEUED",
                                            "jobId": job_id, "language": code}
                session["jobs"][job_id] = {"jobId": job_id, "projectId": match.group(1),
                                           "targetId": target_id, "language": code, "state": "QUEUED"}
                targets.append({"targetId": target_id, "language": code, "generationId": acceptance,
                                "state": "QUEUED", "jobId": job_id, "reservationCycle": 1,
                                "reservedCharacters": "6000"})
            project["generationPolls"] = 0
            self._success({"acceptanceId": acceptance, "generationId": acceptance,
                           "projectId": match.group(1), "quoteId": project["quoteId"],
                           "state": "ACCEPTED", "targets": targets, "existingTargets": [],
                           "newTargets": [{"language": code, "characters": "6000"}
                                          for code in project.get("quoteLanguages", [])],
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
                           "url": self._base() + "/_storage/fixture", "expiresAt": "2027-01-15T16:10:00",
                           "assetVersion": "1", "rangeSupported": True, "renewalAttemptLimit": 3,
                           "requestId": "stub-request"})
            return

        match = re.fullmatch(r"/targets/([^/]+)/exports", rest)
        if match and method == "POST":
            export_id = STORE.next_id(session, "ie")
            session["exports"][export_id] = {"exportId": export_id, "targetId": match.group(1),
                                             "format": (body or {}).get("format") or "wav",
                                             "polls": 0}
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
                               "renewalAttemptLimit": 3, "requestId": "stub-request"})
                return
            expired = scenario == "stub-expired-url" and record["polls"] == 1
            url = self._base() + ("/_storage/expired/%s" % record["exportId"] if expired
                                  else "/_storage/wav/%s" % record["exportId"])
            self._success({"exportId": record["exportId"], "format": record["format"], "state": "READY",
                           "mimeType": "audio/wav", "url": url,
                           "expiresAt": "2027-01-15T16:20:00", "assetVersion": "1",
                           "rangeSupported": True, "renewalAttemptLimit": 3,
                           "requestId": "stub-request"})
            return

        self._send_json({"code": -1, "message": "stub: no interpretation route for %s" % rest,
                         "data": None}, 404)

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

    def _detail(self, session, project_id):
        project = session["projects"][project_id]
        if project["targets"] and project["generationPolls"] < 3:
            project["generationPolls"] += 1
            codes = list(project["targets"].keys())
            for index, code in enumerate(codes):
                target = project["targets"][code]
                if scenario_of(self._api_key()) == "stub-partial" and index == 1:
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
        # Without ffmpeg the music download fixture falls back to the WAV bytes; the
        # examples only require an audio content type and a non-empty payload.
        with open(mp3_path, "wb") as handle:
            handle.write(header + data)
    return wav_path, mp3_path


def main():
    parser = argparse.ArgumentParser(description="MyVocal public API local stub")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--make-fixtures", action="store_true")
    args = parser.parse_args()

    if args.make_fixtures:
        print("fixtures: %s" % (make_fixtures(),))
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("stub listening on http://127.0.0.1:%d" % args.port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
