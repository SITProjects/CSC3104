from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

JobStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]
TaskStatus = Literal["PENDING", "RUNNING", "VOTING", "VERIFIED", "DISPUTED", "FAILED"]
ReplicaStatus = Literal["SCHEDULED", "RUNNING", "EVICTED", "LOST", "DONE", "FAILED"]
NodeState = Literal["IDLE", "BUSY", "OFFLINE"]


# ---- request / response models (move to shared/ once the team agrees) ----

# caps the presigned URLs in one manifest
MAX_FILES_PER_TASK = 50


class TaskIn(BaseModel):
    input: dict[str, Any] = Field(
        examples=[{"files": ["pages/0001.png", "pages/0002.png"], "lang": "eng"}]
    )

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


class CreateJobRequest(BaseModel):
    email: str = Field(examples=["user@example.com"])
    job_type: str = Field(examples=["ocr"])
    params: dict[str, Any] = Field(examples=[{"param1": "value1", "param2": "value2"}])
    tasks: list[TaskIn] = Field(min_length=1)
    replicas_per_task: int = Field(ge=1, le=5, examples=[3])
    cpu_request: int = Field(ge=1, le=16, examples=[2])  # in cores
    mem_request: int = Field(ge=64, le=4096, examples=[256])  # in MiB


class CreateJobResponse(BaseModel):
    job_id: UUID
    task_ids: list[int]
    replica_count: int


class Manifest(BaseModel):
    replica_id: int
    task_index: int
    input: dict[str, Any]
    input_urls: dict[str, str]  # name -> presigned GET URL
    output_upload_url: str  # presigned PUT for result.json
    checkpoint_get_url: str | None  # None on first attempt
    checkpoint_put_url: str
    attempt: int


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
