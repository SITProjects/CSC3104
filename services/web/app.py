import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services.web.coordinator import coordinator
from services.web.settings import settings

BASE_DIR = Path(__file__).parent
log = logging.getLogger("uvicorn.error")

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


def _template_context(request: Request) -> dict:
    return {"signed_in": bool(request.cookies.get(settings.session_cookie))}


templates = Jinja2Templates(
    directory=BASE_DIR / "templates", context_processors=[_template_context]
)
templates.env.globals["nav_items"] = NAV_ITEMS

DEFAULT_AFTER_LOGIN = "/upload"
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


@app.get("/upload", response_class=HTMLResponse, dependencies=[SignedIn])
async def upload(request: Request):
    return templates.TemplateResponse(request, "upload.html")


@app.get("/jobs", response_class=HTMLResponse, dependencies=[SignedIn])
async def jobs(request: Request):
    return templates.TemplateResponse(request, "jobs.html")


@app.get("/jobs/{job_id}", response_class=HTMLResponse, dependencies=[SignedIn])
async def job_detail(request: Request, job_id: UUID):
    return templates.TemplateResponse(request, "job_detail.html", {"job_id": job_id})


@app.get("/account", response_class=HTMLResponse, dependencies=[SignedIn])
async def account(request: Request):
    return templates.TemplateResponse(request, "account.html")


@app.get("/admin/nodes", response_class=HTMLResponse, dependencies=[AdminOnly])
async def admin_nodes(request: Request):
    return templates.TemplateResponse(request, "admin_nodes.html")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
