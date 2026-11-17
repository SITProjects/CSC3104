"""
Runs a user's command for a generic job type and reports the outcome.

The task input holds the command and the uploaded files:
    {"command": ["python", "train.py"], "files": ["inputs/train.py", ...]}

The runner downloads the files into a fresh work folder, runs the command
there, then packs the output folder and the command's log into output.tar.gz,
uploads it and reports the command's exit code. The command sees:
    OUTPUT_DIR      files to return to the user (work folder/output)
    CHECKPOINT_DIR  state to keep if the copy moves (work folder/checkpoint)
    HOME            the work folder, for tools that want a writable home
It does not see the replica token.

The checkpoint folder is uploaded as checkpoint.tar.gz every
CHECKPOINT_INTERVAL_SECONDS when it has changed, and once more when the copy
is stopped, up to the job type's size cap (checkpoint_max_bytes in the
manifest). Each upload is reported to the coordinator with its SHA-256, size
and, when the program wrote one to checkpoint/idlecloud.json, its training
step. A rerun of the copy on another PC starts with the last folder that was
uploaded, so a program that saves its progress there can carry on.

Presigned URLs can stop working before a long job ends (the hub's AWS
credentials rotate). An upload refused with 403 fetches the manifest again
and is tried once more with the fresh URL.

On SIGTERM (the PC's owner is back) the command gets SIGTERM too and a short
time to stop, the checkpoint folder is uploaded, and the copy is reported
evicted. Standard library only, so any approved image with Python 3.12 or newer
can copy this file.
"""

import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from urllib import error, request

REQUEST_TIMEOUT_SECONDS = 20
UPLOAD_TIMEOUT_SECONDS = 120
ATTEMPTS = 3
# The Pod gets 30 s after SIGTERM: up to 15 s for the command to stop, the
# rest for the last checkpoint upload and the eviction report (best effort)
STOP_WAIT_SECONDS = 15
# Leave five seconds of the Pod's grace period for process/container teardown.
SHUTDOWN_BUDGET_SECONDS = 25
EVICTION_REPORT_TIMEOUT_SECONDS = 5
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 100 * 1024 * 1024
# used when the coordinator doesn't send checkpoint_max_bytes
MAX_CHECKPOINT_BYTES = 100 * 1024 * 1024
# the training step the program saved, written by idlecloud_ckpt.py
STEP_FILE = "idlecloud.json"
CHECKPOINT_INTERVAL_SECONDS = float(os.environ.get("CHECKPOINT_INTERVAL_SECONDS", "60"))
EXIT_EVICTED = 143
# exit codes for problems before the command starts, like a shell's
EXIT_BAD_INPUT = 2
EXIT_NOT_FOUND = 127
# a GPU job type's copy landed where CUDA doesn't work (sysexits EX_UNAVAILABLE)
EXIT_NO_CUDA = 69
CUDA_CHECK_TIMEOUT_SECONDS = 120
# Runs in the image's Python. ROCm builds of PyTorch use the same torch.cuda
# API. The runtime shows the copy every GPU on the node; an integrated one (an
# AMD APU next to a discrete card) is hidden from the command when a discrete
# one is there, through the line HIP_VISIBLE_DEVICES=... (CUDA_VISIBLE_DEVICES
# on NVIDIA), which the runner sets for the command.
CUDA_CHECK = """
import sys, torch
if not torch.cuda.is_available():
    sys.exit('torch ' + torch.__version__ + ' sees no CUDA device')
gpus = [torch.cuda.get_device_properties(i) for i in range(torch.cuda.device_count())]
use = [i for i, gpu in enumerate(gpus) if not gpu.is_integrated] or list(range(len(gpus)))
x = torch.ones(1024, device=f'cuda:{use[0]}')
platform = 'ROCm ' + str(torch.version.hip) if torch.version.hip else 'CUDA ' + str(torch.version.cuda)
gpu = gpus[use[0]]
print(platform + ' on ' + gpu.name + ', ' + str(gpu.total_memory // 2**20)
      + ' MiB, check sum ' + str(int((x * 2).sum().item())))
if len(use) < len(gpus):
    variable = 'HIP_VISIBLE_DEVICES' if torch.version.hip else 'CUDA_VISIBLE_DEVICES'
    print(variable + '=' + ','.join(map(str, use)))
"""
GPU_SELECTION = ("HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES")
HIDDEN_FROM_COMMAND = ("REPLICA_TOKEN", "REPLICA_ID", "COORDINATOR_URL", "TASK_INDEX")
# the runner's own folders in the work folder; the web app refuses these file names
OUTPUT_NAME = "output"
CHECKPOINT_NAME = "checkpoint"


def read_environment() -> dict[str, str]:
    """The variables the coordinator sets on the Pod. Exits if one is missing."""
    config = {
        "replica_id": os.environ.get("REPLICA_ID", ""),
        "token": os.environ.get("REPLICA_TOKEN", ""),
        "coordinator_url": os.environ.get("COORDINATOR_URL", "").rstrip("/"),
    }
    missing = [name for name, value in config.items() if not value]
    if missing:
        sys.exit(f"Missing environment variables: {missing}")
    return config


def _send(req: request.Request, timeout: float, attempts: int = ATTEMPTS) -> bytes:
    """Sends a request, trying again after network errors and 5xx replies."""
    attempt = 1
    while True:
        try:
            with request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except error.HTTPError as exc:
            if exc.code < 500 or attempt == attempts:
                raise
        except OSError:  # URLError, timeouts and dropped connections
            if attempt == attempts:
                raise
        time.sleep(2**attempt)
        attempt += 1


def call(
    config: dict[str, str],
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    attempts: int = ATTEMPTS,
):
    """Calls /internal/replicas/{id}/{path} on the coordinator."""
    headers = {"Authorization": f"Bearer {config['token']}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    url = f"{config['coordinator_url']}/internal/replicas/{config['replica_id']}/{path}"
    reply = _send(
        request.Request(url, data=data, headers=headers, method=method),
        timeout,
        attempts,
    )
    return json.loads(reply or b"null")


def upload(url: str, body: bytes, content_type: str) -> None:
    """PUTs bytes to a presigned URL."""
    req = request.Request(
        url, data=body, headers={"Content-Type": content_type}, method="PUT"
    )
    _send(req, UPLOAD_TIMEOUT_SECONDS)


def check_input(task_input: dict) -> tuple[list[str], dict[str, str]]:
    """
    Reads the command and maps each input file to its name in the work folder.

    raises:
        ValueError: If the command is missing or a file name isn't a plain name
            under inputs/.
    """
    command = task_input.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ValueError("The task has no command to run.")
    names = {}
    for key in task_input.get("files", []):
        name = key.removeprefix("inputs/")
        if name == key or not name or "/" in name or name in (".", ".."):
            raise ValueError(f"Unexpected input file {key!r}.")
        if name in (OUTPUT_NAME, CHECKPOINT_NAME):
            raise ValueError(
                f"An input file can't be called {name!r}: the runner keeps that "
                "folder for your results and checkpoints."
            )
        names[key] = name
    return command, names


def download_inputs(manifest: dict, names: dict[str, str], work: Path) -> None:
    for key, name in names.items():
        data = _send(
            request.Request(manifest["input_urls"][key]), UPLOAD_TIMEOUT_SECONDS
        )
        (work / name).write_bytes(data)


class Log:
    """Keeps the first MAX_LOG_BYTES of the command's output and echoes all of it."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.truncated = False
        self.lock = threading.Lock()  # the checkpoint saver writes notes too

    def write(self, line: bytes) -> None:
        with self.lock:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            if self.buffer.tell() + len(line) <= MAX_LOG_BYTES:
                self.buffer.write(line)
            else:
                self.truncated = True

    def note(self, message: str) -> None:
        self.write(f"[idlecloud] {message}\n".encode())

    def value(self) -> bytes:
        tail = b"\n[idlecloud] log truncated\n" if self.truncated else b""
        return self.buffer.getvalue() + tail


def command_env(work: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in HIDDEN_FROM_COMMAND}
    env.update(
        HOME=str(work),
        OUTPUT_DIR=str(work / OUTPUT_NAME),
        CHECKPOINT_DIR=str(work / CHECKPOINT_NAME),
        PYTHONUNBUFFERED="1",
    )
    return env


def check_cuda(log: Log) -> bool:
    """
    For GPU job types (the coordinator sets IDLECLOUD_REQUIRE_CUDA=1): checks
    PyTorch can use a CUDA device here before the command starts, and logs the
    device. A script that falls back to the CPU would otherwise run for hours
    without anyone noticing that the node's GPU setup is broken.
    """
    if os.environ.get("IDLECLOUD_REQUIRE_CUDA") != "1":
        return True
    try:
        check = subprocess.run(
            [sys.executable, "-c", CUDA_CHECK],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=CUDA_CHECK_TIMEOUT_SECONDS,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.note(f"This job type needs a GPU, but the CUDA check couldn't run: {exc}")
        return False
    if check.returncode != 0:
        detail = (check.stderr or check.stdout).strip().splitlines()[-1:] or [
            "no output"
        ]
        log.note(
            "This job type needs a GPU, but CUDA isn't available on this node "
            f"({detail[0]}). The command was not started."
        )
        return False
    for line in check.stdout.strip().splitlines():
        name, _, value = line.partition("=")
        if name in GPU_SELECTION and value:
            # the command (command_env) sees only the discrete GPUs
            os.environ[name] = value
            log.note(f"Integrated GPUs hidden from the command: {line}")
        else:
            log.note(line)
    return True


def run_command(
    command: list[str], work: Path, log: Log, stopping: threading.Event
) -> int:
    """
    Runs the command in its own process group and returns its exit code.

    When stopping is set (SIGTERM), the whole group gets SIGTERM, and SIGKILL
    if it hasn't exited after STOP_WAIT_SECONDS.
    """
    try:
        child = subprocess.Popen(
            command,
            cwd=work,
            env=command_env(work),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        log.note(f"Couldn't start {command[0]!r}: {exc.strerror}")
        return EXIT_NOT_FOUND

    def stop_when_asked() -> None:
        stopping.wait()
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(STOP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)

    threading.Thread(target=stop_when_asked, daemon=True).start()
    for line in child.stdout:
        log.write(line)
    return child.wait()


def folder_size(folder: Path) -> int:
    """Bytes in the folder's regular files. Symlinks are packed as links."""
    return sum(
        p.lstat().st_size
        for p in folder.rglob("*")
        if p.is_file() and not p.is_symlink()
    )


def pack(output: Path, log: Log) -> bytes:
    """output.tar.gz: the output folder and log.txt. Too much output is dropped."""
    size = folder_size(output)
    if size > MAX_OUTPUT_BYTES:
        log.note(
            f"The output folder holds {size} bytes, more than the "
            f"{MAX_OUTPUT_BYTES} allowed, so it was not returned."
        )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        if size <= MAX_OUTPUT_BYTES:
            archive.add(output, arcname="output")
        text = log.value()
        info = tarfile.TarInfo("log.txt")
        info.size = len(text)
        info.mtime = int(time.time())
        archive.addfile(info, io.BytesIO(text))
    return buffer.getvalue()


class Manifest:
    """
    The copy's manifest, fetched again when S3 refuses one of its URLs.

    Presigned URLs are signed with the hub's AWS credentials, which can
    expire before a long job ends. A fresh manifest has fresh URLs.
    """

    def __init__(self, config: dict[str, str]) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.data = call(config, "GET", "manifest")

    def __getitem__(self, key: str):
        return self.data[key]

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def put(self, key: str, body: bytes, content_type: str) -> None:
        """Uploads to the manifest's URL under key, refreshing it once on 403."""
        url = self.get(key)
        try:
            upload(url, body, content_type)
        except error.HTTPError as exc:
            if exc.code != 403:
                raise
            with self.lock:
                if self.get(key) == url:  # another thread may have refreshed it
                    self.data = call(self.config, "GET", "manifest")
            upload(self.get(key), body, content_type)


def read_step(folder: Path) -> int | None:
    """The training step in checkpoint/idlecloud.json, if the program wrote one."""
    try:
        step = json.loads((folder / STEP_FILE).read_text()).get("step")
    except (OSError, ValueError, AttributeError):
        return None
    return step if isinstance(step, int) and step >= 0 else None


class Checkpoints:
    """
    Keeps a copy's checkpoint folder in an attempt-specific S3 archive.

    save() uploads the folder only when it changed since the last upload, so
    an idle folder costs nothing, then reports the upload to the coordinator.
    A failed upload is logged and tried again on the next save; the last good
    upload stays in S3, because a PUT replaces an object whole or not at all.
    """

    def __init__(
        self,
        folder: Path,
        manifest: Manifest,
        log: Log,
        config: dict[str, str] | None = None,
    ) -> None:
        self.folder = folder
        self.manifest = manifest
        self.get_urls = manifest.get("checkpoint_archive_get_urls", [])
        # a coordinator from before checkpoints sends no URL: then nothing is kept
        self.enabled = bool(manifest.get("checkpoint_archive_put_url"))
        self.max_bytes = manifest.get("checkpoint_max_bytes") or MAX_CHECKPOINT_BYTES
        self.config = config
        self.log = log
        self.saved = self.state()  # nothing to upload until the folder changes
        self.lock = threading.Lock()
        self.report_failed = False  # note a failing /checkpoint report only once

    def state(self) -> list[tuple[str, int, int, int, int]]:
        """Paths and metadata, including atomic replacements of equal-size files."""
        found = []
        for path in self.folder.rglob("*"):
            try:
                info = path.lstat()
            except FileNotFoundError:  # the program removed it meanwhile
                continue
            found.append(
                (
                    str(path.relative_to(self.folder)),
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ino,
                    info.st_ctime_ns,
                )
            )
        return sorted(found)

    def restore(self) -> None:
        """Tries earlier attempts newest first, including when a retry saved nothing."""
        for url in self.get_urls:
            try:
                data = _send(request.Request(url), UPLOAD_TIMEOUT_SECONDS)
            except OSError as exc:  # includes HTTPError and network failures
                if isinstance(exc, error.HTTPError) and exc.code == 404:
                    continue
                self.log.note(
                    f"Couldn't fetch a saved checkpoint ({exc}), trying an older one."
                )
                continue
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                    # Reject extraction outside the folder and unsafe special files.
                    archive.extractall(self.folder, filter="data")
            except (tarfile.TarError, OSError, EOFError) as exc:
                self.log.note(f"The saved checkpoint couldn't be unpacked ({exc}).")
                shutil.rmtree(self.folder)
                self.folder.mkdir()
                continue
            self.saved = self.state()
            self.log.note(f"Restored the checkpoint folder ({len(data)} bytes).")
            return
        if self.get_urls:
            self.log.note("No usable checkpoint was saved earlier, starting fresh.")
        self.saved = self.state()

    def save(self, cancel: threading.Event | None = None) -> None:
        with self.lock:
            if not self.enabled or (cancel is not None and cancel.is_set()):
                return
            try:
                now = self.state()
                if now == self.saved:
                    return
                size = folder_size(self.folder)
                if size > self.max_bytes:
                    self.log.note(
                        f"The checkpoint folder holds {size} bytes, more than the "
                        f"{self.max_bytes} allowed, so it was not saved."
                    )
                    self.saved = now  # don't repeat the note until it changes
                    return
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                    for path in sorted(self.folder.iterdir()):
                        archive.add(path, arcname=path.name)
                # tarfile stats before opening paths. Even an atomic rename can
                # otherwise produce a valid archive containing truncated state.
                if self.state() != now or (cancel is not None and cancel.is_set()):
                    return
                archive = buffer.getvalue()
                self.manifest.put(
                    "checkpoint_archive_put_url", archive, "application/gzip"
                )
            except (OSError, tarfile.TarError) as exc:
                if cancel is None or not cancel.is_set():
                    self.log.note(f"Couldn't save the checkpoint folder: {exc}")
                return
            self.saved = now
            self.report(archive)

    def report(self, archive: bytes) -> None:
        """Tells the coordinator about an upload. Best effort: S3 holds it anyway."""
        if self.config is None:
            return
        try:
            call(
                self.config,
                "POST",
                "checkpoint",
                {
                    "attempt": self.manifest["attempt"],
                    "sha256": hashlib.sha256(archive).hexdigest(),
                    "bytes": len(archive),
                    "step": read_step(self.folder),
                },
                timeout=EVICTION_REPORT_TIMEOUT_SECONDS,
                attempts=1,
            )
        except OSError as exc:
            if not self.report_failed:
                self.report_failed = True
                print(f"Couldn't report the checkpoint: {exc}", file=sys.stderr)

    def save_every(self, interval: float, done: threading.Event) -> None:
        """Saves every interval seconds until done is set."""
        while not done.wait(interval):
            self.save(cancel=done)


def save_before_eviction(checkpoints: Checkpoints, deadline: float) -> None:
    """Gives a final save only the time left before the eviction report is due."""
    remaining = deadline - EVICTION_REPORT_TIMEOUT_SECONDS - time.monotonic()
    if remaining <= 0:
        return
    # A periodic upload may own the lock. Keep both lock waiting and network
    # work off the main thread so neither can consume the whole Pod grace.
    saver = threading.Thread(target=checkpoints.save, daemon=True)
    saver.start()
    saver.join(timeout=remaining)
    if saver.is_alive():
        checkpoints.log.note(
            "Checkpoint save is still in progress; using the last completed upload."
        )


def report(
    config: dict[str, str], manifest: Manifest, archive: bytes, code: int
) -> None:
    """Uploads output.tar.gz and result.json, then reports result.json's hash."""
    manifest.put("output_archive_url", archive, "application/gzip")
    result = json.dumps(
        {"archive_sha256": hashlib.sha256(archive).hexdigest(), "exit_code": code},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest.put("output_upload_url", result, "application/json")
    call(
        config,
        "POST",
        "result",
        {"result_hash": hashlib.sha256(result).hexdigest(), "exit_code": code},
    )


def main() -> int:
    config = read_environment()
    stopping = threading.Event()
    shutdown_deadline = None

    def stop(*_):
        nonlocal shutdown_deadline
        if shutdown_deadline is None:
            shutdown_deadline = time.monotonic() + SHUTDOWN_BUDGET_SECONDS
        stopping.set()

    signal.signal(signal.SIGTERM, stop)

    manifest = Manifest(config)
    log = Log()
    with tempfile.TemporaryDirectory(prefix="job-") as folder:
        work = Path(folder)
        output = work / OUTPUT_NAME
        output.mkdir()
        (work / CHECKPOINT_NAME).mkdir()
        checkpoints = Checkpoints(work / CHECKPOINT_NAME, manifest, log, config)
        try:
            command, names = check_input(manifest["input"])
            download_inputs(manifest, names, work)
            checkpoints.restore()
        except ValueError as exc:
            log.note(str(exc))
            code = EXIT_BAD_INPUT
        except OSError as exc:  # a file that couldn't be fetched or written
            log.note(f"Couldn't get the input files ready: {exc}")
            code = EXIT_BAD_INPUT
        else:
            # a stop during the download means the command never starts
            if stopping.is_set():
                code = EXIT_EVICTED
            elif not check_cuda(log):
                code = EXIT_NO_CUDA
            else:
                done = threading.Event()
                saver = threading.Thread(
                    target=checkpoints.save_every,
                    args=(CHECKPOINT_INTERVAL_SECONDS, done),
                    daemon=True,
                )
                saver.start()
                try:
                    code = run_command(command, work, log, stopping)
                finally:
                    done.set()

        if stopping.is_set():
            # keep the latest progress, then let the copy run again elsewhere;
            # a refused report changes nothing
            deadline = shutdown_deadline or time.monotonic() + SHUTDOWN_BUDGET_SECONDS
            save_before_eviction(checkpoints, deadline)
            try:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    call(
                        config,
                        "POST",
                        "evicted",
                        timeout=min(EVICTION_REPORT_TIMEOUT_SECONDS, remaining),
                        attempts=1,
                    )
            except OSError as exc:
                print(f"Couldn't report the eviction: {exc}", file=sys.stderr)
            return EXIT_EVICTED

        log.note(f"Command exited with code {code}.")
        report(config, manifest, pack(output, log), code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
