"""GPU copies on NVIDIA and AMD workers: the Pod a copy gets (k8s.py), the
free GPU slots (capacity.py) and the vendor rules in migration 015."""

import re
from pathlib import Path
from uuid import UUID

from kubernetes import client

from services.coordinator.capacity import free_slots, gpu_node
from services.coordinator.k8s import (
    GPU_RUNTIME_CLASS,
    build_replica_job_manifest,
    gpu_vendors,
)
from shared.db.models import JobType, Replica, Task

MIGRATION = Path(__file__).parents[3] / "shared/migrations/015_gpu_vendors.sql"


def job_type(gpu=False, vendors=None, min_vram=None, image="ghcr.io/x/img:main"):
    return JobType(
        name="t",
        image=image,
        command=None,
        default_cpu=2,
        default_mem=4096,
        timeout_seconds=600,
        gpu=gpu,
        gpu_vendors=vendors,
        min_gpu_vram_gib=min_vram,
    )


def manifest(jt, avoid=()):
    task = Task(id=5, job_id=UUID(int=1), task_index=0, cpu_request=2, mem_request=4096)
    replica = Replica(id=9, attempt=1)
    return build_replica_job_manifest(
        replica, task, jt, "token", "http://coordinator", avoid_nodes=avoid
    )


def pod_spec(m):
    return m["spec"]["template"]["spec"]


def env(m):
    return {e["name"]: e["value"] for e in pod_spec(m)["containers"][0]["env"]}


def required_terms(m):
    node_affinity = pod_spec(m)["affinity"]["nodeAffinity"]
    required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution")
    return required["nodeSelectorTerms"] if required else []


# ---------- gpu_vendors ----------


def test_gpu_vendors_cpu_type_has_none():
    assert gpu_vendors(job_type()) == []


def test_gpu_vendors_defaults_to_nvidia_before_migration_015():
    assert gpu_vendors(job_type(gpu=True)) == ["nvidia"]


def test_gpu_vendors_keeps_known_vendors_in_a_fixed_order():
    assert gpu_vendors(job_type(gpu=True, vendors=["amd", "nvidia"])) == [
        "nvidia",
        "amd",
    ]
    assert gpu_vendors(job_type(gpu=True, vendors=["amd", "intel"])) == ["amd"]


# ---------- manifests ----------


def test_cpu_copy_has_no_gpu_settings():
    m = manifest(job_type())
    spec = pod_spec(m)
    assert "runtimeClassName" not in spec
    assert spec["nodeSelector"] == {"idlecloud/role": "worker"}
    assert "idlecloud/gpu-job" not in m["metadata"]["labels"]
    assert not {"NVIDIA_VISIBLE_DEVICES", "AMD_VISIBLE_DEVICES"} & env(m).keys()
    assert required_terms(m) == []


def test_shared_gpu_copy_runs_on_either_vendor_with_one_pod_spec():
    m = manifest(job_type(gpu=True, vendors=["nvidia", "amd"]))
    spec = pod_spec(m)
    # one image and one runtime class: each node maps both to its own vendor
    assert spec["containers"][0]["image"] == "ghcr.io/x/img:main"
    assert spec["runtimeClassName"] == GPU_RUNTIME_CLASS == "idlecloud-gpu"
    assert spec["nodeSelector"] == {"idlecloud/role": "worker"}
    assert required_terms(m) == [
        {
            "matchExpressions": [
                {"key": "idlecloud/gpu", "operator": "In", "values": ["nvidia", "amd"]}
            ]
        }
    ]
    e = env(m)
    assert e["NVIDIA_VISIBLE_DEVICES"] == "all"
    assert e["AMD_VISIBLE_DEVICES"] == "all"
    assert e["IDLECLOUD_REQUIRE_CUDA"] == "1"
    assert m["metadata"]["labels"]["idlecloud/gpu-job"] == "true"


def test_gpu_copies_never_share_a_node():
    m = manifest(job_type(gpu=True, vendors=["nvidia", "amd"]))
    rules = pod_spec(m)["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]
    assert {"idlecloud/gpu-job": "true"} in [
        r["labelSelector"]["matchLabels"] for r in rules
    ]


def test_nvidia_only_type_gets_no_amd_variables():
    m = manifest(job_type(gpu=True, vendors=["nvidia"]))
    assert "AMD_VISIBLE_DEVICES" not in env(m)
    assert required_terms(m)[0]["matchExpressions"][0]["values"] == ["nvidia"]


def test_amd_only_type_gets_no_nvidia_variables():
    m = manifest(job_type(gpu=True, vendors=["amd"]))
    assert not {"NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES"} & env(m).keys()
    assert env(m)["AMD_VISIBLE_DEVICES"] == "all"
    assert required_terms(m)[0]["matchExpressions"][0]["values"] == ["amd"]


def test_vendor_vram_and_avoided_nodes_share_one_term():
    """Requirements in one term must all hold; separate terms would be ORed."""
    m = manifest(
        job_type(gpu=True, vendors=["nvidia", "amd"], min_vram=8), avoid=["pc-2"]
    )
    (term,) = required_terms(m)
    assert term["matchFields"] == [
        {"key": "metadata.name", "operator": "NotIn", "values": ["pc-2"]}
    ]
    assert term["matchExpressions"] == [
        {"key": "idlecloud/gpu", "operator": "In", "values": ["nvidia", "amd"]},
        {"key": "idlecloud/gpu-vram-gib", "operator": "Gt", "values": ["7"]},
    ]


# ---------- capacity ----------


def node(name, gpu=None, vram=None, cpu="8", mem="16Gi"):
    labels = {"idlecloud/role": "worker"}
    if gpu:
        labels["idlecloud/gpu"] = gpu
    if vram is not None:
        labels["idlecloud/gpu-vram-gib"] = str(vram)
    return client.V1Node(
        metadata=client.V1ObjectMeta(name=name, labels=labels),
        spec=client.V1NodeSpec(),
        status=client.V1NodeStatus(
            allocatable={"cpu": cpu, "memory": mem},
            conditions=[client.V1NodeCondition(type="Ready", status="True")],
        ),
    )


def gpu_pod(node_name):
    return client.V1Pod(
        metadata=client.V1ObjectMeta(labels={"idlecloud/gpu-job": "true"}),
        spec=client.V1PodSpec(
            node_name=node_name,
            containers=[
                client.V1Container(
                    name="worker",
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "2", "memory": "4096Mi"}
                    ),
                )
            ],
        ),
        status=client.V1PodStatus(phase="Running"),
    )


NODES = [
    node("cpu-pc"),
    node("nv-pc", gpu="nvidia", vram=8),
    node("amd-pc", gpu="amd", vram=16),
]


def test_gpu_node_accepts_any_known_vendor_by_default():
    assert [n.metadata.name for n in NODES if gpu_node(n)] == ["nv-pc", "amd-pc"]


def test_gpu_node_filters_by_vendor_and_memory():
    assert gpu_node(NODES[1], vendors=["nvidia"])
    assert not gpu_node(NODES[2], vendors=["nvidia"])
    assert not gpu_node(NODES[1], min_gpu_vram_gib=12)
    assert gpu_node(NODES[2], min_gpu_vram_gib=12, vendors=["amd"])
    assert not gpu_node(node("odd", gpu="intel"))


def test_free_gpu_slots_count_both_vendors_one_per_node():
    assert free_slots(NODES, [], 2, 4096, gpu=True) == (2, 2)
    assert free_slots(NODES, [], 2, 4096, gpu=True, gpu_vendors=["amd"]) == (1, 1)


def test_busy_gpu_node_of_either_vendor_has_no_slot():
    assert free_slots(NODES, [gpu_pod("amd-pc")], 2, 4096, gpu=True) == (1, 2)


def test_cpu_slots_ignore_gpu_vendor():
    assert free_slots(NODES, [], 2, 4096) == (12, 3)


# ---------- migration 015 ----------


def test_migration_015_backfills_old_gpu_types_and_opens_pytorch_gpu_to_amd():
    sql = MIGRATION.read_text()
    assert "SET gpu_vendors = ARRAY['nvidia'] WHERE gpu AND gpu_vendors IS NULL" in sql
    assert re.search(
        r"SET gpu_vendors = ARRAY\['nvidia', 'amd'\].*WHERE name = 'pytorch-gpu'",
        sql,
        re.DOTALL,
    )


def test_model_and_migration_constraints_match():
    sql = " ".join(MIGRATION.read_text().split())
    constraints = {
        c.name: " ".join(str(c.sqltext).split())
        for c in JobType.__table__.constraints
        if c.name in ("job_types_gpu_vendors_check", "job_types_gpu_image_tag_check")
    }
    assert len(constraints) == 2
    assert "gpu_vendors <@ ARRAY['nvidia', 'amd']::TEXT[]" in sql
    assert (
        "gpu_vendors <@ ARRAY['nvidia', 'amd']::TEXT[]"
        in (constraints["job_types_gpu_vendors_check"])
    )
    assert "position('@' IN image) = 0" in sql
    assert "position('@' IN image) = 0" in constraints["job_types_gpu_image_tag_check"]
