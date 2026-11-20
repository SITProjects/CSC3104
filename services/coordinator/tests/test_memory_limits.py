"""GPU job types may ask for more memory than CPU ones (issue #197)."""

import asyncio

import pytest
from fastapi import HTTPException

from services.coordinator.crud.jobs import check_job_type
from services.coordinator.models import CPU_MAX_MEM, GPU_MAX_MEM, JobSpec
from shared.db.models import JobType


def spec(job_type: str, mem: int) -> JobSpec:
    return JobSpec(
        job_type=job_type,
        params={},
        tasks=[{"input": {}}],
        replicas_per_task=1,
        cpu_request=2,
        mem_request=mem,
    )


class Session:
    def __init__(self, kind: JobType):
        self.kind = kind

    async def get(self, _model, _name):
        return self.kind


def check(kind: JobType, mem: int) -> None:
    asyncio.run(check_job_type(Session(kind), spec(kind.name, mem)))


def test_spec_allows_up_to_the_gpu_limit():
    assert spec("pytorch-gpu", GPU_MAX_MEM).mem_request == GPU_MAX_MEM
    with pytest.raises(ValueError):
        spec("pytorch-gpu", GPU_MAX_MEM + 1)


def test_gpu_type_may_go_above_the_cpu_limit():
    check(JobType(name="pytorch-gpu", gpu=True, verify=False), GPU_MAX_MEM)


def test_cpu_type_stays_within_the_cpu_limit():
    cpu = JobType(name="pytorch", gpu=False, verify=False)
    check(cpu, CPU_MAX_MEM)
    with pytest.raises(HTTPException) as e:
        check(cpu, CPU_MAX_MEM + 1)
    assert e.value.status_code == 422
