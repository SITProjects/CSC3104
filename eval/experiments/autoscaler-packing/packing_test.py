"""Offline cost of synthetic queues. PYTHONPATH must include the CSC3104 root."""

import csv
import math
import random
import statistics
from datetime import UTC, datetime
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from services.autoscaler.burst.config import SLOTS
from services.autoscaler.controller.decide import decide
from services.autoscaler.controller.model import AccountUsage, Snapshot, StuckPod
from services.autoscaler.controller.packing import Bin, pack, smallest_fit
from services.autoscaler.controller.settings import (
    CATALOG,
    BUDGET_USD_PER_HOUR,
    ACCOUNT_MAX_INSTANCES,
    ACCOUNT_MAX_VCPU,
)

# SQL migrations 003 + 009 (OCR), 010 (Python), 012 (PyTorch). mCPU, MiB.
DEFAULTS = {"python": (1000, 1024), "pytorch": (2000, 2048), "ocr": (1000, 256)}
SIZES = (2, 4, 8, 16, 32)
HUB_VCPUS = 2  # Synthetic snapshot: one 2-vCPU hub; zero existing worker capacity.
OUTPUT = Path(__file__).resolve().parent / "results"


def queue(size, seed):
    rng = random.Random(seed)
    while True:
        kinds = rng.choices(list(DEFAULTS), k=size)
        if kinds.count("ocr") % 2 == 0:
            break
    pods = []
    ocr = 0
    for i, kind in enumerate(kinds):
        name = f"{kind}-{i}"
        group = name
        if kind == "ocr":
            group = f"pair-{ocr // 2}"
            ocr += 1
        pods.append(StuckPod(name, 600, *DEFAULTS[kind], group))
    return pods, kinds


def naive(pods):
    bins = []
    for pod in pods:
        b = Bin(smallest_fit(pod), new=True)
        b.place(pod)
        bins.append(b)
    return bins


def measure(size, seed):
    pods, kinds = queue(size, seed)
    snapshot = Snapshot(
        now=datetime(2026, 11, 19, tzinfo=UTC),
        unscheduled_pods=size,
        unschedulable=pods,
        idle_campus_nodes=0,
        burst_nodes={},
        claimed_slots={},
        free_slots=len(SLOTS),
        instances={},
        account=AccountUsage(1, HUB_VCPUS),
    )
    machines = [CATALOG[a.machine] for a in decide(snapshot) if a.kind == "launch"]
    remaining = pack(pods, [Bin(m, new=False) for m in machines])
    waiting = sum(len(b.groups) for b in remaining)
    assert sum(m.cost_per_hour for m in machines) <= BUDGET_USD_PER_HOUR + 1e-9
    assert (
        len(machines) <= min(len(SLOTS), ACCOUNT_MAX_INSTANCES - 1)
        and sum(m.vcpu for m in machines) + HUB_VCPUS <= ACCOUNT_MAX_VCPU
    )
    for policy, bins in [("FFD", pack(pods, [])), ("Naive", naive(pods))]:
        assert sum(len(b.groups) for b in bins) == size
        cost = sum(b.machine.cost_per_hour for b in bins)
        row = dict(
            size=size,
            seed=seed,
            policy=policy,
            **{k: kinds.count(k) for k in DEFAULTS},
            machines=len(bins),
            usd_per_hour=cost,
            mean_allocatable_memory_util_pct=statistics.mean(
                100 * (1 - b.mem_left / b.machine.free_mem_mib) for b in bins
            ),
            over_budget=int(cost > BUDGET_USD_PER_HOUR + 1e-9),
            over_instances=int(len(bins) + 1 > ACCOUNT_MAX_INSTANCES),
            over_vcpus=int(
                sum(b.machine.vcpu for b in bins) + HUB_VCPUS > ACCOUNT_MAX_VCPU
            ),
            over_slots=int(len(bins) > len(SLOTS)),
        )
        row.update(
            controller_launch_machines=len(machines) if policy == "FFD" else None,
            controller_pods_now=size - waiting if policy == "FFD" else None,
            controller_pods_waiting=waiting if policy == "FFD" else None,
        )
        yield row


def csvout(filename, rows):
    with (OUTPUT / filename).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0])
        w.writeheader()
        w.writerows(rows)


def main():
    OUTPUT.mkdir(exist_ok=True)
    runs = [
        row
        for size in SIZES
        for i in range(200)
        for row in measure(size, size * 1000 + i)
    ]
    summary = []
    for size in SIZES:
        for policy in ["FFD", "Naive"]:
            group = [r for r in runs if r["size"] == size and r["policy"] == policy]
            s = dict(size=size, policy=policy, n=len(group))
            for metric in list(group[0])[6:]:
                vals = [r[metric] for r in group if r[metric] is not None]
                if vals:
                    m = statistics.mean(vals)
                    h = 1.96 * statistics.stdev(vals) / math.sqrt(len(vals))
                for key, val in zip(
                    ("mean", "ci_low", "ci_high"),
                    (m, m - h, m + h) if vals else (None, None, None),
                ):
                    s[metric + "_" + key] = val
            summary.append(s)
    csvout("packing_runs.csv", runs)
    csvout("packing_summary.csv", summary)
    fig, ax = plt.subplots(figsize=(3.3, 2.4), layout="constrained")
    for policy, color in [("FFD", "#2a78d6"), ("Naive", "#8a8984")]:
        pts = [s for s in summary if s["policy"] == policy]
        means = [s["usd_per_hour_mean"] for s in pts]
        ax.errorbar(
            SIZES,
            means,
            yerr=[s["usd_per_hour_ci_high"] - m for s, m in zip(pts, means)],
            label=policy,
            color=color,
            marker="o",
            ms=3,
            capsize=2,
        )
    ax.axhline(
        BUDGET_USD_PER_HOUR, color="black", ls="--", lw=1, label="Default budget"
    )
    ax.set(xlabel="Waiting pods", ylabel="Planned cost (USD/h)", xticks=SIZES)
    ax.grid(axis="y", alpha=0.2)
    ax.legend(fontsize=7)
    fig.savefig(OUTPUT / "packing.png", dpi=300)
    plt.close(fig)
    print(DEFAULTS)
    print(
        "budget,instance_cap,vcpu_cap,slots,hub_vcpus",
        BUDGET_USD_PER_HOUR,
        ACCOUNT_MAX_INSTANCES,
        ACCOUNT_MAX_VCPU,
        len(SLOTS),
        HUB_VCPUS,
    )
    print((OUTPUT / "packing_summary.csv").read_text())


if __name__ == "__main__":
    main()
