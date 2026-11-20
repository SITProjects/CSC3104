# Website and public API

Provide the New job page (`/jobs/new`: pick a job type, upload a PDF for OCR or
a command and files for a Python script, and choose CPU and memory where the job
type allows it), the public job API, page-ready callbacks, live progress,
result downloads, and the admin dashboard, including the Campus workers pages
that add lab PCs to the cluster. Calculate workload resource requests and pass
jobs to the coordinator.

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
types with `custom_resources` (the test workload, Python, PyTorch and PyTorch GPU)
let the user change them, within the coordinator's limits: 1 to 16 cores, and 64 to
4096 MiB, or up to 6144 MiB for GPU types (`max_mem`, `GPU_MAX_MEM`). The form's
memory field and hint follow the chosen type. Tasks
start with `REPLICAS_PER_TASK` copies (default 1, see `services/verifier/README.md`),
and job types without `voting` always run one.

A test job is created at once. A Python script job (a generic job, `runs_command`)
takes a command and up to 10 files (50 MB in all, none named `output` or
`checkpoint`). The files go to `jobs/{id}/inputs/` and are deleted again only
if the coordinator refuses the job (a 4xx). After a lost reply or a 5xx the job may exist, so they stay.
The job is created at once with one task whose input is
`{"command": [...], "files": ["inputs/..."]}`. `worker/runner` runs it, and
`/jobs/{id}/output.tar.gz` downloads the output folder and log, also after a
failed run. An OCR job takes one PDF (up to 50 MB and 500 pages):
the web app checks it, saves it to `jobs/{id}/input.pdf` and sends the user to
`/jobs/{id}`. Each OCR copy gets 1 core and 256 MiB: Tesseract runs one thread, so
more copies beat bigger ones.

```
POST /jobs/new -> S3 input.pdf -> Lambda renders 20-page batches -> POST /hooks/pages-ready
   first ready batch  -> coordinator POST /jobs (final task_count, tasks so far)
   each later batch   -> coordinator POST /jobs/{id}/tasks (its tasks)
```

* **Task size follows free capacity.** At upload the web app asks the coordinator's
  `GET /capacity` how many one-core copies the workers can take now, and splits the
  PDF so it can all run at once: `free slots ÷ 3` tasks side by side, between 2 and 10
  pages each (`jobs.pages_per_task_for`). With few free slots tasks stay at 10 pages.
  The size is saved with the upload, so every callback plans the same tasks.
* **OCR starts per batch.** Tasks never span two render batches (`plan_tasks`), so a
  batch's tasks start as soon as its pages exist, while later pages still render.
  `render_batch_pages` must equal the Lambda's `PAGES_PER_RENDER`.
* The coordinator calls need the uploader's bearer token, so the upload keeps it in
  `jobs/{id}/pending.json` until every task has been sent, then deletes it. There is
  no in-memory state, so several web replicas work. Which pages exist is answered by
  listing S3, and the coordinator skips task indices it already has, so repeated
  callbacks are harmless.
* If a batch fails or the upload expires after the first batches started the job, the
  job is cancelled, so it never waits for tasks that will not come.
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

## My jobs filters

Each column header on `/jobs` has a down arrow that opens its filter (`templates/jobs.html`,
`jobfilter.py`): Job (search an ID or file name), Status (tick the statuses), Tasks and
Time taken (a minimum and maximum, the time in minutes) and Submitted (a from and to day,
Singapore time). The dropdowns share one form, so applying one keeps the rest. Status and
Submitted are filtered by the coordinator across all of a user's jobs. It can't filter the
others, so when one is set the app loads the newest 100 jobs (the most `GET /jobs`
returns), filters them itself and pages through the matches; the page says so.

## Admin table filters

The admin Users, Jobs and Burst workers tables use the same header dropdowns. Their
columns are declared in `admincolumns.py` and the matching and query-string logic is in
`tablefilter.py`. The coordinator can't filter these lists, so when a filter is set the
app loads the newest 200 users, 100 jobs or 100 burst workers (the most each endpoint
returns), filters them itself and pages through the matches; the page says so. Without a
filter the normal paging is used. On Jobs, the cost filter leaves out jobs with no burst
cost.

## Landing and error pages

`/` shows a public landing page (`templates/landing.html`) to visitors without a
session cookie, and the jobs dashboard (or `/admin` for admins) to everyone else.
Browsers that get an HTTP error (`Accept: text/html`) see `templates/error.html`
instead of JSON: "This page is off-limits" for 403 (a user opening `/admin`, an
admin opening My jobs), "We can't find that page" for 404, "You've been signed out"
for 401, "We're having trouble right now" for 503 and "Something went wrong" for
the rest. The pages show no status codes. Scripts and API clients still get
`{"detail": ...}`. The wording and icons are in `ERROR_PAGES` in `app.py`; a 404 or
other error that carries its own message ("Job not found") shows that message.

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

## Campus workers (admin)

`/admin/workers` lets an admin lend a lab PC to the cluster without a shell on
the hub. The coordinator's `/admin/workers` API does the work through the
enrollment helper on the hub (see `services/coordinator/README.md`, section
"Campus workers"); this app only shows it and passes downloads through.

1. **Add a worker:** enter its node name (lowercase letters, digits and dashes;
   it is lowercased and trimmed for you). The app opens the worker's page.
2. **Download setup zip** and run the installer on the PC with
   `install-worker.ps1 -Bundle <zip>` (the page shows the exact command). The
   zip holds the PC's VPN key and a join token valid for 24 hours. If it
   expires before the PC joins, **Request a new join token** and download the
   zip again.
3. Within a minute of the installer finishing, the page shows the PC as joined.
   **Download agent credentials** (it issues the heartbeat token the first
   time) and run the installer again with `-AgentCredentials <zip>`. Until
   then the coordinator gets no heartbeats from the PC, so it runs no tasks.
4. The heartbeat token lasts a year. From 30 days before it runs out, the page
   warns and offers **Issue a new heartbeat token**; then download and install
   the agent credentials again.
5. **Remove worker** (it asks first) moves the PC's tasks elsewhere and takes
   it out of the cluster and the VPN.

The list shows each worker's status (waiting to join, idle, busy, offline), with
what needs doing under it, and buttons for its download (**Setup zip** before
joining, **Agent credentials** after) and **Refresh token** (a new join token,
or once it has one, a new heartbeat token; it asks first and comes back to the
list). Filter it by name or VPN address and by status, including **Needs
attention**: an expired join token, missing agent credentials, a heartbeat
token running out, or a state the cluster couldn't report. Every change and
download appears in the event log with the admin's email.

- Zips are passed through with `Cache-Control: no-store`. Laptops and burst
  slots never appear here; `infra/wireguard/add-peer.sh` still handles those.
- Messages after an action come from a fixed list (`?done=...`), so nothing in
  the URL reaches the page. Errors from the hub's helper, such as "alice
  already exists as a laptop.", are shown as they are; a failed command shows a
  fixed hint to check `journalctl -u idlecloud-enroll` instead of its text.
- `static/confirm.js` asks before any form with a `data-confirm` question is
  sent. Without it the forms still work, unasked.

## Burst workers and cost

The admin dashboard (`/admin`) shows burst workers and each job's estimated burst
cost, from the coordinator's `GET /admin/burst`, next to the worker nodes, every
user's jobs and the event log. It sends the token in the `session` cookie.
The login handler must set that cookie from the `access_token` that
`/auth/login` returns, ideally HttpOnly, SameSite=Lax and Secure. Details are in
`services/autoscaler/README.md`, section "Admin page".
