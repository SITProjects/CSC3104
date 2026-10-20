from typing import Annotated

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient

from services.web import app as web_app
from services.web.middleware import BodyLimitMiddleware, SecurityMiddleware


def make_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, limit_for=lambda path: 100)
    app.add_middleware(SecurityMiddleware)

    @app.post("/echo")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    @app.get("/page")
    async def page():
        return {"ok": True}

    @app.get("/docs")
    async def docs():
        return {"ok": True}

    return TestClient(app)


def test_body_over_the_limit_is_refused_from_content_length():
    response = make_client().post("/echo", content=b"x" * 101)
    assert response.status_code == 413


def test_chunked_body_over_the_limit_is_refused_while_reading():
    # A generator has no Content-Length, so only counting the bytes can catch it
    chunks = (b"x" * 40 for _ in range(5))
    response = make_client().post("/echo", content=chunks)
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"


def test_body_within_the_limit_passes():
    response = make_client().post("/echo", content=b"x" * 100)
    assert response.status_code == 200
    assert response.json() == {"size": 100}


def test_cross_site_post_is_refused():
    client = make_client()
    refused = client.post(
        "/echo", content=b"hi", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert refused.status_code == 403
    assert "x-frame-options" in refused.headers
    for site in ("same-origin", "same-site", "none"):
        ok = client.post("/echo", content=b"hi", headers={"Sec-Fetch-Site": site})
        assert ok.status_code == 200
    # curl and API clients send no such header
    assert client.post("/echo", content=b"hi").status_code == 200


def test_cross_site_get_is_not_blocked():
    response = make_client().get("/page", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200


def test_security_headers_on_pages_but_no_csp_on_docs():
    client = make_client()
    page = client.get("/page")
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" not in client.get("/docs").headers


def test_web_app_has_both_middlewares_and_limits_the_hook():
    client = TestClient(web_app.app)  # no "with": skips the lifespan's database
    page = client.get("/api-keys")
    assert page.status_code == 200
    assert "content-security-policy" in page.headers
    # Over the 64 KiB default and unsigned: refused for size before the signature
    big = client.post("/hooks/pages-ready", content=b"x" * (64 * 1024 + 1))
    assert big.status_code == 413
    assert web_app.body_limit("/upload") > web_app.MAX_UPLOAD_BYTES
    assert web_app.body_limit("/api/jobs") == web_app.body_limit("/upload")


def test_multipart_upload_is_limited_while_the_form_is_parsed():
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, limit_for=lambda path: 300)

    @app.post("/upload")
    async def upload(pdf: Annotated[UploadFile, File()]):
        return {"size": len(await pdf.read())}

    def multipart(size: int):
        # Sent in pieces, so there is no Content-Length to check up front
        yield b'--b\r\nContent-Disposition: form-data; name="pdf"; filename="a.pdf"\r\n\r\n'
        for _ in range(size // 50):
            yield b"x" * 50
        yield b"\r\n--b--\r\n"

    headers = {"Content-Type": "multipart/form-data; boundary=b"}
    client = TestClient(app)
    assert client.post("/upload", content=multipart(100), headers=headers).json() == {
        "size": 100
    }
    assert (
        client.post("/upload", content=multipart(500), headers=headers).status_code
        == 413
    )
