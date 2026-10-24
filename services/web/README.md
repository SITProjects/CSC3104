# Website and public API

Owner: member 5.

Provide PDF uploads, the public job API, page-ready callbacks, live progress,
result downloads, and the node administration view. Calculate workload resource
requests and pass jobs to the coordinator.

Jinja2 templates are in `templates/`, plain CSS/JavaScript in `static/`. The OCR
workload lives separately in `worker/ocr-task/`.

## Run locally

```sh
uv run uvicorn services.web.app:app --reload --port 8000
```

Or `docker compose up web`. Open http://localhost:8000 (API docs at `/docs`).

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
