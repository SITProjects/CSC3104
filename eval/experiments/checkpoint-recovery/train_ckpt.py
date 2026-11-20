"""CPU checkpoint workload. Epoch indices start at 0; next_epoch is the next index."""
import os
import time
from pathlib import Path
import torch
from sklearn.datasets import load_digits

torch.set_num_threads(1)
torch.manual_seed(17)
x, y = load_digits(return_X_y=True)
x = torch.tensor(x / 16, dtype=torch.float32)
y = torch.tensor(y, dtype=torch.long)
net = torch.nn.Sequential(torch.nn.Linear(64, 64), torch.nn.ReLU(), torch.nn.Linear(64, 10))
optimizer = torch.optim.Adam(net.parameters(), lr=0.001)
state = Path(os.environ["CHECKPOINT_DIR"]) / "state.pt"
tmp = state.with_suffix(".tmp")
start = 0
if state.exists():
    saved = torch.load(state, map_location="cpu", weights_only=True)
    net.load_state_dict(saved["model"])
    optimizer.load_state_dict(saved["optimizer"])
    start = saved["next_epoch"]
    print("RESUMED_AT", start, flush=True)
else:
    print("FRESH_START", flush=True)
for epoch in range(start, 40):
    tick = time.monotonic()
    print("EPOCH", epoch, "START", time.time(), flush=True)
    optimizer.zero_grad()
    torch.nn.functional.cross_entropy(net(x), y).backward()
    optimizer.step()
    time.sleep(max(0, 3 - (time.monotonic() - tick)))
    torch.save(dict(model=net.state_dict(), optimizer=optimizer.state_dict(), next_epoch=epoch+1), tmp)
    os.replace(tmp, state)

