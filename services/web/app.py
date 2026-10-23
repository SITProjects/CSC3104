from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services.web.coordinator import coordinator

BASE_DIR = Path(__file__).parent

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
async def admin_nodes(request: Request):
    return templates.TemplateResponse(request, "admin_nodes.html")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
