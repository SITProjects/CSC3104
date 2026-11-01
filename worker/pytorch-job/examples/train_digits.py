"""
A PyTorch training job that survives the PC's owner coming back.

It trains a small digits classifier one epoch at a time and saves the model,
optimizer and epoch number to $CHECKPOINT_DIR after each one. If the job is
moved to another PC, it starts again from the last saved epoch instead of 0.

Upload this file as a Python with PyTorch (CPU) job and run, for example:
    python train_digits.py --epochs 30 --seconds-per-epoch 2

--seconds-per-epoch only slows the demo down, so there is time to press a key
on the worker. The output folder gets model.pt and metrics.json.
"""

import argparse
import json
import os
import time
from pathlib import Path

import torch
from sklearn.datasets import load_digits
from sklearn.model_selection import train_test_split
from torch import nn

CHECKPOINT = Path(os.environ.get("CHECKPOINT_DIR", "checkpoint")) / "state.pt"
OUTPUT = Path(os.environ.get("OUTPUT_DIR", "output"))


def save(state: dict) -> None:
    """Writes the checkpoint so it is never half written: a new file, then a rename."""
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    partial = CHECKPOINT.with_suffix(".tmp")
    torch.save(state, partial)
    os.replace(partial, CHECKPOINT)


def accuracy(model: nn.Module, x: torch.Tensor, y: torch.Tensor) -> float:
    model.eval()
    with torch.no_grad():
        return float((model(x).argmax(dim=1) == y).float().mean())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--seconds-per-epoch", type=float, default=0)
    parser.add_argument(
        "--threads",
        type=int,
        default=2,
        help="CPU threads to use. Match the CPU cores asked for.",
    )
    args = parser.parse_args()
    torch.set_num_threads(args.threads)
    torch.manual_seed(0)

    # The same data every time, so a resumed run trains on what it started with.
    x, y = load_digits(return_X_y=True)
    x_train, x_test, y_train, y_test = train_test_split(x / 16.0, y, random_state=0)
    x_train = torch.tensor(x_train, dtype=torch.float32)
    x_test = torch.tensor(x_test, dtype=torch.float32)
    y_train = torch.tensor(y_train, dtype=torch.int64)
    y_test = torch.tensor(y_test, dtype=torch.int64)
    model = nn.Sequential(nn.Linear(64, 128), nn.ReLU(), nn.Linear(128, 10))
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    criterion = nn.CrossEntropyLoss()

    epoch = 0
    if CHECKPOINT.exists():
        state = torch.load(CHECKPOINT, weights_only=True)
        epoch = state["epoch"]
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        print(f"Resuming from epoch {epoch}", flush=True)
    resumed_from_epoch = epoch

    while epoch < args.epochs:
        model.train()
        order = torch.randperm(
            len(x_train), generator=torch.Generator().manual_seed(epoch)
        )
        total_loss = 0.0
        for start in range(0, len(order), 64):
            batch = order[start : start + 64]
            optimizer.zero_grad()
            loss = criterion(model(x_train[batch]), y_train[batch])
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach()) * len(batch)
        epoch += 1
        save(
            {
                "epoch": epoch,
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
            }
        )
        loss = total_loss / len(x_train)
        acc = accuracy(model, x_test, y_test)
        print(
            f"epoch {epoch}/{args.epochs} loss {loss:.4f} accuracy {acc:.4f}",
            flush=True,
        )
        time.sleep(args.seconds_per_epoch)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), OUTPUT / "model.pt")
    metrics = {
        "epochs": epoch,
        "accuracy": accuracy(model, x_test, y_test),
        "resumed_from_epoch": resumed_from_epoch,
    }
    (OUTPUT / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics), flush=True)


if __name__ == "__main__":
    main()
