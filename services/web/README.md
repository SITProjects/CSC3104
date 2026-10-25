# Website and public API

Owner: member 5.

Provide the New job page (`/jobs/new`: pick a job type, upload a PDF for OCR,
and choose CPU and memory where the job type allows it), the public job API, page-ready callbacks, live progress,
result downloads, and the node administration view. Calculate workload resource
requests and pass jobs to the coordinator.

Jinja2 templates are in `templates/`, plain JavaScript in `static/`. The OCR
workload lives separately in `worker/ocr-task/`.

Styling uses [Basecoat](https://basecoatui.com) (shadcn/ui components as CSS
classes such as `btn`, `card`, `badge` and `table`) plus Tailwind utility
classes. Both load from a CDN in `templates/base.html`, so there is no CSS build
step and no stylesheet of our own. Shared markup (icons, status badges, error
alerts) is in `templates/_macros.html`.

## Run locally

```sh
uv run uvicorn services.web.app:app --reload --port 8000
```

Or `docker compose up web`. Open http://localhost:8000 (API docs at `/docs`).

## Submitting a job and following it

`/upload` takes one PDF (up to 50 MB and 500 pages). The web app checks it, saves it to
`jobs/{id}/input.pdf` and sends the user to `/jobs/{id}`. Users never choose CPU or
memory: `settings.py` fixes 1 core / 512 MiB, 10 pages per task, 3 replicas.

```
POST /upload -> S3 input.pdf -> Lambda renders pages -> POST /hooks/pages-ready
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

`/jobs/{id}` is the progress page. It opens `GET /jobs/{id}/stream` (server-sent events,
polls the coordinator every 2 s and sends only changes) and shows each task, the PCs
running its copies, restarts after evictions, and the vote. Before the coordinator knows
the job it shows "preparing" or the reason the PDF was rejected. `GET /jobs/{id}/status`
returns the same data once as JSON. `/jobs/{id}/result.txt` downloads the text, built from
the winning replicas' `result.json` (or `output/result.txt` if something wrote it);
`POST /jobs/{id}/cancel` cancels.

## Test

```sh
uv run pytest services/web
```

## Burst workers and cost (member 6)

`/admin/nodes` shows burst workers and the estimated cost per job, from the
coordinator's `GET /admin/burst`. It sends the token in the `session` cookie.
The login handler must set that cookie from the `access_token` that
`/auth/login` returns, ideally HttpOnly, SameSite=Lax and Secure. Details are in
`services/autoscaler/README.md`, section "Admin page".
