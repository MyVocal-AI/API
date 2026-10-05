# Speech to Text examples

These clients call MyVocal with your existing API key. Running them against production performs
real transcription and consumes Characters. No key is included in this repository.

## Batch (Python, standard library)

```bash
export MYVOCAL_API_KEY="<your-api-key>"
# Optional; this variable includes the STT path:
export MYVOCAL_STT_BASE_URL="https://api.myvocal.ai/sound_clone/api/v1/stt"
python stt_batch_minimal.py ./meeting.wav en "Meeting"
```

The example uploads, polls until the task reaches a terminal status, downloads and **deletes its
completed test transcription**. A later run starts a new billable task. Interrupted submissions
retain a local state file for idempotent recovery. `COMPLETED`, `PARTIAL` and `FAILED` all end the
poll: a `PARTIAL` task (possible for a real-time session found in History) prints its readable text
and status from the task view and stays in History, because download is only offered for
`COMPLETED`. Inspect the script before using it with recordings you want to retain.

## Real-time (Python or Node.js)

Use raw signed 16-bit little-endian mono PCM at 16 kHz, not a WAV file with a container header.

```bash
export MYVOCAL_API_BASE="https://api.myvocal.ai"
export MYVOCAL_ACCESS_KEY="<your-api-key>"
export MYVOCAL_AUDIO="./sample-16k-mono-s16le.pcm"

# Python 3.8+: server socket authenticated with the accessKey header
python -m pip install requests websocket-client
python stt_realtime_minimal.py

# Node.js 22+: native fetch/WebSocket; socket authenticated with a one-use ticket
node stt_realtime_minimal.mjs
```

Both send the file the way a live source would: 100 ms frames, each leaving when its audio would
have been captured, so a 60-second file takes about 60 seconds. Node waits while more than 256 KB is
still in the socket's send buffer; Python's blocking send holds the loop back the same way while a
reader thread handles the server's events. Both follow a rotation request (`session.pause` then
`session.resume`, re-sending unacknowledged audio from the new epoch), finish, wait at most 60 s for
`session.completed` and read the session. If the socket fails they finish over REST instead and
print the real status, which may be `PARTIAL`. No recognition-service SDK is needed.

## Browser (same-origin backend + page)

`browser/` is a complete local app: `server.mjs` (Node.js 22+, no dependencies) keeps the API key and
serves the page; the page streams the microphone straight to MyVocal with a single-use ticket.

```bash
cd browser
export MYVOCAL_ACCESS_KEY="<your-api-key>"
# Optional: MYVOCAL_API_BASE (default https://api.myvocal.ai), HOST (127.0.0.1), PORT (8787)
node server.mjs
# open http://127.0.0.1:8787/ , press Start, allow the microphone, speak, press Stop
```

On Windows PowerShell use `$env:MYVOCAL_ACCESS_KEY = "<your-api-key>"` instead of `export`.

- The backend offers only four fixed routes: create a session, issue a ticket, read a session and
  finish it, and only for sessions it created itself. It is not a general proxy, sends no CORS
  headers and accepts JSON requests from its own origin only. The key never reaches the page.
- The page uses one `AudioContext` at the device's real sample rate (16 kHz if that rate is not
  supported) and declares it as `inputEncoding`. An `AudioWorklet` merges the 128-sample audio
  quanta into frames of about 100 ms.
- Stop asks the worklet for the last partial frame and waits (at most 2 s) for its confirmation, so
  `capturedSamples` includes the tail. It then sends the remaining frames, finishes on the socket and
  waits at most 45 s for `session.completed`.
- At most 512 KB may wait in the socket's send buffer; up to 30 s of audio is queued while the
  network is slow, after which the page stops recording and finishes what was received.
- A sent frame that is not acknowledged within 10 s counts as a stalled connection (a socket can stay
  open while the network is gone).
- On a socket error, a disconnect, a stall, a timeout or `session.error`, the microphone is released and the
  page finishes through the backend (three bounded, idempotent attempts, then a status read). It shows
  only the status MyVocal returns (`COMPLETED`, `PARTIAL` or `FAILED`). If the backend cannot be
  reached, the session id and captured samples are kept in `localStorage` with a retry button; the
  page never reports a finish it did not receive.

For your own product, move `server.mjs`'s four routes into your backend behind your own user
authentication. The browser example has been run in an isolated browser with a simulated
microphone, not as real-device acceptance. See https://docs.myvocal.ai/guides/stt-availability for
current limitations.
