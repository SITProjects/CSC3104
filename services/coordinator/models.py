import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

JobStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]
TaskStatus = Literal["PENDING", "RUNNING", "VOTING", "VERIFIED", "DISPUTED", "FAILED"]
ReplicaStatus = Literal["SCHEDULED", "RUNNING", "EVICTED", "LOST", "DONE", "FAILED"]
NodeState = Literal["IDLE", "BUSY", "OFFLINE"]


# ---- request / response models (move to shared/ once the team agrees) ----

# caps the presigned URLs in one manifest
MAX_FILES_PER_TASK = 50
# A 500-page PDF split into single pages, with room to spare
MAX_TASKS_PER_JOB = 1000


class TaskIn(BaseModel):
    input: dict[str, Any] = Field(
        examples=[{"files": ["pages/0001.png", "pages/0002.png"], "lang": "eng"}]
    )
    # Its place in the job (0 to task_count - 1). Needed when the job's tasks
    # arrive in several requests; without it tasks are numbered in list order.
    index: int | None = Field(default=None, ge=0, examples=[0])

    @field_validator("input")
    @classmethod
    def check_files(cls, value: dict[str, Any]) -> dict[str, Any]:
        """
        Checks the optional "files", the objects this task's worker may download.

        Names are relative to jobs/{job_id}/ and are presigned into the manifest's
        input_urls. Everything else in input belongs to the job type and is passed
        to the worker untouched.

        args:
            value: The task's input.
        returns:
            dict: The same input, unchanged.
        raises:
            ValueError: If files is not a list of at most MAX_FILES_PER_TASK safe names.
        """
        files = value.get("files")
        if files is None:
            return value  # job types with no input files, e.g. "test"
        if not isinstance(files, list) or len(files) > MAX_FILES_PER_TASK:
            raise ValueError(
                f'"files" must be a list of at most {MAX_FILES_PER_TASK} names'
            )
        for key in files:
            # relative to jobs/{job_id}/, so a task can never get URLs for another job
            if (
                not isinstance(key, str)
                or not key
                or key.startswith("/")
                or ".." in key.split("/")
            ):
                raise ValueError(f'"files" has an invalid key: {key!r}')
        return value


class JobSpec(BaseModel):
    """A job as a signed-in user submits it to POST /jobs; the owner comes from the token."""

    job_id: UUID | None = Field(
        default=None, examples=["123e4567-e89b-12d3-a456-426614174000"]
    )
    job_type: str = Field(examples=["ocr"])
    params: dict[str, Any] = Field(examples=[{"param1": "value1", "param2": "value2"}])
    tasks: list[TaskIn] = Field(min_length=1)
    # How many tasks the job will have in all. Set it when only the first tasks
    # are sent now and the rest follow with POST /jobs/{id}/tasks, for example
    # OCR pages that are still being rendered. Defaults to len(tasks).
    task_count: int | None = Field(
        default=None, ge=1, le=MAX_TASKS_PER_JOB, examples=[8]
    )
    replicas_per_task: int = Field(ge=1, le=5, examples=[3])
    # The accuracy (0 to 1) the owner wants from a scored model, shown as met or
    # not met. Scoring needs an "eval_file" in the task input (GPU plan phase 3).
    min_score: float | None = Field(default=None, ge=0, le=1, examples=[0.9])
    cpu_request: int = Field(ge=1, le=16, examples=[2])  # in cores
    mem_request: int = Field(ge=64, le=4096, examples=[256])  # in MiB

    @model_validator(mode="after")
    def check_task_indices(self) -> "JobSpec":
        """
        Checks the tasks fit task_count, and that their indices, if any, are all
        given, distinct and in range.

        raises:
            ValueError: If there are more tasks than task_count, only some tasks
                have an index, an index repeats, or one is task_count or more.
        """
        total = self.task_count or len(self.tasks)
        if len(self.tasks) > total:
            raise ValueError("more tasks than task_count")
        check_indices(self.tasks, total, required=self.task_count is not None)
        return self


def check_indices(tasks: list[TaskIn], total: int, required: bool) -> None:
    """
    Checks task indices are all given or all absent, distinct, and below total.

    args:
        tasks: The tasks in one request.
        total: The job's task_count.
        required: Whether every task must have an index.
    raises:
        ValueError: If the indices break any of these rules.
    """
    given = [task.index for task in tasks if task.index is not None]
    if (required or given) and len(given) != len(tasks):
        raise ValueError("give every task an index, or none")
    if len(set(given)) != len(given):
        raise ValueError("task indices must be distinct")
    if any(index >= total for index in given):
        raise ValueError(f"task indices must be below task_count ({total})")


class AddTasksIn(BaseModel):
    """More tasks for a job created with a larger task_count."""

    tasks: list[TaskIn] = Field(min_length=1, max_length=MAX_TASKS_PER_JOB)

    @model_validator(mode="after")
    def check_indexed(self) -> "AddTasksIn":
        """Every task needs its index; the range is checked against the job."""
        check_indices(self.tasks, MAX_TASKS_PER_JOB, required=True)
        return self


class AddTasksResponse(BaseModel):
    job_id: UUID
    added_task_ids: list[int]  # only the tasks that were new
    replica_count: int


class CapacityOut(BaseModel):
    """Free room on usable worker nodes, in task copies of the asked size."""

    slots: int
    nodes: int


class CreateJobResponse(BaseModel):
    job_id: UUID
    task_ids: list[int]
    replica_count: int


class Manifest(BaseModel):
    replica_id: int
    task_index: int
    input: dict[str, Any]
    input_urls: dict[str, str]  # name -> presigned GET URL
    # presigned PUT for result.json. Like output.tar.gz it is per attempt
    # (attempts/{n}/), so an old attempt's late upload can't replace the
    # result a newer one reported.
    output_upload_url: str
    # presigned PUT for output.tar.gz: what a generic job (worker/runner) returns
    # to its owner. Workers that only report result.json, like OCR, ignore it.
    output_archive_url: str
    # Generic checkpoints are separate per attempt, so an old copy's late PUT
    # cannot overwrite a newer copy's progress. Try them in this order: the one
    # last reported to /checkpoint first, then the other earlier attempts,
    # newest first.
    checkpoint_archive_get_urls: list[str]
    checkpoint_archive_put_url: str
    checkpoint_get_url: str | None  # None on first attempt
    checkpoint_put_url: str
    attempt: int
    # The most the runner uploads from the checkpoint folder (job type's cap)
    checkpoint_max_bytes: int = 100 * 1024 * 1024
    # Only for a check copy: what to score (worker/model-check)
    check: "CheckSource | None" = None


class CheckSource(BaseModel):
    """
    The trained model a check copy scores. The checker refuses the archive
    unless its SHA-256 matches the one in result.json, and result.json unless
    its SHA-256 is result_sha256, the hash the training copy reported.
    """

    trained_replica_id: int
    result_url: str
    result_sha256: str
    archive_url: str
    eval_url: str


class CheckIn(BaseModel):
    """A check copy's outcome (POST /internal/replicas/{id}/check)."""

    status: Literal["scored", "not_scored"]
    score: float | None = Field(default=None, ge=0, le=1)
    samples: int | None = Field(default=None, ge=1)
    model_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    eval_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evaluator: str = Field(min_length=1, max_length=64)
    # why the model wasn't scored, e.g. "no_model: output/model.onnx is missing"
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_outcome(self) -> "CheckIn":
        if self.status == "scored" and (
            self.score is None or self.samples is None or self.model_sha256 is None
        ):
            raise ValueError("a scored check needs score, samples and model_sha256")
        if self.status == "not_scored" and not self.reason:
            raise ValueError("a check that didn't score needs a reason")
        return self


Manifest.model_rebuild()  # it names CheckSource, defined after it


class CheckpointIn(BaseModel):
    """A checkpoint archive the runner has just uploaded."""

    attempt: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)
    # the training step the script wrote next to its state, if it did
    step: int | None = Field(default=None, ge=0)


class ProgressIn(BaseModel):
    progress: int = Field(ge=0, le=100)


class ResultIn(BaseModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: int = 0


class HeartbeatIn(BaseModel):
    state: NodeState
    cpu_percent: float = Field(ge=0, le=100)


class Ack(BaseModel):
    ok: bool = True


# ---- user accounts ----

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class Credentials(BaseModel):
    email: str = Field(max_length=254, examples=["user@example.com"])
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        """
        Lower-cases the email so the same address can't sign up twice.

        raises:
            ValueError: If the value does not look like an email address.
        """
        value = value.strip().lower()
        if not re.match(EMAIL_PATTERN, value):
            raise ValueError("not a valid email address")
        return value


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
