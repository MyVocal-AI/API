#!/usr/bin/env python3
"""MyVocal real-time Speech to Text — minimal server-side client.

Requires: Python 3.8+, `requests` and `websocket-client`.

Set the environment first; nothing is read from the command line and no key is
written to a log:

    export MYVOCAL_API_BASE="https://api.myvocal.ai"   # or http://127.0.0.1:8080
    export MYVOCAL_ACCESS_KEY="stt_live_..."        # your MyVocal API key
    export MYVOCAL_AUDIO="sample-16k-mono-s16le.pcm"  # raw PCM16 mono @16 kHz

The script creates a session, streams the raw file, finishes, prints the shared
History view and saves it as JSON. It never connects to any provider directly;
every byte goes to the MyVocal entry point.
"""
import base64
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

import requests
import websocket  # pip install websocket-client

BASE = os.environ["MYVOCAL_API_BASE"].rstrip("/")
KEY = os.environ["MYVOCAL_ACCESS_KEY"]
AUDIO = os.environ["MYVOCAL_AUDIO"]
REALTIME = "/sound_clone/api/v1/stt/realtime"
HEADERS = {"accessKey": KEY, "Content-Type": "application/json"}


def envelope(response):
    payload = response.json()
    if payload.get("code") != 1:
        raise RuntimeError("MyVocal error %s: %s" % (payload.get("code"), payload.get("message")))
    return payload["data"]


def socket_url(path):
    """Builds the socket URL from the HTTP base and the returned path, for http or https."""
    parts = urlsplit(BASE + path)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


def main():
    started = envelope(requests.post(
        BASE + REALTIME + "/sessions",
        headers=dict(HEADERS, **{"Idempotency-Key": os.urandom(12).hex()}),
        json={"languageHint": "en", "options": {"inputEncoding": "pcm_s16le_16000"}},
        timeout=30,
    ))
    session_id = started["sessionId"]
    print("session", session_id, "transcription", started["transcriptionId"])

    # The stream URL is relative to the API host; the scheme follows the API base.
    stream = socket_url(started["streamUrl"])
    socket = websocket.create_connection(stream, header=["accessKey: " + KEY], timeout=30)
    try:
        raw = open(AUDIO, "rb").read()
        frame = 16_000 * 2  # one second of 16 kHz PCM16
        offset = 0
        for start in range(0, len(raw), frame):
            chunk = raw[start:start + frame]
            offset += len(chunk) // 2
            socket.send(json.dumps({
                "eventType": "audio.append",
                "epoch": 1,
                "payload": {
                    "audioBase64": base64.b64encode(chunk).decode("ascii"),
                    "sampleOffset": offset - len(chunk) // 2,
                    "capturedSamples": offset,
                },
            }))
            event = json.loads(socket.recv())
            if event["eventType"] == "session.error":
                raise RuntimeError("stream error %s" % event["payload"].get("errorCode"))
        socket.send(json.dumps({
            "eventType": "session.finish",
            "epoch": 1,
            "payload": {"capturedSamples": offset},
        }))
        while True:
            event = json.loads(socket.recv())
            if event["eventType"] == "transcript.final":
                print("final:", event["payload"].get("text"))
            elif event["eventType"] == "session.completed":
                break
            elif event["eventType"] == "session.error":
                raise RuntimeError("finish error %s" % event["payload"].get("errorCode"))
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
