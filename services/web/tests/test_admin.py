from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from services.web import admin
from services.web.app import nav_active
from services.web.coordinator import coordinator
from services.web.tests.test_app import ADMIN, USER, _signed_in, _status_error

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
JOB_ID = str(UUID(int=7))
NODE = {
    "name": "pc-lab1-07",
    "state": "BUSY",
    "trust_score": 0.75,
    "cpu_percent": 42.0,
    "last_heartbeat": "2026-10-25T11:59:48+00:00",
    "total_agree": 3,
    "total_disagree": 1,
}
JOB = {
    "id": JOB_ID,
    "owner_id": str(UUID(int=2)),
    "job_type": "ocr",
    "status": "RUNNING",
    "task_count": 4,
    "created_at": "2026-10-25T11:00:00+00:00",
    "finished_at": None,
}
BURST = {
    "observed_at": "2026-10-25T12:00:00+00:00",
    "machine_count": 1,
    "machines": [
        {
            "node_name": "burst-200",
            "instance_id": "i-0abc",
            "slot": "10.10.0.200",
            "machine": "t3.small",
            "market": "spot",
            "running": True,
            "state": "IDLE",
            "trust_score": 1.0,
            "cpu_percent": 5.0,
            "last_heartbeat": None,
            "launched_at": "2026-10-25T11:00:00+00:00",
            "ended_at": None,
            "usd_per_hour": 0.0236,
            "total_usd": 0.0236,
            "attributed_usd": 0.01,
            "unattributed_usd": 0.0136,
            "has_estimated_runs": False,
            "has_unknown_price": False,
        }
    ],
    "job_count": 1,
    "jobs": [
        {
            "job_id": JOB_ID,
            "job_type": "ocr",
            "status": "RUNNING",
            "runs": 2,
            "usd": 0.0123,
            "has_unknown_price": False,
            "has_estimated_runs": True,
        }
    ],
}
PROFILE = {
    "id": JOB["owner_id"],
    "email": "student@sit.edu.sg",
    "role": "USER",
    "created_at": "2026-10-01T02:00:00+00:00",
    "job_count": 1,
}
EVENT = {
    "id": 1,
    "time": "2026-10-25T11:30:00+00:00",
    "type": "replica_evicted",
    "job_id": JOB_ID,
    "task_id": 9,
    "node_name": "pc-lab1-07",
    "detail": {"replica_id": 31, "attempt": 2, "message": "owner came back"},
}


def test_ago():
    assert admin.ago(None, NOW) == ""
    assert admin.ago("2026-10-25T11:59:48+00:00", NOW) == "12s ago"
    assert admin.ago("2026-10-25T11:55:00", NOW) == "5m ago"  # naive means UTC
    assert admin.ago("2026-10-23T12:00:00+00:00", NOW) == "2d ago"
    assert admin.ago("2026-10-25T12:00:05+00:00", NOW) == "0s ago"  # clock skew


def test_sg_time():
    assert admin.sg_time(None) == ""
    assert admin.sg_time("2026-10-25T11:05:00+00:00") == "25 Oct 2026 19:05 SGT"
    assert (
        admin.sg_time("2026-10-25T11:05:00") == "25 Oct 2026 19:05 SGT"
    )  # naive is UTC
    # the evening in UTC is already the next day in Singapore
    assert (
        admin.sg_time(datetime(2026, 10, 25, 20, 0, tzinfo=UTC))
        == "26 Oct 2026 04:00 SGT"
    )
    assert admin.sg_time("2026-10-25T11:05:00Z", "%d %b %Y") == "25 Oct 2026"


def _sidebar(monkeypatch, user, path="/account"):
    r = _signed_in(monkeypatch, user).get(path)
    assert r.status_code == 200, path
    return r.text.split('id="profile-menu"')[0]


@pytest.mark.parametrize("path", ["/jobs", "/jobs/new", "/account"])
def test_users_get_their_dashboard_and_jobs_links(monkeypatch, path):
    sidebar = _sidebar(monkeypatch, USER, path)
    assert 'href="/admin"' not in sidebar
    # the logo links to / too
    assert sidebar.count('href="/"') == 2
    assert 'href="/jobs/new"' in sidebar and 'href="/jobs"' in sidebar


def test_admins_get_only_the_admin_links(monkeypatch):
    sidebar = _sidebar(monkeypatch, ADMIN)
    assert 'href="/admin"' in sidebar and "Admin dashboard" in sidebar
    assert sidebar.count('href="/"') == 1  # just the logo
    assert 'href="/jobs/new"' not in sidebar and 'href="/jobs"' not in sidebar


@pytest.mark.parametrize(
    "method, path", [("get", "/jobs/new"), ("post", "/jobs/new"), ("get", "/jobs")]
)
def test_admins_cannot_submit_or_list_jobs(monkeypatch, method, path):
    r = getattr(_signed_in(monkeypatch, ADMIN), method)(path)
    assert r.status_code == 403


def test_home_sends_admins_to_the_admin_dashboard(monkeypatch):
    r = _signed_in(monkeypatch, ADMIN).get("/")
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert _signed_in(monkeypatch, USER).get("/").status_code == 200


def test_node_row_and_counts():
    row = admin.node_row(NODE, NOW)
    assert row["heartbeat_ago"] == "12s ago" and row["agree_rate"] == 75
    fresh = admin.node_row(
        {**NODE, "last_heartbeat": None, "total_agree": 0, "total_disagree": 0}, NOW
    )
    assert fresh["heartbeat_ago"] == "never" and fresh["agree_rate"] is None
    assert admin.node_counts([row, {**row, "state": "IDLE"}, row]) == {
        "IDLE": 1,
        "BUSY": 2,
        "OFFLINE": 0,
    }


def test_event_row():
    row = admin.event_row(EVENT)
    assert row["kind"] == "evicted"
    assert row["message"] == "owner came back"
    assert row["facts"] == ["replica 31", "attempt 2"]
    assert row["time"] == "25 Oct 19:30:00"  # in Singapore time
    assert admin.event_row({**EVENT, "detail": None})["facts"] == []


COUNTS = {"PENDING": 1, "RUNNING": 3, "COMPLETED": 9, "FAILED": 0, "CANCELLED": 0}
ADMIN_PAGES = [
    "/admin",
    "/admin/jobs",
    "/admin/burst",
    "/admin/events",
    "/admin/users",
    f"/admin/users/{PROFILE['id']}",
]


@pytest.fixture
def fake_admin_api(monkeypatch):
    """Fakes every admin coordinator call and records the arguments each got."""
    calls = {}

    async def nodes(token):
        return [NODE]

    async def counts(token):
        return COUNTS

    async def jobs(token, limit=20, offset=0, owner_id=None):
        calls["jobs"] = (limit, offset, owner_id)
        return [JOB]

    async def burst(token, **kwargs):
        calls["burst"] = kwargs
        return BURST

    async def events(token, job_id=None, node_name=None, limit=50, offset=0):
        calls["events"] = (job_id, node_name, limit, offset)
        return [EVENT]

    async def users(token, limit=50, offset=0):
        calls["users"] = (limit, offset)
        return [PROFILE]

    async def user(token, user_id):
        if user_id != UUID(PROFILE["id"]):
            raise _status_error(404)
        return PROFILE

    for name, fake in {
        "admin_nodes": nodes,
        "admin_job_counts": counts,
        "admin_jobs": jobs,
        "burst_overview": burst,
        "admin_events": events,
        "admin_users": users,
        "admin_user": user,
    }.items():
        monkeypatch.setattr(coordinator, name, fake)
    return calls


async def _down(*args, **kwargs):
    raise httpx.ConnectError("down")


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_are_admin_only(monkeypatch, fake_admin_api, path):
    assert _signed_in(monkeypatch, USER).get(path).status_code == 403
    assert _signed_in(monkeypatch, ADMIN).get(path).status_code == 200


def test_overview_shows_counts_and_nodes(monkeypatch, fake_admin_api):
    html = _signed_in(monkeypatch, ADMIN).get("/admin").text
    assert "Jobs running" in html and "1 queued" in html
    assert ">3</p>" in html  # RUNNING
    assert "pc-lab1-07" in html and "Busy" in html and "42%" in html
    assert 'href="/admin/events?node=pc-lab1-07"' in html
    # the other sections now have their own pages
    assert "Event log</h1>" not in html and "i-0abc" not in html


def test_overview_keeps_nodes_when_counts_fail(monkeypatch, fake_admin_api):
    monkeypatch.setattr(coordinator, "admin_job_counts", _down)
    r = _signed_in(monkeypatch, ADMIN).get("/admin")
    assert r.status_code == 200
    assert "Loading the job counts failed" in r.text and "pc-lab1-07" in r.text


def test_overview_survives_an_outdated_coordinator(monkeypatch, fake_admin_api):
    # a coordinator without GET /admin/job-counts answers 404
    async def missing(*args, **kwargs):
        raise _status_error(404)

    monkeypatch.setattr(coordinator, "admin_job_counts", missing)
    r = _signed_in(monkeypatch, ADMIN).get("/admin")
    assert r.status_code == 200
    assert "Loading the job counts failed" in r.text and "pc-lab1-07" in r.text


def test_jobs_page_has_owner_links_costs_and_logs(monkeypatch, fake_admin_api):
    html = _signed_in(monkeypatch, ADMIN).get("/admin/jobs?offset=20").text
    assert f'href="/admin/users/{JOB["owner_id"]}"' in html
    assert f'href="/admin/events?job={JOB_ID}"' in html
    assert "$0.012300" in html and "Includes unfinished runs" in html
    assert fake_admin_api["jobs"] == (21, 20, None)
    # burst jobs are fetched for the same page as the jobs list
    assert fake_admin_api["burst"]["job_offset"] == 20
    assert 'href="?offset=0"' not in html and 'href="?"' in html  # "Newer jobs"


def test_jobs_page_without_costs_still_lists_jobs(monkeypatch, fake_admin_api):
    monkeypatch.setattr(coordinator, "burst_overview", _down)
    r = _signed_in(monkeypatch, ADMIN).get("/admin/jobs")
    assert r.status_code == 200
    assert "Loading the burst workers and costs failed" in r.text
    assert f"/jobs/{JOB_ID}" in r.text


def test_burst_page_lists_workers(monkeypatch, fake_admin_api):
    html = _signed_in(monkeypatch, ADMIN).get("/admin/burst?offset=20").text
    assert "Burst workers (1)" in html
    assert "burst-200" in html and "i-0abc" in html and "$0.023600" in html
    assert "25 Oct 2026 19:00 SGT" in html and "to ongoing" in html
    assert "2026-10-25T" not in html  # no raw ISO timestamps
    assert fake_admin_api["burst"]["machine_offset"] == 20


def test_events_page_filters(monkeypatch, fake_admin_api):
    c = _signed_in(monkeypatch, ADMIN)
    r = c.get("/admin/events", params={"job": JOB_ID, "node": " pc-1 ", "offset": 50})
    assert r.status_code == 200
    assert fake_admin_api["events"] == (UUID(JOB_ID), "pc-1", 51, 50)
    assert "owner came back" in r.text and "replica 31" in r.text
    assert (
        f'href="?job={JOB_ID}&amp;node=pc-1"' in r.text
    )  # "Newer events" keeps filters
    assert "Clear" in r.text
    assert c.get("/admin/events?job=nope").status_code == 422
    # submitting the form with both boxes empty means no filter
    r = c.get("/admin/events?job=&node=")
    assert r.status_code == 200 and fake_admin_api["events"] == (None, None, 51, 0)
    assert "Clear" not in r.text


def test_failing_page_is_503_with_a_message(monkeypatch, fake_admin_api):
    monkeypatch.setattr(coordinator, "admin_events", _down)
    r = _signed_in(monkeypatch, ADMIN).get("/admin/events")
    assert r.status_code == 503 and "Loading the events failed" in r.text


def test_expired_session_goes_to_login(monkeypatch, fake_admin_api):
    async def expired(*args, **kwargs):
        raise _status_error(401)

    monkeypatch.setattr(coordinator, "admin_jobs", expired)
    r = _signed_in(monkeypatch, ADMIN).get("/admin/jobs")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/admin/jobs"


def test_old_admin_nodes_page_is_gone(monkeypatch, fake_admin_api):
    assert _signed_in(monkeypatch, ADMIN).get("/admin/nodes").status_code == 404


def test_nav_active_picks_the_most_specific_link():
    assert nav_active("/admin") == "/admin"
    assert nav_active("/admin/events") == "/admin/events"
    assert nav_active(f"/admin/users/{UUID(int=2)}") == "/admin/users"
    assert nav_active("/jobs/new") == "/jobs/new"
    assert nav_active(f"/jobs/{JOB_ID}") == "/jobs"
    assert nav_active("/account") is None


def test_admin_pages_are_plain_sidebar_links(monkeypatch, fake_admin_api):
    c = _signed_in(monkeypatch, ADMIN)
    sidebar = c.get("/account").text.split('id="profile-menu"')[0]
    assert "<details" not in sidebar and "Admin dashboard" in sidebar
    for path in (
        "/admin",
        "/admin/jobs",
        "/admin/burst",
        "/admin/events",
        "/admin/users",
    ):
        assert f'href="{path}"' in sidebar
    events_page = c.get("/admin/events").text
    assert 'href="/admin/events" aria-current="page"' in events_page
    assert 'href="/admin" aria-current' not in events_page


def test_users_page_lists_accounts(monkeypatch, fake_admin_api):
    html = _signed_in(monkeypatch, ADMIN).get("/admin/users").text
    assert "student@sit.edu.sg" in html
    assert f'href="/admin/users/{PROFILE["id"]}"' in html
    assert "01 Oct 2026 10:00 SGT" in html
    assert fake_admin_api["users"] == (51, 0)


def test_user_profile_shows_account_and_jobs(monkeypatch, fake_admin_api):
    html = _signed_in(monkeypatch, ADMIN).get(f"/admin/users/{PROFILE['id']}").text
    assert "student@sit.edu.sg" in html and PROFILE["id"] in html
    assert f"/jobs/{JOB_ID}" in html and f'href="/admin/events?job={JOB_ID}"' in html
    assert fake_admin_api["jobs"] == (21, 0, UUID(PROFILE["id"]))


def test_unknown_user_is_404(monkeypatch, fake_admin_api):
    r = _signed_in(monkeypatch, ADMIN).get(f"/admin/users/{UUID(int=99)}")
    assert r.status_code == 404


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_do_not_refresh_themselves(monkeypatch, fake_admin_api, path):
    html = _signed_in(monkeypatch, ADMIN).get(path).text
    assert "location.reload" not in html
