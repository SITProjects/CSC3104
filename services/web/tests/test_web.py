from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from services.web import app as web_app
from services.web.jobs import (
    JobSnapshot,
    UserError,
    check_pdf,
    clean_email,
    make_tasks,
    resource_request,
)
from services.web.models import Upload
from services.web.settings import DENSE_PAGE_BYTES, MEM_DENSE, MEM_NORMAL
from services.web.webhooks import check_webhook_url
from shared.db import JobView
from shared.db.models import NodeState


def test_check_pdf_accepts_pdf_and_strips_path():
    assert check_pdf(r"C:\scans\week1.pdf", b"%PDF-1.7 ...") == "week1.pdf"


@pytest.mark.parametrize(
    ("data", "status"),
    [(b"", 400), (b"PK\x03\x04 not a pdf", 400), (b"%PDF-" + b"x" * 60 * 2**20, 413)],
)
def test_check_pdf_rejects(data, status):
    with pytest.raises(UserError) as error:
        check_pdf("a.pdf", data)
    assert error.value.status == status


def test_clean_email():
    assert clean_email("  Alice@Example.COM ") == "alice@example.com"
    with pytest.raises(UserError):
        clean_email("not-an-email")


def test_resource_request_uses_average_page_size():
    assert resource_request([]) == (1, MEM_NORMAL)
    assert resource_request([DENSE_PAGE_BYTES // 2] * 4) == (1, MEM_NORMAL)
    assert resource_request([DENSE_PAGE_BYTES * 2] * 4) == (1, MEM_DENSE)


def test_make_tasks_splits_into_ten_page_chunks():
    tasks = make_tasks(23)
    assert [t["input"]["pages"] for t in tasks] == [[1, 10], [11, 20], [21, 23]]
    assert tasks[0]["input"]["files"][0] == "pages/0001.png"
    assert tasks[2]["input"]["files"] == [
        "pages/0021.png",
        "pages/0022.png",
        "pages/0023.png",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/hook",
        "http://127.0.0.1:8001/internal/jobs",
        "http://169.254.169.254/latest/meta-data/",  # EC2 instance metadata
        "http://10.10.0.1:9000/",  # the WireGuard hub
    ],
)
def test_webhook_rejects_non_public_urls(url):
    with pytest.raises(UserError):
        check_webhook_url(url)


def test_sse_prefixes_every_line():
    assert web_app.sse("<p>\nhi</p>") == "data: <p>\ndata: hi</p>\n\n"


def make_upload(**kwargs) -> Upload:
    defaults = {
        "job_id": uuid4(),
        "owner_email": "a@b.co",
        "filename": "scan.pdf",
        "size_bytes": 2048,
        "status": "SUBMITTED",
        "page_count": 12,
        "pages_per_task": 10,
        "error": None,
        "created_at": datetime.now(UTC) - timedelta(minutes=3),
    }
    return Upload(**(defaults | kwargs))


def test_job_detail_renders_votes_and_evictions():
    final = "a" * 64
    job = JobView.model_validate(
        {
            "id": str(uuid4()),
            "job_type": "ocr",
            "status": "RUNNING",
            "progress": 40,
            "tasks": [
                {
                    "id": 1,
                    "task_index": 0,
                    "status": "VERIFIED",
                    "final_hash": final,
                    "replicas": [
                        {
                            "id": 1,
                            "node_name": "lab-pc-1",
                            "status": "DONE",
                            "attempt": 1,
                            "progress": 100,
                            "result_hash": final,
                        },
                        {
                            "id": 2,
                            "node_name": "lab-pc-2",
                            "status": "DONE",
                            "attempt": 2,
                            "progress": 100,
                            "result_hash": "b" * 64,
                        },
                    ],
                },
                {
                    "id": 2,
                    "task_index": 1,
                    "status": "RUNNING",
                    "final_hash": None,
                    "replicas": [
                        {
                            "id": 3,
                            "node_name": None,
                            "status": "SCHEDULED",
                            "attempt": 1,
                            "progress": 0,
                            "result_hash": None,
                        }
                    ],
                },
            ],
        }
    )
    snap = JobSnapshot(upload=make_upload(), job=job, pages_ready=12)
    html = web_app.templates.get_template("partials/job_detail.html").render(snap=snap)
    assert "✓ agrees" in html and "✗ outvoted" in html
    assert "attempt 2" in html
    assert "11–12" in html  # the last task covers the leftover pages
    assert "waiting for a PC" in html
    assert "Cancel job" in html
    assert "at least 2 results agree" in html
    assert (
        "onsubmit" not in html
    )  # the confirm lives in live.js, so a CSP can ban inline scripts
    assert "data-confirm" in html


def test_job_detail_uses_the_split_stored_with_the_job():
    job = JobView.model_validate(
        {
            "id": str(uuid4()),
            "job_type": "ocr",
            "status": "RUNNING",
            "tasks": [
                {
                    "id": 2,
                    "task_index": 1,
                    "status": "RUNNING",
                    "final_hash": None,
                    "replicas": [],
                }
            ],
        }
    )
    snap = JobSnapshot(upload=make_upload(pages_per_task=5), job=job, pages_ready=12)
    html = web_app.templates.get_template("partials/job_detail.html").render(snap=snap)
    assert "6–10" in html


def test_job_detail_escapes_filename():
    snap = JobSnapshot(
        upload=make_upload(status="FAILED", filename="<script>x</script>.pdf")
    )
    html = web_app.templates.get_template("partials/job_detail.html").render(snap=snap)
    assert "<script>x" not in html
    assert "Cancel job" not in html


def test_admin_detail_renders():
    node = SimpleNamespace(
        name="lab-pc-1",
        state=NodeState.IDLE,
        trust_score=Decimal("0.850"),
        total_agree=10,
        total_disagree=1,
        cpu_percent=12.5,
        last_heartbeat=datetime.now(UTC),
    )
    html = web_app.templates.get_template("partials/admin_detail.html").render(
        nodes=[(node, 2)], machines=[], jobs=[]
    )
    assert "lab-pc-1" in html and "t-high" in html and "0.85" in html


def test_pages_ready_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(web_app, "HMAC_SECRET", "secret")

    async def no_session():
        yield None

    web_app.app.dependency_overrides[web_app.session_manager.get_session] = no_session
    web_app.app.state.coordinator = None
    try:
        client = TestClient(web_app.app)  # no "with": skips the lifespan's database
        response = client.post(
            "/hooks/pages-ready",
            content=b'{"status": "ok"}',
            headers={"X-Idlecloud-Timestamp": "1", "X-Idlecloud-Signature": "bad"},
        )
    finally:
        web_app.app.dependency_overrides.clear()
    assert response.status_code == 401
