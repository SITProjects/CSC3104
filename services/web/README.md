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
