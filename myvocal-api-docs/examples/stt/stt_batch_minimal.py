#!/usr/bin/env python3
"""Minimal MyVocal Speech to Text batch client (R1).

Runs the complete public R1 flow with no third-party dependencies:

    capabilities -> create upload -> PUT part bytes -> complete upload
    -> submit transcription (idempotent) -> poll -> download -> delete

Idempotency and retry
---------------------
Each *logical* transcription gets its own idempotency key (a fresh UUID), so running the script
again on the same file starts a new task instead of colliding with the previous key. A short state
file next to the source file (``.<name>.myvocal-stt.json``) records the upload id and key while a
task is in flight:

* if the state file matches the file and already has a transcription id, the run resumes that task;
* if it matches and has an upload id but no transcription id (for example the submit response was
  lost), the run re-submits the *same* upload id, body and key, which returns the original task
  without a second model call;
* after a successful run the task is deleted and the state file is removed, so the next run starts a
  new task;
* a terminal ``FAILED`` task is reported and its state file is removed; it is never silently retried
  as a new recognition.

Environment:
    MYVOCAL_API_KEY       required; the existing MyVocal API key
    MYVOCAL_STT_BASE_URL  optional; default https://api.myvocal.ai/sound_clone/api/v1/stt

Usage:
    python stt_batch_minimal.py ./meeting.wav [language_hint] [title]
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("MYVOCAL_STT_BASE_URL", "https://api.myvocal.ai/sound_clone/api/v1/stt")
API_KEY = os.environ.get("MYVOCAL_API_KEY", "")


def call(method, path, body=None, idempotency_key=None):
    """Call a JSON endpoint and return the `data` object, raising on a non-1 code."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("accessKey", API_KEY)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if idempotency_key:
        request.add_header("Idempotency-Key", idempotency_key)
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != 1:
        error = payload.get("data") or {}
        raise RuntimeError("%s %s failed: code=%s errorCode=%s message=%s"
                           % (method, path, payload.get("code"), error.get("errorCode"),
                              payload.get("message")))
    return payload.get("data")


# ------------------------------------------------------------------ lightweight task state

def state_path(source_path):
    directory = os.path.dirname(os.path.abspath(source_path))
    return os.path.join(directory, "." + os.path.basename(source_path) + ".myvocal-stt.json")


def fingerprint(source_path):
    stat = os.stat(source_path)
    return {"fileName": os.path.basename(source_path), "sizeBytes": stat.st_size, "mtimeNs": stat.st_mtime_ns}


def load_state(source_path):
    """Returns the in-flight state for this exact file revision, or None."""
    path = state_path(source_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if state.get("fileName") != fingerprint(source_path)["fileName"]:
        return None
    if state.get("sizeBytes") != fingerprint(source_path).get("sizeBytes") \
            or state.get("mtimeNs") != fingerprint(source_path).get("mtimeNs"):
        return None
    return state


def save_state(source_path, **updates):
    state = load_state(source_path) or fingerprint(source_path)
    state.update(updates)
    path = state_path(source_path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    return state


def clear_state(source_path):
    try:
        os.remove(state_path(source_path))
    except OSError:
        pass


# ------------------------------------------------------------------ flow

def create_and_complete_upload(source_path):
    """Create the upload, PUT the bytes to the presigned URLs, then complete it."""
    size = os.path.getsize(source_path)
    created = call("POST", "/uploads", {
        "fileName": os.path.basename(source_path),
        "sizeBytes": size,
        "contentType": "application/octet-stream",
    })
    upload_id = created["uploadId"]
    part_size = created["partSizeBytes"]
    parts = []
    with open(source_path, "rb") as source:
        for part_number in range(1, created["totalParts"] + 1):
            chunk = source.read(part_size)
            signed = call("POST", "/uploads/%s/parts/%d" % (upload_id, part_number))
            request = urllib.request.Request(signed["url"], data=chunk, method="PUT")
            with urllib.request.urlopen(request, timeout=300) as response:
                parts.append({"partNumber": part_number,
                              "etag": response.headers.get("ETag", "").strip('"')})
    completed = call("POST", "/uploads/%s/complete" % upload_id, {"parts": parts})
    if completed["state"] != "READY":
        raise RuntimeError("upload did not become READY: %s" % completed)
    return upload_id, completed["media"]["audioTracks"]


def ensure_upload(source_path, state):
    """Reuse the in-flight upload when the state file has one; otherwise create a new upload."""
    if state and state.get("uploadId"):
        upload = call("GET", "/uploads/%s" % state["uploadId"])
        if upload["state"] == "READY":
            return state["uploadId"], upload["media"]["audioTracks"]
        if upload["state"] in ("FAILED", "ABORTED"):
            # Unusable upload: start this logical task again with a new upload and a new key.
            return None, None
        # Re-completing an upload that was mid-probe is safe and idempotent.
        completed = call("POST", "/uploads/%s/complete" % state["uploadId"], {"parts": state.get("parts", [])})
        return state["uploadId"], (completed.get("media") or {}).get("audioTracks", [])
    return create_and_complete_upload(source_path)


def submit(upload_id, track_index, language_hint, title, idempotency_key):
    body = {"uploadId": upload_id}
    if track_index is not None:
        body["audioTrackIndex"] = track_index
    if language_hint:
        body["languageHint"] = language_hint
    if title:
        body["title"] = title
    return call("POST", "/transcriptions", body, idempotency_key=idempotency_key)


def poll(transcription_id, timeout_seconds=900):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        view = call("GET", "/transcriptions/%s" % transcription_id)
        if view["status"] in ("COMPLETED", "FAILED"):
            return view
        time.sleep(3)
    raise RuntimeError("transcription did not finish within %ds" % timeout_seconds)


def download_text(transcription_id):
    request = urllib.request.Request(
        BASE + "/transcriptions/%s/download?format=txt" % transcription_id, method="GET")
    request.add_header("accessKey", API_KEY)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def main():
    if not API_KEY:
        print("Set MYVOCAL_API_KEY first.", file=sys.stderr)
        return 2
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    source_path = sys.argv[1]
    language_hint = sys.argv[2] if len(sys.argv) > 2 else None
    title = sys.argv[3] if len(sys.argv) > 3 else None

    capabilities = call("GET", "/capabilities")
    print("model:", capabilities["modelId"], "plan:", capabilities["account"].get("planKey"))

    state = load_state(source_path)
    # Resume a task whose submit response was lost, or a task still in flight.
    if state and state.get("transcriptionId"):
        transcription_id = state["transcriptionId"]
        print("resuming in-flight task:", transcription_id)
        view = poll(transcription_id)
    else:
        upload_id, tracks = ensure_upload(source_path, state)
        if upload_id is None:
            state = None
            upload_id, tracks = create_and_complete_upload(source_path)
        if not state or not state.get("idempotencyKey"):
            state = save_state(source_path, uploadId=upload_id, idempotencyKey="stt-example-" + uuid.uuid4().hex)
        idempotency_key = state["idempotencyKey"]
        track_index = tracks[0]["index"] if tracks and len(tracks) == 1 else None
        receipt = submit(upload_id, track_index, language_hint, title, idempotency_key)
        transcription_id = receipt["transcriptionId"]
        save_state(source_path, uploadId=upload_id, idempotencyKey=idempotency_key,
                   transcriptionId=transcription_id)
        print("submitted:", transcription_id, receipt["status"],
              "reserved characters:", receipt["billing"]["reservedCharacters"])
        view = poll(transcription_id)

    print("final status:", view["status"])
    if view["status"] == "FAILED":
        # Terminal failure: never silently re-submit as a new recognition.
        print("error:", view["error"])
        clear_state(source_path)
        return 1

    print("detected language:", view["detectedLanguage"])
    print("text:", view["transcript"]["text"])
    print("billed characters:", view["billing"]["billableCharacters"],
          "over", view["billing"]["billableDurationMs"], "ms")

    listed = call("GET", "/transcriptions?page=1&pageSize=20")
    print("history total:", listed["total"])
    print("download:\n" + download_text(transcription_id))

    call("DELETE", "/transcriptions/%s" % transcription_id)
    clear_state(source_path)
    print("deleted:", transcription_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
