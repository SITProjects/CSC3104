from datetime import date

from starlette.datastructures import QueryParams

from services.web.tablefilter import Column, TableFilter, page_of

COLUMNS = (
    Column("name", "Name", "text", lambda r: r["name"]),
    Column(
        "role",
        "Role",
        "choice",
        lambda r: r["role"],
        choices={"A": "Admin", "U": "User"},
    ),
    Column("jobs", "Jobs", "range", lambda r: r["jobs"]),
    Column("usd", "Cost", "range", lambda r: r["usd"], decimal=True),
    Column("joined", "Joined", "date", lambda r: r["joined"]),
    Column("logs", "Logs", hidden_label=True),  # no filter
)
ROW = {
    "name": "Ada Lovelace",
    "role": "A",
    "jobs": 3,
    "usd": 0.25,
    "joined": "2026-10-24T20:00:00+00:00",  # already the 25th in Singapore
}


def make(query: str) -> TableFilter:
    return TableFilter(COLUMNS, QueryParams(query))


def test_nothing_set_is_inactive_and_matches_everything():
    flt = make("")
    assert not flt.active and flt.query() == "" and flt.matches(ROW)
    assert "logs" not in flt.values  # a column without a filter has no value


def test_text_search_ignores_case_and_surrounding_spaces():
    assert make("name=+LOVE+").matches(ROW)
    assert not make("name=turing").matches(ROW)


def test_choice_ignores_unknown_values():
    assert make("role=A&role=bogus").values["role"] == ["A"]
    assert make("role=A").matches(ROW) and not make("role=U").matches(ROW)
    assert not make("role=bogus").active  # nothing valid chosen


def test_ranges_are_inclusive_and_reject_bad_numbers():
    assert make("jobs_min=3&jobs_max=3").matches(ROW)
    assert not make("jobs_min=4").matches(ROW)
    assert make("jobs_min=abc&jobs_max=-1").values["jobs"] == (None, None)
    assert make("usd_min=0.25&usd_max=0.3").matches(ROW)
    assert not make("usd_max=0.2").matches(ROW)
    for bad in ("nan", "inf", "-0.5", "1e999"):
        assert make(f"usd_min={bad}").values["usd"] == (None, None), bad


def test_a_row_without_the_value_fails_a_range():
    assert not make("jobs_min=0").matches({**ROW, "jobs": None})


def test_dates_are_singapore_days():
    assert make("joined_from=2026-10-25&joined_to=2026-10-25").matches(ROW)
    assert not make("joined_to=2026-10-24").matches(ROW)
    assert make("joined_from=not-a-date").values["joined"] == (None, None)


def test_every_filter_must_match():
    assert make("name=ada&role=A&jobs_min=1").matches(ROW)
    assert not make("name=ada&role=U").matches(ROW)


def test_query_keeps_every_active_filter():
    flt = make("name=ada&role=A&role=U&jobs_min=1&usd_max=0.5&joined_from=2026-10-01")
    assert flt.query() == (
        "name=ada&role=A&role=U&jobs_min=1&usd_max=0.5&joined_from=2026-10-01"
    )
    assert make(flt.query()).query() == flt.query()  # reads back the same


def test_active_filters_are_reported_per_column():
    flt = make("jobs_max=5")
    assert flt.active
    assert [c.key for c in COLUMNS if flt.is_active(c)] == ["jobs"]
    assert flt.values["joined"] == (None, None) and flt.values["jobs"] == (None, 5)
    assert date(2026, 10, 1) == make("joined_from=2026-10-01").values["joined"][0]


def test_page_of():
    assert page_of(list(range(5)), 0, 2) == ([0, 1], True)
    assert page_of(list(range(5)), 4, 2) == ([4], False)
    assert page_of([], 0, 2) == ([], False)
