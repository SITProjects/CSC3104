"""Pure job logic for the web app: checking uploads, splitting them into tasks,
shaping the coordinator's job into what the progress page shows, and building the
final text. Nothing here talks to the network, so it is easy to test.
"""

import json
import math
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pymupdf

from services.web.settings import settings

TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
# Replica states that mean "this copy was knocked off its PC"
LOST_STATES = frozenset({"EVICTED", "LOST"})
# A replica in one of these still counts towards its task's progress
ACTIVE_STATES = frozenset({"SCHEDULED", "RUNNING", "DONE"})


class UploadError(ValueError):
    """The upload can't become a job. The message is safe to show to the user."""


@dataclass(frozen=True)
class JobType:
    """A job type users can pick on the New job page."""

    name: str  # the coordinator's job_types.name
    label: str
    description: str
    needs_pdf: bool
    # the coordinator's default_cpu and default_mem for this type
    default_cpu: int
    default_mem: int
    # whether users may ask for other CPU and memory; if not, the defaults apply
    custom_resources: bool
    # generic jobs: the user gives a command and files, and gets output.tar.gz
    runs_command: bool = False
    # mirrors job_types.verify: whether copies are compared. Without it a task
    # runs once, whatever REPLICAS_PER_TASK says.
    voting: bool = True
    # the command placeholder for generic types
    example: str = ""


# Mirrors the job types seeded in shared/migrations (002, 003, 010, 012, 014 and 015).
# The coordinator has no endpoint to list them, and it refuses an unknown job_type.
JOB_TYPES = {
    t.name: t
    for t in (
        JobType(
            "ocr",
            "OCR",
            "Reads the text from every page of a scanned PDF.",
            needs_pdf=True,
            # Tesseract runs one thread per copy (OMP_THREAD_LIMIT=1), and a
            # second core made a 10-page task only 6% faster, so more copies
            # beat bigger ones. A copy peaks under 100 MiB on dense 300 DPI
            # pages; 256 MiB leaves headroom, and lets a t3.micro fit two.
            default_cpu=1,
            default_mem=256,
            custom_resources=False,
        ),
        JobType(
            "test",
            "Test workload",
            "Runs a short command that prints one line. Use it to check the cluster.",
            needs_pdf=False,
            default_cpu=1,
            default_mem=64,
            # changing the request is how to check where the scheduler places a job
            custom_resources=True,
        ),
        JobType(
            "python",
            "Python script",
            "Runs your own command, such as python train.py, once on an idle PC "
            "with numpy, pandas and scikit-learn. Save files to $OUTPUT_DIR to "
            "download them.",
            needs_pdf=False,
            default_cpu=1,
            default_mem=1024,
            custom_resources=True,
            runs_command=True,
            voting=False,
            example="python train.py --epochs 5",
        ),
        JobType(
            "pytorch",
            "Python with PyTorch (CPU)",
            "Runs your own Python script with PyTorch, numpy, pandas and scikit-learn "
            "once on an idle PC. Save files in the output folder to download them.",
            needs_pdf=False,
            default_cpu=2,
            default_mem=2048,
            custom_resources=True,
            runs_command=True,
            voting=False,
            example="python train_digits.py --epochs 30",
        ),
        JobType(
            "pytorch-gpu",
            "Python with PyTorch (GPU)",
            "Trains your own PyTorch script on the GPU of an idle PC, NVIDIA or AMD: "
            'use the device "cuda" in PyTorch for both. Save '
            "progress in the checkpoint folder every few minutes so it can carry on "
            "if the PC's owner comes back, and save results in the output folder. "
            "Use public or non-sensitive data only: the PC's administrator can see it.",
            needs_pdf=False,
            default_cpu=2,
            default_mem=4096,
            custom_resources=True,
            runs_command=True,
            voting=False,
            example="python train_gpu.py --epochs 40 --save-seconds 300",
        ),
    )
}

# Types whose trained model the platform scores (job_types.check_image,
# migration 017): their form takes an evaluation file and a target accuracy
SCORED_TYPES = frozenset({"pytorch-gpu"})
MAX_EVAL_SAMPLES = 10_000  # worker/model-check/evaluate.py MAX_SAMPLES


def parse_min_score(value: str | None, has_eval_file: bool) -> float | None:
    """
    The target accuracy from the form, a percentage, as a fraction (0 to 1).

    raises:
        UploadError: If it isn't a number from 0 to 100, or there is no
            evaluation file to measure it on.
    """
    if value is None or not value.strip():
        return None
    try:
        percent = float(value)
    except ValueError:
        raise UploadError("The target accuracy must be a number.") from None
    if not 0 <= percent <= 100:
        raise UploadError("The target accuracy must be from 0 to 100%.")
    if not has_eval_file:
        raise UploadError("Add an evaluation file to measure the target accuracy on.")
    return round(percent / 100, 6)


# The coordinator's limits on one copy of a task (JobSpec in its models.py)
MIN_CPU, MAX_CPU = 1, 16  # cores
MIN_MEM, MAX_MEM = 64, 4096  # MiB


def parse_job_type(value: str | None) -> JobType:
    """
    Finds the job type the user picked.

    raises:
        UploadError: If nothing, or an unknown type, was picked.
    """
    if not value or value not in JOB_TYPES:
        raise UploadError("Choose a job type.")
    return JOB_TYPES[value]


def _whole_number(
    value: str | None, default: int, what: str, low: int, high: int
) -> int:
    text = (value or "").strip()
    if not text:
        return default
    try:
        number = int(text)
    except ValueError:
        raise UploadError(f"The {what} must be a whole number.") from None
    if not low <= number <= high:
        raise UploadError(f"The {what} must be between {low} and {high}.")
    return number


def parse_resources(
    job_type: JobType, cpu: str | None, mem: str | None
) -> tuple[int, int]:
    """
    Picks the CPU and memory for a job. Types that don't allow custom resources
    always get their defaults, and so does a blank field.

    returns:
        tuple: (cores, MiB).
    raises:
        UploadError: If either is given but not a whole number or out of range.
    """
    if not job_type.custom_resources:
        return job_type.default_cpu, job_type.default_mem
    return (
        _whole_number(
            cpu, job_type.default_cpu, "number of CPU cores", MIN_CPU, MAX_CPU
        ),
        _whole_number(mem, job_type.default_mem, "memory in MiB", MIN_MEM, MAX_MEM),
    )


def parse_command(text: str | None) -> list[str]:
    """
    Splits the command the user typed, the way a shell would, without running
    a shell: "python train.py --epochs 5" -> ["python", "train.py", "--epochs", "5"].

    raises:
        UploadError: If it is empty, too long or has an unclosed quote.
    """
    text = (text or "").strip()
    if not text:
        raise UploadError("Enter the command to run, for example python main.py.")
    if len(text) > settings.max_command_chars:
        raise UploadError(
            f"The command is limited to {settings.max_command_chars} characters."
        )
    try:
        return shlex.split(text)
    except ValueError:
        raise UploadError("The command has a quote that isn't closed.") from None


# worker/runner makes these folders next to the files: what the job returns,
# and the state it keeps if it moves to another PC
RUNNER_FOLDERS = ("output", "checkpoint")


def input_names(filenames: list[str]) -> list[str]:
    """
    Safe, distinct names for a generic job's uploaded files, in upload order.

    raises:
        UploadError: If there are more than settings.max_input_files, two
            files end up with the same name, or one takes the runner's
            output or checkpoint folder name.
    """
    if len(filenames) > settings.max_input_files:
        raise UploadError(f"Upload at most {settings.max_input_files} files.")
    names = [clean_filename(name, "input") for name in filenames]
    if len(set(names)) != len(names):
        raise UploadError("Two of the files have the same name.")
    for name in RUNNER_FOLDERS:
        if name in names:
            raise UploadError(
                f"A file can't be called {name}: the runner keeps that folder "
                "for your results and checkpoints. Rename it and upload again."
            )
    return names


def output_replica(job: dict[str, Any]) -> int | None:
    """
    The copy whose output.tar.gz a generic job's owner downloads: the accepted
    one, or for a failed job the last copy that finished, so its log can be
    read. None while no copy has finished.
    """
    if not job["tasks"]:
        return None
    task = job["tasks"][0]
    finished = [
        r
        for r in task["replicas"]
        if r["status"] == "DONE" and r.get("check_of") is None
    ]
    accepted = [r for r in finished if r["result_hash"] == task["final_hash"]]
    chosen = accepted or finished
    return max(r["id"] for r in chosen) if chosen else None


def score_view(task: dict[str, Any], min_score: float | None) -> dict | None:
    """
    How the platform's check of the task's model went, or None for a task that
    isn't scored. state is "awaiting" (training done, no check result yet),
    "scored" or "not_scored".
    """
    checks = [r for r in task["replicas"] if r.get("check_of") is not None]
    if not checks:
        return None
    done = [r for r in checks if r["status"] == "DONE" and r.get("check_status")]
    if not done:
        if task["status"] == "SCORING":
            return {"state": "awaiting", "checks": len(checks)}
        # every check failed and the task was accepted without a score
        return {"state": "not_scored", "reason": "The checker failed."}
    check = max(done, key=lambda r: r["id"])
    if check["check_status"] != "scored":
        return {"state": "not_scored", "reason": check.get("check_reason") or ""}
    score = check["check_score"]
    return {
        "state": "scored",
        "score": score,
        "samples": check.get("check_samples"),
        "model_sha256": check.get("check_model_sha256"),
        "eval_sha256": check.get("check_eval_sha256"),
        "target": min_score,
        "met": None if min_score is None else score >= min_score,
    }


def find_replica(job: dict[str, Any], replica_id: int) -> dict[str, Any]:
    """A copy of one of the job's tasks by id."""
    return next(
        replica
        for task in job["tasks"]
        for replica in task["replicas"]
        if replica["id"] == replica_id
    )


def replica_attempts(job: dict[str, Any]) -> dict[int, int]:
    """Each copy's current attempt by replica id; its files are kept per attempt."""
    return {
        replica["id"]: replica.get("attempt", 1)
        for task in job["tasks"]
        for replica in task["replicas"]
    }


class ResultUnavailable(RuntimeError):
    """The job has no agreed output to put together."""


def check_pdf(data: bytes) -> int:
    """
    Checks that the bytes are a usable PDF.

    args:
        data: The whole uploaded file.
    returns:
        int: The number of pages.
    raises:
        UploadError: If it is not a PDF, is damaged or password protected, or
            has too many pages.
    """
    if not data.startswith(b"%PDF-"):
        raise UploadError("That file is not a PDF.")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise UploadError("Password-protected PDFs can't be processed.")
            pages = doc.page_count
    except UploadError:
        raise
    except Exception as exc:  # pymupdf raises several types for damaged files
        raise UploadError("That PDF is damaged or can't be read.") from exc
    if pages < 1:
        raise UploadError("That PDF has no pages.")
    if pages > settings.max_pages:
        raise UploadError(f"PDFs are limited to {settings.max_pages} pages.")
    return pages


def pages_per_task_for(page_count: int, free_slots: int | None, replicas: int) -> int:
    """
    How many pages each task of a PDF gets, so the whole PDF can run at once.

    Each task runs `replicas` copies on one core each, so free_slots // replicas
    tasks fit side by side. Splitting into that many tasks finishes soonest. A
    copy has a few seconds of fixed overhead, so a task never drops below
    settings.min_pages_per_task pages, and never exceeds settings.pages_per_task.

    args:
        page_count: Pages in the PDF.
        free_slots: Free one-core copy slots (GET /capacity), or None if unknown.
        replicas: Copies per task.
    returns:
        int: Pages per task.
    """
    largest = settings.pages_per_task
    if not free_slots:
        return largest
    side_by_side = max(1, free_slots // replicas)
    wanted = math.ceil(page_count / side_by_side)
    return max(settings.min_pages_per_task, min(largest, wanted))


def plan_tasks(
    page_count: int, per_task: int | None = None, batch: int | None = None
) -> list[dict[str, Any]]:
    """
    Splits pages 1..page_count into tasks of up to per_task pages, never across a
    render batch, so a batch's tasks can start as soon as that batch is rendered.

    args:
        page_count: Pages in the PDF.
        per_task: Pages per task (default settings.pages_per_task).
        batch: Pages per Lambda renderer (default settings.render_batch_pages).
    returns:
        list: One {"index", "first", "last", "input"} per task, in page order.
            "input" holds the page image names, relative to jobs/{id}/.
    """
    per_task = per_task or settings.pages_per_task
    batch = batch or settings.render_batch_pages
    tasks = []
    for batch_start in range(1, page_count + 1, batch):
        batch_end = min(batch_start + batch - 1, page_count)
        for first in range(batch_start, batch_end + 1, per_task):
            last = min(first + per_task - 1, batch_end)
            files = [f"pages/{page:04d}.png" for page in range(first, last + 1)]
            tasks.append(
                {
                    "index": len(tasks),
                    "first": first,
                    "last": last,
                    "input": {"files": files, "lang": "eng"},
                }
            )
    return tasks


def task_payload(task: dict[str, Any]) -> dict[str, Any]:
    """A planned task as the coordinator takes it: its index and input."""
    return {"index": task["index"], "input": task["input"]}


def build_tasks(page_count: int) -> list[dict[str, Any]]:
    """
    All of a PDF's tasks at the largest size, in the form POST /jobs takes.

    returns:
        list: One {"input": {"files": [...], "lang": "eng"}} per task.
    """
    return [{"input": task["input"]} for task in plan_tasks(page_count)]


def job_spec(
    job_id: str,
    job_type: str,
    cpu: int,
    mem: int,
    page_count: int = 0,
    tasks: list[dict[str, Any]] | None = None,
    task_count: int | None = None,
    params: dict[str, Any] | None = None,
    task_input: dict[str, Any] | None = None,
    min_score: float | None = None,
) -> dict[str, Any]:
    """
    The body for the coordinator's POST /jobs.

    args:
        job_type: "ocr" splits page_count pages into tasks; any other type runs
            one task, with task_input as its input (a generic job's command and
            files) or none.
        cpu: Cores for each copy of a task.
        mem: MiB for each copy of a task.
        tasks: The tasks to send now (OCR), instead of all of them.
        task_count: How many tasks the job will have in all, when the rest follow
            later with POST /jobs/{id}/tasks.
        params: Extra job parameters to record, such as when it was uploaded.
    """
    if job_type == "ocr":
        job_params = {"page_count": page_count}
        if tasks is None:
            tasks = build_tasks(page_count)
    else:
        job_params, tasks = {}, [{"input": task_input or {}}]
    kind = JOB_TYPES.get(job_type)
    spec = {
        "job_id": job_id,
        "job_type": job_type,
        "params": {**job_params, **(params or {})},
        "tasks": tasks,
        "replicas_per_task": settings.replicas_per_task
        if kind is None or kind.voting
        else 1,
        "cpu_request": cpu,
        "mem_request": mem,
    }
    if task_count is not None:
        spec["task_count"] = task_count
    if min_score is not None:
        spec["min_score"] = min_score
    return spec


def _restarts(replica: dict[str, Any]) -> int:
    # attempt goes up each time the coordinator relaunches an evicted or lost
    # replica; one that is waiting for its relaunch hasn't gone up yet
    waiting = 1 if replica["status"] in LOST_STATES else 0
    return max(replica["attempt"] - 1, 0) + waiting


def _task_progress(task: dict[str, Any]) -> int:
    if task["status"] == "VERIFIED":
        return 100
    live = [r["progress"] for r in task["replicas"] if r["status"] in ACTIVE_STATES]
    return round(sum(live) / len(live)) if live else 0


def _votes(task: dict[str, Any]) -> dict[str, Any] | None:
    """How many finished copies agreed, once the task has been put to a vote."""
    if task["status"] not in ("VOTING", "VERIFIED", "DISPUTED"):
        return None
    hashes = [r["result_hash"] for r in task["replicas"] if r["result_hash"]]
    agreeing = (
        sum(1 for h in hashes if h == task["final_hash"]) if task["final_hash"] else 0
    )
    return {"agree": agreeing, "voted": len(hashes), "copies": len(task["replicas"])}


def progress_view(job: dict[str, Any]) -> dict[str, Any]:
    """
    Shapes the coordinator's JobView for the progress page and its stream.

    args:
        job: The JSON from GET /jobs/{id}.
    returns:
        dict: Overall status, progress and counts, plus one entry per task with
            the PCs that ran it, its restarts and its vote. Safe to send to the browser.
    """
    tasks = []
    for task in sorted(job["tasks"], key=lambda t: t["task_index"]):
        replicas = [
            {
                "id": r["id"],
                "node": r["node_name"],
                "status": r["status"],
                "attempt": r["attempt"],
                "progress": r["progress"],
                "restarts": _restarts(r),
                # the last checkpoint this copy saved, for long jobs
                "saved_at": r.get("checkpoint_at"),
                "saved_step": r.get("checkpoint_step"),
                # the PC is still downloading the job's image, or couldn't
                "pulling_since": r.get("pulling_since"),
                "pull_error": r.get("pull_error"),
                # a check copy scores a trained model on the platform's checker
                "check": r.get("check_of") is not None,
            }
            for r in sorted(task["replicas"], key=lambda r: r["id"])
        ]
        tasks.append(
            {
                "index": task["task_index"],
                "status": task["status"],
                "progress": _task_progress(task),
                "replicas": replicas,
                "restarts": sum(r["restarts"] for r in replicas),
                "votes": _votes(task),
                "score": score_view(task, job.get("min_score")),
            }
        )
    done = sum(1 for t in tasks if t["status"] == "VERIFIED")
    # task_count also counts tasks not created yet (pages still rendering), so
    # progress only reaches 100% once every task exists and is done
    total = job.get("task_count") or len(tasks)
    if job["status"] == "COMPLETED":
        progress = 100
    elif "task_count" in job:
        progress = round(sum(t["progress"] for t in tasks) / total)
    else:
        progress = int(job["progress"])
    return {
        "stage": "active",
        "id": str(job["id"]),
        "job_type": job["job_type"],
        "status": job["status"],
        "files_deleted_at": job.get("files_deleted_at"),
        "terminal": job["status"] in TERMINAL_STATUSES,
        "progress": progress,
        "tasks_total": total,
        "tasks_verified": done,
        "tasks_disputed": sum(1 for t in tasks if t["status"] == "DISPUTED"),
        "restarts": sum(t["restarts"] for t in tasks),
        "tasks": tasks,
    }


def preparing_view(pending: dict[str, Any], job_id: str) -> dict[str, Any]:
    """
    What the progress page shows before the coordinator knows the job, while the
    pages are still being rendered, or after that step failed.
    """
    failed = pending.get("state") == "failed"
    return {
        "stage": "failed" if failed else "preparing",
        "id": job_id,
        "job_type": "ocr",  # only PDF uploads are prepared
        "status": "FAILED" if failed else "PREPARING",
        "terminal": failed,
        "error": pending.get("error") if failed else None,
        "progress": 0,
        "tasks": [],
        "tasks_total": 0,
        "tasks_verified": 0,
        "tasks_disputed": 0,
        "restarts": 0,
    }


def _read_pages(raw: bytes | None) -> dict[str, str] | None:
    """The {file name: text} map in a replica's result.json, or None if unusable."""
    if raw is None:
        return None
    try:
        pages = json.loads(raw)["pages"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(pages, dict) or not all(
        isinstance(text, str) for text in pages.values()
    ):
        return None
    return pages


def clean_filename(name: str | None, fallback: str = "upload.pdf") -> str:
    """
    Makes an uploaded file name safe to store and show: no folders, no control
    characters, at most settings.max_filename_chars long.
    """
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if name in (".", ".."):
        name = ""
    return name[: settings.max_filename_chars] or fallback


def assemble_text(job: dict[str, Any], fetch: Callable[[int], bytes | None]) -> str:
    """
    Joins the agreed OCR text of every task, in page order.

    For each task it reads the result.json of a replica whose hash won the vote.

    args:
        job: The JSON from GET /jobs/{id}.
        fetch: Returns the bytes of a replica's result.json by replica id, or None.
    raises:
        ResultUnavailable: If the job isn't COMPLETED or a winning result is missing.
    """
    if job["status"] != "COMPLETED":
        raise ResultUnavailable("The job hasn't completed.")
    parts: list[str] = []
    for task in sorted(job["tasks"], key=lambda t: t["task_index"]):
        winners = [
            r
            for r in task["replicas"]
            if r["status"] == "DONE"
            and task["final_hash"]
            and r["result_hash"] == task["final_hash"]
        ]
        pages = None
        for replica in winners:
            # a copy whose file is missing or unreadable doesn't sink the job if
            # another copy that agreed with the vote is fine
            pages = _read_pages(fetch(replica["id"]))
            if pages is not None:
                break
        if pages is None:
            raise ResultUnavailable(
                f"The result for task {task['task_index']} is missing or unreadable."
            )
        parts.extend(pages[name] for name in sorted(pages))
    return "\n\n".join(parts) + "\n"
