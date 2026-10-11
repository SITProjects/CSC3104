"""The full Step 1 flow: S3 event -> splitter -> renderers -> callbacks."""

import json

import pytest

from serverless.common.storage import job_id_from_key, page_key
from serverless.renderer import handler as renderer
from serverless.splitter import handler as splitter
from serverless.tests.conftest import make_pdf, s3_event


def test_45_page_pdf(s3, sent):
    s3.files["jobs/job42/input.pdf"] = make_pdf(45)
    result = splitter.split_handler(s3_event("jobs/job42/input.pdf"))

    assert result["jobs"][0]["page_count"] == 45
    assert result["jobs"][0]["chunks"] == 3
    # Every page is in S3 under the contract key
    for page in range(1, 46):
        assert page_key("job42", page) in s3.files
    # One signed callback per batch of 20 pages
    assert [(b["page_start"], b["page_end"]) for b in sent] == [
        (1, 20),
        (21, 40),
        (41, 45),
    ]
    assert all(b["status"] == "ok" and b["page_count"] == 45 for b in sent)
    assert [b["chunk_index"] for b in sent] == [0, 1, 2]
    assert all(b["chunk_count"] == 3 for b in sent)


def test_fan_out_starts_one_async_lambda_per_batch(s3, sent, monkeypatch):
    calls = []

    class FakeLambda:
        def invoke(self, FunctionName, InvocationType, Payload):
            calls.append((FunctionName, InvocationType, json.loads(Payload)))

    import boto3

    monkeypatch.setattr(boto3, "client", lambda name: FakeLambda())
    monkeypatch.setenv("RENDER_FUNCTION_NAME", "idlecloud-render")
    s3.files["jobs/j2/input.pdf"] = make_pdf(25)
    splitter.split_handler(s3_event("jobs/j2/input.pdf"))

    assert [(c[0], c[1]) for c in calls] == [("idlecloud-render", "Event")] * 2
    assert [(c[2]["page_start"], c[2]["page_end"]) for c in calls] == [
        (1, 20),
        (21, 25),
    ]
    assert sent == []  # the renderers send callbacks, not the splitter


def test_bad_pdf_is_reported_and_not_rendered(s3, sent):
    s3.files["jobs/bad/input.pdf"] = b"this is not a pdf"
    result = splitter.split_handler(s3_event("jobs/bad/input.pdf"))
    assert "error" in result["jobs"][0]
    assert sent == [
        {"status": "failed", "job_id": "bad", "error": "file is not a readable PDF"}
    ]
    assert not any("/pages/" in key for key in s3.files)


def test_other_keys_are_ignored(s3, sent):
    result = splitter.split_handler(s3_event("jobs/x/pages/0001.png"))
    assert result == {"jobs": []}
    assert sent == []


def test_job_id_from_encoded_key():
    assert job_id_from_key("jobs/abc-123/input.pdf") == "abc-123"
    assert job_id_from_key("jobs%2Fabc%2Finput.pdf") == "abc"
    assert job_id_from_key("jobs/abc/output/result.txt") is None


def test_render_failure_sends_failed_callback(s3, sent, monkeypatch):
    def broken_render(pdf, start, end, dpi):
        yield start, b"png"
        raise RuntimeError("corrupt page content")

    monkeypatch.setattr(renderer, "render_pages", broken_render)
    s3.files["jobs/j3/input.pdf"] = make_pdf(5)
    splitter.split_handler(s3_event("jobs/j3/input.pdf"))
    assert len(sent) == 1
    assert sent[0]["status"] == "failed"
    assert (sent[0]["page_start"], sent[0]["page_end"]) == (1, 5)
    assert "corrupt page content" in sent[0]["error"]


def test_one_failing_record_does_not_stop_the_others(s3, sent):
    s3.files["jobs/good/input.pdf"] = make_pdf(3)
    event = {
        "Records": [
            s3_event("jobs/missing/input.pdf")["Records"][0],  # not in S3
            s3_event("jobs/good/input.pdf")["Records"][0],
        ]
    }
    with pytest.raises(RuntimeError, match="1 record"):
        splitter.split_handler(event)
    assert page_key("good", 3) in s3.files
    assert [b["job_id"] for b in sent] == ["good"]
