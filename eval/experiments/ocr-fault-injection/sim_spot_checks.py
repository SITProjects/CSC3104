"""Offline OCR policy experiment. Set PYTHONPATH to CSC3104 and use its dependencies.

python sim_spot_checks.py --quick
python sim_spot_checks.py --seed-base 0

Each seed supplies the same worker ordering and fault draws to both policies.
All attempted copies finish. Workers stay eligible. Wrong accepts are counted
at initial acceptance. Reopening results of active jobs is outside the model.
"""
import argparse
import asyncio
import csv
import hashlib
import math
import random
import statistics
import sys
from decimal import Decimal
from dataclasses import dataclass
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

from services.verifier.spot_check import accept_on_trust
from services.verifier.trust import HIGH_TRUST_THRESHOLD, INITIAL_TRUST, update_trust_score
from services.verifier.voting import MAX_REPLICAS, winning_hash

TASKS = 3000
WARMUP = 500
RATE = 0.1
FAULT_PROBABILITIES = (0.02, 0.05, 0.10, 0.20, 0.50)
POLICIES = ("adaptive", "voting")
METRICS = (
    "copies_per_task", "copies_per_task_after_warmup", "initially_accepted_wrong_pct",
    "failed_tasks_pct", "first_trust_drop_task",
)


@dataclass
class SimulatedPC:
    # The live accept rule reads replica.node_name and queries its node score.
    node_name: str
    score: Decimal = INITIAL_TRUST

    async def scalar(self, statement):
        # One simulated PC stands in for the selected row in the read-only query.
        return self.score


@dataclass(frozen=True)
class Task:
    workers: list[int]
    fault_draw: float
    check_draw: float
    correct_hash: str
    wrong_hash: str


def make_workload(seed):
    rng = random.Random(seed)
    tasks = []
    for index in range(TASKS):
        workers = rng.sample(range(10), MAX_REPLICAS)
        correct = hashlib.sha256(f"correct:{index}".encode()).hexdigest()
        wrong = hashlib.sha256(f"wrong:{index}".encode()).hexdigest()
        tasks.append(Task(workers, rng.random(), rng.random(), correct, wrong))
    return tasks


async def simulate(tasks, seed, fault_probability, policy):
    pcs = [SimulatedPC(str(i)) for i in range(10)]
    total_copies = later_copies = wrong = failed = 0
    first_drop = None
    for index, task in enumerate(tasks, start=1):
        before = pcs[0].score
        copies = 1 if policy == "adaptive" else 3

        def output(worker):
            if worker == 0 and task.fault_draw < fault_probability:
                return task.wrong_hash
            return task.correct_hash

        hashes = [output(worker) for worker in task.workers[:copies]]
        first_pc = pcs[task.workers[0]]
        trusted_accept = copies == 1 and await accept_on_trust(
            first_pc, first_pc, rate=RATE, draw=lambda: task.check_draw
        )
        if trusted_accept:
            winner = hashes[0]
        else:
            if copies == 1:
                hashes.append(output(task.workers[1]))
                copies += 1
            winner = winning_hash(hashes, copies)
            while winner is None and copies < MAX_REPLICAS:
                hashes.append(output(task.workers[copies]))
                copies += 1
                winner = winning_hash(hashes, copies)
            if winner is not None:
                for worker, answer in zip(task.workers[:copies], hashes, strict=True):
                    pc = pcs[worker]
                    pc.score = update_trust_score(pc.score, answer == winner)

        if first_drop is None and before >= HIGH_TRUST_THRESHOLD > pcs[0].score:
            first_drop = index
        total_copies += copies
        if index > WARMUP:
            later_copies += copies
        failed += winner is None
        wrong += winner is not None and winner != task.correct_hash

    return dict(
        seed=seed, policy=policy, f=fault_probability, tasks=TASKS,
        total_copies=total_copies, copies_after_warmup=later_copies,
        initially_accepted_wrong_count=wrong, failed_tasks=failed,
        copies_per_task=total_copies / TASKS,
        copies_per_task_after_warmup=later_copies / (TASKS-WARMUP),
        initially_accepted_wrong_pct=100*wrong/TASKS, failed_tasks_pct=100*failed/TASKS,
        first_trust_drop_task=first_drop,
    )


def summarize(runs):
    rows = []
    for f in FAULT_PROBABILITIES:
        for policy in POLICIES:
            group = [r for r in runs if r["policy"] == policy and r["f"] == f]
            summary = dict(f=f, policy=policy, seeds=len(group))
            for metric in METRICS:
                vals = [r[metric] for r in group if r[metric] is not None]
                n = len(vals)
                mean = statistics.mean(vals) if n else None
                half = 1.96*statistics.stdev(vals)/math.sqrt(n) if n >= 2 else None
                summary.update({
                    metric+"_n": n, metric+"_mean": mean,
                    metric+"_ci_low": mean-half if half is not None else None,
                    metric+"_ci_high": mean+half if half is not None else None,
                })
            rows.append(summary)
    return rows


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def plot(path, summary):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8})
    fig, axes = plt.subplots(1, 2, figsize=(6.5, 2.6))
    for ax, metric, label in zip(
        axes, ("copies_per_task_after_warmup", "initially_accepted_wrong_pct"),
        ("Copies per task after warm-up", "Tasks initially accepted wrong (%)"), strict=True,
    ):
        for policy, color, marker, linestyle in (
            ("adaptive", "#2a78d6", "o", "-"), ("voting", "#8a8984", "s", "--")
        ):
            points = [r for r in summary if r["policy"] == policy]
            means = [r[metric+"_mean"] for r in points]
            errors = [r[metric+"_ci_high"]-r[metric+"_mean"] for r in points]
            ax.errorbar([100*r["f"] for r in points], means, yerr=errors, color=color,
                        marker=marker, linestyle=linestyle, markersize=3, linewidth=1.3,
                        capsize=2.5, label="Adaptive" if policy=="adaptive" else "Three-copy voting")
        ax.set_xlabel("Faulty PC error probability (%)")
        ax.set_ylabel(label)
        ax.set_xticks([2, 5, 10, 20, 50])
        ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
        ax.grid(axis="y", color="#d9dde0", linewidth=0.5)
        ax.spines[["right", "top"]].set_visible(False)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2, frameon=False)
    fig.subplots_adjust(left=.093, right=.987, bottom=.205, top=.815, wspace=.35)
    fig.savefig(path, dpi=300)
    plt.close(fig)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-base", type=int, default=0)
    parser.add_argument("--quick", action="store_true", help="Use 3 seeds, default output results/quick.")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    out = args.output_dir or Path(__file__).resolve().parent / "results"
    if args.quick and args.output_dir is None:
        out = out / "quick"
    out.mkdir(parents=True, exist_ok=True)
    runs = []
    for seed in range(args.seed_base, args.seed_base+(3 if args.quick else 30)):
        tasks = make_workload(seed)
        for f in FAULT_PROBABILITIES:
            for policy in POLICIES:
                runs.append(await simulate(tasks, seed, f, policy))
    summary = summarize(runs)
    write_csv(out/"spot_checks_runs.csv", runs)
    write_csv(out/"spot_checks_summary.csv", summary)
    plot(out/"spot_checks.png", summary)
    writer = csv.DictWriter(sys.stdout, fieldnames=summary[0].keys())
    writer.writeheader()
    writer.writerows(summary)


if __name__ == "__main__":
    asyncio.run(main())

