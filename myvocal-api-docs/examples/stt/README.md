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

The example uploads, polls, downloads and **deletes its completed test transcription**.
A later run starts a new billable task. Interrupted submissions retain a local state file for
idempotent recovery. Inspect the script before using it with recordings you want to retain.

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

No recognition-service SDK is needed. Each example finishes the session and reads its final state.

## Browser demo

Serve `stt_realtime_browser.html` on localhost or HTTPS, enter the MyVocal API base and your
own test key, then grant microphone access. The page declares the actual capture rate, obtains a
single-use socket ticket and releases the microphone on exit. This is a developer demo; in a
customer-facing application keep the API key on your backend and expose only the session/ticket.

The microphone demo has simulated behavior coverage, not real-device acceptance.
See https://docs.myvocal.ai/guides/stt-availability for current limitations.
