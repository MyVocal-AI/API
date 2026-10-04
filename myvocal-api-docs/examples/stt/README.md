# Speech to Text examples

Runnable clients for the MyVocal Speech to Text API. Every example talks only to MyVocal and uses
environment variables for configuration; none installs or targets a provider SDK.

| File | What it shows |
|---|---|
| `stt_batch_minimal.py` | Batch: upload a file, submit, poll and download the result. |
| `stt_realtime_minimal.py` | Real-time server socket with the `accessKey` header (Python 3.8+, `requests`, `websocket-client`). |
| `stt_realtime_minimal.mjs` | Real-time server socket with the `accessKey` header (Node 22+, global `fetch`/`WebSocket`, `node:crypto`). |
| `stt_realtime_browser.html` | Browser microphone capture: exchanges the key for a single-use ticket, declares the real sample rate, flushes the tail and releases the microphone. |

Set the environment before running:

```bash
export MYVOCAL_API_BASE="https://api.myvocal.ai"   # or http://127.0.0.1:8080 for a local build
export MYVOCAL_ACCESS_KEY="stt_live_..."
export MYVOCAL_AUDIO="./sample-16k-mono-s16le.pcm" # batch/real-time server clients
```

Real-time server clients must send the encoding they declared: a PCM16 mono file at 16 kHz uses
`pcm_s16le_16000`. The browser example reads the microphone's own sample rate and declares the
matching encoding.

Read the guides first: [Batch quickstart](/guides/stt-batch-quickstart) and
[Real-time quickstart](/guides/stt-realtime-quickstart).
