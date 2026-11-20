"""Plot submission, observed node placement and completion from one live batch."""
import csv
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D


def read(path):
    with path.open(newline="") as source:
        return list(csv.DictReader(source))


def plot(out):
    jobs = sorted(read(out / "burst_jobs.csv"), key=lambda j: float(j["submit_unix"]))
    timeline = read(out / "burst_timeline.csv")
    workers = read(out / "burst_workers.csv")
    summary, = read(out / "burst_summary.csv")
    origin = float(jobs[0]["submit_unix"])

    def minutes(t):
        return (float(t) - origin) / 60

    assert len(jobs) == int(summary["completed"]) == int(summary["requested"])
    assert all(j["final_status"] == "COMPLETED" for j in jobs)
    assert all(w["active"] == "False" and w["ended_first_seen_unix"] for w in workers)
    known_nodes = {w["node"] for w in workers}
    assert all(known_nodes.intersection(j["nodes"].split("|")) for j in jobs)
    assert len({w["instance_id"] for w in workers}) == len(workers)
    assert abs(sum(float(w["total_usd"]) for w in workers) - float(summary["whole_lifetime_estimated_usd"])) < 1e-6
    elapsed = max(float(j["finish_observed_unix"]) for j in jobs) - origin
    assert abs(elapsed-float(summary["batch_seconds"])) < 0.001

    fig, ax = plt.subplots(figsize=(3.45, 2.2), layout="constrained")
    ax.set_axisbelow(True)
    ax.grid(axis="x", color="#dededb", lw=.5)
    labels=[]
    for y,j in enumerate(jobs):
        node_event = next(e for e in timeline if e["job_id"]==j["job_id"] and
                          known_nodes.intersection(e["nodes"].split("|")))
        start, placed, done = map(minutes,(j["submit_unix"], node_event["observed_unix"], j["finish_observed_unix"]))
        assert start <= placed <= done
        ax.hlines(y,start,placed,color="#a6a6a3",lw=4)
        ax.hlines(y,placed,done,color="#2a78d6",lw=4)
        labels.append(f"Job {y+1}")
    for i,w in enumerate(workers, start=len(jobs)):
        ax.hlines(i,minutes(w["first_seen_unix"]),minutes(w["ended_first_seen_unix"]),color="#303844",lw=3)
        labels.append(w["node"])
    ax.set_yticks(range(len(labels)),labels)
    ax.set_ylim(len(labels)-.45,-.55)
    ax.set_xlim(left=0)
    ax.set_xlabel("Minutes since first submission",fontsize=8)
    ax.tick_params(labelsize=8)
    ax.tick_params(axis="y",length=0)
    ax.spines[["top","right","left"]].set_visible(False)
    ax.legend(handles=[Line2D([],[],color="#a6a6a3",lw=4,label="Before batch EC2 placement"),
                       Line2D([],[],color="#2a78d6",lw=4,label="Until completion")],
              loc="lower left",bbox_to_anchor=(0,1),fontsize=7,ncol=2,frameon=False,
              borderaxespad=0,handlelength=1.2,columnspacing=1)
    fig.savefig(out/"burst_timeline.png", dpi=300)
    plt.close(fig)
    print(summary)
if __name__=="__main__":
    plot(Path(sys.argv[1]))

