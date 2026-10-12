import httpx
import sensor


def test_read_state_has_contract_fields(monkeypatch):
    monkeypatch.setattr(sensor, "get_idle_seconds", lambda: 12.345)
    monkeypatch.setattr(sensor.psutil, "cpu_percent", lambda interval=None: 7.5)

    assert sensor.read_state() == {"idle_seconds": 12.3, "cpu_percent": 7.5}


def test_send_state_posts_reading():
    received = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append((str(request.url), request.read()))
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert sensor.send_state(client, {"idle_seconds": 1.0, "cpu_percent": 2.0})

    assert received == [(sensor.AGENT_URL, b'{"idle_seconds":1.0,"cpu_percent":2.0}')]


def test_send_state_survives_agent_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert not sensor.send_state(client, {"idle_seconds": 1.0, "cpu_percent": 2.0})
