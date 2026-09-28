# Interpretation example clients

Two runnable clients for the MyVocal Interpretation API:

| File | Runtime | Notes |
|---|---|---|
| `quickstart.py` | Python 3.8+ | standard library only, no dependencies |
| `quickstart.mjs` | Node.js 18+ | built-in `fetch`, no dependencies |

Each client runs the complete documented flow:

```text
capabilities -> create project -> upload session -> sign/upload/complete parts
              -> poll the probe -> quote -> generate -> poll targets
              -> playback -> export -> download
```

## What the clients demonstrate

- `accessKey` header authentication, and the fact that a failure is **HTTP 200 with `code = 401`** — both clients check the HTTP status *and* the JSON business `code`.
- The multipart upload in the order the API requires: read `partSizeBytes`/`totalParts` from the service, sign each part, `PUT` that part's bytes to the presigned URL, collect the `ETag` of every part, then send the **complete** list to `complete`.
- Never sending the `accessKey` to the presigned object-storage URL. That URL's signature is the authorization, and it is scoped to one part.
- Reading the language catalog, accepted formats, export formats and size limit from `capabilities` instead of hard-coding them; `exportFormats` is validated before the run starts.
- `Idempotency-Key` on the create and generate calls, persisted to `interpretation_state.json` together with the returned ids, so a restart resumes instead of re-uploading from scratch.
- Handling `ALL_TARGETS_EXIST`: when every requested language already exists, `quoteId` is `null` and `totalCharacters` is `"0"`, so the client never calls generation with a null `quoteId`.
- Polling each target until it is `READY` or `FAILED_RELEASED`, treating `RECONCILING`, `PARTIAL_READY` and export `RETRY` as "still working" rather than as failures.
- Fetching the retry plan for a `FAILED_RELEASED` target with `action = "RETRY"` rather than pricing a retry locally.
- Tolerating a temporary download URL: if the product URL has expired, the client asks the download endpoint for a fresh one and retries, instead of failing.
- Bounded waits: a timeout is reported with the project/upload/export ids so the work can be queried later. A timeout is never treated as a failure to refund.

Characters values are JSON **strings** and are parsed with `BigInt` (Node.js) or `int` (Python) before comparison.

## Running against the local stub

```bash
# 1. one-time fixture generation (a real, decodable WAV)
python ../_stub/myvocal_stub.py --make-fixtures

# 2. start the stub
python ../_stub/myvocal_stub.py --port 8765

# 3. run a client against it, uploading the generated fixture
export MYVOCAL_API_KEY=stub-happy
export MYVOCAL_API_BASE_URL=http://127.0.0.1:8765
export MYVOCAL_MEDIA_FILE=../_stub/fixtures/sample_source.wav
export MYVOCAL_OUTPUT_DIR=/tmp/myvocal-interpretation
python quickstart.py
```

The stub selects its behaviour from the API key value, so the clients stay realistic:

| `MYVOCAL_API_KEY` | Behaviour |
|---|---|
| `stub-happy` (or anything else) | normal happy path |
| `stub-401` | every call returns HTTP 200 with `{"code":401,...}` |
| `stub-partial` | one target ready, one `FAILED_RELEASED` with `action = "RETRY"` |
| `stub-export-retry` | the export returns `RETRY` before it becomes `READY` |
| `stub-expired-url` | the first download URL has already expired |

## Running against MyVocal

```bash
export MYVOCAL_API_KEY=<your_api_key>
export MYVOCAL_MEDIA_FILE=./your-file.mp4
export MYVOCAL_OUTPUT_DIR=./out
python quickstart.py     # or: node quickstart.mjs
```

| Variable | Required | Default |
|---|---|---|
| `MYVOCAL_API_KEY` | yes | — |
| `MYVOCAL_API_BASE_URL` | no | `https://api.myvocal.ai` |
| `MYVOCAL_MEDIA_FILE` | no | `../_stub/fixtures/sample_source.wav` |
| `MYVOCAL_TARGET_LANGUAGES` | no | `es,fr` |
| `MYVOCAL_EXPORT_FORMAT` | no | `wav` |
| `MYVOCAL_OUTPUT_DIR` | no | current directory |
| `MYVOCAL_MAX_WAIT_SECONDS` | no | `1800` |

The media file must be an audio or video container the service accepts — read `audioFormats` and `videoFormats` from `capabilities` for the current list, and keep the file under `maxSourceBytes`. Use a container the service lists rather than assuming a fixed format.

Created files:

- `interpretation_output.<format>` — the downloaded export (for example `interpretation_output.wav`).
- `interpretation_state.json` — project id, upload id, quote id, acceptance id and idempotency keys, so a restart resumes. It never contains the API key, a signed URL or media.

<Warning>
Running these clients against the production host performs **real, billable work** and reserves Characters per target language. Use the stub for development, and read `availableCharacters` before accepting a real quote.
</Warning>

## Exit codes

| Code | Meaning |
|---|---|
| `0` | the flow completed (the file was downloaded, or every target already existed) |
| `1` | an API error (for example `code = 401`, `47102`, or a target that cannot be retried) |
| `2` | the bounded wait expired; the resource ids are printed so the work can be queried later |

See the [Interpretation quickstart guide](/guides/interpretation-quickstart) and the
[async jobs guide](/guides/async-jobs-and-retries) for the documented behaviour behind each step.
