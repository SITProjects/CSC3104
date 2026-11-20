"""
Trains a small convolutional digits classifier on the GPU if the PC has one,
otherwise on the CPU, so the same script can be timed on both.

It saves progress to $CHECKPOINT_DIR after every epoch and resumes from it,
writes output/metrics.json with the training time, and exports
output/model.onnx for the platform's checker. The held-out 25% of the digits
(the split in examples/make_eval_digits.py) is never used for training.

    python train_bench.py      # 30 epochs
    python train_bench.py 10   # 10 epochs
"""

import json
import os
import sys
import time
from pathlib import Path

import torch
from sklearn.datasets import load_digits
from sklearn.model_selection import train_test_split
from torch import nn

OUTPUT = Path(os.environ.get("OUTPUT_DIR", "output"))
CKPT = Path(os.environ.get("CHECKPOINT_DIR", "checkpoint"))
EPOCHS = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("EPOCHS", "30"))
BATCH = 64

OUTPUT.mkdir(parents=True, exist_ok=True)
CKPT.mkdir(parents=True, exist_ok=True)
dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
if dev.type == "cpu":
    torch.set_num_threads(2)  # the job types both get 2 cores
name = torch.cuda.get_device_name(0) if dev.type == "cuda" else "CPU"
print(f"DEVICE {dev.type} {name}", flush=True)

torch.manual_seed(0)
x, y = load_digits(return_X_y=True)
x_train, _, y_train, _ = train_test_split(x / 16.0, y, random_state=0)
X = torch.tensor(x_train, dtype=torch.float32).reshape(-1, 1, 8, 8).to(dev)
Y = torch.tensor(y_train, dtype=torch.long).to(dev)

model = nn.Sequential(
    nn.Upsample(scale_factor=4, mode="bilinear"),
    nn.Conv2d(1, 32, 3, padding=1), nn.ReLU(),
    nn.Conv2d(32, 64, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
    nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
    nn.Flatten(), nn.Linear(64 * 8 * 8, 128), nn.ReLU(), nn.Linear(128, 10),
).to(dev)
opt = torch.optim.Adam(model.parameters(), lr=1e-3)
loss_fn = nn.CrossEntropyLoss()

start, train_seconds = 0, 0.0
state_file = CKPT / "state.pt"
if state_file.exists():
    state = torch.load(state_file, map_location=dev)
    model.load_state_dict(state["model"])
    opt.load_state_dict(state["opt"])
    start, train_seconds = state["epoch"], state["train_seconds"]
    print(f"RESUMED_AT {start}", flush=True)
else:
    print("FRESH_START", flush=True)


def sync() -> None:
    if dev.type == "cuda":
        torch.cuda.synchronize()


for epoch in range(start, EPOCHS):
    sync()
    t0 = time.perf_counter()
    model.train()
    order = torch.randperm(len(X), generator=torch.Generator().manual_seed(epoch)).to(dev)
    total = 0.0
    for i in range(0, len(X), BATCH):
        idx = order[i : i + BATCH]
        opt.zero_grad()
        loss = loss_fn(model(X[idx]), Y[idx])
        loss.backward()
        opt.step()
        total += float(loss) * len(idx)
    sync()
    took = time.perf_counter() - t0
    train_seconds += took
    print(f"EPOCH {epoch + 1}/{EPOCHS} loss {total / len(X):.4f} seconds {took:.3f}", flush=True)
    tmp = CKPT / "state.tmp"
    torch.save({"model": model.state_dict(), "opt": opt.state_dict(),
                "epoch": epoch + 1, "train_seconds": train_seconds}, tmp)
    tmp.replace(state_file)

model.eval()
with torch.no_grad():
    train_acc = float((model(X).argmax(dim=1) == Y).float().mean())
metrics = {"device": dev.type, "device_name": name, "epochs": EPOCHS,
           "train_seconds": round(train_seconds, 3), "train_accuracy": round(train_acc, 4)}
(OUTPUT / "metrics.json").write_text(json.dumps(metrics, indent=2))
print("METRICS " + json.dumps(metrics), flush=True)

try:
    torch.onnx.export(
        model.cpu(), (torch.zeros(2, 1, 8, 8),), OUTPUT / "model.onnx",
        input_names=["x"], output_names=["logits"], dynamo=True, external_data=False,
        dynamic_shapes=({0: torch.export.Dim("batch")},),
    )
    print("Exported output/model.onnx", flush=True)
except Exception as exc:  # noqa: BLE001 - the CPU image has no ONNX exporter
    print(f"No model.onnx: {exc}", flush=True)
