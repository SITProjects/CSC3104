"""Cross-check only this batch's final job records and event log in the live app."""
import csv
import getpass
import json
import os
import sys
import time
from pathlib import Path

import burst_trial as burst

out = Path(sys.argv[1])
site = os.environ["BURST_SITE_URL"]
admin = burst.login(site, {"email": os.environ["BURST_ADMIN_EMAIL"],"password":getpass.getpass("Admin password (hidden): ")})
deadline = time.monotonic()+1800
while not (out/"burst_summary.csv").exists():
    if time.monotonic()>deadline:
        raise TimeoutError("Batch recorder still incomplete")
    time.sleep(5)
with (out/"burst_jobs.csv").open(newline="") as f:
    jobs = list(csv.DictReader(f))
data=[]
for job in jobs:
    job_id=job["job_id"]
    r=admin.get(site+"/jobs/"+job_id+"/status",timeout=20,allow_redirects=False)
    item={"job_id":job_id,"status_http":r.status_code}
    if r.status_code==200:
        view=r.json()
        item["final_status"]=view["status"]
        item["replicas"]=[{k:p.get(k) for k in ("status","node","attempt","restarts")} for task in view.get("tasks",[]) for p in task["replicas"]]
    page=burst.html(admin,site,"/admin/events?job="+job_id)
    item["events"]=[tr.get_text(" | ",strip=True) for tr in page.select("tbody tr") if len(tr.select("td"))==5]
    data.append(item)
(out/"job_checks.json").write_text(json.dumps(data,indent=2)+"\n")
print(json.dumps(data))

