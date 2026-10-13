"""Start, stop and list burst workers by hand. The step 3 autoscaler reuses these functions.

Run from the repo root:
    python -m services.autoscaler.burst.launch up --slot 200 [--spot]
    python -m services.autoscaler.burst.launch list
    python -m services.autoscaler.burst.launch down --slot 200
"""

import argparse

from services.autoscaler.burst import config
from services.autoscaler.burst.aws import ec2_client
from services.autoscaler.burst.slot_store import load_slot
from services.autoscaler.burst.user_data import render_user_data

# shutting-down counts as live, because the old instance still holds the WireGuard peer
LIVE_STATES = ["pending", "running", "shutting-down", "stopping", "stopped"]


def _ec2():
    return ec2_client()


def slot_of(instance: dict) -> int | None:
    """The burst slot an instance holds, from the current or the legacy tag."""
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    for key in (config.SLOT_TAG_KEY, config.LEGACY_SLOT_TAG_KEY):
        value = tags.get(key, "")
        if value.isdigit():
            return int(value)
    return None


def find_instances(ec2, slot: int | None = None) -> list[dict]:
    filters = [
        {
            "Name": f"tag:{config.PROJECT_TAG['Key']}",
            "Values": [config.PROJECT_TAG["Value"]],
        },
        {"Name": "instance-state-name", "Values": LIVE_STATES},
        # Either slot tag. Values in one filter are ORed by EC2.
        {
            "Name": "tag-key",
            "Values": [config.SLOT_TAG_KEY, config.LEGACY_SLOT_TAG_KEY],
        },
    ]
    reservations = ec2.describe_instances(Filters=filters)["Reservations"]
    found = [instance for r in reservations for instance in r["Instances"]]
    if slot is None:
        return [i for i in found if slot_of(i) is not None]
    return [i for i in found if slot_of(i) == slot]


def launch_params(
    slot: int,
    wg_conf: str,
    token: str,
    spot: bool = False,
    k3s_version: str | None = None,
    instance_type: str | None = None,
) -> dict:
    """The exact run_instances request for one burst worker. No AWS calls here.
    instance_type overrides the launch template's default size."""
    instance_type = instance_type or config.INSTANCE_TYPE
    params = {
        "LaunchTemplate": {
            "LaunchTemplateName": config.TEMPLATE_NAME,
            "Version": "$Default",
        },
        "MinCount": 1,
        "MaxCount": 1,
        # boto3 base64-encodes this for us
        "InstanceType": instance_type,
        "UserData": render_user_data(slot, wg_conf, token, k3s_version, instance_type),
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    config.PROJECT_TAG,
                    {"Key": "Name", "Value": f"idlecloud-{config.node_name(slot)}"},
                    {"Key": config.SLOT_TAG_KEY, "Value": str(slot)},
                ],
            }
        ],
    }
    if spot:  # used from step 4. Falls back to on-demand if AWS Academy blocks Spot.
        params["InstanceMarketOptions"] = {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate",
            },
        }
    return params


def launch(slot: int, spot: bool = False, instance_type: str | None = None) -> str:
    ec2 = _ec2()
    if find_instances(ec2, slot):
        raise RuntimeError(f"slot {slot} is already in use")

    wg_conf, token = load_slot(slot)
    params = launch_params(slot, wg_conf, token, spot, instance_type=instance_type)
    # The last instance on this slot may have ended without `down` (lifetime limit,
    # failed boot or Spot reclaim). Clear its node so the new one is not rejected.
    remove_node(slot)
    instance_id = ec2.run_instances(**params)["Instances"][0]["InstanceId"]
    print(
        f"Launched {instance_id} ({params['InstanceType']}) as "
        f"{config.node_name(slot)} ({config.node_ip(slot)})"
    )
    return instance_id


def remove_node(slot: int) -> None:
    """Delete the K8s node and its K3s node password. Without this, a new
    instance on the same slot is refused with 'Node password rejected'."""
    from kubernetes import client
    from kubernetes import config as kube_config
    from kubernetes.client.rest import ApiException

    try:
        kube_config.load_incluster_config()
    except kube_config.ConfigException:
        try:
            kube_config.load_kube_config()
        except (kube_config.ConfigException, FileNotFoundError):
            print(
                "No kubeconfig yet, skipping node cleanup (fine before the cluster exists)"
            )
            return
    core = client.CoreV1Api()
    name = config.node_name(slot)
    for delete in (
        lambda: core.delete_node(name),
        lambda: core.delete_namespaced_secret(
            f"{name}.node-password.k3s", "kube-system"
        ),
    ):
        try:
            delete()
        except ApiException as error:
            if error.status != 404:
                raise
    print(f"Removed node {name} from the cluster")


def terminate(slot: int) -> None:
    ec2 = _ec2()
    ids = [instance["InstanceId"] for instance in find_instances(ec2, slot)]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        print(f"Terminating {', '.join(ids)}")
    else:
        print(f"No instance on slot {slot}")
    remove_node(slot)


def list_workers() -> None:
    for instance in find_instances(_ec2()):
        market = instance.get("InstanceLifecycle", "on-demand")
        print(
            f"{instance['InstanceId']}  slot {slot_of(instance)}  "
            f"{instance['InstanceType']}  "
            f"{instance['State']['Name']}  {market}  since {instance['LaunchTime']:%H:%M}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--slot", type=int, required=True, choices=list(config.SLOTS))
    up.add_argument("--spot", action="store_true")
    up.add_argument("--type", help=f"EC2 instance type, default {config.INSTANCE_TYPE}")
    down = sub.add_parser("down")
    down.add_argument("--slot", type=int, required=True, choices=list(config.SLOTS))
    sub.add_parser("list")
    args = parser.parse_args()

    if args.command == "up":
        launch(args.slot, args.spot, args.type)
    elif args.command == "down":
        terminate(args.slot)
    else:
        list_workers()


if __name__ == "__main__":
    main()
