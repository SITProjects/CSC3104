"""Start, stop and list burst workers by hand, and inject a Spot reclaim warning
for testing. The step 3 autoscaler reuses these functions.

Run from the repo root:
    python -m services.autoscaler.burst.launch up --slot 200 [--spot]
    python -m services.autoscaler.burst.launch list
    python -m services.autoscaler.burst.launch down --slot 200
    python -m services.autoscaler.burst.launch reclaim --slot 200
"""

import argparse
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from services.autoscaler.burst import config
from services.autoscaler.burst.aws import ONE_ATTEMPT, ec2_client
from services.autoscaler.burst.market import MarketChooser
from services.autoscaler.burst.slot_store import load_slot
from services.autoscaler.burst.user_data import render_user_data

# shutting-down counts as live, because the old instance still holds the WireGuard peer
LIVE_STATES = ["pending", "running", "shutting-down", "stopping", "stopped"]


def _ec2():
    return ec2_client()


def _launch_ec2():
    """The client that sends run_instances: no automatic retries (see aws.py)."""
    return ec2_client(ONE_ATTEMPT)


def tags_of(instance: dict) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in instance.get("Tags", [])}


def slot_of(instance: dict) -> int | None:
    """The burst slot an instance holds, from the current or the legacy tag."""
    tags = tags_of(instance)
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


def pinned_template(ec2) -> dict:
    """The launch template's id and current default version as fixed values.
    A request that may be replayed later (controller/pending.py) must start
    the same machine, even if launch_template.py changes the default meanwhile."""
    found = ec2.describe_launch_templates(LaunchTemplateNames=[config.TEMPLATE_NAME])
    template = found["LaunchTemplates"][0]
    return {
        "LaunchTemplateId": template["LaunchTemplateId"],
        "Version": str(template["DefaultVersionNumber"]),
    }


def launch_params(
    slot: int,
    wg_conf: str,
    token: str,
    spot: bool = False,
    k3s_version: str | None = None,
    instance_type: str | None = None,
    template: dict | None = None,
    client_token: str | None = None,
) -> dict:
    """The exact run_instances request for one burst worker. No AWS calls here.
    instance_type overrides the launch template's default size. template pins
    the template version (see pinned_template). client_token makes the request
    safe to replay: AWS starts at most one instance per token."""
    instance_type = instance_type or config.INSTANCE_TYPE
    params = {
        "LaunchTemplate": template
        or {
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
    if client_token:
        params["ClientToken"] = client_token
    if spot:  # BURST_MARKET and market.py decide when, and what happens if refused
        params["InstanceMarketOptions"] = {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate",
            },
        }
    return params


def launch(
    slot: int,
    spot: bool = False,
    instance_type: str | None = None,
    chooser: MarketChooser | None = None,
    on_request: Callable[[dict, str], None] = lambda request, market: None,
) -> str:
    """Start one burst worker on this slot. The chooser decides Spot or
    On-Demand (see market.py). Without one, `spot` picks the market and a
    refusal is raised as it is. on_request receives each exact request just
    before it is sent. An unknown outcome raises market.LaunchUnknown."""
    instance_type = instance_type or config.INSTANCE_TYPE
    if chooser is None:
        chooser = MarketChooser("spot" if spot else "on-demand", cooldown_s=0)
    ec2 = _ec2()
    if find_instances(ec2, slot):
        raise RuntimeError(f"slot {slot} is already in use")

    wg_conf, token = load_slot(slot)
    template = pinned_template(ec2)

    def params_for(use_spot: bool) -> dict:
        # A new ClientToken for every new request. Only an exact replay of a
        # request (pending.py) reuses its token.
        return launch_params(
            slot,
            wg_conf,
            token,
            use_spot,
            instance_type=instance_type,
            template=template,
            client_token=uuid.uuid4().hex,
        )

    # The last instance on this slot may have ended without `down` (lifetime limit,
    # failed boot or Spot reclaim). Clear its node so the new one is not rejected.
    remove_node(slot)
    launcher = _launch_ec2()
    instance_id, market = chooser.launch(
        launcher, params_for, instance_type, on_request
    )
    print(
        f"Launched {instance_id} ({instance_type}, {market}) as "
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


def metadata_problem(instance: dict) -> str | None:
    """Why the watcher on this instance cannot read its tags, or None.
    Workers from an older template version have tags in metadata turned off.
    State "pending" means changed settings are not in effect yet."""
    options = instance.get("MetadataOptions", {})
    relaunch = "Re-run launch_template.py, then launch a new worker on this slot."
    if options.get("HttpEndpoint") != "enabled":
        return f"has the instance metadata service turned off. {relaunch}"
    if options.get("InstanceMetadataTags") != "enabled":
        return f"cannot read its tags through instance metadata. {relaunch}"
    if options.get("State") != "applied":
        return "has metadata settings that are not applied yet. Try again in a minute."
    return None


def reclaim(slot: int) -> str:
    """Inject a Spot reclaim warning by tagging the worker on this slot with a
    deadline two minutes from now. Only the warning is fake. The Spot watcher on
    the node reads the tag and runs the same handling as for a real warning.
    Returns the deadline written."""
    ec2 = _ec2()
    running = [i for i in find_instances(ec2, slot) if i["State"]["Name"] == "running"]
    if len(running) != 1:
        ids = ", ".join(i["InstanceId"] for i in running) or "none"
        raise RuntimeError(
            f"slot {slot} needs exactly one running instance, found {ids}"
        )
    instance = running[0]
    instance_id = instance["InstanceId"]

    # Do not move an existing warning. A real Spot warning is never pushed back.
    # Best effort only: this check and create_tags are not atomic, and EC2 reads
    # are eventually consistent, so two runs at once (or a quick retry) can still
    # overwrite the deadline. Meant for one person testing at a time.
    existing = tags_of(instance).get(config.RECLAIM_TAG_KEY)
    if existing is not None:
        raise RuntimeError(
            f"{instance_id} already has a reclaim warning ({existing}). "
            "It will not be changed."
        )
    problem = metadata_problem(instance)
    if problem:
        raise RuntimeError(
            f"Spot watcher on {instance_id} cannot see a warning. It {problem}"
        )

    deadline = datetime.now(UTC) + timedelta(seconds=config.RECLAIM_WARNING_S)
    value = deadline.strftime("%Y-%m-%dT%H:%M:%SZ")
    ec2.create_tags(
        Resources=[instance_id], Tags=[{"Key": config.RECLAIM_TAG_KEY, "Value": value}]
    )
    print(
        f"Reclaim warning set on {instance_id} ({config.node_name(slot)}), "
        f"deadline {value}"
    )
    return value


def list_workers() -> None:
    for instance in find_instances(_ec2()):
        market = instance.get("InstanceLifecycle", "on-demand")
        warning = tags_of(instance).get(config.RECLAIM_TAG_KEY)
        print(
            f"{instance['InstanceId']}  slot {slot_of(instance)}  "
            f"{instance['InstanceType']}  "
            f"{instance['State']['Name']}  {market}  since {instance['LaunchTime']:%H:%M}"
            + (f"  reclaim at {warning}" if warning else "")
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
    warn = sub.add_parser(
        "reclaim", help="inject a Spot reclaim warning (deadline in 2 minutes)"
    )
    warn.add_argument("--slot", type=int, required=True, choices=list(config.SLOTS))
    args = parser.parse_args()

    if args.command == "up":
        launch(args.slot, args.spot, args.type)
    elif args.command == "down":
        terminate(args.slot)
    elif args.command == "reclaim":
        reclaim(args.slot)
    else:
        list_workers()


if __name__ == "__main__":
    main()
