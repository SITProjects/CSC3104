import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit
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

from services.web import jobs as jobtools
from services.web import storage, uploads
from services.web.coordinator import coordinator
from services.web.settings import settings

BASE_DIR = Path(__file__).parent
log = logging.getLogger("uvicorn.error")
BURST_PAGE_SIZE = 20  # rows per page in each admin burst table

# Main navigation: (label, path).
NAV_ITEMS = [
    ("New job", "/jobs/new"),
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


def _template_context(request: Request) -> dict:
    return {"signed_in": bool(request.cookies.get(settings.session_cookie))}


templates = Jinja2Templates(
    directory=BASE_DIR / "templates", context_processors=[_template_context]
)
templates.env.globals["nav_items"] = NAV_ITEMS

DEFAULT_AFTER_LOGIN = "/jobs/new"
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
        return await coordinator.me(token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise LoginRequired(target) from exc
        raise HTTPException(503, "Sign-in service unavailable") from exc
    except httpx.RequestError as exc:
        raise HTTPException(503, "Sign-in service unavailable") from exc


async def admin_user(user: Annotated[dict, Depends(current_user)]) -> dict:
    if user.get("role") != "ADMIN":
        raise HTTPException(403, "Admins only")
    return user


SignedIn = Depends(current_user)
AdminOnly = Depends(admin_user)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not request.cookies.get(settings.session_cookie):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "index.html")


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
):
    """The New job page; form refills what the user entered after an error."""
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
        },
        status_code=status,
    )


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
@app.get(NEW_JOB_PATH, response_class=HTMLResponse, dependencies=[SignedIn])
async def new_job(request: Request):
    return _new_job_page(request)


@app.post(NEW_JOB_PATH, response_class=HTMLResponse)
async def new_job_submit(
    request: Request,
    background: BackgroundTasks,
    user: Annotated[dict, Depends(current_user)],
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
                    "created": created.strftime("%d %b %Y %H:%M UTC")
                    if created
                    else "",
                    "took": "" if failed else _duration(created, None),
                    "running": not failed,
                    "error": pending.get("error") if failed else None,
                    "filename": pending.get("filename"),
                    "pending": True,
                }
            )
    except (BotoCoreError, ClientError, ValueError):
        log.warning("couldn't list pending uploads", exc_info=True)
    return rows


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(
    request: Request,
    user: Annotated[dict, Depends(current_user)],
    offset: int = Query(0, ge=0),
):
    token = request.cookies[settings.session_cookie]
    error = None
    rows: list[dict] = []
    has_more = False
    try:
        listed = await coordinator.list_jobs(token, JOBS_PAGE_SIZE + 1, offset)
        has_more = len(listed) > JOBS_PAGE_SIZE
        for job in listed[:JOBS_PAGE_SIZE]:
            created, finished = _when(job["created_at"]), _when(job["finished_at"])
            rows.append(
                {
                    **job,
                    "created": created.strftime("%d %b %Y %H:%M UTC")
                    if created
                    else "",
                    "took": _duration(created, finished),
                    "running": finished is None,
                }
            )
    except (httpx.HTTPStatusError, httpx.RequestError):
        error = "Your jobs can't be loaded right now. Please try again shortly."
    if offset == 0 and not error:
        known = {row["id"] for row in rows}
        rows = [r for r in await _pending_rows(user) if r["id"] not in known] + rows
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "rows": rows,
            "error": error,
            "offset": offset,
            "page_size": JOBS_PAGE_SIZE,
            "has_more": has_more,
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
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


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


@app.get("/account", response_class=HTMLResponse, dependencies=[SignedIn])
async def account(request: Request):
    return templates.TemplateResponse(request, "account.html")


@app.get("/admin/nodes", response_class=HTMLResponse, dependencies=[AdminOnly])
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
