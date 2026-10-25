"""Eviction speed results (member 2). Joins the worker's node-agent event log with
run_trials.py's CSV, writes one row per trial and draws the box plot.

    python analyse.py

Two clocks are involved, and no time is ever subtracted across them:
- key press -> taint applied: worker clock (both from the event log). The key
  press is the BUSY line's time minus the idle_seconds the sensor reported.
- taint seen -> pod stopped: EC2 clock (both from run_trials.py).
Each trial is matched to its taint by nearest time, so the two clocks only need
to agree to within MATCH_S.
"""

import argparse
import csv
import json
import statistics
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

EVAL = Path(__file__).resolve().parents[1]
MATCH_S = 5

# Report figure colours: one series, so one hue; ink and grid stay neutral
SERIES = "#2a78d6"
INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#e4e3df"


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def key_presses(events_path: Path) -> list[dict]:
    """Each taint added because of input, with when the key was pressed."""
    found = []
    busy = None  # the latest BUSY state line
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event["event"] == "state":
            busy = event if event["new"] == "BUSY" else None
        elif event["event"] == "taint" and event["added"] and busy is not None:
            reason = busy["reason"]
            if reason.startswith("input") and busy["idle_seconds"] is not None:
                taint = epoch(event["time"])
                found.append(
                    {
                        "taint_applied": taint,
                        "key_to_taint": taint - epoch(busy["time"]) + busy["idle_seconds"],
                    }
                )
            busy = None
    return found


def join(trials: list[dict], presses: list[dict]) -> list[dict]:
    rows = []
    unused = list(presses)
    for trial in trials:
        seen = float(trial["taint_seen"])
        match = min(unused, key=lambda p: abs(p["taint_applied"] - seen), default=None)
        if match is None or abs(match["taint_applied"] - seen) > MATCH_S:
            print(f"trial {trial['trial']}: no key-press taint in the event log, skipped")
            continue
        unused.remove(match)
        taint_to_stop = float(trial["pod_stopped"]) - seen
        rows.append(
            {
                "trial": int(trial["trial"]),
                "key_to_taint_s": round(match["key_to_taint"], 3),
                "taint_to_pod_stopped_s": round(taint_to_stop, 3),
                "key_to_pod_stopped_s": round(match["key_to_taint"] + taint_to_stop, 3),
                "taint_to_delete_s": round(float(trial["pod_deleting"]) - seen, 3),
                # Propagation plus clock difference, to check the match
                "clock_gap_s": round(seen - match["taint_applied"], 3),
            }
        )
    return rows


def summary(name: str, values: list[float]) -> str:
    q = statistics.quantiles(values, n=4) if len(values) > 1 else [values[0]] * 3
    return (
        f"{name:<24} median {statistics.median(values):6.2f}s   "
        f"IQR {q[0]:.2f}-{q[2]:.2f}s   range {min(values):.2f}-{max(values):.2f}s"
    )


def plot(rows: list[dict], path: Path) -> None:
    groups = [
        ("Key press\nto taint", [r["key_to_taint_s"] for r in rows]),
        ("Key press\nto pod stopped", [r["key_to_pod_stopped_s"] for r in rows]),
    ]
    fig, ax = plt.subplots(figsize=(6, 4.2), dpi=200)
    ax.boxplot(
        [values for _, values in groups],
        widths=0.45,
        showfliers=False,  # every point is drawn below, outliers included
        medianprops={"color": SERIES, "linewidth": 2},
        boxprops={"color": MUTED, "linewidth": 1},
        whiskerprops={"color": MUTED, "linewidth": 1},
        capprops={"color": MUTED, "linewidth": 1},
    )
    for i, (_, values) in enumerate(groups, start=1):
        # Spread points sideways so equal values do not hide each other
        offsets = [((j % 7) - 3) * 0.035 for j in range(len(values))]
        ax.scatter(
            [i + o for o in offsets],
            values,
            s=22,
            color=SERIES,
            edgecolor="white",
            linewidth=0.8,
            zorder=3,
        )
    ax.set_xticks([1, 2], [label for label, _ in groups], color=INK)
    ax.set_ylabel("Seconds", color=MUTED)
    ax.set_ylim(bottom=0)
    ax.set_title(
        f"Owners get their PC back fast ({len(rows)} trials)", color=INK, loc="left"
    )
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events", type=Path, default=EVAL / "results" / "eviction_events.jsonl")
    parser.add_argument("--trials", type=Path, default=EVAL / "results" / "eviction_k8s.csv")
    parser.add_argument("--out", type=Path, default=EVAL / "results" / "eviction_speed.csv")
    parser.add_argument("--graph", type=Path, default=EVAL / "graphs" / "eviction_speed.png")
    args = parser.parse_args()

    with args.trials.open(newline="") as file:
        trials = list(csv.DictReader(file))
    rows = join(trials, key_presses(args.events))
    if not rows:
        raise SystemExit("no trials matched; is the event log from the same run?")

    with args.out.open("w", newline="") as file:
        writer = csv.DictWriter(file, rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    plot(rows, args.graph)

    print(f"{len(rows)} of {len(trials)} trials matched\n")
    for column in ("key_to_taint_s", "taint_to_pod_stopped_s", "key_to_pod_stopped_s"):
        print(summary(column, [r[column] for r in rows]))
    gaps = [r["clock_gap_s"] for r in rows]
    print(f"\nclock gap (EC2 saw taint - worker applied it): median {statistics.median(gaps):.3f}s")
    print(f"\nwrote {args.out}\nwrote {args.graph}")


if __name__ == "__main__":
    main()
