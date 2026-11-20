"""Log changes in the batch recorder's CSVs without making extra web requests."""
import csv
import sys
import time
from pathlib import Path

results = Path(__file__).resolve().parent / "results"
def rows(path):
    if not path.exists():
        return []
    with path.open(newline="") as source:
        return list(csv.DictReader(source))

def collect():
    deadline = time.monotonic() + 45 * 60
    prev = {}
    while time.monotonic() < deadline:
        batches = [p for p in results.glob("fallback-batch-*") if (p/"burst_jobs.csv").exists()]
        if batches:
            out = max(batches, key=lambda p: p.stat().st_mtime)
            break
        time.sleep(1)
    else:
        sys.exit("Batch CSV did not appear")
    with (out/"burst_timeline.csv").open("x",newline="") as file:
        writer=csv.writer(file)
        writer.writerow(["observed_unix","job_id","final_status","nodes"])
        while time.monotonic()<deadline:
            for j in rows(out/"burst_jobs.csv"):
                state=(j["final_status"],j["nodes"])
                if prev.get(j["job_id"])!=state:
                    writer.writerow([time.time(),j["job_id"],*state])
                    prev[j["job_id"]]=state
                    file.flush()
            if (out/"burst_summary.csv").exists():
                return
            time.sleep(1)
    raise TimeoutError("Incomplete timeline")
if __name__=="__main__":
    collect()

