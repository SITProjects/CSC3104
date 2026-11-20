"""Offline, CPU-only checker experiment. Requires CSC3104 on PYTHONPATH."""

import csv
import hashlib
import importlib.util
import io
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import torch
from sklearn.datasets import load_digits
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parent


def checker():
    path = next(
        Path(p) / "worker/model-check/evaluate.py"
        for p in sys.path
        if (Path(p) / "worker/model-check/evaluate.py").is_file()
    )
    spec = importlib.util.spec_from_file_location("platform_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def export(seed, trained, x, y):
    torch.manual_seed(seed)
    net = torch.nn.Sequential(
        torch.nn.Linear(64, 64), torch.nn.ReLU(), torch.nn.Linear(64, 10)
    )
    if trained:
        optimizer = torch.optim.Adam(net.parameters(), lr=1e-3)
        net.train()
        for _ in range(30):
            for indices in torch.randperm(len(x)).split(64):
                optimizer.zero_grad()
                loss = torch.nn.functional.cross_entropy(net(x[indices]), y[indices])
                loss.backward()
                optimizer.step()
    net.eval()
    model = io.BytesIO()
    torch.onnx.export(
        net,
        torch.zeros(1, 64),
        model,
        input_names=["x"],
        output_names=["logits"],
        dynamic_axes={"x": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )
    return model.getvalue()


def main():
    evaluator = checker()
    torch.set_num_threads(1)
    x, y = load_digits(return_X_y=True)
    xtrain, xeval, ytrain, yeval = train_test_split(
        (x / 16).astype(np.float32),
        y.astype(np.int64),
        test_size=0.30,
        random_state=17,
        stratify=y,
    )
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    np.savez(out / "eval.npz", x=xeval, y=yeval)
    train_x, train_y = torch.from_numpy(xtrain), torch.from_numpy(ytrain)
    check_x, check_y = evaluator.load_eval((out / "eval.npz").read_bytes())
    rows = []
    for seed in range(1, 6):
        for kind in ("trained", "random"):
            data = export(seed, kind == "trained", train_x, train_y)
            (out / f"{kind}-{seed}.onnx").write_bytes(data)
            scores = []
            times = []
            for _ in range(5):
                start = time.perf_counter()
                scores.append(evaluator.accuracy(data, check_x, check_y))
                times.append((time.perf_counter() - start) * 1000)
            assert len(set(scores)) == 1
            rows.append(
                dict(
                    kind=kind,
                    seed=seed,
                    sha256=hashlib.sha256(data).hexdigest(),
                    accuracy=scores[0],
                    sample_count=len(check_y),
                    median_scoring_ms=statistics.median(times),
                )
            )
    with (out / "checker_test.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    print((out / "checker_test.csv").read_text(), end="")
    print(
        "distinct_trained_hashes",
        len({r["sha256"] for r in rows if r["kind"] == "trained"}),
    )
    for kind in ("trained", "random"):
        scores = [r["accuracy"] for r in rows if r["kind"] == kind]
        print(kind, min(scores), max(scores))


if __name__ == "__main__":
    main()
