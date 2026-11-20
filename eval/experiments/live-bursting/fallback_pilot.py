"""One live fallback pilot with no idle campus capacity. Run beside burst_trial.py."""
import csv
import getpass
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import burst_trial as burst
import requests

ROOT = Path(__file__).resolve().parent
SITE = os.environ["BURST_SITE_URL"]
def timestamp():
    return datetime.now(timezone.utc).isoformat()

def main():
    admin = burst.login(SITE, {"email": os.environ["BURST_ADMIN_EMAIL"],
                              "password": getpass.getpass("Admin password (hidden): ")})
    before = burst.scan(admin, SITE)
    idle = burst.campus_idle(admin, SITE)
    if idle or any(m["active"] for m in before.values()):
        raise ValueError("Initial conditions changed; recheck the trial design.")
    trial = uuid.uuid4().hex[:12]
    output = ROOT / "results" / ("fallback-" + trial)
    output.mkdir(parents=True)
    creds = {"email": "group17-burst-" + trial + "@example.test", "password": secrets.token_urlsafe(24)}
    signup = requests.post(SITE + "/signup", data={**creds, "confirm_password": creds["password"]},
                           allow_redirects=False, timeout=30)
    if signup.status_code != 303:
        raise RuntimeError(f"Normal test account creation returned {signup.status_code}")
    user = burst.login(SITE, creds)
    started = time.monotonic()
    submitted = timestamp()
    job = burst.submit(user, SITE)
    info = dict(job_id=job, account_email=creds["email"], submitted_utc=submitted, python_job_seconds=120,
                initial_idle_campus=idle, initial_active_burst=0,
                start_observed_utc=None, finished_observed_utc=None, elapsed_to_terminal_s=None,
                cancelled_by_harness=False)
    workers = {}
    done = None
    summary = output / "summary.json"
    def record():
        summary.write_text(json.dumps(dict(info, workers=workers),default=str,indent=2)+"\n")
    record()
    print("Submitted", job, str(output), flush=True)
    last = None
    with (output/"observations.csv").open("w",newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["utc","elapsed_s","job_status","replica_states","assigned_nodes","idle_campus","active_burst","new_workers"])
        while True:
            r = user.get(SITE+"/jobs/"+job+"/status",timeout=20)
            r.raise_for_status()
            view = r.json()
            machines = burst.scan(admin,SITE)
            campus = burst.campus_idle(admin,SITE)
            current = timestamp()
            elapsed = time.monotonic()-started
            for key,m in machines.items():
                if key not in before:
                    first_seen = workers.get(key,{}).get("first_seen_utc",current)
                    workers[key] = dict(m, first_seen_utc=first_seen, last_seen_utc=current)
            replicas = [r for task in view.get("tasks",[]) for r in task["replicas"]]
            states = sorted({r["status"] for r in replicas})
            nodes = sorted({r["node"] for r in replicas if r.get("node")})
            status = view["status"]
            active = sum(m["active"] for m in machines.values())
            writer.writerow([current,round(elapsed,3),status,"|".join(states),"|".join(nodes),"|".join(campus),active,len(workers)])
            stream.flush()
            if (status,states,nodes,active)!=last:
                print(round(elapsed,1),status,states,nodes,"active EC2",active,flush=True)
                last = (status,states,nodes,active)
            info.update(last_status=status,last_observed_utc=current,observed_seconds=round(elapsed,3))
            if "RUNNING" in states and not info["start_observed_utc"]:
                info["start_observed_utc"]=current
            if status in burst.TERMINAL and done is None:
                done=time.monotonic()
                info.update(finished_observed_utc=current,elapsed_to_terminal_s=round(elapsed,3))
            record()
            if done is not None and (not active or time.monotonic()-done>=600):
                break
            if done is None and not info["cancelled_by_harness"] and (
                (elapsed >= 600 and not workers and not nodes and not info["start_observed_utc"]) or elapsed>=1200
            ):
                rr=user.post(SITE+"/jobs/"+job+"/cancel",allow_redirects=False,timeout=25)
                if rr.status_code!=303:
                    raise RuntimeError(f"Cancel returned {rr.status_code}")
                info.update(cancelled_by_harness=True,cancel_requested_utc=timestamp(),elapsed_at_cancel_s=round(elapsed,3))
                record()
            if done is None and elapsed>1260:
                raise TimeoutError("Cancellation not confirmed")
            time.sleep(10)
    events = burst.html(admin,SITE,"/admin/events?job="+job)
    events_rows = [tr.get_text(" | ",strip=True) for tr in events.select("tbody tr")]
    (output/"job_events.txt").write_text("\n".join(events_rows)+"\n")
    final = all(not m["active"] and m["pricing_complete"] for m in workers.values())
    info["estimated_worker_lifetime_usd"] = str(sum(m["total_usd"] for m in workers.values())) if final else None
    record()
    print(json.dumps(info),flush=True)
if __name__=="__main__":
    main()

