"""
A training job that survives the PC's owner coming back.

It trains a scikit-learn classifier one epoch at a time and saves the model
and the epoch number to $CHECKPOINT_DIR after each one. If the job is moved to
another PC, it starts again from the last saved epoch instead of epoch 0.

Upload this file as a Python script job and run, for example:
    python train_resumable.py --epochs 40 --seconds-per-epoch 3

--seconds-per-epoch only slows the demo down, so there is time to press a key
on the worker. The output folder gets model.pkl and metrics.json.
"""

import argparse
import json
import os
import pickle
import time
from pathlib import Path

from sklearn.datasets import make_classification
from sklearn.linear_model import SGDClassifier
from sklearn.model_selection import train_test_split

CHECKPOINT = Path(os.environ.get("CHECKPOINT_DIR", "checkpoint")) / "state.pkl"
OUTPUT = Path(os.environ.get("OUTPUT_DIR", "output"))


def save(state: dict) -> None:
    """Writes the checkpoint so it is never half written: a new file, then a rename."""
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    partial = CHECKPOINT.with_suffix(".tmp")
    partial.write_bytes(pickle.dumps(state))
    os.replace(partial, CHECKPOINT)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--seconds-per-epoch", type=float, default=0)
    args = parser.parse_args()

    # the same data every time, so a resumed run trains on what it started with
    x, y = make_classification(n_samples=20_000, n_features=20, random_state=0)
    x_train, x_test, y_train, y_test = train_test_split(x, y, random_state=0)
    classes = sorted(set(y))

    if CHECKPOINT.exists():
        state = pickle.loads(CHECKPOINT.read_bytes())
        print(f"Resuming from epoch {state['epoch']}", flush=True)
    else:
        state = {"epoch": 0, "model": SGDClassifier(random_state=0)}
    state["resumed_from"] = state["epoch"]

    while state["epoch"] < args.epochs:
        state["model"].partial_fit(x_train, y_train, classes=classes)
        state["epoch"] += 1
        save(state)
        score = state["model"].score(x_test, y_test)
        print(f"epoch {state['epoch']}/{args.epochs} accuracy {score:.4f}", flush=True)
        time.sleep(args.seconds_per_epoch)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "model.pkl").write_bytes(pickle.dumps(state["model"]))
    metrics = {
        "epochs": state["epoch"],
        "accuracy": state["model"].score(x_test, y_test),
        "resumed_from_epoch": state["resumed_from"],
    }
    (OUTPUT / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics), flush=True)


if __name__ == "__main__":
    main()
