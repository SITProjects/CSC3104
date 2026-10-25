import json

import pymupdf
import pytest

from services.web import jobs
from services.web.settings import settings


def make_pdf(pages: int, password: str | None = None) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    if password:
        return doc.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw=password,
            user_pw=password,
        )
    return doc.tobytes()


# --- check_pdf ---------------------------------------------------------------


def test_check_pdf_returns_page_count():
    assert jobs.check_pdf(make_pdf(3)) == 3


@pytest.mark.parametrize(
    "data, message",
    [
        (b"hello world", "not a PDF"),
        (b"%PDF-1.7 but then nonsense", "damaged"),
        (make_pdf(2, password="s3cret"), "Password"),
    ],
)
def test_check_pdf_rejects_bad_files(data, message):
    with pytest.raises(jobs.UploadError, match=message):
        jobs.check_pdf(data)


def test_check_pdf_rejects_too_many_pages(monkeypatch):
    monkeypatch.setattr(settings, "max_pages", 2)
    with pytest.raises(jobs.UploadError, match="limited to 2 pages"):
        jobs.check_pdf(make_pdf(3))


# --- splitting into tasks ----------------------------------------------------


def test_build_tasks_groups_pages_in_tens():
    tasks = jobs.build_tasks(25)
    sizes = [len(t["input"]["files"]) for t in tasks]
    assert sizes == [10, 10, 5]
    assert tasks[0]["input"]["files"][0] == "pages/0001.png"
    assert tasks[1]["input"]["files"][0] == "pages/0011.png"
    assert tasks[2]["input"]["files"][-1] == "pages/0025.png"
    assert tasks[0]["input"]["lang"] == "eng"


def test_build_tasks_exact_multiple_and_single_page():
    assert len(jobs.build_tasks(20)) == 2
    assert [t["input"]["files"] for t in jobs.build_tasks(1)] == [["pages/0001.png"]]


def test_job_spec_uses_fixed_resources_and_given_id():
    spec = jobs.job_spec("abc", 12)
    assert spec["job_id"] == "abc"
    assert spec["job_type"] == "ocr"
    assert spec["replicas_per_task"] == 3
    assert (spec["cpu_request"], spec["mem_request"]) == (1, 512)
    assert len(spec["tasks"]) == 2


# --- progress view -----------------------------------------------------------


def replica(id, status, progress=0, attempt=1, node="pc-1", result_hash=None):
    return {
        "id": id,
        "node_name": node,
        "status": status,
        "attempt": attempt,
        "progress": progress,
        "result_hash": result_hash,
    }


H = "a" * 64


def sample_job(status="RUNNING"):
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "job_type": "ocr",
        "status": status,
        "progress": 62,
        "tasks": [
            {
                "id": 2,
                "task_index": 1,
                "status": "RUNNING",
                "final_hash": None,
                "replicas": [
                    replica(4, "RUNNING", 40, attempt=2),
                    replica(5, "EVICTED", 20, node=None),
                    replica(6, "FAILED", 0),
                ],
            },
            {
                "id": 1,
                "task_index": 0,
                "status": "VERIFIED",
                "final_hash": H,
                "replicas": [
                    replica(1, "DONE", 100, result_hash=H),
                    replica(2, "DONE", 100, node="pc-2", result_hash=H),
                    replica(3, "DONE", 100, node="pc-3", result_hash="b" * 64),
                ],
            },
        ],
    }


def test_progress_view_orders_tasks_and_counts_restarts():
    view = jobs.progress_view(sample_job())
    assert [t["index"] for t in view["tasks"]] == [0, 1]
    assert view["tasks_verified"] == 1 and view["tasks_total"] == 2
    # replica 4 restarted once; replica 5 is evicted and waiting to be relaunched
    assert view["tasks"][1]["restarts"] == 2
    assert view["restarts"] == 2
    assert view["terminal"] is False and view["progress"] == 62


def test_progress_view_task_progress_and_votes():
    tasks = jobs.progress_view(sample_job())["tasks"]
    assert tasks[0]["progress"] == 100
    assert tasks[0]["votes"] == {"agree": 2, "voted": 3, "copies": 3}
    # like the coordinator, only copies that are running or done count, so a copy
    # waiting for a new PC or a failed one doesn't drag the task down
    assert tasks[1]["progress"] == 40
    assert tasks[1]["votes"] is None


def test_progress_view_completed_is_terminal_and_full():
    view = jobs.progress_view(sample_job("COMPLETED"))
    assert view["terminal"] is True and view["progress"] == 100


def test_preparing_view_states():
    waiting = jobs.preparing_view({"state": "rendering"}, "id1")
    assert (waiting["stage"], waiting["terminal"]) == ("preparing", False)
    failed = jobs.preparing_view({"state": "failed", "error": "Password"}, "id1")
    assert (failed["stage"], failed["terminal"], failed["error"]) == (
        "failed",
        True,
        "Password",
    )


# --- final text --------------------------------------------------------------


def test_assemble_text_joins_winning_results_in_page_order():
    job = sample_job("COMPLETED")
    job["tasks"][0] = {
        "id": 2,
        "task_index": 1,
        "status": "VERIFIED",
        "final_hash": H,
        "replicas": [replica(7, "DONE", 100, result_hash=H)],
    }
    results = {
        1: {"pages": {"pages/0002.png": "two", "pages/0001.png": "one"}},
        7: {"pages": {"pages/0011.png": "eleven"}},
    }
    text = jobs.assemble_text(job, lambda rid: json.dumps(results[rid]).encode())
    assert text == "one\n\ntwo\n\neleven\n"


def test_assemble_text_needs_completed_job_and_every_result():
    with pytest.raises(jobs.ResultUnavailable):
        jobs.assemble_text(sample_job("RUNNING"), lambda rid: None)
    # task 1 never finished, so it has no winning copy to read
    with pytest.raises(jobs.ResultUnavailable, match="task 1"):
        jobs.assemble_text(sample_job("COMPLETED"), lambda rid: b'{"pages": {}}')


# --- unreadable results and file names -----------------------------------------


def completed_with_two_winners():
    job = sample_job("COMPLETED")
    job["tasks"] = [
        {
            "id": 1,
            "task_index": 0,
            "status": "VERIFIED",
            "final_hash": H,
            "replicas": [
                replica(1, "DONE", 100, result_hash=H),
                replica(2, "DONE", 100, node="pc-2", result_hash=H),
            ],
        }
    ]
    return job


def test_assemble_text_skips_an_unreadable_copy_and_uses_the_next():
    files = {
        1: b'{"pages": ',
        2: json.dumps({"pages": {"pages/0001.png": "ok"}}).encode(),
    }
    assert jobs.assemble_text(completed_with_two_winners(), files.get) == "ok\n"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'{"nope": 1}',
        b'{"pages": [1]}',
        b'{"pages": {"a": 5}}',
    ],
)
def test_assemble_text_with_only_unreadable_results_is_unavailable(raw):
    with pytest.raises(jobs.ResultUnavailable, match="missing or unreadable"):
        jobs.assemble_text(completed_with_two_winners(), lambda rid: raw)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("scan.pdf", "scan.pdf"),
        ("C:\\Users\\me\\scan.pdf", "scan.pdf"),
        ("../../etc/passwd", "passwd"),
        ("bad\x00na\nme.pdf", "badname.pdf"),
        ("", "upload.pdf"),
        (None, "upload.pdf"),
    ],
)
def test_clean_filename(name, expected):
    assert jobs.clean_filename(name) == expected


def test_clean_filename_caps_length(monkeypatch):
    monkeypatch.setattr(settings, "max_filename_chars", 10)
    assert jobs.clean_filename("x" * 50) == "x" * 10
