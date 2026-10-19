import asyncio
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

from core.settings import JOB_NAMESPACE
from shared.db.models import JobType, Replica, Task

# delete finished jobs and their pods after 10 minutes to avoid cluttering the cluster
JOB_TTL_SECONDS = 600
# time for worker to save checkpoint after terminate signal
TERMINATION_GRACE_SECONDS = 30
TOLERATION_SECONDS = 30
REPLICA_SELECTOR = "app.kubernetes.io/managed-by=idlecloud-coordinator"


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


def build_replica_job_manifest(
    replica: Replica, task: Task, job_type: JobType, token: str, coordinator_url: str
):
    labels = {
        "app.kubernetes.io/managed-by": "idlecloud-coordinator",
        "idlecloud/job-id": str(task.job_id),
        "idlecloud/task-id": str(task.id),
        "idlecloud/replica-id": str(replica.id),
        "idlecloud/attempt": str(replica.attempt),
    }

    # cpu_request is whole cores (a bare number in Kubernetes), mem_request is MiB
    resources = {"cpu": str(task.cpu_request), "memory": f"{task.mem_request}Mi"}

    container = {
        "name": "worker",
        "image": job_type.image,
        "env": [
            {"name": "COORDINATOR_URL", "value": coordinator_url},
            {"name": "REPLICA_ID", "value": str(replica.id)},
            {"name": "TASK_INDEX", "value": str(task.task_index)},
            {"name": "REPLICA_TOKEN", "value": token},
        ],
        "resources": {"requests": resources, "limits": resources},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }

    if job_type.command:
        container["command"] = job_type.command

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
                    # campus PCs and burst workers only, never the control plane
                    "nodeSelector": {"idlecloud/role": "worker"},
                    # replicas of the same task must land on different nodes for voting
                    "affinity": {
                        "podAntiAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "topologyKey": "kubernetes.io/hostname",
                                    "labelSelector": {
                                        "matchLabels": {
                                            "idlecloud/task-id": str(task.id)
                                        }
                                    },
                                }
                            ]
                        },
                        "nodeAffinity": {
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
                            ]
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
