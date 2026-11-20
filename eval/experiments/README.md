# Report experiments

These are the scripts, input files and raw results behind Section IV (Evaluation) of the
article `GROUP17.pdf`. Each folder holds one experiment. Results sit in its `results/` folder.

Run the offline scripts from the repository root with the repository on `PYTHONPATH`, for example:

```bash
PYTHONPATH=. uv run python eval/experiments/ocr-fault-injection/sim_spot_checks.py
```

| Folder | Report section | What it measures | Main result |
|---|---|---|---|
| `gpu-training/` | IV-A, Fig. 4 | The same 30-epoch PyTorch job on a campus GPU and on two CPU cores of the same desktop, submitted through the live web app | Median epoch after the first: 0.25 s on the AMD RX 7800 XT against 1.45 s on the CPU (5.8 times faster). Checker score 98.7% on 450 held-out digits |
| `../eviction/` and `../results/eviction_*` | IV-B, Fig. 5 | Key press to Pod stop over 20 trials on one Windows worker | Estimated median 3.33 s |
| `checkpoint-recovery/` | IV-C, Table I | Ten owner interruptions of a CPU PyTorch job on the local Docker Compose stack | 10 of 10 resumed on another worker, median 14.76 s from taint to first resumed epoch |
| `model-checker/` | IV-D | The platform checker scoring five trained and five untrained ONNX models | Trained 94.8% to 95.9%, untrained 9.6% to 17.8%, five different SHA-256 hashes |
| `ocr-fault-injection/` | IV-D, Fig. 6 | The verifier's trust and voting functions with one faulty PC among ten, 30 seeds per error rate | 1.105 to 1.244 copies per task against 3.00 for voting |
| `live-bursting/` | IV-E, Table II | Four 120 s CPU jobs on the live system, with and without an idle campus PC | 462 s and US$0.0087 with two EC2 workers, 266 s and US$0 on the campus desktop |
| `autoscaler-packing/` | IV-E | First-fit decreasing packing against one machine per Pod over 200 queues of 32 Pods | 14% lower mean hourly cost (US$0.73 against US$0.85) |

## Notes per experiment

**gpu-training.** Upload `train_bench.py` as a "Python with PyTorch (GPU)" job with the command
`python train_bench.py 30` and `eval.npz` as the evaluation file. Run the same command as a
"Python with PyTorch (CPU)" job for the comparison. `results/summary.json` records both job ids,
the worker, the checker score and the model hash. `results/epoch_times.csv` holds the time of every epoch.

**checkpoint-recovery.** `recovery_trials.py` runs against the local stack from the repository root.
It submits `train_ckpt.py`, applies the owner-active taint at a random point and records the
timings in `results/recovery_trials.csv`. The `*-old.log` and `*-new.log` files are the job logs
from before and after each move.

**model-checker.** `checker_test.py` trains the models, exports them to ONNX and scores them with
the code in `worker/model-check`. It runs on a CPU.

**ocr-fault-injection.** `sim_spot_checks.py` drives the verifier's own trust-update and voting
functions with generated results. Wrong answers are counted when a task is first accepted.

**live-bursting.** `run_fallback_batch.py` submits the batch and records placement, timing and the
estimated EC2 cost. It asks for the admin password at run time and stores no credentials.
`plot_burst_timeline.py` draws `burst_timeline.png` from a results folder.

**autoscaler-packing.** `packing_test.py` calls the autoscaler's own packing and decision code on
synthetic queues.
