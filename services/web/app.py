import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID, uuid4

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services.web import admin as admintools
from services.web import jobs as jobtools
from services.web import storage, uploads
from services.web.coordinator import coordinator
from services.web.settings import settings

BASE_DIR = Path(__file__).parent
log = logging.getLogger("uvicorn.error")
BURST_PAGE_SIZE = 20  # burst workers per page on the admin dashboard

# Main navigation. "admin": True items are shown to admins only, "admin": False
# items to everyone but admins, and the rest to everyone (the pages check the
# role themselves too).
NAV_ITEMS = [
    {"label": "Dashboard", "path": "/", "icon": "house", "admin": False},
    {"label": "New job", "path": "/jobs/new", "icon": "plus", "admin": False},
    {"label": "My jobs", "path": "/jobs", "icon": "list", "admin": False},
    {"label": "Overview", "path": "/admin", "icon": "gauge", "admin": True},
    {"label": "Jobs", "path": "/admin/jobs", "icon": "list", "admin": True},
    {"label": "Burst workers", "path": "/admin/burst", "icon": "server", "admin": True},
    {"label": "Event log", "path": "/admin/events", "icon": "activity", "admin": True},
    {"label": "Users", "path": "/admin/users", "icon": "users", "admin": True},
]
NAV_PATHS = [item["path"] for item in NAV_ITEMS]


def nav_active(path: str) -> str | None:
    """The nav link for a page: the longest nav path that is it or contains it."""
    matches = [p for p in NAV_PATHS if path == p or path.startswith(p + "/")]
    return max(matches, key=len, default=None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await coordinator.aclose()


app = FastAPI(title="Idle-Compute Cloud", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def _template_context(request: Request) -> dict:
    return {
        "signed_in": bool(request.cookies.get(settings.session_cookie)),
        # set by current_user; pages that don't look up the user show no email
        "user": getattr(request.state, "user", None),
    }


templates = Jinja2Templates(
    directory=BASE_DIR / "templates", context_processors=[_template_context]
)
templates.env.globals["nav_items"] = NAV_ITEMS
templates.env.globals["nav_active"] = nav_active


def static_version(name: str) -> int:
    """When a static file last changed, to put in its URL so caches refetch it."""
    return int((BASE_DIR / "static" / name).stat().st_mtime)


templates.env.globals["static_version"] = static_version
templates.env.filters["sg_time"] = admintools.sg_time

DEFAULT_AFTER_LOGIN = "/"
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def _safe_next(target: str | None) -> str:
    """Only allows redirects to a path on this site, never to another host."""
    if not target:
        return DEFAULT_AFTER_LOGIN
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/"):
        return DEFAULT_AFTER_LOGIN
    if target.startswith("//") or "\\" in target:
        return DEFAULT_AFTER_LOGIN
    return target


def _start_session(token_out: dict, next_url: str) -> RedirectResponse:
    """Redirects to next_url with the coordinator's bearer token in a cookie."""
    max_age = 24 * 3600
    try:
        expires = datetime.fromisoformat(token_out["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        max_age = max(int((expires - datetime.now(UTC)).total_seconds()), 0)
    except (KeyError, ValueError, TypeError):
        pass
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        settings.session_cookie,
        token_out["access_token"],
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


def _auth_error(
    request: Request, template: str, message: str, status: int, **context
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, template, {"error": message, **context}, status_code=status
    )


def _coordinator_error(exc: Exception, conflict_msg: str = "") -> tuple[int, str]:
    """Maps a failed coordinator call to a status and a message safe to show."""
    log.warning("coordinator auth call failed: %r", exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code >= 500:
            log.warning("coordinator said %s: %s", code, exc.response.text[:500])
        if code == 401:
            return 401, "Wrong email or password."
        if code == 409:
            return 409, conflict_msg or "An account with this email already exists."
        if code == 422:
            return 422, "Enter a valid email and a password of 8 to 128 characters."
    return 503, "Can't reach the sign-in service. Please try again shortly."


class LoginRequired(Exception):
    """Raised when a page needs a signed-in user and the visitor isn't one."""

    def __init__(self, next_url: str):
        self.next_url = next_url


@app.exception_handler(LoginRequired)
async def send_to_login(request: Request, exc: LoginRequired):
    response = RedirectResponse(f"/login?next={quote(exc.next_url)}", status_code=303)
    # drop a stale or expired token so /login doesn't bounce the visitor back
    response.delete_cookie(settings.session_cookie, path="/")
    return response


async def current_user(request: Request) -> dict:
    """
    Finds the signed-in user from the session cookie, asking the coordinator.

    raises:
        LoginRequired: If there is no cookie or the coordinator rejects the token.
    """
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    token = request.cookies.get(settings.session_cookie)
    if not token:
        raise LoginRequired(target)
    try:
        user = await coordinator.me(token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise LoginRequired(target) from exc
        raise HTTPException(503, "Sign-in service unavailable") from exc
    except httpx.RequestError as exc:
        raise HTTPException(503, "Sign-in service unavailable") from exc
    request.state.user = user  # for the profile menu in base.html
    return user


async def admin_user(user: Annotated[dict, Depends(current_user)]) -> dict:
    if user.get("role") != "ADMIN":
        raise HTTPException(403, "Admins only")
    return user


async def regular_user(user: Annotated[dict, Depends(current_user)]) -> dict:
    """For pages about the user's own jobs, which admins don't have."""
    if user.get("role") == "ADMIN":
        raise HTTPException(403, "Not available to admins")
    return user


SignedIn = Depends(current_user)
AdminOnly = Depends(admin_user)


async def _has_valid_session(request: Request) -> bool:
    try:
        await current_user(request)
    except (LoginRequired, HTTPException):
        return False
    return True


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str | None = None):
    if await _has_valid_session(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": _safe_next(next) if next else ""}
    )


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "",
):
    email = email.strip()
    try:
        token_out = await coordinator.login(email, password)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        status, message = _coordinator_error(exc)
        return _auth_error(
            request, "login.html", message, status, email=email, next=next
        )
    return _start_session(token_out, _safe_next(next))


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    if await _has_valid_session(request):
        return RedirectResponse(DEFAULT_AFTER_LOGIN, status_code=303)
    return templates.TemplateResponse(request, "signup.html")


@app.post("/signup", response_class=HTMLResponse)
async def signup(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
):
    email = email.strip()
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        return _auth_error(
            request,
            "signup.html",
            f"Password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters.",
            422,
            email=email,
        )
    if password != confirm_password:
        return _auth_error(
            request, "signup.html", "Passwords do not match.", 422, email=email
        )
    try:
        token_out = await coordinator.signup(email, password)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        status, message = _coordinator_error(exc)
        return _auth_error(request, "signup.html", message, status, email=email)
    return _start_session(token_out, DEFAULT_AFTER_LOGIN)


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(settings.session_cookie)
    if token:
        try:
            await coordinator.logout(token)
        except (httpx.HTTPStatusError, httpx.RequestError):
            pass  # the cookie is cleared either way; the token expires on its own
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie, path="/")
    return response


# ---- job submission and progress ----

JOBS_PAGE_SIZE = 20
MIB = 1024 * 1024


NEW_JOB_PATH = "/jobs/new"


def _new_job_page(
    request: Request,
    error: str | None = None,
    status: int = 200,
    form: dict | None = None,
    at_limit: str | None = None,
):
    """
    The New job page; form refills what the user entered after an error, and
    at_limit, if set, says why no more jobs can be started right now.
    """
    return templates.TemplateResponse(
        request,
        "new_job.html",
        {
            "error": error,
            "form": form or {},
            "job_types": list(jobtools.JOB_TYPES.values()),
            "cpu_range": (jobtools.MIN_CPU, jobtools.MAX_CPU),
            "mem_range": (jobtools.MIN_MEM, jobtools.MAX_MEM),
            "max_mb": settings.max_upload_mb,
            "max_pages": settings.max_pages,
            "at_limit": at_limit,
        },
        status_code=status,
    )


# for the coordinator's 429, which came without a count to show
LIMIT_REACHED = (
    "You already have the most jobs allowed running at once. "
    "Wait for one to finish, or cancel one, then try again."
)


def _limit_message(limit: int) -> str:
    return (
        f"You already have {limit} jobs running or being prepared, the most "
        "allowed at once. Wait for one to finish, or cancel one, then try again."
    )


async def _job_limit_reached(token: str, user: dict) -> str | None:
    """
    Why the user may not start another job right now, or None if they may.

    Counts the coordinator's unfinished jobs plus uploads still being prepared,
    which the coordinator doesn't know yet. Best effort: if the count can't be
    loaded this returns None, and the coordinator still enforces the limit when
    the job is created.
    """
    try:
        quota = await coordinator.job_quota(token)
    except httpx.HTTPError as exc:
        log.warning("couldn't check the job limit: %r", exc)
        return None
    preparing = sum(1 for r in await _pending_rows(user) if r["status"] == "PREPARING")
    if quota["active"] + preparing >= quota["limit"]:
        return _limit_message(quota["limit"])
    return None


class UploadSizeGuard:
    """
    Refuses an oversized new job from its Content-Length header, before the body
    is parsed, so the app doesn't first spool a huge file to disk. A client that
    sends no length still hits the check in upload_submit, and the proxy's own
    body limit is the real protection once deployed.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == NEW_JOB_PATH
        ):
            declared = dict(scope["headers"]).get(b"content-length", b"")
            # one extra MiB covers the multipart framing around the file
            if (
                declared.isdigit()
                and int(declared) > (settings.max_upload_mb + 1) * MIB
            ):
                request = Request(scope, receive)
                response = _new_job_page(
                    request, f"That file is over {settings.max_upload_mb} MB.", 413
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(UploadSizeGuard)


# Declared before /jobs/{job_id}, which would otherwise catch "new"
@app.get(NEW_JOB_PATH, response_class=HTMLResponse)
async def new_job(request: Request, user: Annotated[dict, Depends(regular_user)]):
    token = request.cookies[settings.session_cookie]
    return _new_job_page(request, at_limit=await _job_limit_reached(token, user))


@app.post(NEW_JOB_PATH, response_class=HTMLResponse)
async def new_job_submit(
    request: Request,
    background: BackgroundTasks,
    user: Annotated[dict, Depends(regular_user)],
    job_type: Annotated[str | None, Form()] = None,
    cpu: Annotated[str | None, Form()] = None,
    mem: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
):
    form = {"job_type": job_type, "cpu": cpu, "mem": mem}
    try:
        kind = jobtools.parse_job_type(job_type)
        cpu_cores, mem_mib = jobtools.parse_resources(kind, cpu, mem)
    except jobtools.UploadError as exc:
        return _new_job_page(request, str(exc), 422, form)
    token = request.cookies[settings.session_cookie]
    if limit := await _job_limit_reached(token, user):
        return _new_job_page(request, limit, 429, form, at_limit=limit)
    if not kind.needs_pdf:
        return await _create_job_now(request, form, kind.name, cpu_cores, mem_mib)

    if file is None or not file.filename:
        return _new_job_page(request, "Choose a PDF to upload.", 422, form)
    limit = settings.max_upload_mb * MIB
    data = await file.read(limit + 1)
    if len(data) > limit:
        return _new_job_page(
            request, f"That file is over {settings.max_upload_mb} MB.", 413, form
        )
    try:
        pages = await asyncio.to_thread(jobtools.check_pdf, data)
    except jobtools.UploadError as exc:
        return _new_job_page(request, str(exc), 422, form)

    job_id = uuid4()
    per_task = await _pages_per_task(token, pages, cpu_cores, mem_mib)
    try:
        await uploads.save_upload(
            job_id,
            str(user["id"]),
            request.cookies[settings.session_cookie],
            jobtools.clean_filename(file.filename),
            data,
            pages,
            cpu_cores,
            mem_mib,
            per_task,
        )
    except (BotoCoreError, ClientError):
        log.exception("saving upload %s to S3 failed", job_id)
        return _new_job_page(
            request, "Couldn't store the file. Please try again.", 503, form
        )
    if settings.local_render:
        background.add_task(uploads.render_locally, job_id, data)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


async def _create_job_now(
    request: Request, form: dict, job_type: str, cpu: int, mem: int
) -> Response:
    """Creates a job that needs no upload straight away and opens its page."""
    job_id = str(uuid4())
    try:
        await coordinator.create_job(
            request.cookies[settings.session_cookie],
            jobtools.job_spec(job_id, job_type, cpu, mem),
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise LoginRequired(NEW_JOB_PATH) from exc
        if exc.response.status_code == 429:
            return _new_job_page(request, LIMIT_REACHED, 429, form)
        log.warning("creating %s job failed: %r", job_type, exc)
        return _new_job_page(
            request, "The job couldn't be created. Please try again.", 503, form
        )
    except httpx.RequestError as exc:
        log.warning("creating %s job failed: %r", job_type, exc)
        return _new_job_page(
            request,
            "The job service is unavailable. Please try again shortly.",
            503,
            form,
        )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _duration(start: datetime | None, end: datetime | None) -> str:
    if start is None:
        return ""
    seconds = int(((end or datetime.now(UTC)) - start).total_seconds())
    minutes, seconds = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def _job_row(job: dict) -> dict:
    """A coordinator JobSummary as a job list row."""
    created, finished = _when(job["created_at"]), _when(job["finished_at"])
    return {
        **job,
        "created": admintools.sg_time(created),
        "took": _duration(created, finished),
        "running": finished is None,
    }


async def _pending_rows(user: dict) -> list[dict]:
    """
    The user's uploads that the coordinator doesn't know yet, as job list rows.

    Best effort: if S3 can't be read, the list just shows the coordinator's jobs.
    """
    rows: list[dict] = []
    try:
        job_ids = await asyncio.to_thread(storage.list_pending, str(user["id"]))
        for job_id in job_ids:
            pending = await asyncio.to_thread(
                storage.get_json, storage.pending_key(job_id)
            )
            if pending is None or pending.get("owner_id") != str(user["id"]):
                continue  # the job was created meanwhile, or the marker is stale
            pending = await uploads.expire_if_stale(job_id, pending)
            failed = pending.get("state") == "failed"
            created = _when(pending.get("created_at"))
            rows.append(
                {
                    "id": job_id,
                    "job_type": "ocr",
                    "status": "FAILED" if failed else "PREPARING",
                    "task_count": None,
                    "created": admintools.sg_time(created),
                    "took": "" if failed else _duration(created, None),
                    "running": not failed,
                    "error": pending.get("error") if failed else None,
                    "filename": pending.get("filename"),
                    "created_at": pending.get("created_at"),
                    "pending": True,
                }
            )
    except (BotoCoreError, ClientError, ValueError):
        log.warning("couldn't list pending uploads", exc_info=True)
    return rows


# ---- home: the user's dashboard ----

DASHBOARD_RECENT = 10  # jobs in the "Recent jobs" table
# The newest jobs searched for running ones and counted for the stat cards; 100
# is the most the coordinator's GET /jobs returns at once.
DASHBOARD_SCAN = 100
ACTIVE_STATUSES = frozenset({"PREPARING", "PENDING", "RUNNING"})


async def _with_progress(token: str, row: dict) -> dict:
    """
    Adds live progress to an active job's row. Best effort: an upload still being
    prepared, or a job that can't be loaded, keeps its row without progress.
    """
    if row.get("pending"):
        return row
    try:
        view = jobtools.progress_view(await coordinator.get_job(token, row["id"]))
    except httpx.HTTPError:
        return row
    return {
        **row,
        "progress": view["progress"],
        "tasks_verified": view["tasks_verified"],
        "tasks_total": view["tasks_total"],
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """The signed-in user's homepage: running jobs, counts and recent jobs."""
    if not request.cookies.get(settings.session_cookie):
        return RedirectResponse("/login", status_code=303)
    user = await current_user(request)
    if user.get("role") == "ADMIN":
        return RedirectResponse("/admin", status_code=303)  # admins have their own
    token = _token(request)
    error = None
    listed: list[dict] = []
    try:
        listed = await coordinator.list_jobs(token, DASHBOARD_SCAN, 0)
    except httpx.HTTPError:
        error = "Your jobs can't be loaded right now. Please try again shortly."
    rows = [_job_row(job) for job in listed]
    try:
        quota = await coordinator.job_quota(token)
    except httpx.HTTPError:
        quota = None  # the slots card shows a dash
    if not error:
        known = {str(row["id"]) for row in rows}
        rows = [r for r in await _pending_rows(user) if r["id"] not in known] + rows
    running_jobs = await asyncio.gather(
        *(_with_progress(token, r) for r in rows if r["status"] in ACTIVE_STATUSES)
    )
    statuses = [r["status"] for r in rows]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "error": error,
            "running_jobs": running_jobs,
            "recent": rows[:DASHBOARD_RECENT],
            "has_more": len(rows) > DASHBOARD_RECENT,
            "counts": {s: statuses.count(s) for s in ("COMPLETED", "FAILED")},
            "capped": len(listed) >= DASHBOARD_SCAN,
            "scan": DASHBOARD_SCAN,
            "quota": quota,
        },
        status_code=503 if error else 200,
    )


# The My jobs status filter: {status: label}. PREPARING is an upload the
# coordinator doesn't know yet; the rest are the coordinator's job statuses.
JOB_FILTERS = {
    "PREPARING": "Preparing",
    "PENDING": "Queued",
    "RUNNING": "Running",
    "COMPLETED": "Completed",
    "FAILED": "Failed",
    "CANCELLED": "Cancelled",
}


def _today() -> date:
    """Today in Singapore, the days the job list's filter and times use."""
    return datetime.now(admintools.SGT).date()


def _day_start(day: date) -> datetime:
    """Midnight at the start of a day in Singapore."""
    return datetime.combine(day, time.min, admintools.SGT)


def _parse_day(value: str | None) -> date | None:
    """
    A YYYY-MM-DD from a date input, or None if it is blank, not a date, or in
    the future (no job can have been submitted then).
    """
    try:
        day = date.fromisoformat(value) if value else None
    except ValueError:
        return None
    return day if day is None or day <= _today() else None


def _filter_query(statuses: list[str], start: date | None, end: date | None) -> str:
    """The My jobs filter as a query string, without the page offset."""
    pairs = [("status", s) for s in statuses]
    pairs += [
        (name, day.isoformat()) for name, day in (("from", start), ("to", end)) if day
    ]
    return urlencode(pairs)


def _created_between(row: dict, start: date | None, end: date | None) -> bool:
    """Whether a job list row was submitted on a Singapore day in [start, end]."""
    if start is None and end is None:
        return True
    created = _when(row.get("created_at"))
    if created is None:
        return False
    day = created.astimezone(admintools.SGT).date()
    return (start is None or day >= start) and (end is None or day <= end)


async def _has_any_job(token: str) -> bool:
    """Whether the coordinator has any job for this user, ignoring filters."""
    try:
        return bool(await coordinator.list_jobs(token, 1, 0))
    except httpx.HTTPError:
        return True  # can't tell, so keep the filter on screen to change it


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(
    request: Request,
    user: Annotated[dict, Depends(regular_user)],
    offset: int = Query(0, ge=0),
    status: Annotated[list[str] | None, Query()] = None,
    date_from: Annotated[str | None, Query(alias="from")] = None,
    date_to: Annotated[str | None, Query(alias="to")] = None,
):
    token = request.cookies[settings.session_cookie]
    # unknown values are ignored; nothing chosen means every status
    selected = [s for s in JOB_FILTERS if s in (status or [])]
    wanted = [s for s in selected if s != "PREPARING"]
    start, end = _parse_day(date_from), _parse_day(date_to)
    error = None
    rows: list[dict] = []
    has_more = False
    try:
        # only "Preparing" chosen: the coordinator has none of those jobs
        if wanted or not selected:
            listed = await coordinator.list_jobs(
                token,
                JOBS_PAGE_SIZE + 1,
                offset,
                wanted,
                _day_start(start) if start else None,
                # end is at most today, so the next day always exists
                _day_start(end + timedelta(days=1)) if end else None,
            )
            has_more = len(listed) > JOBS_PAGE_SIZE
            rows = [_job_row(job) for job in listed[:JOBS_PAGE_SIZE]]
    except (httpx.HTTPStatusError, httpx.RequestError):
        error = "Your jobs can't be loaded right now. Please try again shortly."
    filtered = bool(selected or start or end)
    has_jobs = bool(rows) or offset > 0
    if offset == 0 and not error:
        pending = await _pending_rows(user)
        known = {row["id"] for row in rows}
        rows = [
            r
            for r in pending
            if r["id"] not in known
            and (not selected or r["status"] in selected)
            and _created_between(r, start, end)
        ] + rows
        # nothing matched: the filter is only shown if the user has any job
        has_jobs = (
            bool(rows) or bool(pending) or (filtered and await _has_any_job(token))
        )

    def url(statuses: list[str]) -> str:
        query = _filter_query(statuses, start, end)
        return f"/jobs?{query}" if query else "/jobs"

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "rows": rows,
            "error": error,
            "offset": offset,
            "page_size": JOBS_PAGE_SIZE,
            "has_more": has_more,
            "selected": selected,
            "filtered": filtered,
            "has_jobs": has_jobs,
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "today": _today().isoformat(),  # the latest day the date inputs allow
            # statuses that can still be added to the filter
            "options": {
                s: label for s, label in JOB_FILTERS.items() if s not in selected
            },
            # one removable chip per chosen status, linking to the filter without it
            "chips": [
                {"label": JOB_FILTERS[s], "href": url([o for o in selected if o != s])}
                for s in selected
            ],
            # keeps the filter on the Newer/Older links
            "filter_query": _filter_query(selected, start, end),
        },
        status_code=503 if error else 200,
    )


async def load_progress(token: str, user: dict, job_id: UUID) -> dict | None:
    """
    Finds what to show for a job: the coordinator's job, or the upload still being
    prepared. None means there is no such job for this user.

    raises:
        httpx.HTTPError: If the coordinator can't answer (anything but "not found").
        BotoCoreError, ClientError: If S3 can't answer.
    """
    try:
        return jobtools.progress_view(await coordinator.get_job(token, job_id))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    pending = await asyncio.to_thread(storage.get_json, storage.pending_key(job_id))
    if pending is not None and pending.get("owner_id") == str(user["id"]):
        pending = await uploads.expire_if_stale(job_id, pending)
        return jobtools.preparing_view(pending, str(job_id))
    return None


def _unavailable(request: Request, exc: Exception) -> HTTPException:
    """Turns a failed coordinator or S3 call into the error to send back."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        return HTTPException(401, "Your session has expired. Log in again.")
    log.warning("loading job failed: %r", exc)
    return HTTPException(503, "Job status is unavailable. Please try again shortly.")


_LOAD_ERRORS = (httpx.HTTPError, BotoCoreError, ClientError)


@app.get("/jobs/{job_id}", response_class=HTMLResponse, dependencies=[SignedIn])
async def job_detail(request: Request, job_id: UUID):
    return templates.TemplateResponse(request, "job_detail.html", {"job_id": job_id})


@app.get("/jobs/{job_id}/status")
async def job_status(
    request: Request, job_id: UUID, user: Annotated[dict, Depends(current_user)]
):
    """One snapshot of the job's progress as JSON."""
    token = request.cookies[settings.session_cookie]
    try:
        view = await load_progress(token, user, job_id)
    except _LOAD_ERRORS as exc:
        raise _unavailable(request, exc) from exc
    if view is None:
        raise HTTPException(404, "Job not found")
    return view


def _sse(data: dict, event: str | None = None) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data)}\n\n"


@app.get("/jobs/{job_id}/stream")
async def job_stream(
    request: Request, job_id: UUID, user: Annotated[dict, Depends(current_user)]
):
    """
    Live progress as server-sent events: a message each time something changes,
    then a "done" event once the job has finished.
    """
    token = request.cookies[settings.session_cookie]

    async def events():
        last = None
        quiet = 0.0
        while not await request.is_disconnected():
            try:
                view = await load_progress(token, user, job_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    yield _sse({}, "auth")
                    return
                view, failed = None, True
            except _LOAD_ERRORS:
                view, failed = None, True
            else:
                failed = False
            if view is None and not failed:
                yield _sse({}, "gone")
                return
            if view is not None and view != last:
                last, quiet = view, 0.0
                yield _sse(view)
                if view["terminal"]:
                    yield _sse({}, "done")
                    return
            elif quiet >= settings.stream_keepalive:
                quiet = 0.0
                yield ": keepalive\n\n"
            await asyncio.sleep(settings.stream_interval)
            quiet += settings.stream_interval

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _pages_per_task(token: str, pages: int, cpu: int, mem: int) -> int:
    """
    Pages per task for a new PDF, from how many copies the workers can take now
    (jobs.pages_per_task_for). Unknown capacity keeps the largest task size.
    """
    try:
        free = (await coordinator.capacity(token, cpu, mem))["slots"]
    except (httpx.HTTPError, KeyError, TypeError) as exc:
        log.warning("couldn't read free capacity, using full-size tasks: %r", exc)
        free = None
    return jobtools.pages_per_task_for(pages, free, settings.replicas_per_task)


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(
    request: Request, job_id: UUID, user: Annotated[dict, Depends(current_user)]
):
    """
    Cancels a job. An upload still being prepared (or one that failed) has no
    coordinator job yet, so cancelling it, or dismissing it from the list, just
    forgets it and deletes its stored token.
    """
    token = request.cookies[settings.session_cookie]
    try:
        await coordinator.cancel_job(token, job_id)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 404:
            return await _discard_pending_upload(request, job_id, user)
        if code != 409:  # 409: it had already ended, so there is nothing to cancel
            raise _unavailable(request, exc) from exc
    except httpx.RequestError as exc:
        raise _unavailable(request, exc) from exc
    # pages may still be rendering: forget the upload, so later batches don't
    # try to add tasks to the cancelled job (they would be refused anyway)
    await _forget_pending(job_id, user)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


async def _forget_pending(job_id: UUID, user: dict) -> None:
    """Deletes the upload record of one of the user's jobs, if it still has one."""
    try:
        pending = await asyncio.to_thread(storage.get_json, storage.pending_key(job_id))
        if pending is not None and pending.get("owner_id") == str(user["id"]):
            await uploads.discard_pending(job_id, pending)
    except (BotoCoreError, ClientError):
        log.warning("couldn't forget the upload of cancelled job %s", job_id)


async def _discard_pending_upload(request: Request, job_id: UUID, user: dict):
    try:
        pending = await asyncio.to_thread(storage.get_json, storage.pending_key(job_id))
        if pending is None or pending.get("owner_id") != str(user["id"]):
            raise HTTPException(404, "Job not found")
        await uploads.discard_pending(job_id, pending)
    except (BotoCoreError, ClientError) as exc:
        raise _unavailable(request, exc) from exc
    return RedirectResponse("/jobs", status_code=303)


@app.get("/jobs/{job_id}/result.txt")
async def job_result(
    request: Request, job_id: UUID, user: Annotated[dict, Depends(current_user)]
):
    """Downloads the agreed text of a completed job."""
    token = request.cookies[settings.session_cookie]
    try:
        job = await coordinator.get_job(token, job_id)
        text = await asyncio.to_thread(storage.get_bytes, storage.result_key(job_id))
        if text is None:
            if job["status"] != "COMPLETED":
                raise HTTPException(409, "The job hasn't completed yet.")
            text = (
                await asyncio.to_thread(
                    jobtools.assemble_text,
                    job,
                    lambda rid: storage.get_bytes(
                        storage.replica_result_key(job_id, rid)
                    ),
                )
            ).encode()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, "Job not found") from exc
        raise _unavailable(request, exc) from exc
    except jobtools.ResultUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except (httpx.RequestError, BotoCoreError, ClientError) as exc:
        raise _unavailable(request, exc) from exc
    return Response(
        text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="result-{str(job_id)[:8]}.txt"'
        },
    )


async def _read_limited(request: Request, limit: int) -> bytes:
    """Reads the request body, giving up with 413 as soon as it passes limit bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "Body too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "Body too large")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/hooks/pages-ready")
async def pages_ready(
    request: Request,
    x_idlecloud_timestamp: Annotated[str | None, Header()] = None,
    x_idlecloud_signature: Annotated[str | None, Header()] = None,
):
    """The Lambda page splitter's callback; creates the job once every page exists."""
    if not settings.hmac_secret:
        raise HTTPException(503, "Page callbacks are not configured")
    # refuse stale or unsigned requests, and big ones, before reading the body
    if not uploads.timestamp_is_fresh(x_idlecloud_timestamp) or not (
        x_idlecloud_signature
    ):
        raise HTTPException(401, "Bad signature")
    body = await _read_limited(request, settings.callback_max_bytes)
    if not uploads.verify_signature(
        settings.hmac_secret, body, x_idlecloud_timestamp, x_idlecloud_signature
    ):
        raise HTTPException(401, "Bad signature")
    try:
        event = uploads.PagesReady.model_validate_json(body)
    except ValueError as exc:
        raise HTTPException(422, "Bad callback body") from exc
    try:
        await uploads.on_pages_ready(event)
    except _LOAD_ERRORS as exc:
        # a 5xx makes the Lambda try again
        log.warning("pages-ready for %s failed: %r", event.job_id, exc)
        raise HTTPException(503, "Try again") from exc
    return {"ok": True}


@app.get("/account", response_class=HTMLResponse)
async def account(request: Request, user: Annotated[dict, Depends(current_user)]):
    """Account settings, opened from the profile menu."""
    joined = _when(user.get("created_at"))
    return templates.TemplateResponse(
        request,
        "account.html",
        {"joined": admintools.sg_time(joined, "%d %b %Y")},
    )


# ---- admin dashboard: Overview, Jobs, Burst workers, Event log, Users ----

ADMIN_JOBS_PAGE_SIZE = 20
ADMIN_EVENTS_PAGE_SIZE = 50
ADMIN_USERS_PAGE_SIZE = 50
# What each coordinator call loads, as error messages name it
ADMIN_SECTIONS = {
    "nodes": "worker nodes",
    "counts": "job counts",
    "jobs": "jobs",
    "burst": "burst workers and costs",
    "events": "events",
    "users": "users",
    "user": "user",
}


async def _admin_load(
    request: Request, calls: dict, not_found: str | None = None
) -> tuple[dict, dict, int]:
    """
    Runs an admin page's coordinator calls together, so one failing call
    doesn't blank the whole page.

    args:
        calls: {section name: coroutine}, names from ADMIN_SECTIONS.
        not_found: The section whose 404 means the page itself doesn't exist
            (an unknown user). Any other 404 is shown as that section's error.
    returns:
        tuple: (results, errors, status). A failed call's result is None and its
            message is in errors; status is 503 only if every call failed.
    raises:
        LoginRequired: If the session has expired.
        HTTPException: 404 if the not_found section's call answered 404.
    """
    done = await asyncio.gather(*calls.values(), return_exceptions=True)
    results, errors = {}, {}
    for name, result in zip(calls, done):
        if isinstance(result, httpx.HTTPStatusError):
            if result.response.status_code == 401:
                raise LoginRequired(request.url.path)
            if result.response.status_code == 404 and name == not_found:
                raise HTTPException(
                    404, f"{ADMIN_SECTIONS[name].capitalize()} not found"
                )
        if isinstance(result, httpx.HTTPError):
            log.warning("admin page: loading %s failed: %r", name, result)
            errors[name] = (
                f"Loading the {ADMIN_SECTIONS[name]} failed. Try again shortly."
            )
            result = None
        elif isinstance(result, BaseException):
            raise result
        results[name] = result
    return results, errors, 503 if len(errors) == len(calls) else 200


def _admin_page(
    request: Request, template: str, errors: dict, status: int, **context
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {
            "errors": errors,
            "updated": admintools.sg_time(datetime.now(UTC), "%H:%M:%S SGT"),
            **context,
        },
        status_code=status,
    )


def _token(request: Request) -> str:
    return request.cookies[settings.session_cookie]


@app.get("/admin", response_class=HTMLResponse, dependencies=[AdminOnly])
async def admin_overview(request: Request):
    """Node and job counts at a glance, and every worker node."""
    token = _token(request)
    results, errors, status = await _admin_load(
        request,
        {
            "nodes": coordinator.admin_nodes(token),
            "counts": coordinator.admin_job_counts(token),
        },
    )
    now = datetime.now(UTC)
    nodes = [admintools.node_row(n, now) for n in results["nodes"] or []]
    return _admin_page(
        request,
        "admin_overview.html",
        errors,
        status,
        nodes=nodes,
        node_counts=admintools.node_counts(nodes),
        job_counts=results["counts"],
    )


@app.get("/admin/jobs", response_class=HTMLResponse, dependencies=[AdminOnly])
async def admin_jobs(request: Request, offset: int = Query(0, ge=0)):
    """Every user's jobs, newest first, with their burst cost."""
    token = _token(request)
    results, errors, status = await _admin_load(
        request,
        {
            "jobs": coordinator.admin_jobs(token, ADMIN_JOBS_PAGE_SIZE + 1, offset),
            # the coordinator orders burst jobs like admin_jobs (newest first,
            # then id), so the same offset gives the costs of the jobs shown
            "burst": coordinator.burst_overview(
                token,
                machine_limit=1,
                job_limit=ADMIN_JOBS_PAGE_SIZE,
                job_offset=offset,
            ),
        },
    )
    jobs = results["jobs"] or []
    burst = results["burst"]
    costs = {c["job_id"]: c for c in burst["jobs"]} if burst else {}
    return _admin_page(
        request,
        "admin_jobs.html",
        errors,
        status,
        jobs=[
            {**_job_row(j), "cost": costs.get(str(j["id"]))}
            for j in jobs[:ADMIN_JOBS_PAGE_SIZE]
        ],
        offset=offset,
        page_size=ADMIN_JOBS_PAGE_SIZE,
        has_more=len(jobs) > ADMIN_JOBS_PAGE_SIZE,
    )


@app.get("/admin/burst", response_class=HTMLResponse, dependencies=[AdminOnly])
async def admin_burst(request: Request, offset: int = Query(0, ge=0)):
    """EC2 burst workers, active first, with their estimated cost."""
    token = _token(request)
    results, errors, status = await _admin_load(
        request,
        {
            "burst": coordinator.burst_overview(
                token, machine_limit=BURST_PAGE_SIZE, machine_offset=offset, job_limit=1
            )
        },
    )
    burst = results["burst"]
    return _admin_page(
        request,
        "admin_burst.html",
        errors,
        status,
        burst=burst,
        offset=offset,
        page_size=BURST_PAGE_SIZE,
        has_more=bool(burst) and offset + BURST_PAGE_SIZE < burst["machine_count"],
    )


@app.get("/admin/events", response_class=HTMLResponse, dependencies=[AdminOnly])
async def admin_events(
    request: Request,
    job: Annotated[str | None, Query(max_length=45)] = None,
    node: Annotated[str | None, Query(max_length=253)] = None,
    offset: int = Query(0, ge=0),
):
    """Evictions, lost and failed replicas, optionally for one job or node."""
    # a blank box in the filter form means any job, like a blank node
    job_text = (job or "").strip()
    try:
        job_id = UUID(job_text) if job_text else None
    except ValueError:
        raise HTTPException(422, "Enter a full job ID") from None
    node = (node or "").strip() or None
    results, errors, status = await _admin_load(
        request,
        {
            "events": coordinator.admin_events(
                _token(request), job_id, node, ADMIN_EVENTS_PAGE_SIZE + 1, offset
            )
        },
    )
    events = results["events"] or []
    return _admin_page(
        request,
        "admin_events.html",
        errors,
        status,
        events=[admintools.event_row(e) for e in events[:ADMIN_EVENTS_PAGE_SIZE]],
        filter_job=job_id,
        filter_node=node,
        offset=offset,
        page_size=ADMIN_EVENTS_PAGE_SIZE,
        has_more=len(events) > ADMIN_EVENTS_PAGE_SIZE,
    )


@app.get("/admin/users", response_class=HTMLResponse, dependencies=[AdminOnly])
async def admin_users(request: Request, offset: int = Query(0, ge=0)):
    """Every account, newest first, with how many jobs each has submitted."""
    results, errors, status = await _admin_load(
        request,
        {
            "users": coordinator.admin_users(
                _token(request), ADMIN_USERS_PAGE_SIZE + 1, offset
            )
        },
    )
    users = results["users"] or []
    return _admin_page(
        request,
        "admin_users.html",
        errors,
        status,
        users=[
            {**u, "joined": admintools.sg_time(u["created_at"])}
            for u in users[:ADMIN_USERS_PAGE_SIZE]
        ],
        offset=offset,
        page_size=ADMIN_USERS_PAGE_SIZE,
        has_more=len(users) > ADMIN_USERS_PAGE_SIZE,
    )


@app.get(
    "/admin/users/{user_id}", response_class=HTMLResponse, dependencies=[AdminOnly]
)
async def admin_user_profile(
    request: Request, user_id: UUID, offset: int = Query(0, ge=0)
):
    """One account and its jobs, newest first."""
    token = _token(request)
    results, errors, status = await _admin_load(
        request,
        {
            "user": coordinator.admin_user(token, user_id),
            "jobs": coordinator.admin_jobs(
                token, ADMIN_JOBS_PAGE_SIZE + 1, offset, user_id
            ),
        },
        not_found="user",
    )
    profile = results["user"]
    jobs = results["jobs"] or []
    return _admin_page(
        request,
        "admin_user.html",
        errors,
        status,
        profile={**profile, "joined": admintools.sg_time(profile["created_at"])}
        if profile
        else None,
        jobs=[_job_row(j) for j in jobs[:ADMIN_JOBS_PAGE_SIZE]],
        offset=offset,
        page_size=ADMIN_JOBS_PAGE_SIZE,
        has_more=len(jobs) > ADMIN_JOBS_PAGE_SIZE,
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
