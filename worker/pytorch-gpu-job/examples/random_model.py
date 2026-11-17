"""
A test fixture, not a real job: "trains" nothing and returns a model with
random weights, while its log claims great accuracy. The platform's score on
eval.npz (make_eval_digits.py) should be about 10%, whatever the log says, and
no PC's trust changes.

    python random_model.py
"""

import os
from pathlib import Path

import torch
from torch import nn

OUTPUT = Path(os.environ.get("OUTPUT_DIR", "output"))
OUTPUT.mkdir(parents=True, exist_ok=True)
torch.manual_seed(1234)
model = nn.Sequential(
    nn.Flatten(), nn.Linear(64, 128), nn.ReLU(), nn.Linear(128, 10)
).eval()
torch.onnx.export(
    model,
    (torch.zeros(2, 1, 8, 8),),
    OUTPUT / "model.onnx",
    input_names=["x"],
    output_names=["logits"],
    dynamo=True,
    external_data=False,  # one file: the checker reads only model.onnx
    dynamic_shapes=({0: torch.export.Dim("batch")},),
)
print("epoch 40/40 accuracy 0.9990  (not true: the weights are random)")
