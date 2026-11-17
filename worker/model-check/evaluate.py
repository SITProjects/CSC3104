"""
Scores a trained model on the owner's evaluation file, for the platform.

Runs as a check copy on a node the platform controls (idlecloud/checker=true),
never on the PC that trained the model. The manifest's "check" names what to
score (services/coordinator/models.py, CheckSource):

1. result.json of the training attempt, refused unless its SHA-256 is the hash
   the training copy reported (stored in the database);
2. output.tar.gz, refused unless its SHA-256 is the archive_sha256 in that
   result.json, so the model scored is exactly the one the owner downloads;
3. output/model.onnx from it, read without unpacking anything else;
4. the evaluation file, eval.npz with x (float samples) and y (integer labels).

The contract: one float input, logits (samples x classes) out, top-1 accuracy
on at most MAX_SAMPLES samples. A model or file that breaks it is reported as
"not_scored" with the reason. Anything else, such as a network error, ends the
copy with exit code 1, and the verifier starts another check: a checker
failure is never the owner's.
"""

import hashlib
import io
import json
import os
import sys
import tarfile
import time
from urllib import error, request

import numpy as np
import onnxruntime as ort

EVALUATOR = "model-check/1"
MAX_RESULT_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_MODEL_BYTES = 100 * 1024 * 1024
MAX_EVAL_BYTES = 100 * 1024 * 1024
MAX_SAMPLES = 10_000
BATCH = 256
MODEL_NAMES = ("output/model.onnx", "./output/model.onnx")
TIMEOUT_SECONDS = 60
ATTEMPTS = 3


class NotScored(Exception):
    """The model or the evaluation file broke the contract: a result, not a crash."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.reason = f"{code}: {detail}"[:500]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _send(req: request.Request) -> bytes:
    """Sends a request, trying again after network errors and 5xx replies."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except error.HTTPError as exc:
            if exc.code < 500 or attempt == ATTEMPTS:
                raise
        except OSError:
            if attempt == ATTEMPTS:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def call(config: dict[str, str], method: str, path: str, body: dict | None = None):
    """Calls /internal/replicas/{id}/{path} on the coordinator."""
    headers = {"Authorization": f"Bearer {config['token']}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    url = f"{config['coordinator_url']}/internal/replicas/{config['replica_id']}/{path}"
    reply = _send(request.Request(url, data=data, headers=headers, method=method))
    return json.loads(reply or b"null")


def fetch(url: str, limit: int, what: str) -> bytes:
    """Downloads at most limit bytes. A missing or larger file isn't scored."""
    try:
        with request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            data = response.read(limit + 1)
    except error.HTTPError as exc:
        if exc.code == 404:
            raise NotScored("missing", f"the {what} is gone from storage") from exc
        raise
    if len(data) > limit:
        raise NotScored("too_large", f"the {what} is over {limit // 2**20} MB")
    return data


def trained_archive(check: dict) -> bytes:
    """The training attempt's output.tar.gz, checked against both digests."""
    result = fetch(check["result_url"], MAX_RESULT_BYTES, "result.json")
    if sha256(result) != check["result_sha256"]:
        raise NotScored(
            "result_mismatch", "result.json is not the one the training reported"
        )
    try:
        expected = json.loads(result)["archive_sha256"]
    except (ValueError, KeyError, TypeError) as exc:
        raise NotScored("result_invalid", "result.json has no archive_sha256") from exc
    archive = fetch(check["archive_url"], MAX_ARCHIVE_BYTES, "output archive")
    if sha256(archive) != expected:
        raise NotScored(
            "archive_mismatch", "output.tar.gz changed after the training reported it"
        )
    return archive


def model_from(archive: bytes) -> bytes:
    """
    output/model.onnx from the archive, read in memory. Nothing is unpacked to
    disk, so paths, links and special files in the archive can do no harm.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                if member.name not in MODEL_NAMES:
                    continue
                if not member.isfile():
                    raise NotScored("no_model", "output/model.onnx is not a file")
                if member.size > MAX_MODEL_BYTES:
                    raise NotScored(
                        "too_large",
                        f"model.onnx is over {MAX_MODEL_BYTES // 2**20} MB",
                    )
                file = tar.extractfile(member)
                if file is None:
                    break
                return file.read()
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise NotScored("archive_invalid", f"output.tar.gz can't be read ({exc})")
    raise NotScored(
        "no_model", "the job returned no output/model.onnx (save it to $OUTPUT_DIR)"
    )


def load_eval(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """x and y from eval.npz, with no pickled objects allowed."""
    if not data.startswith(b"PK"):
        raise NotScored("eval_invalid", "the evaluation file is not an .npz file")
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as npz:
            if "x" not in npz or "y" not in npz:
                raise NotScored("eval_invalid", "eval.npz needs arrays named x and y")
            x, y = npz["x"], npz["y"]
    except NotScored:
        raise
    except (ValueError, OSError, EOFError) as exc:
        raise NotScored("eval_invalid", f"eval.npz can't be read ({exc})") from exc
    if x.dtype.kind not in "fiu" or x.ndim < 1:
        raise NotScored("eval_invalid", f"x must hold numbers, not {x.dtype}")
    if y.dtype.kind not in "iu" or y.ndim != 1:
        raise NotScored("eval_invalid", "y must be a 1-D array of integer labels")
    if len(x) != len(y) or len(y) == 0:
        raise NotScored(
            "eval_invalid", f"x has {len(x)} samples and y {len(y)}; they must match"
        )
    if len(y) > MAX_SAMPLES:
        raise NotScored(
            "eval_invalid", f"{len(y)} samples, at most {MAX_SAMPLES} are scored"
        )
    return x.astype(np.float32), y.astype(np.int64)


def accuracy(model: bytes, x: np.ndarray, y: np.ndarray) -> float:
    """Top-1 accuracy of the model's logits on x, in batches, on one CPU core."""
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    try:
        session = ort.InferenceSession(
            model, sess_options=options, providers=["CPUExecutionProvider"]
        )
    except Exception as exc:  # onnxruntime has its own error types
        if "external data" in str(exc).lower():
            raise NotScored(
                "model_invalid",
                "model.onnx keeps its weights in another file; export it with "
                "torch.onnx.export(..., external_data=False)",
            ) from exc
        raise NotScored("model_invalid", f"model.onnx can't be loaded ({exc})")
    inputs = session.get_inputs()
    if len(inputs) != 1:
        raise NotScored(
            "contract", f"the model has {len(inputs)} inputs; it must have one"
        )
    if inputs[0].type != "tensor(float)":
        raise NotScored(
            "contract", f"the model's input is {inputs[0].type}, not tensor(float)"
        )
    # a model exported with a fixed batch of 1 is run one sample at a time
    batch = 1 if inputs[0].shape and inputs[0].shape[0] == 1 else BATCH
    correct = 0
    for start in range(0, len(x), batch):
        try:
            logits = session.run(None, {inputs[0].name: x[start : start + batch]})[0]
        except Exception as exc:  # noqa: BLE001 - as above
            raise NotScored("model_error", f"the model failed on the samples ({exc})")
        logits = np.asarray(logits)
        if logits.ndim != 2 or logits.shape[0] != len(x[start : start + batch]):
            raise NotScored(
                "contract",
                f"the model returned shape {logits.shape}; it must be "
                "(samples, classes) logits",
            )
        correct += int((logits.argmax(axis=1) == y[start : start + batch]).sum())
    return correct / len(x)


def score(check: dict) -> dict:
    """The outcome to report: scored with the accuracy, or not scored and why."""
    model = model_from(trained_archive(check))
    eval_data = fetch(check["eval_url"], MAX_EVAL_BYTES, "evaluation file")
    outcome = {
        "status": "not_scored",
        "model_sha256": sha256(model),
        "eval_sha256": sha256(eval_data),
        "evaluator": EVALUATOR,
    }
    try:
        x, y = load_eval(eval_data)
        value = accuracy(model, x, y)
    except NotScored as exc:
        return {**outcome, "reason": exc.reason}
    return {**outcome, "status": "scored", "score": round(value, 6), "samples": len(y)}


def main() -> int:
    config = {
        "replica_id": os.environ.get("REPLICA_ID", ""),
        "token": os.environ.get("REPLICA_TOKEN", ""),
        "coordinator_url": os.environ.get("COORDINATOR_URL", "").rstrip("/"),
    }
    if not all(config.values()):
        sys.exit("Missing REPLICA_ID, REPLICA_TOKEN or COORDINATOR_URL")
    manifest = call(config, "GET", "manifest")
    check = manifest.get("check")
    if not check:
        sys.exit("The manifest names nothing to check")
    try:
        outcome = score(check)
    except NotScored as exc:
        outcome = {"status": "not_scored", "evaluator": EVALUATOR, "reason": exc.reason}
    print(json.dumps(outcome), flush=True)
    call(config, "POST", "check", outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
