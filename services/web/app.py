from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services.web.coordinator import coordinator
from services.web.settings import settings

BASE_DIR = Path(__file__).parent
BURST_PAGE_SIZE = 20  # rows per page in each admin burst table

# Main navigation: (label, path).
NAV_ITEMS = [
    ("Upload", "/upload"),
    ("My jobs", "/jobs"),
    ("Account", "/account"),
    ("Admin: nodes", "/admin/nodes"),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await coordinator.aclose()


app = FastAPI(title="Idle-Compute Cloud", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["nav_items"] = NAV_ITEMS


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.get("/signup", response_class=HTMLResponse)
async def signup(request: Request):
    return templates.TemplateResponse(request, "signup.html")


@app.get("/upload", response_class=HTMLResponse)
async def upload(request: Request):
    return templates.TemplateResponse(request, "upload.html")


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request):
    return templates.TemplateResponse(request, "jobs.html")


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: UUID):
    return templates.TemplateResponse(request, "job_detail.html", {"job_id": job_id})


@app.get("/account", response_class=HTMLResponse)
async def account(request: Request):
    return templates.TemplateResponse(request, "account.html")


@app.get("/admin/nodes", response_class=HTMLResponse)
async def admin_nodes(
    request: Request,
    machine_offset: int = Query(0, ge=0),
    job_offset: int = Query(0, ge=0),
):
    token = request.cookies.get(settings.session_cookie)
    context = {
        "machine_offset": machine_offset,
        "job_offset": job_offset,
        "page_size": BURST_PAGE_SIZE,
    }
    status = 200
    if not token:
        context["error"] = (
            "Log in with an admin account to view burst workers and costs."
        )
        status = 401
    else:
        try:
            context["burst"] = await coordinator.burst_overview(
                token,
                machine_limit=BURST_PAGE_SIZE,
                machine_offset=machine_offset,
                job_limit=BURST_PAGE_SIZE,
                job_offset=job_offset,
            )
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            status = code if code in (401, 403) else 503
            context["error"] = {
                401: "Your session has expired. Log in again to view burst costs.",
                403: "An admin account is required to view burst workers and costs.",
            }.get(code, "Burst cost data is unavailable. Please try again shortly.")
        except httpx.RequestError:
            status = 503
            context["error"] = (
                "The coordinator is unavailable. Please try again shortly."
            )
    return templates.TemplateResponse(
        request, "admin_nodes.html", context, status_code=status
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
