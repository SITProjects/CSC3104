"""
Checkpoint helper for the GPU job type: save training state by time, safely.

The runner uploads $CHECKPOINT_DIR when it changes, and a moved job starts
again with the last upload. This helper writes that folder so an upload never
holds a half-written file, and records the training step so the job page can
show how far the last save got.

    from idlecloud_ckpt import Checkpointer

    ckpt = Checkpointer(every_seconds=300)
    state = ckpt.load()                # None on a fresh start
    if state:
        model.load_state_dict(state["model"])
        step = state["step"]
    ...
    if ckpt.due():                     # every_seconds since the last save
        ckpt.save({"model": model.state_dict(), "step": step}, step=step)

load() maps tensors to this node's GPU (or the CPU without one), so a job
saved on one GPU can carry on on another, NVIDIA or AMD.
"""

import json
import os
import time
from pathlib import Path

import torch

STEP_FILE = "idlecloud.json"  # read by the runner, see worker/runner/runner.py


class Checkpointer:
    def __init__(
        self,
        every_seconds: float = 300,
        folder: str | os.PathLike | None = None,
        name: str = "state.pt",
    ) -> None:
        self.folder = Path(folder or os.environ.get("CHECKPOINT_DIR", "checkpoint"))
        self.path = self.folder / name
        self.every_seconds = every_seconds
        self.last_save = time.monotonic()

    def load(self, map_location=None, weights_only: bool = True) -> dict | None:
        """
        The last saved state, or None if there is none.

        weights_only=True (the default) loads tensors, numbers, strings and
        containers of them, which is what model, optimizer and random states
        are. Pass False only for your own state you trust.
        """
        if not self.path.exists():
            return None
        if map_location is None:
            map_location = "cuda" if torch.cuda.is_available() else "cpu"
        return torch.load(
            self.path, map_location=map_location, weights_only=weights_only
        )

    def due(self) -> bool:
        """Whether every_seconds have passed since the last save (or the start)."""
        return time.monotonic() - self.last_save >= self.every_seconds

    def save(self, state: dict, step: int | None = None) -> None:
        """
        Writes state, replacing the last save in one step: a new file, then a
        rename. step is the training step (batches done, say) it was saved at.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + ".tmp")
        torch.save(state, partial)
        os.replace(partial, self.path)
        if step is not None:
            info = self.folder / STEP_FILE
            partial = info.with_name(STEP_FILE + ".tmp")
            partial.write_text(json.dumps({"step": int(step), "file": self.path.name}))
            os.replace(partial, info)
        self.last_save = time.monotonic()
