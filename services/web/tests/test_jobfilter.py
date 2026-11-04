from datetime import date

from services.web.jobfilter import JobFilter, parse_whole


def row(**fields):
    return {"id": "abc-123", "job_type": "ocr", "task_count": 4, **fields}


def test_parse_whole_accepts_only_whole_numbers():
    assert parse_whole("12") == 12 and parse_whole(" 0 ") == 0
    for bad in (None, "", "abc", "-1", "1.5", "\u0661"):
        assert parse_whole(bad) is None
    assert parse_whole("99999999999") == 1_000_000  # capped


def test_nothing_set_matches_everything_and_is_not_active():
    flt = JobFilter()
    assert not flt.active and not flt.in_app and flt.query() == ""
    assert flt.matches(row(task_count=None))


def test_status_and_dates_are_active_but_not_in_app():
    flt = JobFilter(statuses=["FAILED"], start=date(2026, 10, 1))
    assert flt.active and not flt.in_app


def test_text_matches_id_file_name_and_type_ignoring_case():
    flt = JobFilter(text="THE")
    assert flt.matches(row(filename="thesis.pdf"))
    assert not flt.matches(row())
    assert JobFilter(text="ABC-1").matches(row())
    assert JobFilter(text="ocr").matches(row())


def test_ranges_include_their_ends_and_reject_missing_values():
    flt = JobFilter(tasks_min=2, tasks_max=4)
    assert flt.matches(row(task_count=2)) and flt.matches(row(task_count=4))
    assert not flt.matches(row(task_count=5)) and not flt.matches(row(task_count=None))
    took = JobFilter(took_min=1, took_max=2)
    assert took.matches(row(took_seconds=60)) and took.matches(row(took_seconds=179))
    assert not took.matches(row(took_seconds=59))
    assert not took.matches(row(took_seconds=180))
    assert not took.matches(row())  # no time yet, e.g. a failed upload


def test_query_keeps_every_filter():
    flt = JobFilter(
        statuses=["RUNNING", "FAILED"],
        start=date(2026, 10, 1),
        text="a b",
        tasks_min=0,
        took_max=9,
    )
    assert flt.query() == (
        "status=RUNNING&status=FAILED&from=2026-10-01&q=a+b&tasks_min=0&took_max=9"
    )
