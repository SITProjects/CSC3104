import asyncio
from collections.abc import Collection
from typing import cast

from kubernetes.client import (
    ApiException,
    AuthenticationV1Api,
    BatchV1Api,
    CoreV1Api,
    V1DeleteOptions,
    V1Job,
    V1JobList,
    V1Pod,
    V1PodList,
    V1Preconditions,
    V1TokenReview,
    V1TokenReviewSpec,
    V1UserInfo,
)

from core.settings import JOB_NAMESPACE, TASK_IMAGE_PULL_POLICY
from shared.db.models import JobType, Replica, Task

# delete finished jobs and their pods after 10 minutes to avoid cluttering the cluster
JOB_TTL_SECONDS = 600
# time for worker to save checkpoint after terminate signal
TERMINATION_GRACE_SECONDS = 30
TOLERATION_SECONDS = 30
REPLICA_SELECTOR = "app.kubernetes.io/managed-by=idlecloud-coordinator"
# GPU job types (job_types.gpu and gpu_vendors, migrations 014 and 015).
# infra/join-worker.sh labels a worker whose GPU works with its vendor.
GPU_NODE_LABEL = "idlecloud/gpu"
GPU_VENDORS = ("nvidia", "amd")
GPU_VRAM_LABEL = "idlecloud/gpu-vram-gib"
# One RuntimeClass for every vendor (infra/k8s/base/gpu-runtimeclass.yml), so a
# copy's Pod is the same whichever GPU node takes it. Each GPU node maps the
# handler to its own vendor's container runtime in its containerd config:
# nvidia-container-runtime or amd-container-runtime. Likewise each node pulls
# its own build of the image (an AMD node's K3s rewrites the name to the ROCm
# build), so the scheduler alone decides the vendor, after the Pod exists.
GPU_RUNTIME_CLASS = "idlecloud-gpu"
# On every GPU copy's Pod, so a node runs at most one of them: the copy gets all
# of the node's GPUs (NVIDIA_VISIBLE_DEVICES=all), so a second one would share
GPU_JOB_LABEL = "idlecloud/gpu-job"


def pull_policy(image: str) -> str:
    """
    Always for a tag, which can move to a new build (checking costs one registry
    request when nothing changed), IfNotPresent for a digest, which cannot.
    TASK_IMAGE_PULL_POLICY overrides both.
    """
    if TASK_IMAGE_PULL_POLICY:
        return TASK_IMAGE_PULL_POLICY
    return "IfNotPresent" if "@sha256:" in image else "Always"


async def list_replica_jobs(batch_api: BatchV1Api) -> list[V1Job]:
    """List coordinator-managed Jobs without blocking the event loop."""
    jobs = cast(
        V1JobList,
        await asyncio.to_thread(
            batch_api.list_namespaced_job,
            JOB_NAMESPACE,
            label_selector=REPLICA_SELECTOR,
        ),
    )
    return jobs.items or []


async def list_replica_pods(core_api: CoreV1Api) -> list[V1Pod]:
    """List coordinator-managed Pods without blocking the event loop."""
    pods = cast(
        V1PodList,
        await asyncio.to_thread(
            core_api.list_namespaced_pod,
            JOB_NAMESPACE,
            label_selector=REPLICA_SELECTOR,
        ),
    )
    return pods.items or []


def gpu_vendors(job_type: JobType) -> list[str]:
    """
    The GPU vendors a job type's copies may run on, in GPU_VENDORS order: none
    for a CPU type, and NVIDIA for a GPU type from before migration 015.
    """
    if not job_type.gpu:
        return []
    accepted = job_type.gpu_vendors or ["nvidia"]
    return [vendor for vendor in GPU_VENDORS if vendor in accepted]


def _required_nodes(
    avoid: Collection[str],
    min_gpu_vram_gib: int | None,
    vendors: Collection[str] = (),
) -> dict:
    """
    The hard node rules for a copy, or nothing: off these nodes (by name), on a
    GPU node of one of these vendors, and with at least this much GPU memory.

    A node-name field selector takes one value per NotIn, so each node gets its
    own requirement; requirements in one term must all hold.
    """
    term: dict = {}
    if avoid:
        term["matchFields"] = [
            {"key": "metadata.name", "operator": "NotIn", "values": [name]}
            for name in sorted(avoid)
        ]
    expressions = []
    if vendors:
        expressions.append(
            {"key": GPU_NODE_LABEL, "operator": "In", "values": list(vendors)}
        )
    if min_gpu_vram_gib:
        # Gt compares the label as an integer: more than n - 1 is at least n
        expressions.append(
            {
                "key": GPU_VRAM_LABEL,
                "operator": "Gt",
                "values": [str(min_gpu_vram_gib - 1)],
            }
        )
    if expressions:
        term["matchExpressions"] = expressions
    if not term:
        return {}
    return {
        "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [term]}
    }


def _gpu_env(job_type: JobType) -> list[dict]:
    """
    What a GPU copy's container needs: the node's vendor runtime exposes every
    GPU on the node to it (each runtime reads its own variables and ignores the
    other's), and the runner refuses to start the command unless PyTorch sees a
    GPU, rather than let it train on the CPU unnoticed. ROCm builds of PyTorch
    answer through the same torch.cuda API, so one check covers both vendors.
    """
    vendors = gpu_vendors(job_type)
    if not vendors:
        return []
    env = []
    if "nvidia" in vendors:
        env += [
            {"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
            {"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "compute,utility"},
        ]
    if "amd" in vendors:
        env.append({"name": "AMD_VISIBLE_DEVICES", "value": "all"})
    env.append({"name": "IDLECLOUD_REQUIRE_CUDA", "value": "1"})
    return env


def build_replica_job_manifest(
    replica: Replica,
    task: Task,
    job_type: JobType,
    token: str,
    coordinator_url: str,
    avoid_nodes: Collection[str] = (),
):
    """
    The Kubernetes Job for one replica attempt.

    args:
        avoid_nodes: Nodes this copy must not use: where its task's other copies
            ran or run (crud.sibling_nodes), so the vote compares different PCs.
    """
    labels = {
        "app.kubernetes.io/managed-by": "idlecloud-coordinator",
        "idlecloud/job-id": str(task.job_id),
        "idlecloud/task-id": str(task.id),
        "idlecloud/replica-id": str(replica.id),
        "idlecloud/attempt": str(replica.attempt),
    }
    if job_type.gpu:
        labels[GPU_JOB_LABEL] = "true"

    # cpu_request is whole cores (a bare number in Kubernetes), mem_request is MiB
    resources = {"cpu": str(task.cpu_request), "memory": f"{task.mem_request}Mi"}

    container = {
        "name": "worker",
        "image": job_type.image,
        "imagePullPolicy": pull_policy(job_type.image),
        "env": [
            {"name": "COORDINATOR_URL", "value": coordinator_url},
            {"name": "REPLICA_ID", "value": str(replica.id)},
            {"name": "TASK_INDEX", "value": str(task.task_index)},
            {"name": "REPLICA_TOKEN", "value": token},
            *_gpu_env(job_type),
        ],
        "resources": {"requests": resources, "limits": resources},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }

    if job_type.command:
        container["command"] = job_type.command

    # copies of a task never share a node; GPU copies never share one at all
    anti_affinity = [
        {
            "topologyKey": "kubernetes.io/hostname",
            "labelSelector": {"matchLabels": {"idlecloud/task-id": str(task.id)}},
        }
    ]
    vendors = gpu_vendors(job_type)
    gpu_spec = {}
    if vendors:
        anti_affinity.append(
            {
                "topologyKey": "kubernetes.io/hostname",
                "labelSelector": {"matchLabels": {GPU_JOB_LABEL: "true"}},
            }
        )
        gpu_spec["runtimeClassName"] = GPU_RUNTIME_CLASS

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"replica-{replica.id}-a{replica.attempt}",
            "namespace": JOB_NAMESPACE,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": JOB_TTL_SECONDS,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    # on the pod, not the Job: counts from when a node starts the pod,
                    # so Jobs can wait in Pending for a free node for as long as needed
                    "activeDeadlineSeconds": job_type.timeout_seconds,
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "tolerations": [
                        {
                            "key": "node.kubernetes.io/not-ready",
                            "operator": "Exists",
                            "effect": "NoExecute",
                            "tolerationSeconds": TOLERATION_SECONDS,
                        },
                        {
                            "key": "node.kubernetes.io/unreachable",
                            "operator": "Exists",
                            "effect": "NoExecute",
                            "tolerationSeconds": TOLERATION_SECONDS,
                        },
                    ],
                    "terminationGracePeriodSeconds": TERMINATION_GRACE_SECONDS,
                    # campus PCs and burst workers only, never the control plane;
                    # GPU copies only on workers with a working GPU of an accepted
                    # vendor (the node affinity below)
                    "nodeSelector": {"idlecloud/role": "worker"},
                    **gpu_spec,
                    "affinity": {
                        "podAntiAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": anti_affinity
                        },
                        "nodeAffinity": {
                            **_required_nodes(
                                avoid_nodes,
                                job_type.min_gpu_vram_gib if vendors else None,
                                vendors,
                            ),
                            "preferredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "weight": 100,
                                    "preference": {
                                        "matchExpressions": [
                                            {
                                                "key": "idlecloud/trust",
                                                "operator": "In",
                                                "values": ["high"],
                                            }
                                        ]
                                    },
                                }
                            ],
                        },
                    },
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65534,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [container],
                },
            },
        },
    }


async def create_job(manifest: dict, k3_api: BatchV1Api) -> None:
    """
    Submit k8s job. If job already exist, it will be deleted and recreated.
    """
    name = manifest["metadata"]["name"]
    try:
        await asyncio.to_thread(k3_api.create_namespaced_job, JOB_NAMESPACE, manifest)
    except ApiException as create_err:
        if create_err.status != 409:
            raise
        try:
            await asyncio.to_thread(
                k3_api.delete_namespaced_job,
                name,
                JOB_NAMESPACE,
                propagation_policy="Background",
            )
        except ApiException as delete_err:
            if delete_err.status != 404:
                raise
        await asyncio.to_thread(k3_api.create_namespaced_job, JOB_NAMESPACE, manifest)


async def cancel_k3_job(job_name: str, k3_api: BatchV1Api) -> None:
    """
    Deletes a replica's Kubernetes Job and its Pods. A Job that no longer exists
    is not an error.
    """
    try:
        await asyncio.to_thread(
            k3_api.delete_namespaced_job,
            job_name,
            JOB_NAMESPACE,
            propagation_policy="Background",
        )
    except ApiException as e:
        if e.status != 404:
            raise


async def cancel_k3_pod(pod_name: str, core_api: CoreV1Api, uid: str | None) -> None:
    """Delete an orphan Pod, protecting any replacement with the same name."""
    try:
        await asyncio.to_thread(
            core_api.delete_namespaced_pod,
            pod_name,
            JOB_NAMESPACE,
            body=V1DeleteOptions(preconditions=V1Preconditions(uid=uid)),
        )
    except ApiException as e:
        if e.status != 404:
            raise


async def review_token(
    auth_api: AuthenticationV1Api, token: str, audience: str
) -> V1UserInfo | None:
    """
    Asks the API server who a service-account token belongs to (TokenReview).

    args:
        auth_api: The Kubernetes authentication API.
        token: The bearer token from the request.
        audience: The audience the token must have been issued for.
    returns:
        V1UserInfo | None: The token's user, with the node its Pod runs on in
            extra["authentication.kubernetes.io/node-name"], or None if the token
            is invalid, expired, or meant for another audience.
    """
    review = cast(
        V1TokenReview,
        await asyncio.to_thread(
            auth_api.create_token_review,
            V1TokenReview(spec=V1TokenReviewSpec(token=token, audiences=[audience])),
        ),
    )
    status = review.status
    if (
        not status
        or not status.authenticated
        or audience not in (status.audiences or [])
    ):
        return None
    return status.user


# Check copies (GPU plan phase 3): they score a trained model on a node the
# platform controls, never on a lending PC
CHECKER_NODE_LABEL = "idlecloud/checker"
CHECK_CPU = 1  # cores
CHECK_MEM_MIB = 1024
CHECK_TIMEOUT_SECONDS = 600


def build_check_job_manifest(
    replica: Replica,
    task: Task,
    job_type: JobType,
    token: str,
    coordinator_url: str,
    avoid_nodes: Collection[str] = (),
):
    """
    The Kubernetes Job for a check copy: the job type's check_image, 1 core,
    1 GiB and 10 minutes, only on nodes labelled idlecloud/checker=true (the hub
    or a CPU burst worker). avoid_nodes is ignored: the checker is never one of
    the PCs that trained the model.
    """
    del avoid_nodes
    if not job_type.check_image:
        raise ValueError(f"job type {job_type.name} has no check_image")
    labels = {
        "app.kubernetes.io/managed-by": "idlecloud-coordinator",
        "idlecloud/job-id": str(task.job_id),
        "idlecloud/task-id": str(task.id),
        "idlecloud/replica-id": str(replica.id),
        "idlecloud/attempt": str(replica.attempt),
        "idlecloud/check": "true",
    }
    resources = {"cpu": str(CHECK_CPU), "memory": f"{CHECK_MEM_MIB}Mi"}
    container = {
        "name": "worker",
        "image": job_type.check_image,
        "imagePullPolicy": pull_policy(job_type.check_image),
        "env": [
            {"name": "COORDINATOR_URL", "value": coordinator_url},
            {"name": "REPLICA_ID", "value": str(replica.id)},
            {"name": "REPLICA_TOKEN", "value": token},
        ],
        "resources": {"requests": resources, "limits": resources},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
        },
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"replica-{replica.id}-a{replica.attempt}",
            "namespace": JOB_NAMESPACE,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": JOB_TTL_SECONDS,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "activeDeadlineSeconds": CHECK_TIMEOUT_SECONDS,
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "terminationGracePeriodSeconds": TERMINATION_GRACE_SECONDS,
                    "nodeSelector": {CHECKER_NODE_LABEL: "true"},
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65534,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [container],
                },
            },
        },
    }
