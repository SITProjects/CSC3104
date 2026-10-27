"""Upload, page-ready callback, progress status/stream, download and cancel."""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from services.web import storage, uploads
from services.web.admin import SGT
from services.web.app import app
from services.web.coordinator import coordinator
from services.web.settings import settings
from services.web.tests.test_jobs import H, make_pdf, replica, sample_job

USER = {"id": str(UUID(int=2)), "email": "a@b.com", "role": "USER"}
JOB_ID = UUID(int=7)
SECRET = "shh"


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def install(self, monkeypatch):
        monkeypatch.setattr(
            storage, "put_bytes", lambda k, b, t: self.objects.update({k: b})
        )
        monkeypatch.setattr(storage, "get_bytes", lambda k: self.objects.get(k))
        monkeypatch.setattr(storage, "delete", lambda k: self.objects.pop(k, None))
        monkeypatch.setattr(
            storage,
            "count_pages",
            lambda job_id: sum(
                k.startswith(f"jobs/{job_id}/pages/") for k in self.objects
            ),
        )
        monkeypatch.setattr(
            storage,
            "put_json",
            lambda k, v: self.objects.update({k: json.dumps(v).encode()}),
        )
        monkeypatch.setattr(
            storage,
            "get_json",
            lambda k: json.loads(self.objects[k]) if k in self.objects else None,
        )

        prefix = "pending-index/"
        monkeypatch.setattr(
            storage,
            "list_pending",
            lambda owner_id, limit=20: [
                k.rsplit("/", 1)[1]
                for k in self.objects
                if k.startswith(f"{prefix}{owner_id}/")
            ][:limit],
        )

    def json(self, key):
        return json.loads(self.objects[key])


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    fake.install(monkeypatch)
    return fake


def status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://coordinator/x")
    return httpx.HTTPStatusError(
        "err", request=req, response=httpx.Response(code, request=req)
    )


@pytest.fixture
def client(monkeypatch, s3):
    async def fake_me(token):
        assert token == "tok123"
        return USER

    monkeypatch.setattr(coordinator, "me", fake_me)
    set_quota(monkeypatch, active=0)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(settings.session_cookie, "tok123")
    return c


def set_quota(monkeypatch, active: int, limit: int = 3):
    async def fake_quota(token):
        return {"active": active, "limit": limit}

    monkeypatch.setattr(coordinator, "job_quota", fake_quota)


@pytest.fixture
def created(monkeypatch):
    """Records POST /jobs calls to the coordinator."""
    calls = []

    async def fake_create(token, spec):
        calls.append((token, spec))
        return {"job_id": spec["job_id"]}

    monkeypatch.setattr(coordinator, "create_job", fake_create)
    return calls


# --- new job -----------------------------------------------------------------


def post_pdf(client, data, name="scan.pdf", **fields):
    return client.post(
        "/jobs/new",
        data={"job_type": "ocr", **fields},
        files={"file": (name, data, "application/pdf")},
    )


def post_test_job(client, **fields):
    return client.post("/jobs/new", data={"job_type": "test", **fields})


def test_new_job_page_lists_job_types_and_limits(client):
    r = client.get("/jobs/new")
    assert r.status_code == 200
    assert 'name="job_type"' in r.text
    assert 'value="ocr"' in r.text and 'value="test"' in r.text
    assert 'name="file"' in r.text and "50 MB" in r.text
    assert 'name="cpu"' in r.text and 'name="mem"' in r.text


def test_new_job_needs_a_known_job_type(client, s3, created):
    for fields in ({}, {"job_type": "bitcoin"}):
        r = client.post("/jobs/new", data=fields)
        assert r.status_code == 422 and "Choose a job type" in r.text
    assert s3.objects == {} and created == []


def test_ocr_job_uses_its_default_resources_whatever_is_sent(client, s3):
    r = post_pdf(client, make_pdf(1), cpu="16", mem="4096")
    job_id = r.headers["location"].removeprefix("/jobs/")
    pending = s3.json(f"jobs/{job_id}/pending.json")
    assert (pending["cpu_request"], pending["mem_request"]) == (1, 512)


def test_test_job_is_created_at_once_with_chosen_resources(client, s3, created):
    r = post_test_job(client, cpu="4", mem="1024")
    assert r.status_code == 303
    ((token, spec),) = created
    assert token == "tok123"
    assert r.headers["location"] == f"/jobs/{spec['job_id']}"
    assert spec["job_type"] == "test" and spec["tasks"] == [{"input": {}}]
    assert (spec["cpu_request"], spec["mem_request"]) == (4, 1024)
    assert s3.objects == {}  # nothing to upload


def test_test_job_falls_back_to_defaults_when_blank(client, created):
    post_test_job(client, cpu="", mem=" ")
    ((_, spec),) = created
    assert (spec["cpu_request"], spec["mem_request"]) == (1, 64)


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"cpu": "0"}, "between 1 and 16"),
        ({"cpu": "1.5"}, "whole number"),
        ({"mem": "8192"}, "between 64 and 4096"),
        ({"mem": "lots"}, "whole number"),
    ],
)
def test_test_job_rejects_bad_resources(client, created, fields, message):
    r = post_test_job(client, **fields)
    assert r.status_code == 422 and message in r.text
    assert created == []


def test_test_job_reports_coordinator_failure(client, monkeypatch):
    async def down(token, spec):
        raise status_error(500)

    monkeypatch.setattr(coordinator, "create_job", down)
    r = post_test_job(client, cpu="2")
    assert r.status_code == 503 and "be created. Please try again" in r.text
    assert 'value="2"' in r.text  # what the user typed is kept


def test_test_job_with_expired_session_goes_to_login(client, monkeypatch):
    async def expired(token, spec):
        raise status_error(401)

    monkeypatch.setattr(coordinator, "create_job", expired)
    r = post_test_job(client)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_upload_saves_pdf_and_redirects_to_job(client, s3):
    r = post_pdf(client, make_pdf(3))
    assert r.status_code == 303
    job_id = r.headers["location"].removeprefix("/jobs/")
    UUID(job_id)  # a real job id
    assert s3.objects[f"jobs/{job_id}/input.pdf"].startswith(b"%PDF-")
    pending = s3.json(f"jobs/{job_id}/pending.json")
    assert pending["state"] == "rendering"
    assert f"pending-index/{USER['id']}/{job_id}" in s3.objects
    assert (pending["owner_id"], pending["token"], pending["page_count"]) == (
        USER["id"],
        "tok123",
        3,
    )


# --- running jobs limit ---------------------------------------------------------


def test_new_job_page_below_limit_has_no_notice(client):
    html = client.get("/jobs/new").text
    assert "Job limit reached" not in html
    assert 'id="job-submit">' in html  # not disabled


def test_new_job_page_at_limit_shows_notice_and_disables_submit(client, monkeypatch):
    set_quota(monkeypatch, active=3)
    html = client.get("/jobs/new").text
    assert "Job limit reached" in html and "3 jobs running" in html
    assert 'id="job-submit" disabled' in html


def test_upload_at_limit_is_refused_before_saving(client, s3, monkeypatch, created):
    set_quota(monkeypatch, active=3)
    r = post_pdf(client, make_pdf(1))
    assert r.status_code == 429 and "3 jobs running" in r.text
    assert s3.objects == {} and created == []


def test_uploads_being_prepared_count_toward_limit(client, s3, monkeypatch):
    set_quota(monkeypatch, active=2)
    assert post_pdf(client, make_pdf(1)).status_code == 303  # 2 jobs + 1 upload
    r = post_pdf(client, make_pdf(1))
    assert r.status_code == 429


def test_failed_uploads_do_not_count_toward_limit(client, s3, monkeypatch):
    set_quota(monkeypatch, active=2)
    seed_pending(s3, state="failed")
    s3.objects[f"pending-index/{USER['id']}/{JOB_ID}"] = b""
    assert post_pdf(client, make_pdf(1)).status_code == 303


def test_test_job_at_limit_is_refused(client, monkeypatch, created):
    set_quota(monkeypatch, active=3)
    assert post_test_job(client).status_code == 429 and created == []


def test_coordinator_limit_is_shown_on_new_job_page(client, monkeypatch):
    async def too_many(token, spec):
        raise status_error(429)

    monkeypatch.setattr(coordinator, "create_job", too_many)
    r = post_test_job(client)
    assert r.status_code == 429 and "most jobs allowed" in r.text


def test_limit_check_failing_does_not_block_new_jobs(client, monkeypatch, created):
    async def down(token):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(coordinator, "job_quota", down)
    assert client.get("/jobs/new").status_code == 200
    assert post_test_job(client).status_code == 303  # the coordinator still enforces it


def test_upload_rejects_non_pdf(client, s3):
    r = post_pdf(client, b"not a pdf at all", "notes.pdf")
    assert r.status_code == 422 and "not a PDF" in r.text
    assert s3.objects == {}


def test_upload_rejects_oversized_file(client, s3, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    r = post_pdf(client, make_pdf(1))
    assert r.status_code == 413 and "over 0 MB" in r.text
    assert s3.objects == {}


def test_upload_without_file_asks_for_one(client):
    r = client.post("/jobs/new", data={"job_type": "ocr"})
    assert r.status_code == 422 and "Choose a PDF" in r.text


def test_upload_requires_login(s3):
    r = TestClient(app, follow_redirects=False).post(
        "/jobs/new",
        data={"job_type": "ocr"},
        files={"file": ("a.pdf", make_pdf(1), "application/pdf")},
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert s3.objects == {}


def test_upload_reports_storage_failure(client, monkeypatch):
    from botocore.exceptions import EndpointConnectionError

    def boom(*args):
        raise EndpointConnectionError(endpoint_url="http://minio")

    monkeypatch.setattr(storage, "put_json", boom)
    r = post_pdf(client, make_pdf(1))
    assert r.status_code == 503 and "store the file" in r.text


# --- pages-ready callback ----------------------------------------------------


def signed_post(client, payload, secret=SECRET, now=None):
    body = json.dumps(payload).encode()
    ts = str(int(now or time.time()))
    import hashlib
    import hmac

    sig = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/hooks/pages-ready",
        content=body,
        headers={
            "X-Idlecloud-Timestamp": ts,
            "X-Idlecloud-Signature": sig,
            "Content-Type": "application/json",
        },
    )


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setattr(settings, "hmac_secret", SECRET)


def seed_pending(s3, pages=25, state="rendering"):
    s3.objects[f"jobs/{JOB_ID}/pending.json"] = json.dumps(
        {"state": state, "owner_id": USER["id"], "token": "tok123", "page_count": pages}
    ).encode()


def seed_pages(s3, count):
    for n in range(1, count + 1):
        s3.objects[f"jobs/{JOB_ID}/pages/{n:04d}.png"] = b"png"


def ok_event(**extra):
    return {"status": "ok", "job_id": str(JOB_ID), "page_count": 25, **extra}


def test_hook_rejects_bad_signature_and_old_timestamps(client, hook, created):
    assert signed_post(client, ok_event(), secret="wrong").status_code == 401
    assert signed_post(client, ok_event(), now=time.time() - 3600).status_code == 401
    assert client.post("/hooks/pages-ready", json=ok_event()).status_code == 401
    assert created == []


def test_hook_is_off_without_a_secret(client, created):
    assert client.post("/hooks/pages-ready", json=ok_event()).status_code == 503


def test_hook_waits_until_every_page_exists(client, hook, s3, created):
    seed_pending(s3)
    seed_pages(s3, 20)
    assert signed_post(client, ok_event(page_start=1, page_end=20)).status_code == 200
    assert created == []
    assert f"jobs/{JOB_ID}/pending.json" in s3.objects


def test_hook_creates_job_with_uploaders_token_once_complete(client, hook, s3, created):
    seed_pending(s3)
    seed_pages(s3, 25)
    assert signed_post(client, ok_event(page_start=21, page_end=25)).status_code == 200
    ((token, spec),) = created
    assert token == "tok123"
    assert spec["job_id"] == str(JOB_ID) and len(spec["tasks"]) == 3
    # the token is not kept once it has been used
    assert f"jobs/{JOB_ID}/pending.json" not in s3.objects
    assert f"pending-index/{USER['id']}/{JOB_ID}" not in s3.objects


def test_hook_creates_job_with_the_resources_saved_at_upload(client, hook, s3, created):
    seed_pending(s3)
    pending = s3.json(f"jobs/{JOB_ID}/pending.json")
    s3.objects[f"jobs/{JOB_ID}/pending.json"] = json.dumps(
        {**pending, "cpu_request": 2, "mem_request": 768}
    ).encode()
    seed_pages(s3, 25)
    signed_post(client, ok_event())
    ((_, spec),) = created
    assert (spec["cpu_request"], spec["mem_request"]) == (2, 768)


def test_hook_falls_back_to_fixed_resources_for_older_uploads(
    client, hook, s3, created
):
    seed_pending(s3)  # saved before uploads recorded their resources
    seed_pages(s3, 25)
    signed_post(client, ok_event())
    ((_, spec),) = created
    assert (spec["cpu_request"], spec["mem_request"]) == (
        settings.task_cpu,
        settings.task_mem_mib,
    )


def test_hook_duplicate_callback_is_harmless(client, hook, s3, created):
    seed_pending(s3)
    seed_pages(s3, 25)
    signed_post(client, ok_event())
    assert signed_post(client, ok_event()).status_code == 200
    assert len(created) == 1


def test_hook_treats_coordinator_conflict_as_done(client, hook, s3, monkeypatch):
    async def conflict(token, spec):
        raise status_error(409)

    monkeypatch.setattr(coordinator, "create_job", conflict)
    seed_pending(s3)
    seed_pages(s3, 25)
    assert signed_post(client, ok_event()).status_code == 200
    assert f"jobs/{JOB_ID}/pending.json" not in s3.objects


def test_hook_asks_lambda_to_retry_when_coordinator_is_down(
    client, hook, s3, monkeypatch
):
    async def down(token, spec):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(coordinator, "create_job", down)
    seed_pending(s3)
    seed_pages(s3, 25)
    assert signed_post(client, ok_event()).status_code == 503
    assert f"jobs/{JOB_ID}/pending.json" in s3.objects


def test_hook_expired_session_fails_upload_and_drops_token(
    client, hook, s3, monkeypatch
):
    async def expired(token, spec):
        raise status_error(401)

    monkeypatch.setattr(coordinator, "create_job", expired)
    seed_pending(s3)
    seed_pages(s3, 25)
    assert signed_post(client, ok_event()).status_code == 200
    pending = s3.json(f"jobs/{JOB_ID}/pending.json")
    assert pending["state"] == "failed" and "token" not in pending


def test_hook_over_limit_fails_upload_without_retry(client, hook, s3, monkeypatch):
    async def too_many(token, spec):
        raise status_error(429)

    monkeypatch.setattr(coordinator, "create_job", too_many)
    seed_pending(s3)
    seed_pages(s3, 25)
    assert signed_post(client, ok_event()).status_code == 200  # no Lambda retry
    pending = s3.json(f"jobs/{JOB_ID}/pending.json")
    assert pending["state"] == "failed" and "too many jobs" in pending["error"]
    assert "token" not in pending


def test_hook_failure_is_shown_to_the_user(client, hook, s3, created):
    seed_pending(s3)
    r = signed_post(
        client,
        {
            "status": "failed",
            "job_id": str(JOB_ID),
            "error": "PDF is password protected",
        },
    )
    assert r.status_code == 200 and created == []
    pending = s3.json(f"jobs/{JOB_ID}/pending.json")
    assert (
        pending["state"] == "failed" and pending["error"] == "PDF is password protected"
    )
    assert "token" not in pending


def test_hook_ignores_unknown_jobs(client, hook, created):
    assert signed_post(client, ok_event()).status_code == 200
    assert created == []


def test_verify_signature_unit():
    body = b"{}"
    ts = 1_000_000
    import hashlib
    import hmac

    sig = hmac.new(b"k", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert uploads.verify_signature("k", body, str(ts), sig, now=ts + 10)
    assert not uploads.verify_signature("k", body, str(ts), sig, now=ts + 301)
    assert not uploads.verify_signature("", body, str(ts), sig, now=ts)
    assert not uploads.verify_signature("k", body, "abc", sig, now=ts)


# --- progress ----------------------------------------------------------------


def stub_job(monkeypatch, job=None, error=None):
    async def fake_get(token, job_id):
        if error:
            raise error
        return job

    monkeypatch.setattr(coordinator, "get_job", fake_get)


def test_job_page_lists_every_stage_in_order(client):
    html = client.get(f"/jobs/{JOB_ID}").text
    steps = ["submitted", "prepare", "queue", "run", "check", "done"]
    positions = [html.index(f'data-step="{step}"') for step in steps]
    assert positions == sorted(positions)


def test_job_page_renders_shell(client):
    r = client.get(f"/jobs/{JOB_ID}")
    assert r.status_code == 200
    assert f'data-job-id="{JOB_ID}"' in r.text and "job.js" in r.text


def test_status_returns_progress_view(client, monkeypatch):
    stub_job(monkeypatch, sample_job())
    body = client.get(f"/jobs/{JOB_ID}/status").json()
    assert body["stage"] == "active" and body["tasks_total"] == 2
    assert body["tasks"][1]["replicas"][0]["restarts"] == 1


def test_status_while_pages_are_being_prepared(client, monkeypatch, s3):
    stub_job(monkeypatch, error=status_error(404))
    seed_pending(s3)
    body = client.get(f"/jobs/{JOB_ID}/status").json()
    assert (body["stage"], body["status"], body["terminal"]) == (
        "preparing",
        "PREPARING",
        False,
    )


def test_status_shows_failed_preparation(client, monkeypatch, s3):
    stub_job(monkeypatch, error=status_error(404))
    s3.objects[f"jobs/{JOB_ID}/pending.json"] = json.dumps(
        {
            "state": "failed",
            "owner_id": USER["id"],
            "error": "PDF is password protected",
        }
    ).encode()
    body = client.get(f"/jobs/{JOB_ID}/status").json()
    assert (body["stage"], body["error"]) == ("failed", "PDF is password protected")


def test_status_hides_other_users_pending_uploads(client, monkeypatch, s3):
    stub_job(monkeypatch, error=status_error(404))
    seed_pending(s3)
    s3.objects[f"jobs/{JOB_ID}/pending.json"] = json.dumps(
        {"state": "rendering", "owner_id": str(UUID(int=99))}
    ).encode()
    assert client.get(f"/jobs/{JOB_ID}/status").status_code == 404


def test_status_unknown_job_is_404(client, monkeypatch):
    stub_job(monkeypatch, error=status_error(404))
    assert client.get(f"/jobs/{JOB_ID}/status").status_code == 404


def test_status_coordinator_down_is_503(client, monkeypatch):
    stub_job(monkeypatch, error=httpx.ConnectError("down"))
    assert client.get(f"/jobs/{JOB_ID}/status").status_code == 503


def sse_events(text: str) -> list[tuple[str | None, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        if block.startswith(":"):
            continue
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((event, data))
    return out


def test_stream_sends_update_then_done_for_finished_job(client, monkeypatch):
    stub_job(monkeypatch, sample_job("COMPLETED"))
    r = client.get(f"/jobs/{JOB_ID}/stream")
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    assert events[0][0] is None and events[0][1]["status"] == "COMPLETED"
    assert events[-1][0] == "done"


def test_stream_pushes_changes_until_job_ends(client, monkeypatch):
    monkeypatch.setattr(settings, "stream_interval", 0)
    states = iter(
        [sample_job("RUNNING"), sample_job("RUNNING"), sample_job("COMPLETED")]
    )

    async def fake_get(token, job_id):
        return next(states)

    monkeypatch.setattr(coordinator, "get_job", fake_get)
    events = sse_events(client.get(f"/jobs/{JOB_ID}/stream").text)
    # the repeated RUNNING snapshot is not sent twice
    assert [e[1].get("status") for e in events if e[0] is None] == [
        "RUNNING",
        "COMPLETED",
    ]
    assert events[-1][0] == "done"


def test_stream_reports_unknown_job(client, monkeypatch):
    stub_job(monkeypatch, error=status_error(404))
    assert sse_events(client.get(f"/jobs/{JOB_ID}/stream").text)[0][0] == "gone"


def test_stream_reports_expired_session(client, monkeypatch):
    stub_job(monkeypatch, error=status_error(401))
    assert sse_events(client.get(f"/jobs/{JOB_ID}/stream").text)[0][0] == "auth"


def test_stream_survives_a_coordinator_blip(client, monkeypatch):
    monkeypatch.setattr(settings, "stream_interval", 0)
    calls = iter([httpx.ConnectError("blip"), sample_job("FAILED")])

    async def flaky(token, job_id):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(coordinator, "get_job", flaky)
    events = sse_events(client.get(f"/jobs/{JOB_ID}/stream").text)
    assert events[0][1]["status"] == "FAILED" and events[-1][0] == "done"


# --- download and cancel -----------------------------------------------------


def completed_job():
    job = sample_job("COMPLETED")
    job["tasks"] = [
        {
            "id": 1,
            "task_index": 0,
            "status": "VERIFIED",
            "final_hash": H,
            "replicas": [replica(1, "DONE", 100, result_hash=H)],
        }
    ]
    return job


def test_download_builds_text_from_winning_replicas(client, monkeypatch, s3):
    stub_job(monkeypatch, completed_job())
    s3.objects[f"jobs/{JOB_ID}/replicas/1/result.json"] = json.dumps(
        {"pages": {"pages/0001.png": "hello", "pages/0002.png": "world"}}
    ).encode()
    r = client.get(f"/jobs/{JOB_ID}/result.txt")
    assert r.status_code == 200
    assert r.text == "hello\n\nworld\n"
    assert "attachment" in r.headers["content-disposition"]


def test_download_prefers_prebuilt_result(client, monkeypatch, s3):
    stub_job(monkeypatch, completed_job())
    s3.objects[f"jobs/{JOB_ID}/output/result.txt"] = b"prebuilt"
    assert client.get(f"/jobs/{JOB_ID}/result.txt").text == "prebuilt"


def test_download_not_ready_while_running(client, monkeypatch):
    stub_job(monkeypatch, sample_job("RUNNING"))
    assert client.get(f"/jobs/{JOB_ID}/result.txt").status_code == 409


def test_download_missing_result_file_is_409(client, monkeypatch):
    stub_job(monkeypatch, completed_job())
    assert client.get(f"/jobs/{JOB_ID}/result.txt").status_code == 409


def test_download_unknown_job_is_404(client, monkeypatch):
    stub_job(monkeypatch, error=status_error(404))
    assert client.get(f"/jobs/{JOB_ID}/result.txt").status_code == 404


def test_cancel_redirects_back_to_job(client, monkeypatch):
    called = {}

    async def fake_cancel(token, job_id):
        called["args"] = (token, job_id)

    monkeypatch.setattr(coordinator, "cancel_job", fake_cancel)
    r = client.post(f"/jobs/{JOB_ID}/cancel")
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{JOB_ID}"
    assert called["args"] == ("tok123", JOB_ID)


@pytest.mark.parametrize("code, expected", [(409, 303), (404, 404), (500, 503)])
def test_cancel_maps_coordinator_errors(client, monkeypatch, code, expected):
    async def fail(token, job_id):
        raise status_error(code)

    monkeypatch.setattr(coordinator, "cancel_job", fail)
    assert client.post(f"/jobs/{JOB_ID}/cancel").status_code == expected


# --- job list ----------------------------------------------------------------


def test_job_list_shows_rows_and_paging(client, monkeypatch):
    rows = [
        {
            "id": str(UUID(int=i)),
            "owner_id": USER["id"],
            "job_type": "ocr",
            "status": "COMPLETED" if i % 2 else "RUNNING",
            "task_count": 4,
            "created_at": "2026-10-24T10:00:00+00:00",
            "finished_at": "2026-10-24T10:02:05+00:00" if i % 2 else None,
        }
        for i in range(1, 22)
    ]

    async def fake_list(
        token, limit=20, offset=0, statuses=(), created_after=None, created_before=None
    ):
        assert limit == 21
        return rows

    monkeypatch.setattr(coordinator, "list_jobs", fake_list)
    r = client.get("/jobs")
    assert r.status_code == 200
    assert "2m 5s" in r.text and "24 Oct 2026 18:00 SGT" in r.text
    assert "Older jobs" in r.text and "Newer jobs" not in r.text
    assert r.text.count("/jobs/00000000-") == 20  # the 21st row only signals "more"


def test_job_list_empty_and_error_states(client, monkeypatch):
    async def empty(
        token, limit=20, offset=0, statuses=(), created_after=None, created_before=None
    ):
        return []

    monkeypatch.setattr(coordinator, "list_jobs", empty)
    assert "Create a job" in client.get("/jobs").text

    async def down(
        token, limit=20, offset=0, statuses=(), created_after=None, created_before=None
    ):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(coordinator, "list_jobs", down)
    r = client.get("/jobs")
    assert r.status_code == 503 and "be loaded right now" in r.text


# --- My jobs status filter -----------------------------------------------------


def test_job_list_filters_by_status_and_keeps_it_when_paging(client, monkeypatch, s3):
    calls = []

    async def fake_list(
        token, limit=20, offset=0, statuses=(), created_after=None, created_before=None
    ):
        calls.append(list(statuses))
        return [
            {
                "id": str(UUID(int=i)),
                "owner_id": USER["id"],
                "job_type": "ocr",
                "status": "FAILED",
                "task_count": 1,
                "created_at": "2026-10-24T10:00:00+00:00",
                "finished_at": "2026-10-24T10:01:00+00:00",
            }
            for i in range(11, 32)
        ]

    monkeypatch.setattr(coordinator, "list_jobs", fake_list)
    mark_pending(s3)  # preparing, so hidden by this filter
    r = client.get("/jobs?status=FAILED&status=PENDING&status=bogus")
    assert r.status_code == 200
    assert calls == [["PENDING", "FAILED"]]  # unknown values are dropped
    # a chip per status, each linking to the filter without it
    assert 'href="/jobs?status=FAILED" aria-label="Remove the Queued filter"' in r.text
    assert 'href="/jobs?status=PENDING" aria-label="Remove the Failed filter"' in r.text
    # only statuses not yet chosen can be added
    assert (
        '<option value="RUNNING">' in r.text and '<option value="FAILED">' not in r.text
    )
    assert "thesis.pdf" not in r.text
    assert "?status=PENDING&amp;status=FAILED&amp;offset=20" in r.text
    assert 'href="/jobs">Clear all' in r.text


def test_job_list_preparing_filter_skips_the_coordinator(client, monkeypatch, s3):
    async def must_not_call(*args, **kwargs):
        raise AssertionError("the coordinator has no preparing jobs")

    monkeypatch.setattr(coordinator, "list_jobs", must_not_call)
    mark_pending(s3)
    r = client.get("/jobs?status=PREPARING")
    assert r.status_code == 200 and "thesis.pdf" in r.text


def test_job_list_says_when_nothing_matches(client, monkeypatch, s3):
    async def fake_list(
        token, limit=20, offset=0, statuses=(), created_after=None, created_before=None
    ):
        # the user has a job, just none that is cancelled
        return [] if statuses else [summary(11, "COMPLETED")]

    monkeypatch.setattr(coordinator, "list_jobs", fake_list)
    r = client.get("/jobs?status=CANCELLED")
    assert "No jobs match this filter" in r.text and "Create a job" not in r.text
    assert 'href="/jobs" aria-label="Remove the Cancelled filter"' in r.text


def test_job_list_filters_by_date_and_keeps_it_everywhere(client, monkeypatch, s3):
    calls = []

    async def fake_list(
        token, limit=20, offset=0, statuses=(), created_after=None, created_before=None
    ):
        calls.append((list(statuses), created_after, created_before))
        return [
            {
                "id": str(UUID(int=i)),
                "owner_id": USER["id"],
                "job_type": "ocr",
                "status": "COMPLETED",
                "task_count": 1,
                "created_at": "2026-10-24T10:00:00+00:00",
                "finished_at": "2026-10-24T10:01:00+00:00",
            }
            for i in range(11, 32)
        ]

    monkeypatch.setattr(coordinator, "list_jobs", fake_list)
    r = client.get("/jobs?status=COMPLETED&from=2026-10-20&to=2026-10-25")
    assert r.status_code == 200
    # Singapore days: from the start of the 20th to the start of the 26th
    assert calls == [
        (
            ["COMPLETED"],
            datetime(2026, 10, 20, tzinfo=SGT),
            datetime(2026, 10, 26, tzinfo=SGT),
        )
    ]
    assert 'name="from" value="2026-10-20" max="2026-10-25"' in r.text
    assert 'name="to" value="2026-10-25" min="2026-10-20"' in r.text
    # removing the status chip keeps the dates, and so does paging
    assert (
        'href="/jobs?from=2026-10-20&amp;to=2026-10-25" aria-label="Remove the Completed filter"'
        in r.text
    )
    assert (
        "?status=COMPLETED&amp;from=2026-10-20&amp;to=2026-10-25&amp;offset=20"
        in r.text
    )


def test_job_list_does_not_allow_future_dates(client, monkeypatch, s3):
    calls = []

    async def fake_list(
        token, limit=20, offset=0, statuses=(), created_after=None, created_before=None
    ):
        calls.append((created_after, created_before))
        return [summary(11, "COMPLETED")]  # so the filter boxes are shown

    monkeypatch.setattr(coordinator, "list_jobs", fake_list)
    today = datetime.now(SGT).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    r = client.get(f"/jobs?from={tomorrow}&to=9999-12-31")
    assert r.status_code == 200 and calls == [(None, None)]  # both ignored
    # the pickers stop at today too
    assert f'name="from" value="" max="{today}"' in r.text
    assert f'name="to" value="" max="{today}"' in r.text
    r = client.get(f"/jobs?to={today}")
    # today itself is allowed: up to the start of tomorrow
    tomorrow = today + timedelta(days=1)
    assert calls[-1] == (None, datetime(*tomorrow.timetuple()[:3], tzinfo=SGT))


def test_job_list_has_no_filters_when_the_user_has_no_jobs(client, monkeypatch, s3):
    stub_list(monkeypatch, [])
    for path in ("/jobs", "/jobs?status=CANCELLED&from=2026-10-20"):
        r = client.get(path)
        assert 'aria-label="Filter jobs"' not in r.text, path
        assert "Create a job" in r.text and "No jobs match" not in r.text


def test_job_list_ignores_a_bad_date(client, monkeypatch, s3):
    calls = []

    async def fake_list(
        token, limit=20, offset=0, statuses=(), created_after=None, created_before=None
    ):
        calls.append((created_after, created_before))
        return []

    monkeypatch.setattr(coordinator, "list_jobs", fake_list)
    r = client.get("/jobs?from=not-a-date&to=")
    assert r.status_code == 200 and calls == [(None, None)]
    assert "Create a job" in r.text  # not filtered, so the usual empty state


def test_job_list_date_filter_applies_to_uploads_being_prepared(
    client, monkeypatch, s3
):
    stub_list(monkeypatch, [])
    # 23:30 UTC on the 24th is 07:30 on the 25th in Singapore
    mark_pending(s3, created_at="2026-10-24T23:30:00+00:00")
    assert "thesis.pdf" in client.get("/jobs?from=2026-10-25&to=2026-10-25").text
    assert "thesis.pdf" not in client.get("/jobs?from=2026-10-26").text
    assert "thesis.pdf" not in client.get("/jobs?to=2026-10-24").text


# --- local rendering fallback ------------------------------------------------


@pytest.mark.asyncio
async def test_local_render_makes_pages_and_creates_job(monkeypatch, s3, created):
    seed_pending(s3, pages=3)
    await uploads.render_locally(JOB_ID, make_pdf(3))
    assert sorted(k for k in s3.objects if "/pages/" in k) == [
        f"jobs/{JOB_ID}/pages/{n:04d}.png" for n in (1, 2, 3)
    ]
    assert s3.objects[f"jobs/{JOB_ID}/pages/0001.png"].startswith(b"\x89PNG")
    ((_token, spec),) = created
    assert spec["tasks"][0]["input"]["files"] == [
        f"pages/{n:04d}.png" for n in (1, 2, 3)
    ]


# --- uploads still being prepared show up in the job list --------------------


def mark_pending(s3, job_id=JOB_ID, owner=USER["id"], **fields):
    s3.objects[f"pending-index/{owner}/{job_id}"] = b""
    s3.objects[f"jobs/{job_id}/pending.json"] = json.dumps(
        {
            "state": "rendering",
            "owner_id": owner,
            "filename": "thesis.pdf",
            "created_at": datetime.now(UTC).isoformat(),
            **fields,
        }
    ).encode()


def stub_list(monkeypatch, rows):
    async def fake_list(
        token, limit=20, offset=0, statuses=(), created_after=None, created_before=None
    ):
        return rows

    monkeypatch.setattr(coordinator, "list_jobs", fake_list)


def test_job_list_shows_uploads_still_preparing(client, monkeypatch, s3):
    stub_list(monkeypatch, [])
    mark_pending(s3)
    r = client.get("/jobs")
    assert r.status_code == 200
    assert "Preparing" in r.text and "thesis.pdf" in r.text
    assert f"/jobs/{JOB_ID}" in r.text
    assert "location.reload" not in r.text  # My jobs never refreshes itself


def test_job_list_shows_failed_uploads_with_reason(client, monkeypatch, s3):
    stub_list(monkeypatch, [])
    mark_pending(s3, state="failed", error="PDF is password protected")
    r = client.get("/jobs")
    assert "Failed" in r.text and "PDF is password protected" in r.text
    assert "location.reload" not in r.text


def test_job_list_hides_other_users_and_finished_uploads(client, monkeypatch, s3):
    stub_list(monkeypatch, [])
    mark_pending(s3, job_id=UUID(int=8), owner=str(UUID(int=99)))  # someone else's
    # created already: the marker is stale and its pending.json is gone
    s3.objects[f"pending-index/{USER['id']}/{UUID(int=9)}"] = b""
    r = client.get("/jobs")
    assert str(UUID(int=8)) not in r.text and str(UUID(int=9)) not in r.text
    assert "Create a job" in r.text  # still the empty state


def test_job_list_does_not_repeat_a_job_the_coordinator_has(client, monkeypatch, s3):
    stub_list(
        monkeypatch,
        [
            {
                "id": str(JOB_ID),
                "owner_id": USER["id"],
                "job_type": "ocr",
                "status": "RUNNING",
                "task_count": 2,
                "created_at": "2026-10-24T10:00:00+00:00",
                "finished_at": None,
            }
        ],
    )
    mark_pending(s3)  # the marker hasn't been cleaned up yet
    assert client.get("/jobs").text.count(f"/jobs/{JOB_ID}") == 1


def test_job_list_still_works_when_s3_is_down(client, monkeypatch):
    from botocore.exceptions import EndpointConnectionError

    stub_list(monkeypatch, [])

    def boom(*args, **kwargs):
        raise EndpointConnectionError(endpoint_url="http://minio")

    monkeypatch.setattr(storage, "list_pending", boom)
    assert client.get("/jobs").status_code == 200


# --- expiry of uploads that never get their pages ------------------------------


def long_ago(minutes=31):
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def test_stale_upload_is_failed_and_token_deleted_when_viewed(client, monkeypatch, s3):
    stub_job(monkeypatch, error=status_error(404))
    mark_pending(s3, token="tok123", created_at=long_ago())
    body = client.get(f"/jobs/{JOB_ID}/status").json()
    assert body["stage"] == "failed" and "took too long" in body["error"]
    pending = s3.json(f"jobs/{JOB_ID}/pending.json")
    assert pending["state"] == "failed" and "token" not in pending


def test_recent_upload_is_left_alone(client, monkeypatch, s3):
    stub_job(monkeypatch, error=status_error(404))
    mark_pending(s3, token="tok123", created_at=long_ago(5))
    assert client.get(f"/jobs/{JOB_ID}/status").json()["stage"] == "preparing"
    assert s3.json(f"jobs/{JOB_ID}/pending.json")["token"] == "tok123"


def test_job_list_expires_stale_uploads_too(client, monkeypatch, s3):
    stub_list(monkeypatch, [])
    mark_pending(s3, token="tok123", created_at=long_ago())
    r = client.get("/jobs")
    assert "Failed" in r.text and "took too long" in r.text
    assert "token" not in s3.json(f"jobs/{JOB_ID}/pending.json")


def test_late_callback_does_not_start_an_expired_upload(client, hook, s3, created):
    seed_pending(s3, pages=3)
    pending = s3.json(f"jobs/{JOB_ID}/pending.json")
    pending["created_at"] = long_ago()
    s3.objects[f"jobs/{JOB_ID}/pending.json"] = json.dumps(pending).encode()
    seed_pages(s3, 3)
    assert signed_post(client, ok_event(page_count=3)).status_code == 200
    assert created == []
    assert "token" not in s3.json(f"jobs/{JOB_ID}/pending.json")


# --- cancelling and dismissing uploads that have no coordinator job --------------


def stub_cancel_404(monkeypatch):
    async def not_found(token, job_id):
        raise status_error(404)

    monkeypatch.setattr(coordinator, "cancel_job", not_found)


def test_cancel_while_preparing_forgets_the_upload(client, monkeypatch, s3):
    stub_cancel_404(monkeypatch)
    mark_pending(s3, token="tok123")
    r = client.post(f"/jobs/{JOB_ID}/cancel")
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    assert f"jobs/{JOB_ID}/pending.json" not in s3.objects
    assert f"pending-index/{USER['id']}/{JOB_ID}" not in s3.objects


def test_dismiss_a_failed_upload(client, monkeypatch, s3):
    stub_cancel_404(monkeypatch)
    mark_pending(s3, state="failed", error="PDF is password protected")
    assert client.post(f"/jobs/{JOB_ID}/cancel").status_code == 303
    assert f"jobs/{JOB_ID}/pending.json" not in s3.objects


def test_cannot_cancel_someone_elses_upload(client, monkeypatch, s3):
    stub_cancel_404(monkeypatch)
    other = str(UUID(int=99))
    mark_pending(s3, owner=other)
    assert client.post(f"/jobs/{JOB_ID}/cancel").status_code == 404
    assert f"jobs/{JOB_ID}/pending.json" in s3.objects


def test_cancel_unknown_job_is_still_404(client, monkeypatch):
    stub_cancel_404(monkeypatch)
    assert client.post(f"/jobs/{JOB_ID}/cancel").status_code == 404


def test_job_list_offers_cancel_and_dismiss_for_pending_rows(client, monkeypatch, s3):
    stub_list(monkeypatch, [])
    mark_pending(s3)
    mark_pending(s3, job_id=UUID(int=8), state="failed", error="bad")
    html = client.get("/jobs").text
    assert f'action="/jobs/{JOB_ID}/cancel"' in html and ">Cancel<" in html
    assert f'action="/jobs/{UUID(int=8)}/cancel"' in html and ">Dismiss<" in html


# --- callback and upload size limits -----------------------------------------


def test_hook_refuses_big_bodies_before_checking_them(client, hook, created):
    ts = str(int(time.time()))
    r = client.post(
        "/hooks/pages-ready",
        content=b"x" * (settings.callback_max_bytes + 1),
        headers={"X-Idlecloud-Timestamp": ts, "X-Idlecloud-Signature": "abc"},
    )
    assert r.status_code == 413


def test_hook_refuses_stale_or_unsigned_requests_without_reading(client, hook):
    old = str(int(time.time()) - 3600)
    big = b"x" * (settings.callback_max_bytes + 1)
    stale = client.post(
        "/hooks/pages-ready",
        content=big,
        headers={"X-Idlecloud-Timestamp": old, "X-Idlecloud-Signature": "abc"},
    )
    unsigned = client.post(
        "/hooks/pages-ready",
        content=big,
        headers={"X-Idlecloud-Timestamp": str(int(time.time()))},
    )
    assert stale.status_code == 401 and unsigned.status_code == 401


def test_oversized_upload_is_refused_from_its_content_length(client, s3, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    data = b"%PDF-" + b"0" * (3 * 1024 * 1024)
    r = post_pdf(client, data)
    assert r.status_code == 413 and "over 1 MB" in r.text
    assert s3.objects == {}


def test_filename_is_cleaned_and_capped(client, s3):
    name = "../../etc/" + "a" * 400 + "\x00\x07.pdf"
    r = post_pdf(client, make_pdf(1), name)
    job_id = r.headers["location"].removeprefix("/jobs/")
    stored = s3.json(f"jobs/{job_id}/pending.json")["filename"]
    assert len(stored) <= settings.max_filename_chars
    assert "/" not in stored and "\x00" not in stored and "etc" not in stored


# --- home page dashboard -------------------------------------------------------


def summary(i: int, status: str) -> dict:
    """A coordinator JobSummary, as GET /jobs lists it."""
    finished = status not in ("PENDING", "RUNNING")
    return {
        "id": str(UUID(int=i)),
        "owner_id": USER["id"],
        "job_type": "ocr",
        "status": status,
        "task_count": 2,
        "created_at": "2026-10-24T10:00:00+00:00",
        "finished_at": "2026-10-24T10:02:05+00:00" if finished else None,
    }


def test_home_shows_running_jobs_counts_and_recent(client, monkeypatch, s3):
    statuses = ["RUNNING", "COMPLETED", "COMPLETED", "FAILED"] + ["COMPLETED"] * 8
    # ids from 11, since the upload marked below is job 7
    stub_list(monkeypatch, [summary(i, s) for i, s in enumerate(statuses, 11)])
    loaded = []

    async def fake_get(token, job_id):
        loaded.append(str(job_id))
        return sample_job("RUNNING")

    monkeypatch.setattr(coordinator, "get_job", fake_get)
    set_quota(monkeypatch, active=1, limit=3)
    mark_pending(s3)  # an upload still being prepared is active too
    r = client.get("/")
    assert r.status_code == 200
    # progress is loaded only for the coordinator's running job
    assert loaded == [str(UUID(int=11))]
    running = r.text.split('id="active-heading"')[1].split('id="recent-heading"')[0]
    assert "thesis.pdf" in running and "Preparing" in running
    assert 'aria-valuenow="62"' in running and "1 of 2 tasks verified" in running
    assert str(UUID(int=12)) not in running  # completed jobs aren't "running now"
    counts = [int(n) for n in re.findall(r"text-3xl[^>]*>(\d+)<", r.text)]
    assert counts == [2, 10, 1, 2]  # active, completed, failed, free slots
    recent = r.text.split('id="recent-heading"')[1]
    assert recent.count('href="/jobs/0') == 10  # the upload and 9 of 12 jobs
    assert 'href="/jobs">View all' in recent  # 13 jobs, only 10 shown
    assert "location.reload" in r.text  # refreshes while something is running


def test_home_empty_state(client, monkeypatch, s3):
    stub_list(monkeypatch, [])
    r = client.get("/")
    assert r.status_code == 200
    assert "Nothing is running right now" in r.text and "Create a job" in r.text
    assert "View all" not in r.text and "location.reload" not in r.text


def test_home_survives_progress_and_quota_failures(client, monkeypatch, s3):
    stub_list(monkeypatch, [summary(1, "RUNNING")])

    async def down(*args):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(coordinator, "get_job", down)
    monkeypatch.setattr(coordinator, "job_quota", down)
    r = client.get("/")
    assert r.status_code == 200
    assert str(UUID(int=1))[:8] in r.text and "so far" in r.text
    assert 'role="progressbar"' not in r.text
    assert "allowed at once" not in r.text  # the slots card shows a dash


def test_home_when_jobs_cannot_be_loaded(client, monkeypatch):
    async def down(token, limit=20, offset=0):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(coordinator, "list_jobs", down)
    r = client.get("/")
    assert r.status_code == 503 and "be loaded right now" in r.text


def test_home_notes_when_counts_are_capped(client, monkeypatch, s3):
    stub_list(monkeypatch, [summary(i, "COMPLETED") for i in range(11, 111)])
    assert "Counts cover your newest 100 jobs" in client.get("/").text
    stub_list(monkeypatch, [summary(i, "COMPLETED") for i in range(11, 110)])
    mark_pending(s3)  # 99 jobs plus an upload: every job was counted
    assert "Counts cover" not in client.get("/").text
