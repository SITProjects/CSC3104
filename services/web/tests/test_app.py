from fastapi.testclient import TestClient

from services.web.app import app

client = TestClient(app)


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_pages_render():
    assert client.get("/").status_code == 200
    assert "Log in" in client.get("/login").text
