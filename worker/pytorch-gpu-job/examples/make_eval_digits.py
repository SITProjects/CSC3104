"""
Writes eval.npz for train_gpu.py: the digits it holds out for testing, in the
shape its model takes (samples x 1 x 8 x 8, float32) with integer labels.

    python make_eval_digits.py   # then add eval.npz as the job's evaluation file

The platform's checker scores output/model.onnx on it (worker/model-check).
"""

import numpy as np
from sklearn.datasets import load_digits
from sklearn.model_selection import train_test_split

x, y = load_digits(return_X_y=True)
# the same split as train_gpu.py, so these are digits it never trained on
_, x_test, _, y_test = train_test_split(x / 16.0, y, random_state=0)
np.savez(
    "eval.npz",
    x=x_test.astype(np.float32).reshape(-1, 1, 8, 8),
    y=y_test.astype(np.int64),
)
print(f"eval.npz: {len(y_test)} samples")
