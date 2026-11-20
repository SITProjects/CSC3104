"""Load a slot's WireGuard config and the K3s join token.

Normal path: read the K8s Secrets the hub setup creates (needs the shared kubeconfig).
File path: set BURST_WG_DIR (folder with slot-200.conf ...) and K3S_TOKEN_FILE
in services/autoscaler/.env.aws to read the same values from local files.
"""

import base64
import os

from services.autoscaler.burst import config


def _from_files(slot: int) -> tuple[str, str]:
    wg_conf = (
        config.repo_path(os.environ["BURST_WG_DIR"]) / f"slot-{slot}.conf"
    ).read_text()
    token = config.repo_path(os.environ["K3S_TOKEN_FILE"]).read_text().strip()
    return wg_conf, token


def _from_cluster(slot: int) -> tuple[str, str]:
    from kubernetes import client
    from kubernetes import config as kube_config

    try:
        kube_config.load_incluster_config()  # when the autoscaler runs in the cluster (step 3)
    except kube_config.ConfigException:
        try:
            kube_config.load_kube_config()  # in the dev container with the shared kubeconfig
        except (kube_config.ConfigException, FileNotFoundError) as error:
            raise SystemExit(
                "No kubeconfig found to read the burst Secrets. Get the shared kubeconfig "
                "from the hub, or set BURST_WG_DIR and K3S_TOKEN_FILE in .env.aws."
            ) from error
    core = client.CoreV1Api()

    secret = core.read_namespaced_secret(
        config.WG_SLOTS_SECRET, config.SECRETS_NAMESPACE
    )
    slots = secret.data or {}
    key = f"slot-{slot}"
    if key not in slots:
        raise KeyError(f"secret {config.WG_SLOTS_SECRET} has no key {key}")
    token_secret = core.read_namespaced_secret(
        config.K3S_TOKEN_SECRET, config.SECRETS_NAMESPACE
    )
    token = token_secret.data["token"]
    return base64.b64decode(slots[key]).decode(), base64.b64decode(
        token
    ).decode().strip()


def load_slot(slot: int) -> tuple[str, str]:
    if os.environ.get("BURST_WG_DIR"):
        return _from_files(slot)
    return _from_cluster(slot)
