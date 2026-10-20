"""
Website and public API (member 5).

Pages are Jinja2 templates. Live status uses Server-Sent Events: the server
renders the same partial template it used for the first page load and the
browser swaps it in (static/live.js), so there is one copy of the markup.
"""

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from serverless.common.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from services.web import jobs, storage
from services.web.coordinator import CoordinatorClient
from services.web.local_split import split_locally
from services.web.middleware import BodyLimitMiddleware, SecurityMiddleware
from services.web.migrate import run_web_migrations
from services.web.settings import (
    ADMIN_PASSWORD,
    COORDINATOR_URL,
    DATABASE_URL,
    HMAC_SECRET,
    LOCAL_SPLIT,
    MAX_RUNNING_JOBS,
    MAX_UPLOAD_BYTES,
    PAGES_PER_TASK,
    REPLICAS_PER_TASK,
    STREAM_INTERVAL,
    STREAM_KEEPALIVE,
    VOTES_TO_VERIFY,
)
from services.web.shared_render import SharedRender
from services.web.webhooks import check_webhook_url, webhook_loop, webhook_secret
from shared.db import session_manager
from shared.migrate import run_migrations

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("web")

BASE_DIR = Path(__file__).parent
DbSession = Annotated[AsyncSession, Depends(session_manager.get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(run_migrations, DATABASE_URL)
    # After the shared ones: the web tables sit alongside jobs, nodes and events
    await asyncio.to_thread(run_web_migrations, DATABASE_URL)
    session_manager.init_db(
        DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    app.state.coordinator = CoordinatorClient(COORDINATOR_URL)
    app.state.background = set()  # local split tasks, kept so they are not collected
    webhooks = asyncio.create_task(webhook_loop(session_manager.session_factory))
    try:
        yield
    finally:
        for task in (webhooks, *app.state.background):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await app.state.coordinator.close()
        await session_manager.close()


app = FastAPI(title="Idle-Compute Cloud", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
# Drop the blank lines {% set %} and {% if %} tags leave, which every SSE message repeats
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True


def body_limit(path: str) -> int:
    """Largest request body each route accepts, checked before it is read."""
    if path in ("/upload", "/api/jobs"):
        return MAX_UPLOAD_BYTES + 2**20  # multipart framing around the file
    return 64 * 1024


# Added in this order, so the headers also go on the body limit's refusals
app.add_middleware(BodyLimitMiddleware, limit_for=body_limit)
app.add_middleware(SecurityMiddleware)
# Live streams on the same page share one render per half interval
renders = SharedRender(ttl=STREAM_INTERVAL / 2)


def coordinator(request: Request) -> CoordinatorClient:
    return request.app.state.coordinator


Coordinator = Annotated[CoordinatorClient, Depends(coordinator)]


# ---- template filters ----


def ago(value: datetime | None) -> str:
    if value is None:
        return "never"
    seconds = int((datetime.now(UTC) - value).total_seconds())
    if seconds < 60:
        return f"{max(seconds, 0)}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return value.strftime("%d %b %Y")


def duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "–"
    seconds = int((end - start).total_seconds())
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def filesize(n: int | None) -> str:
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def trust_label(score: Any) -> str:
    # Same thresholds member 4's verifier uses for the idlecloud/trust node label
    score = float(score)
    return "high" if score >= 0.8 else "low" if score < 0.3 else "medium"


templates.env.filters.update(
    ago=ago,
    duration=duration,
    filesize=filesize,
    trust_label=trust_label,
    short_hash=lambda h: (h or "")[:10],
)
templates.env.globals.update(
    PAGES_PER_TASK=PAGES_PER_TASK,
    REPLICAS_PER_TASK=REPLICAS_PER_TASK,
    MAX_UPLOAD_MB=MAX_UPLOAD_BYTES // 2**20,
    MAX_RUNNING_JOBS=MAX_RUNNING_JOBS,
    VOTES_TO_VERIFY=VOTES_TO_VERIFY,
)


def render(request: Request, name: str, status: int = 200, **context: Any):
    return templates.TemplateResponse(request, name, context, status_code=status)


@app.exception_handler(jobs.UserError)
async def user_error(request: Request, error: jobs.UserError) -> Response:
    if request.url.path.startswith(("/api/", "/hooks/")):
        return JSONResponse({"detail": error.message}, status_code=error.status)
    return render(request, "error.html", error.status, message=error.message)


# ---- shared steps for the website and the API ----


async def read_upload(pdf: UploadFile) -> tuple[str, bytes]:
    """Reads an uploaded file, stopping just past the size limit."""
    # body_limit() already refused a request far over the limit; this is the exact check
    data = await pdf.read(MAX_UPLOAD_BYTES + 1)
    return jobs.check_pdf(pdf.filename or "", data), data


async def accept_upload(
    request: Request,
    session: AsyncSession,
    email: str,
    pdf: UploadFile,
    webhook_url: str | None,
) -> UUID:
    filename, data = await read_upload(pdf)
    if webhook_url:
        webhook_url = await asyncio.to_thread(check_webhook_url, webhook_url)
    job_id = await jobs.start_upload(session, email, filename, data, webhook_url)
    if LOCAL_SPLIT:
        task = asyncio.create_task(
            split_locally(
                session_manager.session_factory,
                request.app.state.coordinator,
                job_id,
                data,
            )
        )
        request.app.state.background.add(task)
        task.add_done_callback(request.app.state.background.discard)
    return job_id


async def load_snapshot(
    session: AsyncSession, coord: CoordinatorClient, job_id: UUID
) -> jobs.JobSnapshot:
    snap = await jobs.snapshot(session, coord, job_id)
    if snap is None:
        raise jobs.UserError(404, "Job not found.")
    return snap


async def result_text(job_id: UUID) -> bytes:
    data = await asyncio.to_thread(storage.get_object, storage.result_key(str(job_id)))
    if data is None:
        raise jobs.UserError(404, "The result is not ready yet.")
    return data


def sse(html: str) -> str:
    # Every line of a multi-line message needs its own "data:" prefix
    return "".join(f"data: {line}\n" for line in html.splitlines()) + "\n"


def live_stream(
    request: Request,
    key: Hashable,
    render_once: Callable[[], Awaitable[tuple[str, bool]]],
) -> StreamingResponse:
    """
    Streams a partial template as Server-Sent Events, sending it again only
    when it changes. render_once returns (html, finished); a finished stream
    sends a "done" event so the browser stops reconnecting. key names the page,
    so viewers of the same page share one render_once per interval.
    """

    async def events() -> AsyncIterator[str]:
        last = None
        quiet = 0.0
        while not await request.is_disconnected():
            html, finished = await renders.get(key, render_once)
            if html != last:
                last, quiet = html, 0.0
                yield sse(html)
            elif quiet >= STREAM_KEEPALIVE:
                quiet = 0.0
                yield ": keepalive\n\n"
            if finished:
                yield "event: done\ndata: \n\n"
                return
            await asyncio.sleep(STREAM_INTERVAL)
            quiet += STREAM_INTERVAL

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # no-transform and X-Accel-Buffering stop proxies holding events back
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


# ---- website ----


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return render(request, "index.html")


@app.post("/upload")
async def upload(
    request: Request,
    session: DbSession,
    email: Annotated[str, Form()],
    pdf: Annotated[UploadFile, File()],
    webhook_url: Annotated[str, Form()] = "",
):
    try:
        email = jobs.clean_email(email)
        job_id = await accept_upload(request, session, email, pdf, webhook_url or None)
    except jobs.UserError as error:
        # Back to the form with what was typed, rather than a bare error page
        return render(
            request,
            "index.html",
            error.status,
            error=error.message,
            email=email,
            webhook_url=webhook_url,
        )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(
    request: Request, job_id: UUID, session: DbSession, coord: Coordinator
):
    snap = await load_snapshot(session, coord, job_id)
    return render(request, "job.html", snap=snap)


@app.get("/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: UUID, coord: Coordinator):
    async def render_once() -> tuple[str, bool]:
        # A new session each time, so the status is read fresh from Postgres
        async with session_manager.session_factory() as session:
            snap = await load_snapshot(session, coord, job_id)
        html = templates.get_template("partials/job_detail.html").render(snap=snap)
        return html, snap.finished

    return live_stream(request, ("job", job_id), render_once)


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(job_id: UUID, session: DbSession, coord: Coordinator):
    await jobs.cancel(session, coord, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}/result")
async def job_result(job_id: UUID):
    return PlainTextResponse(
        await result_text(job_id),
        headers={"Content-Disposition": f'attachment; filename="{job_id}.txt"'},
    )


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, session: DbSession, email: str = ""):
    rows = None
    if email:
        try:
            email = jobs.clean_email(email)
            rows = await jobs.history(session, email)
        except jobs.UserError as error:
            return render(
                request, "history.html", 400, email=email, error=error.message
            )
    return render(request, "history.html", email=email, rows=rows)


@app.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request):
    return render(request, "api_keys.html")


@app.post("/api-keys", response_class=HTMLResponse)
async def api_keys_create(
    request: Request, session: DbSession, email: Annotated[str, Form()]
):
    try:
        email = jobs.clean_email(email)
    except jobs.UserError as error:
        return render(request, "api_keys.html", 400, email=email, error=error.message)
    key = await jobs.create_api_key(session, email)
    return render(
        request,
        "api_keys.html",
        email=email,
        key=key,
        webhook_secret=webhook_secret(email),
    )


# ---- admin ----

basic_auth = HTTPBasic(auto_error=False)


def require_admin(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_auth)],
) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(404, "Not Found")
    ok = credentials is not None and (
        hmac.compare_digest(credentials.username.encode(), b"admin")
        & hmac.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    )
    if not ok:
        raise HTTPException(
            401, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="admin"'}
        )


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin(request: Request, session: DbSession):
    overview = await jobs.admin_overview(session)
    return render(request, "admin.html", **overview)


@app.get("/admin/stream", dependencies=[Depends(require_admin)])
async def admin_stream(request: Request):
    async def render_once() -> tuple[str, bool]:
        async with session_manager.session_factory() as session:
            overview = await jobs.admin_overview(session)
        return templates.get_template("partials/admin_detail.html").render(
            **overview
        ), False

    return live_stream(request, "admin", render_once)


# ---- Lambda callback ----


@app.post("/hooks/pages-ready")
async def hook_pages_ready(request: Request, session: DbSession, coord: Coordinator):
    body = await request.body()
    if not HMAC_SECRET or not verify(
        HMAC_SECRET,
        body,
        request.headers.get(TIMESTAMP_HEADER),
        request.headers.get(SIGNATURE_HEADER),
    ):
        raise HTTPException(401, "bad signature")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "body is not JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")
    # A CoordinatorError propagates as a 500, so the Lambda retries the callback
    await jobs.pages_ready(session, coord, payload)
    return {"ok": True}


# ---- public API ----


async def api_owner(x_api_key: Annotated[str | None, Header()] = None) -> str:
    owner = None
    if x_api_key:
        # Its own session: the request's one must stay free for session.begin()
        async with session_manager.session_factory() as session:
            owner = await jobs.api_key_owner(session, x_api_key)
    if owner is None:
        raise HTTPException(401, "missing or invalid X-API-Key")
    return owner


ApiOwner = Annotated[str, Depends(api_owner)]


async def owned_snapshot(
    session: AsyncSession, coord: CoordinatorClient, job_id: UUID, owner: str
) -> jobs.JobSnapshot:
    snap = await jobs.snapshot(session, coord, job_id)
    # Someone else's job looks the same as a missing one
    if snap is None or snap.upload.owner_email != owner:
        raise jobs.UserError(404, "job not found")
    return snap


@app.post("/api/jobs", status_code=202)
async def api_create_job(
    request: Request,
    session: DbSession,
    owner: ApiOwner,
    pdf: Annotated[UploadFile, File()],
    webhook_url: Annotated[str, Form()] = "",
):
    job_id = await accept_upload(request, session, owner, pdf, webhook_url or None)
    return {"job_id": str(job_id), "status": "SPLITTING", "url": f"/api/jobs/{job_id}"}


@app.get("/api/jobs")
async def api_list_jobs(session: DbSession, owner: ApiOwner):
    return [
        {
            "job_id": str(upload.job_id),
            "filename": upload.filename,
            "status": job.status.value if job else upload.status,
            "page_count": upload.page_count,
            "created_at": upload.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat()
            if job and job.finished_at
            else None,
        }
        for upload, job in await jobs.history(session, owner)
    ]


@app.get("/api/jobs/{job_id}")
async def api_get_job(
    job_id: UUID, session: DbSession, coord: Coordinator, owner: ApiOwner
):
    return (await owned_snapshot(session, coord, job_id, owner)).as_api()


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(
    job_id: UUID, session: DbSession, coord: Coordinator, owner: ApiOwner
):
    await owned_snapshot(session, coord, job_id, owner)
    await session.rollback()  # end the read so cancel can open its own transaction
    await jobs.cancel(session, coord, job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/result")
async def api_job_result(
    job_id: UUID, session: DbSession, coord: Coordinator, owner: ApiOwner
):
    await owned_snapshot(session, coord, job_id, owner)
    return PlainTextResponse(await result_text(job_id))


@app.get("/healthz")
async def healthcheck(session: DbSession):
    try:
        await asyncio.wait_for(session.execute(select(1)), timeout=2)
    except Exception:
        log.exception("healthcheck failed")
        raise HTTPException(503, "database unavailable")
    return {"ok": True}
