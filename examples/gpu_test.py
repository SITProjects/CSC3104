"""GPU smoke test and small training run for the IdleCloud pytorch-gpu job type.

Submit it as a pytorch-gpu job with the command:
    python gpu_test.py
Options: --epochs 30 --samples 250000 --save-seconds 30
         --amp (train in FP16 mixed precision; off by default)
         --allow-cpu (run anyway without a GPU)

--samples sets the dataset size, which decides the RAM the job needs: about
0.7 GiB plus 5.3 KiB per sample at its peak. Up to about 600000 fits the
default 4096 MiB; ask for 6144 MiB on the New job page for up to about 900000.

--amp is faster only on GPUs with tensor cores (RTX 20 series and newer). On a
GTX 16 series card FP16 is several times slower than FP32, so leave it off; the
benchmark in step 2 shows which is faster on the PC the job landed on.

What it does, in three steps, all logged:
  1. Reports the GPU PyTorch sees (name, compute capability, memory, CUDA).
  2. Benchmarks matrix multiplies on the GPU against the CPU (FP32 and FP16)
     and checks the GPU gets the same answer.
  3. Trains an MLP classifier on a synthetic dataset (no download needed),
     in FP32 or, with --amp, mixed precision, checkpointing to $CHECKPOINT_DIR every --save-seconds so a
     moved job resumes where it stopped.
Results go to $OUTPUT_DIR: metrics.json and model.pt.
"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import numpy as np
import torch
from sklearn.datasets import make_classification
from sklearn.model_selection import train_test_split
from torch import nn

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
CHECKPOINT_DIR = Path(os.environ.get("CHECKPOINT_DIR", "checkpoints"))
CHECKPOINT = CHECKPOINT_DIR / "gpu_test.pt"


def report_progress(percent: float) -> None:
    """Moves the job page's progress bar (0 to 100): the runner reads $PROGRESS_FILE."""
    path = os.environ.get("PROGRESS_FILE")
    if not path:
        return
    tmp = f"{path}.tmp"
    Path(tmp).write_text(f"{max(0.0, min(100.0, percent)):.1f}\n")
    os.replace(tmp, path)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def gpu_info(device: torch.device) -> dict:
    info = {
        "torch": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "cuda_build": torch.version.cuda,
        "hip_build": getattr(torch.version, "hip", None),  # set on ROCm (AMD) builds
        "device": str(device),
    }
    if device.type == "cuda":
        props = torch.cuda.get_device_properties(device)
        info.update(
            name=props.name,
            compute_capability=f"{props.major}.{props.minor}",
            memory_gib=round(props.total_memory / 2**30, 2),
            multiprocessors=props.multi_processor_count,
            device_count=torch.cuda.device_count(),
        )
    return info


def time_matmul(device: torch.device, dtype: torch.dtype, n: int, repeats: int) -> float:
    """Returns TFLOPS for an n x n matrix multiply."""
    a = torch.randn(n, n, device=device, dtype=dtype)
    b = torch.randn(n, n, device=device, dtype=dtype)
    a @ b  # warm-up (kernel selection, lazy init)
    if device.type == "cuda":
        torch.cuda.synchronize()
    start = time.perf_counter()
    for _ in range(repeats):
        a @ b
    if device.type == "cuda":
        torch.cuda.synchronize()
    seconds = (time.perf_counter() - start) / repeats
    return 2 * n**3 / seconds / 1e12


def benchmark(device: torch.device) -> dict:
    results = {}
    cpu_tflops = time_matmul(torch.device("cpu"), torch.float32, 1024, 3)
    results["cpu_fp32_tflops"] = round(cpu_tflops, 3)
    log(f"CPU  FP32 matmul 1024x1024: {cpu_tflops:.3f} TFLOPS")
    if device.type != "cuda":
        return results

    for dtype, name in [(torch.float32, "fp32"), (torch.float16, "fp16")]:
        tflops = time_matmul(device, dtype, 4096, 20)
        results[f"gpu_{name}_tflops"] = round(tflops, 2)
        log(f"GPU  {name.upper()} matmul 4096x4096: {tflops:.2f} TFLOPS")
    results["gpu_speedup_fp32"] = round(results["gpu_fp32_tflops"] / cpu_tflops, 1)
    log(f"GPU is {results['gpu_speedup_fp32']}x the CPU at FP32")

    # Same answer on both devices (TF32 off so FP32 is compared like for like)
    torch.backends.cuda.matmul.allow_tf32 = False
    a, b = torch.randn(512, 512), torch.randn(512, 512)
    diff = (a @ b - (a.to(device) @ b.to(device)).cpu()).abs().max().item()
    results["gpu_vs_cpu_max_abs_diff"] = diff
    if diff > 1e-2:
        raise SystemExit(f"GPU result differs from CPU by {diff}: the GPU is not computing correctly")
    log(f"GPU and CPU results match (max difference {diff:.2e})")
    torch.backends.cuda.matmul.allow_tf32 = True
    return results


def make_model(features: int, classes: int) -> nn.Module:
    return nn.Sequential(
        nn.Linear(features, 2048), nn.BatchNorm1d(2048), nn.ReLU(), nn.Dropout(0.2),
        nn.Linear(2048, 1024), nn.BatchNorm1d(1024), nn.ReLU(), nn.Dropout(0.2),
        nn.Linear(1024, 512), nn.BatchNorm1d(512), nn.ReLU(), nn.Dropout(0.2),
        nn.Linear(512, 256), nn.ReLU(),
        nn.Linear(256, classes),
    )


def save_checkpoint(state: dict) -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT.with_suffix(".tmp")
    torch.save(state, tmp)
    tmp.replace(CHECKPOINT)  # never leave a half-written checkpoint


def evaluate(model, x, y, batch: int) -> float:
    model.eval()
    correct = 0
    with torch.no_grad():
        for i in range(0, len(x), batch):
            correct += (model(x[i : i + batch]).argmax(1) == y[i : i + batch]).sum().item()
    model.train()
    return correct / len(x)


def train(device: torch.device, args) -> dict:
    # Fixed seed, so a resumed run trains on exactly the same data
    x, y = make_classification(
        n_samples=args.samples, n_features=256, n_informative=96, n_redundant=32,
        n_classes=10, n_clusters_per_class=2, class_sep=1.2, random_state=0,
    )
    x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.2, random_state=0)
    del x, y  # free the float64 copy before PyTorch makes its own
    x_train = torch.tensor(x_train, dtype=torch.float32, device=device)
    y_train = torch.tensor(y_train, dtype=torch.long, device=device)
    x_test = torch.tensor(x_test, dtype=torch.float32, device=device)
    y_test = torch.tensor(y_test, dtype=torch.long, device=device)
    log(f"Data: {len(x_train)} train, {len(x_test)} test samples, 256 features, 10 classes")

    model = make_model(256, 10).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    use_amp = device.type == "cuda" and args.amp
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
    loss_fn = nn.CrossEntropyLoss()

    epoch, step, history, resumed_from = 0, 0, [], None
    if CHECKPOINT.exists():
        state = torch.load(CHECKPOINT, map_location=device, weights_only=False)
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        scheduler.load_state_dict(state["scheduler"])
        scaler.load_state_dict(state["scaler"])
        epoch, step, history = state["epoch"], state["step"], state["history"]
        torch.set_rng_state(state["rng_cpu"].cpu())  # map_location moved it to the GPU
        if device.type == "cuda":
            torch.cuda.set_rng_state(state["rng_cuda"].cpu())
        resumed_from = {"epoch": epoch, "step": step}
        log(f"Resumed from checkpoint at epoch {epoch + 1}, step {step}")

    steps_per_epoch = (len(x_train) + args.batch - 1) // args.batch
    total_steps = args.epochs * steps_per_epoch
    last_save = last_progress = time.monotonic()
    train_start = time.perf_counter()

    def checkpoint_state():
        return {
            "model": model.state_dict(), "optimizer": optimizer.state_dict(),
            "scheduler": scheduler.state_dict(), "scaler": scaler.state_dict(),
            "epoch": epoch, "step": step, "history": history,
            "rng_cpu": torch.get_rng_state(),
            "rng_cuda": torch.cuda.get_rng_state() if device.type == "cuda" else None,
        }

    while epoch < args.epochs:
        # The order of each epoch depends only on the epoch number, so resuming
        # mid-epoch skips exactly the batches already trained on
        order = torch.randperm(len(x_train), generator=torch.Generator().manual_seed(epoch)).to(device)
        epoch_start, running_loss, batches = time.perf_counter(), 0.0, 0
        while step < steps_per_epoch:
            idx = order[step * args.batch : (step + 1) * args.batch]
            with torch.autocast(device.type, dtype=torch.float16, enabled=use_amp):
                loss = loss_fn(model(x_train[idx]), y_train[idx])
            optimizer.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            running_loss += loss.item()
            batches += 1
            step += 1
            if time.monotonic() - last_progress >= 5:
                report_progress(100 * (epoch * steps_per_epoch + step) / total_steps)
                last_progress = time.monotonic()
            if time.monotonic() - last_save >= args.save_seconds:
                save_checkpoint(checkpoint_state())
                last_save = time.monotonic()
                log(f"Checkpoint saved (epoch {epoch + 1}, step {step}/{steps_per_epoch})")

        scheduler.step()
        accuracy = evaluate(model, x_test, y_test, 8192)
        seconds = time.perf_counter() - epoch_start
        history.append({
            "epoch": epoch + 1, "loss": round(running_loss / max(batches, 1), 4),
            "test_accuracy": round(accuracy, 4), "seconds": round(seconds, 2),
            "samples_per_second": round(batches * args.batch / seconds),
        })
        log(f"Epoch {epoch + 1}/{args.epochs}: loss {history[-1]['loss']:.4f}, "
            f"test accuracy {accuracy:.2%}, {history[-1]['samples_per_second']:,} samples/s")
        epoch, step = epoch + 1, 0
        save_checkpoint(checkpoint_state())
        report_progress(100 * epoch / args.epochs)
        last_save = time.monotonic()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), OUTPUT_DIR / "model.pt")
    result = {
        "epochs": args.epochs, "batch_size": args.batch, "mixed_precision": use_amp,
        "final_test_accuracy": history[-1]["test_accuracy"] if history else None,
        "training_seconds_this_run": round(time.perf_counter() - train_start, 1),
        "resumed_from": resumed_from, "history": history,
    }
    if device.type == "cuda":
        result["peak_gpu_memory_mib"] = round(torch.cuda.max_memory_allocated(device) / 2**20)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch", type=int, default=2048)
    # ~2 GiB peak RAM; about 600000 fit the default 4096 MiB, 900000 fit 6144 MiB
    parser.add_argument("--samples", type=int, default=250_000)
    parser.add_argument("--amp", action="store_true", help="train in FP16 mixed precision (needs tensor cores to pay off)")
    parser.add_argument("--save-seconds", type=int, default=30)
    parser.add_argument("--allow-cpu", action="store_true", help="run on the CPU if there is no GPU")
    args = parser.parse_args()

    random.seed(0)
    np.random.seed(0)
    torch.manual_seed(0)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    info = gpu_info(device)
    backend = f"HIP {info['hip_build']}" if info["hip_build"] else f"CUDA build {info['cuda_build']}"
    log(f"cuda: {info['cuda_available']}  torch {info['torch']}  {backend}")
    if device.type == "cuda":
        log(f"GPU: {info['name']}, compute capability {info['compute_capability']}, "
            f"{info['memory_gib']} GiB, {info['multiprocessors']} SMs")
    elif not args.allow_cpu:
        sys.exit("No CUDA GPU visible to PyTorch. This is a GPU test, so it stops here "
                 "(use --allow-cpu to run anyway).")

    started = time.perf_counter()
    metrics = {"gpu": info, "benchmark": benchmark(device)}
    metrics["training"] = train(device, args)
    metrics["total_seconds"] = round(time.perf_counter() - started, 1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2))
    log(f"Done in {metrics['total_seconds']} s. Final test accuracy "
        f"{metrics['training']['final_test_accuracy']:.2%}. Wrote metrics.json and model.pt to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
