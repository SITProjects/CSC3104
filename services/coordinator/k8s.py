import asyncio

from kubernetes.client import ApiException, BatchV1Api

from core.settings import JOB_NAMESPACE
from shared.db.models import JobType, Replica, Task

# delete finished jobs and their pods after 10 minutes to avoid cluttering the cluster
JOB_TTL_SECONDS = 600
# time for worker to save checkpoint after terminate signal
TERMINATION_GRACE_SECONDS = 30


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
                    "terminationGracePeriodSeconds": TERMINATION_GRACE_SECONDS,
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
        if create_err.status == 409:
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
