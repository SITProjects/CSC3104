# IdleCloud UI

Owner: member 5.

Reusable **Jinja2 templates** for the Campus Idle-Compute Cloud web app, styled
with vendored **Tabler UI 1.4.0** (Bootstrap-based). A small FastAPI server renders
the templates and serves their assets. There are no standalone HTML pages under
`static/` and no pipeline implementation. Existing services, migrations,
dependencies, and Docker Compose are unchanged.

## Run locally

From the repository root:

```sh
uv run uvicorn services.web.app:app --reload
```

Open **http://localhost:8000**. Navigation works between Overview, New OCR job,
Jobs, Compute nodes, and Cloud costs. `/job` previews the empty job-detail screen.
Unknown paths show the error template. No database, AWS credentials, or other
services are required. The pages show empty states until data is connected.

Refresh the browser after changing templates or styles. Python changes restart
the development server automatically. Press **Ctrl+C** to stop.

In a dev container, forward port **8000** in VS Code. To choose another port, add
`--port 8080` to the command.

## Layout

```text
app.py                       navigation-only FastAPI server
templates/
  base.html                  shared shell, page blocks, assets, preview notice
  dashboard.html             overview and recent jobs
  upload.html                PDF selection and local validation
  jobs.html                  job list, status filters, pagination
  job.html                   job detail view
  nodes.html                 read-only node overview
  costs.html                 machine and job cost estimates
  error.html                 reusable error view
  components/
    sidebar.html             shared responsive navigation
    ui.html                  icons, badges, money, timestamps, progress, empty states
    jobs_table.html          shared job rows
    job_detail.html          tasks, replicas, batches, events, progress
static/
  app.css                    small app-specific styling layer
  app.js                     browser-local UI behavior
  vendor/tabler/             pinned CSS, JavaScript, and license
```

Use Tabler components and utilities for layout, spacing, buttons, tables, forms,
alerts, and navigation. The vendored assets need no CDN or frontend build step.

## Template context

All page templates render with no context, showing empty states instead of
invented records. Render with HTML autoescaping enabled. Values below are plain
dictionaries/lists; statuses are strings, timestamps are ISO 8601 strings, and
monetary values are numbers/Decimals or `None` for unknown.

Common optional values:

- `static_url`: asset prefix; defaults to `/static`.
- `urls`: dictionary with `dashboard`, `upload`, `jobs`, `nodes`, and `costs` links;
  defaults to `/`, `/upload`, `/jobs`, `/nodes`, and `/costs`.
- `preview_data=True`: labels supplied fixture records as sample data.

| Template | Optional context |
| --- | --- |
| `dashboard.html` | `data`: `total`, `active`, `completed`, `idle_nodes`, `failed`, `known_total`, `has_unknown_price`, `recent` job rows |
| `upload.html` | `max_upload_bytes` (50,000,000), `cpu` (1), `memory` (512 MiB) |
| `jobs.html` | `jobs`: list of job rows |
| `job.html` | `job`: detail dictionary described below |
| `nodes.html` | `nodes`: `name`, `state`, `trust_score`, `active_replicas`, `total_agree`, `total_disagree`, `last_heartbeat` |
| `costs.html` | `data`: `machines`, `jobs`, `known_total`, `has_unknown_price`; rows follow the existing `burst_machine_cost` and `burst_job_cost` view columns |
| `error.html` | `code`, `message` |

Job rows contain `id`, `filename`, `owner_email`, `status`, `page_count`, and
`created_at`; optional `render_state` adds page-preparation status, and `url`
overrides the default `/jobs/{id}` link. Cost job rows also accept a `url` override.

Job details use `id`, `filename`, `owner_email`, `status`, and optional:

- `page_count`, `rendered_pages`, `processing_percent`, `verified_tasks`, `error`.
- `tasks`: `index`, `status`, `pages`, `cpu`, `memory`, `replicas`.
- Each replica: `id`, `node`, `status`, `attempt`, `progress`.
- `batches`: `start`, `end`, `state`.
- `events`: `time`, `message`.
- `cost`: `usd`, `has_unknown_price`.

These are display contexts, not new API contracts. A future route can adapt
existing service responses to these values without changing service contracts.

## Local behavior and integration TODOs

The upload form checks the selected filename, nonempty file size, configured
size limit, and email validity. It does not read PDF contents, upload files,
create jobs, or persist data. **Check selection** confirms local checks only.
Job filters and pagination operate on the rows passed to the template.
Downloads stay disabled; there is no polling or simulated processing.
Dates are formatted in Singapore time by the browser.

Inline `TODO` comments mark future integration points: data loading, real upload
validation, progress updates, verified downloads, and read-only node/cost data.
Connect only implemented services in the separate backend/pipeline PR.

## Render check

With the project's Python dependencies available, this checks all page templates
without adding a server or generating committed static pages:

```sh
.venv/bin/python - <<'PY'
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

env = Environment(
    loader=FileSystemLoader('services/web/templates'),
    undefined=StrictUndefined,
    autoescape=select_autoescape(['html']),
)
for name in ['dashboard', 'upload', 'jobs', 'job', 'nodes', 'costs', 'error']:
    env.get_template(name + '.html').render()
    print(name + ': OK')
PY
```
