"""
A PyTorch training job for the GPU job type that survives the PC's owner
coming back.

It trains a small convolutional digits classifier on the GPU and saves
everything needed to carry on (model, optimizer, step, epoch and random state)
to $CHECKPOINT_DIR every --save-seconds, also in the middle of an epoch, with
the image's idlecloud_ckpt helper. If the job is moved, it starts again from
the last uploaded save, not from 0, on whichever GPU it lands on.

Upload this file as a "Python with PyTorch (GPU)" job and run, for example:
    python train_gpu.py --epochs 40 --save-seconds 30

It stops with an error when there is no CUDA device, so a broken GPU setup is
never mistaken for slow training. The output folder gets model.pt and
metrics.json.
"""

import argparse
import json
import os
import time
from pathlib import Path

import torch
from idlecloud_ckpt import Checkpointer
from sklearn.datasets import load_digits
from sklearn.model_selection import train_test_split
from torch import nn

OUTPUT = Path(os.environ.get("OUTPUT_DIR", "output"))
BATCH = 64


def accuracy(model: nn.Module, x: torch.Tensor, y: torch.Tensor) -> float:
    model.eval()
    with torch.no_grad():
        return float((model(x).argmax(dim=1) == y).float().mean())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument(
        "--save-seconds",
        type=float,
        default=300,
        help="Save progress at least this often, even inside an epoch.",
    )
    parser.add_argument(
        "--slow-down",
        type=float,
        default=0,
        help="Seconds to wait after each batch, only to give a demo time to evict.",
    )
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit("No CUDA device: this job needs the GPU job type's worker.")
    device = torch.device("cuda")
    print(f"cuda: True, device {torch.cuda.get_device_name(0)}", flush=True)
    torch.manual_seed(0)

    # The same data every time, so a resumed run trains on what it started with.
    x, y = load_digits(return_X_y=True)
    x_train, x_test, y_train, y_test = train_test_split(x / 16.0, y, random_state=0)
    x_train = torch.tensor(x_train, dtype=torch.float32, device=device).view(
        -1, 1, 8, 8
    )
    x_test = torch.tensor(x_test, dtype=torch.float32, device=device).view(-1, 1, 8, 8)
    y_train = torch.tensor(y_train, dtype=torch.int64, device=device)
    y_test = torch.tensor(y_test, dtype=torch.int64, device=device)
    model = nn.Sequential(
        nn.Conv2d(1, 32, 3, padding=1),
        nn.ReLU(),
        nn.Conv2d(32, 64, 3, padding=1),
        nn.ReLU(),
        nn.Flatten(),
        nn.Linear(64 * 8 * 8, 128),
        nn.ReLU(),
        nn.Linear(128, 10),
    ).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    criterion = nn.CrossEntropyLoss()

    batches_per_epoch = -(-len(x_train) // BATCH)
    ckpt = Checkpointer(every_seconds=args.save_seconds)
    epoch, batch_index = 0, 0
    state = ckpt.load(map_location=device)
    if state:
        epoch, batch_index = state["epoch"], state["batch_index"]
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        torch.set_rng_state(state["cpu_rng"].cpu())
        # The GPU random state is only kept on the same kind of GPU
        if state.get("cuda_rng_device") == torch.cuda.get_device_name(0):
            torch.cuda.set_rng_state(state["cuda_rng"].cpu())
        print(f"Resuming from epoch {epoch}, batch {batch_index}", flush=True)
    resumed_from = {"epoch": epoch, "batch": batch_index}

    def checkpoint() -> None:
        ckpt.save(
            {
                "epoch": epoch,
                "batch_index": batch_index,
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "cpu_rng": torch.get_rng_state(),
                "cuda_rng": torch.cuda.get_rng_state(),
                "cuda_rng_device": torch.cuda.get_device_name(0),
            },
            step=epoch * batches_per_epoch + batch_index,
        )

    loss = torch.tensor(float("nan"))  # a resumed epoch may have no batches left
    while epoch < args.epochs:
        model.train()
        # The order depends only on the epoch, so a resumed epoch skips exactly
        # the batches it already trained on.
        order = torch.randperm(
            len(x_train), generator=torch.Generator().manual_seed(epoch)
        ).to(device)
        batches = range(0, len(order), BATCH)
        for start in list(batches)[batch_index:]:
            batch = order[start : start + BATCH]
            optimizer.zero_grad()
            loss = criterion(model(x_train[batch]), y_train[batch])
            loss.backward()
            optimizer.step()
            batch_index += 1
            if ckpt.due():
                checkpoint()
            time.sleep(args.slow_down)
        epoch, batch_index = epoch + 1, 0
        checkpoint()
        print(
            f"epoch {epoch}/{args.epochs} loss {float(loss):.4f} "
            f"accuracy {accuracy(model, x_test, y_test):.4f}",
            flush=True,
        )

    OUTPUT.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), OUTPUT / "model.pt")
    metrics = {
        "epochs": epoch,
        "accuracy": accuracy(model, x_test, y_test),
        "resumed_from": resumed_from,
        "device": torch.cuda.get_device_name(0),
    }
    (OUTPUT / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics), flush=True)


if __name__ == "__main__":
    main()
