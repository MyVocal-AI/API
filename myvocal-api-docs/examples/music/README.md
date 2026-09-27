# Text-to-Music example clients

Two runnable clients for the MyVocal Text-to-Music API:

| File | Runtime | Notes |
|---|---|---|
| `quickstart.py` | Python 3.8+ | standard library only, no dependencies |
| `quickstart.mjs` | Node.js 18+ | built-in `fetch`, no dependencies |

Each client runs the complete documented flow:

```text
capabilities -> create project -> poll the arrangement -> quote
              -> generate -> poll the song -> playback URL -> download MP3
```

## What the clients demonstrate

- `accessKey` header authentication, and the fact that a failure is **HTTP 200 with `code = 401`** — both clients check the HTTP status *and* the JSON business `code`, so neither can mistake an authentication failure for success.
- Reading the rate, the duration list and the accepted enum values from `capabilities` instead of hard-coding them.
- `Idempotency-Key` on the create and generate calls, persisted to `music_state.json` together with the returned ids.
- Polling with a bounded wait: `nextPollAfterMs` is handled when present (it may be `null`) and a jittered 2–5 second interval is used otherwise.
- Recovery from a lost response or a crash: re-running the client in the same output directory resumes the existing project from the state file instead of creating a second one.
- The distinct billing-safety rules: quote first, respect `affordable`/`shortfall`, and never treat a consumed quote (`47007`) as something to retry with a new key.
- Resuming after a timeout: the timeout is reported together with the resource ids, so the work can be queried later. A timeout is never treated as a failure to refund.

### Exact 64-bit integers

Every Characters value is a JSON **string** and can exceed 2^53, so the clients convert before comparing:

- Python parses with `int(...)`.
- Node.js parses with `BigInt(...)` and never runs `JSON.stringify` on a `BigInt`.

Both print an exact comparison line, for example:

```text
  quotedCharacters = 9007199254740993
  balance total    = 9007199254740992
  affordable       = True (shortfall 1)
  balance >= quote : False (exact integer comparison)
```

A float-based comparison would report that these two values are equal (both round to `9007199254740992.0`), which is exactly the bug these clients avoid.

## Running against the local stub

The clients accept any base URL, so they can be exercised without a real key and without spending Characters. A deterministic stub is included at `../_stub/myvocal_stub.py`.

```bash
# 1. one-time fixture generation (a real, decodable WAV and MP3)
python ../_stub/myvocal_stub.py --make-fixtures

# 2. start the stub
python ../_stub/myvocal_stub.py --port 8765

# 3. run a client against it
export MYVOCAL_API_KEY=stub-happy
export MYVOCAL_API_BASE_URL=http://127.0.0.1:8765
export MYVOCAL_OUTPUT_DIR=/tmp/myvocal-music
python quickstart.py
```

The stub selects its behaviour from the API key value, so the clients stay realistic:

| `MYVOCAL_API_KEY` | Behaviour |
|---|---|
| `stub-happy` (or anything else) | normal happy path |
| `stub-401` | every call returns HTTP 200 with `{"code":401,...}` |
| `stub-bignum` | Characters values above 2^53 |
| `stub-timeout` | the arrangement never becomes ready |

## Running against MyVocal

```bash
export MYVOCAL_API_KEY=<your_api_key>
export MYVOCAL_OUTPUT_DIR=./out
python quickstart.py     # or: node quickstart.mjs
```

| Variable | Required | Default |
|---|---|---|
| `MYVOCAL_API_KEY` | yes | — |
| `MYVOCAL_API_BASE_URL` | no | `https://api.myvocal.ai` |
| `MYVOCAL_OUTPUT_DIR` | no | current directory |
| `MYVOCAL_MAX_WAIT_SECONDS` | no | `900` |
| `MYVOCAL_DURATION_SEC` | no | `90` |
| `MYVOCAL_VOCAL_LANGUAGE` | no | first supported code |

Created files:

- `music_output.mp3` — the downloaded song.
- `music_state.json` — project id, job id, quote id and idempotency keys, so a restart can resume. It never contains the API key, a signed URL or media.

<Warning>
Running these clients against the production host performs **real, billable work** and consumes Characters from the account that owns the API key. Use the stub for development, and check `affordable`/`shortfall` before accepting a real quote.
</Warning>

## Exit codes

| Code | Meaning |
|---|---|
| `0` | the flow completed and a media file was written |
| `1` | an API error (for example `code = 401`, or a domain error such as `47009`) |
| `2` | the bounded wait expired; the resource ids are printed so the work can be queried later |

See the [Text-to-Music quickstart guide](/guides/music-quickstart) and the
[async jobs guide](/guides/async-jobs-and-retries) for the documented behaviour behind each step.
