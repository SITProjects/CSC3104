# Serverless page pipeline

Owner: member 6. Implementation plan, member 6, Step 1.

When the web app saves a PDF to `jobs/{job_id}/input.pdf`, S3 starts the
**splitter** Lambda. It counts the pages and starts one **renderer** Lambda per
20 pages, all at the same time. Each renderer saves its pages as PNGs and sends
a signed callback to the web app, so tasks can start as soon as their pages exist.

```
web app saves jobs/{job_id}/input.pdf
        |
        v  (S3 event)
idlecloud-split -- counts pages, splits into batches of 20
        |
        |--> idlecloud-render (pages 1-20)  --> PNGs + POST /hooks/pages-ready
        |--> idlecloud-render (pages 21-40) --> PNGs + POST /hooks/pages-ready
        '--> ...                                 (all running in parallel)
```

Both Lambdas run from one container image built from this folder.

| Folder | What it holds |
| --- | --- |
| `splitter/` | The splitter handler and the 20-page batching |
| `renderer/` | The renderer handler |
| `common/` | S3 keys, PDF rendering, HMAC signing and the callback, used by both |

## Contract with the web app (member 5)

**S3 keys** (from the shared contracts)

```
jobs/{job_id}/input.pdf              the web app writes this, it starts the splitter
jobs/{job_id}/pages/{page:04d}.png   the renderer writes these, pages start at 1
```

**Callback** `POST {WEBHOOK_URL}`, normally `https://<site>/hooks/pages-ready`.
One call per batch of 20 pages:

```json
{"status": "ok", "job_id": "a1b2c3", "page_count": 45,
 "page_start": 21, "page_end": 40, "chunk_index": 1, "chunk_count": 3, "render_ms": 4210}
```

If the file cannot be used (not a PDF, password protected, over 500 pages), the
splitter sends one call instead and renders nothing:

```json
{"status": "failed", "job_id": "a1b2c3", "error": "PDF is password protected"}
```

If one batch of pages cannot be rendered (a corrupt page), its renderer sends a
`failed` call for that batch instead, so the job never waits forever:

```json
{"status": "failed", "job_id": "a1b2c3", "page_start": 21, "page_end": 40,
 "error": "pages 21-40 could not be rendered: ..."}
```

`WEBHOOK_URL` must be set in a deployed Lambda. A missing value is an error,
unless `ALLOW_NO_WEBHOOK=1` is set on purpose to only log callbacks.

Headers `X-Idlecloud-Timestamp` and `X-Idlecloud-Signature` carry
`hex(HMAC-SHA256(secret, timestamp + "." + body))`. The endpoint should reject
bad signatures and anything older than 5 minutes, then reply 2xx.
`common/signing.py` has a `verify()` function to reuse. The same batch can arrive
twice after a retry, so ignore a `(job_id, page_start)` already seen.
All pages are ready once the `page_end` values cover `page_count`.

## AWS deployment

Deploying to Lambda (building the image in ECR and adding the S3 trigger) comes
in a separate PR, once the group's AWS account and bucket are agreed.
