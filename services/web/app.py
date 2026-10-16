"""Navigation-only UI server; backend and pipeline integration is deferred."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

ROOT = Path(__file__).resolve().parent

app = FastAPI(title="IdleCloud", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")

PAGES = {
    "/": "dashboard.html",
    "/upload": "upload.html",
    "/jobs": "jobs.html",
    "/job": "job.html",
    "/nodes": "nodes.html",
    "/costs": "costs.html",
}


async def render_page(request: Request):
    # TODO: Supply page context from implemented services in the integration PR.
    # Empty-state templates currently need no database or external services.
    return templates.TemplateResponse(
        request=request,
        name=PAGES[request.url.path],
        context={},
    )


for path, template in PAGES.items():
    app.add_api_route(
        path, render_page, methods=["GET"], response_class=HTMLResponse,
        name=template.removesuffix(".html"),
    )


@app.exception_handler(HTTPException)
async def render_error(request: Request, error: HTTPException):
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"code": error.status_code, "message": error.detail},
        status_code=error.status_code,
        headers=error.headers,
    )


# TODO: Add /jobs/{job_id}, upload handling, progress, and downloads only when
# their data sources and pipeline behavior are implemented in the separate PR.
