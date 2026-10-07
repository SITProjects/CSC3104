from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]
TaskStatus = Literal["PENDING", "RUNNING", "VOTING", "VERIFIED", "DISPUTED", "FAILED"]
ReplicaStatus = Literal["SCHEDULED", "RUNNING", "EVICTED", "LOST", "DONE", "FAILED"]
NodeState = Literal["IDLE", "BUSY", "OFFLINE"]


# ---- request / response models (move to shared/ once the team agrees) ----


class TaskIn(BaseModel):
    input: dict[str, Any] = Field(examples=[{"pages": [1, 10]}])


class CreateJobRequest(BaseModel):
    job_id: int
    tasks: list[TaskIn] = Field(min_length=1)


class CreateJobResponse(BaseModel):
    job_id: int
    task_ids: list[int]
    replica_count: int


class ReplicaView(BaseModel):
    id: int
    node_name: str | None
    status: ReplicaStatus
    attempt: int
    progress: int
    result_hash: str | None


class TaskView(BaseModel):
    id: int
    task_index: int
    status: TaskStatus
    final_hash: str | None
    replicas: list[ReplicaView]


class JobView(BaseModel):
    id: int
    job_type: str
    status: JobStatus
    progress: int  # 0-100, average over tasks
    tasks: list[TaskView]


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
