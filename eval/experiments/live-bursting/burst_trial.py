"""Run one externally configured EC2 comparison arm over public HTTPS.
Dependencies: requests, beautifulsoup4. Run arms sequentially in an isolated window.
Set BURST_SITE_URL, BURST_ADMIN_EMAIL, BURST_ADMIN_PASSWORD, BURST_USERS_JSON.
The last variable is an array of objects with email and password fields. Use
dedicated normal users with no active jobs. Supply the same users for both arms.
"""

import argparse
import csv
import json
import os
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
JOB = "label job_id account submit_unix first_running_observed_unix finish_observed_unix final_status nodes".split()
WORKER = (
    "label instance_id node size market first_seen_unix ended_first_seen_unix launched_sgt "
    "removed_sgt active hourly_usd total_usd attributed_usd unattributed_usd pricing_complete"
).split()
SUMMARY = (
    "label requested completed account_count campus_idle_at_start campus_idle_at_end "
    "campus_idle_changed peak_burst_workers_observed worker_count batch_seconds "
    "whole_lifetime_estimated_usd costs_final"
).split()
SCRIPT = b"""import time
deadline = time.monotonic() + 120
count = 0
while time.monotonic() < deadline:
    count += 1
print(count)
"""


def login(site, credentials):
    session = requests.Session()
    # Retry status GETs on temporary hub errors. A POST can create work even
    # if its response is lost, so never replay login or job submission.
    retry = Retry(
        total=3,
        connect=0,
        backoff_factor=0.3,
        allowed_methods={"GET"},
        status_forcelist=(429, 502, 503, 504),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    r = session.post(
        site + "/login", data=credentials, timeout=20, allow_redirects=False
    )
    if r.status_code != 303:
        raise RuntimeError(f"Login failed (HTTP {r.status_code})")
    return session


def html(session, site, path):
    response = session.get(site + path, allow_redirects=False, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"GET {path} returned {response.status_code}")
    return BeautifulSoup(response.content, "html.parser")


def rows(page, region):
    table = page.select_one(f'[aria-label="{region}"] table')
    if table is None:
        raise ValueError(f"Missing {region} table")
    return [tr.find_all("td", recursive=False) for tr in table.select("tbody tr")]


def first(cell):
    return next(cell.stripped_strings)


def amount(cell):
    value = first(cell)
    return Decimal(value[1:]) if value.startswith("$") else None


def campus_idle(admin, site):
    page = html(admin, site, "/admin/workers")
    return sorted(
        first(c[0])
        for c in rows(page, "Campus workers")
        if len(c) == 5 and first(c[2]).upper() == "IDLE"
    )


def burst_page(page):
    # These two admin views are HTML only. The dashboard's total already includes
    # disk, boot and idle cost. The attributed and unattributed columns partition it.
    for c in rows(page, "Burst workers"):
        if len(c) == 1 and c[0].has_attr("colspan"):
            continue
        if len(c) != 8:
            raise ValueError("Unexpected burst table columns")
        instance_id = first(c[0].find("div")).split()[0]
        launched, ended = list(c[3].stripped_strings)
        ended = ended.removeprefix("to ")
        state = first(c[2])
        if state not in {"Active", "Ended"}:
            raise ValueError("Unknown burst worker state")
        yield dict(
            instance_id=instance_id,
            node=first(c[0]),
            size=first(c[1]),
            market=first(c[1].find("div")),
            launched_sgt=launched,
            removed_sgt="" if ended == "ongoing" else ended,
            active=state == "Active",
            hourly_usd=amount(c[4]),
            total_usd=amount(c[5]),
            attributed_usd=amount(c[6]),
            unattributed_usd=amount(c[7]),
            pricing_complete="Incomplete pricing" not in c[6].get_text(" ", strip=True)
            and amount(c[5]) is not None,
        )


def scan(admin, site):
    path = "/admin/burst"
    machines = {}
    seen = set()
    while path:
        if path in seen:
            raise RuntimeError("Repeated pagination link")
        seen.add(path)
        page = html(admin, site, path)
        machines.update((m["instance_id"], m) for m in burst_page(page))
        older = page.find("a", string=lambda s: s and s.strip() == "Older workers")
        path = ""
        if older:
            query = urlsplit(older["href"])
            if query.scheme or query.netloc or query.path not in {"", "/admin/burst"}:
                raise ValueError("Unexpected pagination URL")
            path = "/admin/burst?" + query.query
    return machines


def save(path, fields, label, records):
    prior = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as src:
            reader = csv.DictReader(src)
            if reader.fieldnames != fields:
                raise ValueError(f"Incompatible CSV header in {path}")
            prior = [row for row in reader if row["label"] != label]
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as dest:
        writer = csv.DictWriter(dest, fields)
        writer.writeheader()
        writer.writerows(prior)
        writer.writerows(records)
    tmp.replace(path)


def submit(user, site):
    response = user.post(
        site + "/jobs/new",
        data={"job_type": "python", "command": "python bench.py"},
        files={"inputs": ("bench.py", SCRIPT, "text/plain")},
        timeout=45,
        allow_redirects=False,
    )
    location = urlsplit(response.headers.get("Location", "")).path
    if response.status_code != 303 or not re.fullmatch(
        r"/jobs/[0-9a-fA-F-]{36}", location
    ):
        raise RuntimeError(
            f"Job submission failed (HTTP {response.status_code}); check account capacity"
        )
    return str(uuid.UUID(location.rsplit("/", 1)[-1]))


def refresh(job, user, site):
    # The JSON status view omits server-side start/finish timestamps.
    # Keep first observations on the client clock. A missed RUNNING stays blank.
    r = user.get(
        site + "/jobs/" + job["job_id"] + "/status", timeout=20, allow_redirects=False
    )
    r.raise_for_status()
    view = r.json()
    now = time.time()
    replicas = [r for task in view.get("tasks", []) for r in task["replicas"]]
    nodes = set(filter(None, job["nodes"].split("|")))
    job["nodes"] = "|".join(
        sorted(nodes | {r["node"] for r in replicas if r.get("node")})
    )
    if not job["first_running_observed_unix"] and any(
        r["status"] == "RUNNING" for r in replicas
    ):
        job["first_running_observed_unix"] = now
    job["final_status"] = view["status"]
    if view.get("terminal") or job["final_status"] in TERMINAL:
        job["finish_observed_unix"] = now


def run(args):
    site = os.environ["BURST_SITE_URL"].rstrip("/")
    url = urlsplit(site)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
    ):
        raise ValueError("BURST_SITE_URL must be an HTTPS origin")
    admin_email = os.environ["BURST_ADMIN_EMAIL"]
    creds = json.loads(os.environ["BURST_USERS_JSON"])
    names = [u["email"].strip().casefold() for u in creds]
    if (
        not names
        or len(names) != len(set(names))
        or admin_email.strip().casefold() in names
    ):
        raise ValueError(
            "Provide distinct normal submitters and a separate admin account"
        )
    admin = login(
        site, {"email": admin_email, "password": os.environ["BURST_ADMIN_PASSWORD"]}
    )
    users = [
        login(site, {"email": u["email"], "password": u["password"]}) for u in creds
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    jobs_path, workers_path, summary_path = [
        args.out / ("burst_" + k + ".csv") for k in ("jobs", "workers", "summary")
    ]
    for path in (jobs_path, workers_path, summary_path):
        if path.exists():
            with path.open(newline="", encoding="utf-8") as src:
                if any(r["label"] == args.label for r in csv.DictReader(src)):
                    raise ValueError("Use a distinct label for each run")
    initial = campus_idle(admin, site)
    if getattr(args, "fallback_only", False) and args.match_label:
        raise ValueError("A fallback observation cannot be used as a matched arm")
    if not initial and not getattr(args, "fallback_only", False):
        raise RuntimeError("Comparison needs non-zero idle campus capacity")
    before = scan(admin, site)
    if any(m["active"] for m in before.values()):
        raise RuntimeError(
            "A burst worker is already active; its lifetime spans another workload"
        )
    if args.match_label:
        with summary_path.open(newline="", encoding="utf-8") as src:
            previous = next(
                (r for r in csv.DictReader(src) if r["label"] == args.match_label), None
            )
        if (
            not previous
            or previous["campus_idle_at_start"] != "|".join(initial)
            or int(previous["requested"]) != args.jobs
            or int(previous["account_count"]) != len(users)
        ):
            raise ValueError(
                "Matched arm must use the same initial workers, batch size and submitter count"
            )
    jobs, workers = [], {}
    peak = 0
    changed = False
    next_workers = 0
    end = time.monotonic() + args.timeout_minutes * 60
    next_account = 0
    while True:
        if time.monotonic() >= end and not (
            len(jobs) == args.jobs and all(j["finish_observed_unix"] for j in jobs)
        ):
            raise TimeoutError(
                "Batch timeout; partial job and worker rows are in the output directory"
            )
        for job in jobs:
            if not job["finish_observed_unix"]:
                refresh(job, users[job["account"] - 1], site)
        while len(jobs) < args.jobs:
            active = [j["account"] - 1 for j in jobs if not j["finish_observed_unix"]]
            order = [(next_account + d) % len(users) for d in range(len(users))]
            account = next((k for k in order if active.count(k) < 3), None)
            if account is None:
                break
            start = time.time()
            job_id = submit(users[account], site)
            jobs.append(
                dict(
                    label=args.label,
                    job_id=job_id,
                    account=account + 1,
                    submit_unix=start,
                    first_running_observed_unix="",
                    finish_observed_unix="",
                    final_status="SUBMITTED",
                    nodes="",
                )
            )
            save(jobs_path, JOB, args.label, jobs)
            next_account = (account + 1) % len(users)
        finished = len(jobs) == args.jobs and all(
            j["finish_observed_unix"] for j in jobs
        )
        save(jobs_path, JOB, args.label, jobs)
        if time.monotonic() >= next_workers:
            current = scan(admin, site)
            last_idle = campus_idle(admin, site)
            changed |= last_idle != initial
            at = time.time()
            for key, m in current.items():
                if key in before:
                    continue
                old = workers.get(key, {})
                workers[key] = dict(
                    m,
                    label=args.label,
                    first_seen_unix=old.get("first_seen_unix", at),
                    ended_first_seen_unix=old.get("ended_first_seen_unix")
                    or ("" if m["active"] else at),
                )
            peak = max(peak, sum(m["active"] for m in workers.values()))
            save(workers_path, WORKER, args.label, workers.values())
            next_workers = time.monotonic() + 10
            if finished:
                finish = max(j["finish_observed_unix"] for j in jobs)
                if all(not m["active"] for m in workers.values()) or at >= finish + 600:
                    break
        time.sleep(2)
    final = all(not m["active"] and m["pricing_complete"] for m in workers.values())
    summary = dict(
        label=args.label,
        requested=args.jobs,
        completed=sum(j["final_status"] == "COMPLETED" for j in jobs),
        account_count=len(users),
        campus_idle_at_start="|".join(initial),
        campus_idle_at_end="|".join(last_idle),
        campus_idle_changed=changed,
        peak_burst_workers_observed=peak,
        worker_count=len(workers),
        batch_seconds=finish - jobs[0]["submit_unix"],
        costs_final=final,
        whole_lifetime_estimated_usd=sum(
            (m["total_usd"] for m in workers.values()), Decimal(0)
        )
        if final
        else "",
    )
    save(summary_path, SUMMARY, args.label, [summary])
    print(json.dumps(summary, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--fallback-only", action="store_true",
                        help="Measure an unpaired backlog, including with zero idle campus workers")
    parser.add_argument(
        "--match-label",
        help="Compare initial capacity and batch configuration with a previous arm",
    )
    parser.add_argument("--jobs", default=4, type=int)
    parser.add_argument("--timeout-minutes", default=45, type=int)
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).resolve().parent / "results"
    )
    options = parser.parse_args()
    if options.jobs < 1 or options.timeout_minutes < 1:
        parser.error("Counts and timeout must be positive")
    run(options)
