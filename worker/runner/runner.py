"""
Runs a user's command for a generic job type and reports the outcome.

The task input holds the command and the uploaded files:
    {"command": ["python", "train.py"], "files": ["inputs/train.py", ...]}

The runner downloads the files into a fresh work folder, runs the command
there, then packs the output folder and the command's log into output.tar.gz,
uploads it and reports the command's exit code. The command sees:
    OUTPUT_DIR   files to return to the user (work folder/output)
    HOME         the work folder, for tools that want a writable home
It does not see the replica token.

On SIGTERM (the PC's owner is back) the command gets SIGTERM too and a short
time to stop, then the copy is reported evicted and runs again from the start
on another PC. Standard library only, so any approved image can copy this file.
"""

import hashlib
import io
import json
import os
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
# the Pod gets 30 s after SIGTERM; leave time to report the eviction
STOP_WAIT_SECONDS = 20
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 100 * 1024 * 1024
EXIT_EVICTED = 143
# exit codes for problems before the command starts, like a shell's
EXIT_BAD_INPUT = 2
EXIT_NOT_FOUND = 127
HIDDEN_FROM_COMMAND = ("REPLICA_TOKEN", "REPLICA_ID", "COORDINATOR_URL", "TASK_INDEX")
# the runner's own folder in the work folder; the web app refuses it as a file name
OUTPUT_NAME = "output"


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


def _send(req: request.Request, timeout: float) -> bytes:
    """Sends a request, trying again after network errors and 5xx replies."""
    attempt = 1
    while True:
        try:
            with request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except error.HTTPError as exc:
            if exc.code < 500 or attempt == ATTEMPTS:
                raise
        except OSError:  # URLError, timeouts and dropped connections
            if attempt == ATTEMPTS:
                raise
        time.sleep(2**attempt)
        attempt += 1


def call(config: dict[str, str], method: str, path: str, body: dict | None = None):
    """Calls /internal/replicas/{id}/{path} on the coordinator."""
    headers = {"Authorization": f"Bearer {config['token']}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    url = f"{config['coordinator_url']}/internal/replicas/{config['replica_id']}/{path}"
    reply = _send(
        request.Request(url, data=data, headers=headers, method=method),
        REQUEST_TIMEOUT_SECONDS,
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
        if name == OUTPUT_NAME:
            raise ValueError(
                f"An input file can't be called {OUTPUT_NAME!r}: "
                "that folder holds your results."
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

    def write(self, line: bytes) -> None:
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


def command_env(work: Path, output: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in HIDDEN_FROM_COMMAND}
    env.update(HOME=str(work), OUTPUT_DIR=str(output), PYTHONUNBUFFERED="1")
    return env


def run_command(
    command: list[str], work: Path, output: Path, log: Log, stopping: threading.Event
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
            env=command_env(work, output),
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


def report(config: dict[str, str], manifest: dict, archive: bytes, code: int) -> None:
    """Uploads output.tar.gz and result.json, then reports result.json's hash."""
    upload(manifest["output_archive_url"], archive, "application/gzip")
    result = json.dumps(
        {"archive_sha256": hashlib.sha256(archive).hexdigest(), "exit_code": code},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    upload(manifest["output_upload_url"], result, "application/json")
    call(
        config,
        "POST",
        "result",
        {"result_hash": hashlib.sha256(result).hexdigest(), "exit_code": code},
    )


def main() -> int:
    config = read_environment()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())

    manifest = call(config, "GET", "manifest")
    log = Log()
    with tempfile.TemporaryDirectory(prefix="job-") as folder:
        work = Path(folder)
        output = work / OUTPUT_NAME
        output.mkdir()
        try:
            command, names = check_input(manifest["input"])
            download_inputs(manifest, names, work)
        except ValueError as exc:
            log.note(str(exc))
            code = EXIT_BAD_INPUT
        except OSError as exc:  # a file that couldn't be fetched or written
            log.note(f"Couldn't get the input files ready: {exc}")
            code = EXIT_BAD_INPUT
        else:
            # a stop during the download means the command never starts
            code = (
                EXIT_EVICTED
                if stopping.is_set()
                else run_command(command, work, output, log, stopping)
            )

        if stopping.is_set():
            # the copy runs again elsewhere; a refused report changes nothing
            try:
                call(config, "POST", "evicted")
            except OSError as exc:
                print(f"Couldn't report the eviction: {exc}", file=sys.stderr)
            return EXIT_EVICTED

        log.note(f"Command exited with code {code}.")
        report(config, manifest, pack(output, log), code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
