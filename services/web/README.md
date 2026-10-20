# Website and public API

Owner: member 5.

Provide PDF uploads, the public job API, page-ready callbacks, live progress,
result downloads, and the node administration view. Calculate workload resource
requests and pass jobs to the coordinator.

Put Jinja2 templates in `templates/` and plain JavaScript, CSS, and other website
assets in `static/`. The OCR workload lives separately in `worker/ocr-task/`.

## How a job flows through the web app

1. `POST /upload` (or `POST /api/jobs`) checks the file is a PDF up to 50 MB and
   the user has fewer than 3 running jobs. It saves an `uploads` row and then
   `jobs/{job_id}/input.pdf`, which starts member 6's splitter Lambda.
2. Each render Lambda calls `POST /hooks/pages-ready`. The HMAC signature is
   checked with `serverless/common/signing.py`. Repeated batches are ignored
   through `upload_batches`.
3. Once the batches cover every page, the web app sizes the job from the
   rendered PNG sizes (`resource_request` in `jobs.py`) and calls
   `POST /internal/jobs` with 10-page tasks and 3 replicas, using the
   upload's id as the job id. If the coordinator is down, nothing is saved and
   the reply is 500, so the Lambda's retry submits again.
4. `/jobs/{id}` shows tasks, which PCs ran each replica, evictions and votes.
   `/jobs/{id}/stream` pushes updates with Server-Sent Events. It re-renders
   `templates/partials/job_detail.html` and `static/live.js` swaps it in.

## Pages and endpoints

| Path | What |
| --- | --- |
| `GET /` | Upload form |
| `GET /jobs/{id}` | Live job status, cancel button, download |
| `GET /history?email=` | A user's jobs with time taken and pages |
| `GET /api-keys` | Create an API key, shown once and stored hashed |
| `GET /admin` | Nodes, trust, burst workers and cost. HTTP Basic, user `admin` |
| `POST /hooks/pages-ready` | Lambda callback |
| `POST /api/jobs`, `GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel`, `GET /api/jobs/{id}/result` | Public API, `X-API-Key` header |

A user may give a webhook URL. It is POSTed when the job ends. Only public
addresses are allowed, so a user cannot make the web app call cluster services
or the EC2 metadata endpoint.

Each webhook is signed like the pages-ready callback (`X-Idlecloud-Timestamp`,
`X-Idlecloud-Signature`), with a per-owner secret derived from `HMAC_SECRET` and
shown on the API page. Changing `HMAC_SECRET` changes every owner's secret. A
delivery that does not get a 2xx answer is tried again on the next pass, up to
`WEBHOOK_MAX_ATTEMPTS` failures `WEBHOOK_INTERVAL` seconds apart. A web pod that
dies mid-delivery loses that webhook.

## Request limits and headers

`middleware.py` refuses a request body over its limit before it is read: the
PDF size (plus multipart framing) for `/upload` and `/api/jobs`, 64 KiB for
everything else. It also sets CSP and other security headers, and refuses a
POST that the browser marks `Sec-Fetch-Site: cross-site`. The pages carry no
inline scripts or handlers; the CSP allows inline styles only, for the progress
bars. Live streams for the same page share one render per half
`STREAM_INTERVAL` (`shared_render.py`), so more viewers do not mean more
queries.

## Settings

All of these are in `settings.py`.

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL`, `COORDINATOR_URL` | As for the coordinator |
| `S3_BUCKET`, `S3_ENDPOINT_URL` | Endpoint only for MinIO, unset on AWS |
| `HMAC_SECRET` | Same value as the Lambdas. Empty rejects every callback |
| `ADMIN_PASSWORD` | Empty turns `/admin` off |
| `LOCAL_SPLIT=1` | No Lambda: render pages in the web app with the renderer's code |
| `PUBLIC_URL` | Optional, for links in webhook payloads |

## Run locally

With Postgres, MinIO and the coordinator running (from the repo root):

```
LOCAL_SPLIT=1 HMAC_SECRET=local-dev-secret ADMIN_PASSWORD=admin \
COORDINATOR_URL=http://localhost:8001 S3_ENDPOINT_URL=http://minio:9000 \
python -m uvicorn services.web.app:app --port 8000 --reload --reload-dir services/web
```

Tests: `pytest services/web/tests`.

## Database tables

The web app's own tables are created by `migrations/` in this folder, run by
`migrate.py` at startup after the shared migrations. They are tracked in
`web_schema_migrations`, separate from the shared `schema_migrations`.

The resource rule's numbers (`DENSE_PAGE_BYTES`, `MEM_NORMAL`, `MEM_DENSE`) are
placeholders until the sample PDFs are profiled with `docker stats`. CPU is
always 1 core, because the coordinator only takes whole cores.
