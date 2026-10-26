# Website and public API

Owner: member 5.

Provide the New job page (`/jobs/new`: pick a job type, upload a PDF for OCR,
and choose CPU and memory where the job type allows it), the public job API, page-ready callbacks, live progress,
result downloads, and the admin dashboard. Calculate workload resource
requests and pass jobs to the coordinator.

Jinja2 templates are in `templates/`, plain JavaScript in `static/`. The OCR
workload lives separately in `worker/ocr-task/`.

Styling uses [Basecoat](https://basecoatui.com) (shadcn/ui components as CSS
classes such as `btn`, `card`, `badge` and `table`) plus Tailwind utility
classes. Pages load no code from a CDN: Basecoat is vendored (see
`styles/vendor/README.md`), and `static/app.css` is built ahead of time from
`styles/app.css` by a pinned, checksum-verified Tailwind CLI. Shared markup
(icons, status badges, error alerts) is in `templates/_macros.html`.

After adding or changing classes in `templates/` or `static/*.js`, rebuild the
stylesheet and commit `static/app.css` with your change (the pre-commit hook
does this for you):

```sh
services/web/build-css.sh          # or --watch while editing
```

## Run locally

```sh
uv run uvicorn services.web.app:app --reload --port 8000
```

Or run the whole system, with local worker nodes that run jobs end to end:
see "Local stack" in `infra/README.md`. Open http://localhost:8000 (API docs at
`/docs`).

## Submitting a job and following it

`/jobs/new` lets the user pick a job type (`JOB_TYPES` in `jobs.py`, mirroring the
types seeded in `shared/migrations`). Each type has a default CPU and memory; only
types with `custom_resources` (today the test workload) let the user change them,
within the coordinator's limits (1 to 16 cores, 64 to 4096 MiB). Tasks always run 3
replicas.

A test job is created at once. An OCR job takes one PDF (up to 50 MB and 500 pages,
10 pages per task): the web app checks it, saves it to `jobs/{id}/input.pdf` and sends
the user to `/jobs/{id}`.

```
POST /jobs/new -> S3 input.pdf -> Lambda renders pages -> POST /hooks/pages-ready
               -> (all pages in S3) -> coordinator POST /jobs with the uploader's token
```

* The coordinator's `POST /jobs` needs the uploader's bearer token, so the upload keeps
  it in `jobs/{id}/pending.json` until the job exists, then deletes it. There is no
  in-memory state, so several web replicas work. "Are all pages ready?" is answered by
  listing S3, which also makes repeated callbacks harmless.
* `/hooks/pages-ready` checks the HMAC signature (`HMAC_SECRET`, same as the Lambda's)
  and answers 5xx if the coordinator is down, so the Lambda retries.
* **Locally** there is no Lambda: `LOCAL_RENDER=true` (the compose default) renders the
  pages inside the web app. Never set it on AWS.

`/jobs/{id}` is the progress page. A tracker shows the stages every job moves through
(submitted, preparing pages, queued, running, checking results, completed) and where it
stopped if it failed. The page opens `GET /jobs/{id}/stream` (server-sent events,
polls the coordinator every 2 s and sends only changes) and shows each task, the PCs
running its copies, restarts after evictions, and the vote. Before the coordinator knows
the job it shows "preparing" or the reason the PDF was rejected. `GET /jobs/{id}/status`
returns the same data once as JSON. `/jobs/{id}/result.txt` downloads the text, built from
the winning replicas' `result.json` (or `output/result.txt` if something wrote it);
`POST /jobs/{id}/cancel` cancels.

## Deploy

`infra/k8s/base/web.yml` runs the web app in `idlecloud-system` on the AWS control
plane (2 replicas, `idlecloud/role=control`), behind a ClusterIP Service `web` on
port 80. The AWS overlay (`infra/k8s/overlays/aws/web-s3.yml`) switches it to real S3
and the Lambda. `infra/k8s/deploy.sh` builds the image, creates the `idlecloud-web`
Secret (`hmac-secret`, the Lambda's `HMAC_SECRET`) and rolls it out.

The Service is not reachable from outside the cluster. Put an HTTPS proxy or Ingress in
front of it: the session cookie is Secure, and the Lambda only calls `https://` URLs.

## Test

```sh
uv run pytest services/web
```

## Burst workers and cost (member 6)

The admin dashboard (`/admin`) shows burst workers and each job's estimated burst
cost, from the coordinator's `GET /admin/burst`, next to the worker nodes, every
user's jobs and the event log. It sends the token in the `session` cookie.
The login handler must set that cookie from the `access_token` that
`/auth/login` returns, ideally HttpOnly, SameSite=Lax and Secure. Details are in
`services/autoscaler/README.md`, section "Admin page".
