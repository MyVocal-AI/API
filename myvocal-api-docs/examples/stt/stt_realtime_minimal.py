#!/usr/bin/env python3
"""MyVocal real-time Speech to Text — minimal server-side client.

Requires: Python 3.8+, `requests` and `websocket-client`.

Set the environment first; nothing is read from the command line and no key is
written to a log:

    export MYVOCAL_API_BASE="https://api.myvocal.ai"   # or http://127.0.0.1:8080
    export MYVOCAL_ACCESS_KEY="stt_live_..."        # your MyVocal API key
    export MYVOCAL_AUDIO="sample-16k-mono-s16le.pcm"  # raw PCM16 mono @16 kHz

The script streams the raw file the way a live source would: 100 ms frames, each
sent when its audio would have been captured. Sends are blocking, so a slow
network holds the sender back instead of growing a local queue; a separate reader
thread consumes the server's events. It follows a rotation request, finishes,
waits (bounded) for the terminal event, prints the shared History view and saves
it as JSON. If the socket fails it finishes over REST instead; the result may be
PARTIAL. Every byte goes to the MyVocal entry point.
"""
import base64
import json
import os
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import requests
import websocket  # pip install websocket-client

BASE = os.environ["MYVOCAL_API_BASE"].rstrip("/")
KEY = os.environ["MYVOCAL_ACCESS_KEY"]
AUDIO = os.environ["MYVOCAL_AUDIO"]
REALTIME = "/sound_clone/api/v1/stt/realtime"
HEADERS = {"accessKey": KEY, "Content-Type": "application/json"}
RATE = 16_000
FRAME_SAMPLES = RATE // 10  # 100 ms
COMPLETE_TIMEOUT_S = 60


def envelope(response):
    payload = response.json()
    if payload.get("code") != 1:
        error = (payload.get("data") or {}).get("errorCode") or payload.get("message")
        raise RuntimeError("MyVocal error %s: %s" % (payload.get("code"), error))
    return payload["data"]


def socket_url(path):
    """Builds the socket URL from the HTTP base and the returned path, for http or https."""
    parts = urlsplit(BASE + path)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


class Stream:
    """Reads server events on its own thread and keeps the cursors the sender needs."""

    def __init__(self, socket):
        self.socket = socket
        self.lock = threading.Condition()
        self.epoch = 1
        self.epoch_start = 0
        self.sent = 0
        self.acked = 0
        self.rotating = False
        self.failure = None
        self.completed = None
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()

    def send(self, event_type, payload):
        self.socket.send(json.dumps({"eventType": event_type, "epoch": self.epoch, "payload": payload}))

    def _read(self):
        while True:
            try:
                event = json.loads(self.socket.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:  # closed or broken
                with self.lock:
                    self.closed = True
                    if self.completed is None and self.failure is None:
                        self.failure = "SOCKET_CLOSED"
                    self.lock.notify_all()
                return
            self._on_event(event)

    def _on_event(self, event):
        kind, p = event.get("eventType"), event.get("payload") or {}
        if kind == "transcript.final":
            print("final:", p.get("text"))
        elif kind == "transcript.revision":
            print("revised:", p.get("revisedText"))
        elif kind == "session.notice":
            print("notice:", p.get("code"))
        with self.lock:
            if kind == "session.ready":
                self.epoch = p.get("epoch") or self.epoch
            elif kind == "session.completed":
                self.completed = p
            elif kind == "session.error":
                self.failure = p.get("errorCode") or "SESSION_ERROR"
            elif kind == "usage.updated":
                if p.get("epoch") == self.epoch and p.get("sentSamples") is not None:
                    self.acked = max(self.acked, int(p["sentSamples"]))
                if p.get("rotate") and not self.rotating:
                    # The frame that got this answer was not stored: open a new epoch and continue
                    # after the last acknowledged sample, starting again at sampleOffset 0.
                    self.rotating = True
                    captured = str(self.epoch_start + self.sent)
                    self.send("session.pause", {"capturedSamples": captured})
                    self.send("session.resume", {"capturedSamples": captured})
                elif self.rotating and (p.get("epoch") or 0) > self.epoch and p.get("accepting"):
                    self.epoch_start += self.acked
                    self.epoch, self.sent, self.acked, self.rotating = p["epoch"], 0, 0, False
            self.lock.notify_all()


def main():
    raw = open(AUDIO, "rb").read()
    total = len(raw) // 2
    started = envelope(requests.post(
        BASE + REALTIME + "/sessions",
        # Keep this key and body for a retry of the same create.
        headers=dict(HEADERS, **{"Idempotency-Key": str(uuid.uuid4())}),
        json={"languageHint": "en", "options": {"inputEncoding": "pcm_s16le_%d" % RATE}},
        timeout=30,
    ))
    session_id = started["sessionId"]
    print("session", session_id, "transcription", started["transcriptionId"])

    # The stream URL is relative to the API host; the scheme follows the API base.
    socket = websocket.create_connection(socket_url(started["streamUrl"]),
                                         header=["accessKey: " + KEY], timeout=30)
    stream = Stream(socket)
    captured = 0
    begin = time.monotonic()
    try:
        while True:
            with stream.lock:
                if stream.failure:
                    raise RuntimeError(stream.failure)
                if stream.rotating:
                    stream.lock.wait(0.05)
                    continue
                position = stream.epoch_start + stream.sent
                if position >= total:
                    break
                end = min(total, position + FRAME_SAMPLES)
                due = begin + end / RATE - time.monotonic()
                if due > 0:
                    stream.lock.wait(min(due, 0.1))
                    continue
                captured = max(captured, end)
                epoch, offset = stream.epoch, stream.sent
            # Blocking send outside the lock: a slow network holds this loop back instead of
            # queueing audio, while the reader keeps draining the server's events.
            stream.socket.send(json.dumps({"eventType": "audio.append", "epoch": epoch, "payload": {
                "audioBase64": base64.b64encode(raw[position * 2:end * 2]).decode("ascii"),
                "sampleOffset": offset,
                "capturedSamples": str(captured),
            }}))
            with stream.lock:
                if stream.epoch == epoch and stream.sent == offset:
                    stream.sent += end - position
        with stream.lock:
            stream.send("session.finish", {"capturedSamples": str(total)})
            deadline = time.monotonic() + COMPLETE_TIMEOUT_S
            while stream.completed is None:
                if stream.failure:
                    raise RuntimeError(stream.failure)
                if time.monotonic() > deadline:
                    raise RuntimeError("FINISH_TIMEOUT")
                stream.lock.wait(0.1)
        print("status", stream.completed.get("status"))
    except RuntimeError as stopped:
        # A disconnect is not a finish. Settle what the server can confirm; repeating finish never
        # charges twice and returns the same task.
        print("stream stopped: %s - finishing over REST" % stopped, file=sys.stderr)
        view = envelope(requests.post(BASE + REALTIME + "/sessions/" + session_id + "/finish",
                                      headers=HEADERS, json={"capturedSamples": str(captured)}, timeout=70))
        print("status", view["status"])
    finally:
        socket.close()

    view = envelope(requests.get(BASE + REALTIME + "/sessions/" + session_id,
                                 headers=HEADERS, timeout=30))
    print("status", view["status"], "billable", view["usage"]["billableCharacters"], "Characters")
    with open("stt_realtime_result.json", "w", encoding="utf-8") as out:
        json.dump(view, out, ensure_ascii=False, indent=2)
    print("wrote stt_realtime_result.json")


if __name__ == "__main__":
    try:
        main()
    except KeyError as missing:
        sys.exit("set environment variable " + str(missing))
