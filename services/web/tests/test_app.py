import re
from urllib.parse import quote
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from services.web.app import app
from services.web.coordinator import coordinator
from services.web.settings import settings

client = TestClient(app)


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_pages_render():
    assert client.get("/").status_code == 200
    assert "Log in" in client.get("/login").text


# --- login and signup -------------------------------------------------------


TOKEN = {
    "access_token": "tok123",
    "token_type": "bearer",
    "expires_at": "2099-01-01T00:00:00Z",
}


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://coordinator/auth")
    return httpx.HTTPStatusError(
        "err", request=req, response=httpx.Response(code, request=req)
    )


@pytest.fixture
def no_redirects():
    return TestClient(app, follow_redirects=False)


def test_login_success_sets_httponly_cookie(monkeypatch, no_redirects):
    async def fake_login(email, password):
        assert (email, password) == ("a@b.com", "secret123")
        return TOKEN

    monkeypatch.setattr(coordinator, "login", fake_login)
    r = no_redirects.post("/login", data={"email": "a@b.com", "password": "secret123"})
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs/new"
    cookie = r.headers["set-cookie"]
    assert f"{settings.session_cookie}=tok123" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_login_wrong_password_shows_error_and_keeps_email(monkeypatch, no_redirects):
    async def fake_login(email, password):
        raise _status_error(401)

    monkeypatch.setattr(coordinator, "login", fake_login)
    r = no_redirects.post("/login", data={"email": "a@b.com", "password": "nope-nope"})
    assert r.status_code == 401
    assert "Wrong email or password" in r.text
    assert 'value="a@b.com"' in r.text
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize(
    "next_url", ["https://evil.example", "//evil.example", "javascript:alert(1)"]
)
def test_login_ignores_offsite_next(monkeypatch, no_redirects, next_url):
    async def fake_login(email, password):
        return TOKEN

    monkeypatch.setattr(coordinator, "login", fake_login)
    r = no_redirects.post(
        "/login", data={"email": "a@b.com", "password": "secret123", "next": next_url}
    )
    assert r.headers["location"] == "/jobs/new"


def test_login_follows_local_next(monkeypatch, no_redirects):
    async def fake_login(email, password):
        return TOKEN

    monkeypatch.setattr(coordinator, "login", fake_login)
    r = no_redirects.post(
        "/login", data={"email": "a@b.com", "password": "secret123", "next": "/jobs"}
    )
    assert r.headers["location"] == "/jobs"


def test_login_coordinator_down(monkeypatch, no_redirects):
    async def fake_login(email, password):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(coordinator, "login", fake_login)
    r = no_redirects.post("/login", data={"email": "a@b.com", "password": "secret123"})
    assert r.status_code == 503
    assert "sign-in service" in r.text


def test_signup_success(monkeypatch, no_redirects):
    async def fake_signup(email, password):
        return TOKEN

    monkeypatch.setattr(coordinator, "signup", fake_signup)
    r = no_redirects.post(
        "/signup",
        data={
            "email": "a@b.com",
            "password": "secret123",
            "confirm_password": "secret123",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs/new"
    assert "HttpOnly" in r.headers["set-cookie"]


def test_signup_password_mismatch(no_redirects):
    r = no_redirects.post(
        "/signup",
        data={
            "email": "a@b.com",
            "password": "secret123",
            "confirm_password": "different1",
        },
    )
    assert r.status_code == 422
    assert "do not match" in r.text


def test_signup_short_password(no_redirects):
    r = no_redirects.post(
        "/signup",
        data={"email": "a@b.com", "password": "short", "confirm_password": "short"},
    )
    assert r.status_code == 422
    assert "8 to 128" in r.text


def test_signup_duplicate_email(monkeypatch, no_redirects):
    async def fake_signup(email, password):
        raise _status_error(409)

    monkeypatch.setattr(coordinator, "signup", fake_signup)
    r = no_redirects.post(
        "/signup",
        data={
            "email": "a@b.com",
            "password": "secret123",
            "confirm_password": "secret123",
        },
    )
    assert r.status_code == 409
    assert "already exists" in r.text


def test_logout_clears_cookie(monkeypatch):
    called = {}

    async def fake_logout(token):
        called["token"] = token

    monkeypatch.setattr(coordinator, "logout", fake_logout)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(settings.session_cookie, "tok123")
    r = c.post("/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert called["token"] == "tok123"
    assert (
        "Max-Age=0" in r.headers["set-cookie"]
        or "expires=" in r.headers["set-cookie"].lower()
    )


PROTECTED = [
    "/jobs/new",
    "/jobs",
    f"/jobs/{UUID(int=1)}",
    "/account",
    "/admin",
]
USER = {"id": str(UUID(int=2)), "email": "a@b.com", "role": "USER"}
ADMIN = {**USER, "role": "ADMIN"}


def _signed_in(monkeypatch, user=USER):
    async def fake_me(token):
        assert token == "tok123"
        return user

    async def fake_list_jobs(token, limit=20, offset=0):
        return []

    async def fake_quota(token):
        return {"active": 0, "limit": 3}

    monkeypatch.setattr(coordinator, "me", fake_me)
    monkeypatch.setattr(coordinator, "list_jobs", fake_list_jobs)
    monkeypatch.setattr(coordinator, "job_quota", fake_quota)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(settings.session_cookie, "tok123")
    return c


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_pages_redirect_when_logged_out(no_redirects, path):
    r = no_redirects.get(path)
    assert r.status_code == 303
    assert r.headers["location"] == f"/login?next={quote(path)}"


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_pages_clear_expired_token(monkeypatch, path):
    async def fake_me(token):
        raise _status_error(401)

    monkeypatch.setattr(coordinator, "me", fake_me)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(settings.session_cookie, "old")
    r = c.get(path)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")
    assert "Max-Age=0" in r.headers["set-cookie"]


@pytest.mark.parametrize("path", PROTECTED[:4])
def test_protected_pages_open_when_logged_in(monkeypatch, path):
    assert _signed_in(monkeypatch).get(path).status_code == 200


def test_coordinator_down_is_not_a_login_redirect(monkeypatch):
    async def fake_me(token):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(coordinator, "me", fake_me)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(settings.session_cookie, "tok123")
    assert c.get("/jobs").status_code == 503


def test_signed_in_users_skip_auth_pages(monkeypatch):
    c = _signed_in(monkeypatch)
    assert c.get("/login").status_code == 303
    assert c.get("/signup").status_code == 303


def test_stale_cookie_still_shows_login_form(monkeypatch):
    async def fake_me(token):
        raise _status_error(401)

    monkeypatch.setattr(coordinator, "me", fake_me)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(settings.session_cookie, "old")
    assert c.get("/login").status_code == 200


def test_no_navbar_when_logged_out(no_redirects):
    for path in ("/login", "/signup"):
        html = no_redirects.get(path).text
        assert "<aside" not in html and "<nav" not in html
        assert "/jobs/new" not in html and 'href="/admin"' not in html
        assert "Campus OCR" in html and "idle campus PCs" in html


def test_home_redirects_to_login_when_logged_out(no_redirects):
    r = no_redirects.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


# Only Google Fonts may come from elsewhere; scripts and styles are served here.
ALLOWED_HOSTS = ("https://fonts.googleapis.com", "https://fonts.gstatic.com")


def test_pages_load_scripts_and_styles_only_from_this_site(monkeypatch):
    signed_in = _signed_in(monkeypatch)
    pages = [client.get("/login").text, client.get("/signup").text]
    pages += [
        signed_in.get(path).text
        for path in ("/jobs/new", "/account", f"/jobs/{UUID(int=1)}")
    ]
    for html in pages:
        for url in re.findall(r'<(?:script|link)[^>]*(?:src|href)="([^"]+)"', html):
            # Relative, so behind an HTTPS proxy (CloudFront) that talks plain
            # HTTP to the app, the browser still loads them over HTTPS
            relative = url.startswith("/") and not url.startswith("//")
            assert relative or url.startswith(ALLOWED_HOSTS), url
        assert 'href="/static/app.css"' in html


def test_built_stylesheet_and_vendored_scripts_are_served():
    for path in (
        "app.css",
        "vendor/basecoat/basecoat.min.js",
        "vendor/basecoat/sidebar.min.js",
        "vendor/basecoat/dropdown-menu.min.js",
    ):
        r = client.get(f"/static/{path}")
        assert r.status_code == 200 and r.content, path


def test_profile_menu_replaces_account_link(monkeypatch):
    html = _signed_in(monkeypatch).get("/jobs").text
    sidebar_links = html.split('id="profile-menu"')[0]
    assert 'href="/account"' not in sidebar_links  # no longer a sidebar link
    menu = html.split('id="profile-menu"')[1]
    assert USER["email"] in menu
    assert 'role="menuitem" href="/account"' in menu
    assert 'action="/logout"' in menu
    # without its script the menu never opens, so Log out can't be reached
    assert 'src="/static/vendor/basecoat/dropdown-menu.min.js"' in html


def test_account_page_shows_the_user(monkeypatch):
    html = _signed_in(monkeypatch, ADMIN).get("/account").text
    assert USER["email"] in html and "Administrator" in html


def test_nav_shown_when_logged_in(monkeypatch):
    html = _signed_in(monkeypatch).get("/account").text
    assert 'href="/jobs/new"' in html and 'href="/jobs"' in html
    assert "Log out" in html and 'href="/signup"' not in html
