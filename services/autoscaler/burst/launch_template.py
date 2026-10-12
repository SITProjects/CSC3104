"""Create or update the EC2 Launch Template and security group for burst workers.

Run from the repo root once, and again whenever the settings change:
    python -m services.autoscaler.burst.launch_template [--key-name vockey] [--ssh-cidr 1.2.3.4/32]
"""

import argparse

from services.autoscaler.burst import config
from services.autoscaler.burst.aws import ec2_client

UBUNTU_OWNER = "099720109477"  # Canonical
UBUNTU_NAME = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"


def latest_ubuntu_ami(ec2) -> str:
    images = ec2.describe_images(
        Owners=[UBUNTU_OWNER],
        Filters=[
            {"Name": "name", "Values": [UBUNTU_NAME]},
            {"Name": "state", "Values": ["available"]},
        ],
    )["Images"]
    if not images:
        raise SystemExit("No Ubuntu 24.04 AMI found in this region")
    return max(images, key=lambda image: image["CreationDate"])["ImageId"]


def ensure_security_group(ec2, ssh_cidr: str | None) -> str:
    """No inbound rules by default. All cluster traffic arrives through the
    WireGuard tunnel, which the node opens outbound to the hub."""
    found = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [config.SECURITY_GROUP_NAME]}]
    )
    if found["SecurityGroups"]:
        group_id = found["SecurityGroups"][0]["GroupId"]
    else:
        group_id = ec2.create_security_group(
            GroupName=config.SECURITY_GROUP_NAME,
            Description="Idlecloud burst workers, no inbound, traffic via WireGuard",
            TagSpecifications=[
                {"ResourceType": "security-group", "Tags": [config.PROJECT_TAG]}
            ],
        )["GroupId"]
        print(f"Created security group {group_id}")

    if ssh_cidr:  # only for debugging, remove it again afterwards
        try:
            ec2.authorize_security_group_ingress(
                GroupId=group_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 22,
                        "ToPort": 22,
                        "IpRanges": [{"CidrIp": ssh_cidr, "Description": "debug ssh"}],
                    }
                ],
            )
        except ec2.exceptions.ClientError as error:
            if "InvalidPermission.Duplicate" not in str(error):
                raise
    return group_id


def template_data(ami: str, group_id: str, key_name: str | None) -> dict:
    tags = [config.PROJECT_TAG, {"Key": "idlecloud/role", "Value": "burst-worker"}]
    data = {
        "ImageId": ami,
        "InstanceType": config.INSTANCE_TYPE,
        "SecurityGroupIds": [group_id],
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": config.DISK_GB,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            }
        ],
        # IMDSv2 only, and hop limit 1 so pods cannot reach the metadata service.
        # No IAM instance profile at all: workers never hold AWS keys.
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
            "HttpEndpoint": "enabled",
        },
        "CreditSpecification": {
            "CpuCredits": "standard"
        },  # avoids surprise charges on t3
        "InstanceInitiatedShutdownBehavior": "terminate",  # makes the lifetime safety net work
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
        ],
    }
    if key_name:
        data["KeyName"] = key_name
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key-name", help="EC2 key pair for SSH debugging, e.g. vockey in AWS Academy"
    )
    parser.add_argument(
        "--ssh-cidr", help="your public IP as x.x.x.x/32, opens SSH for debugging"
    )
    args = parser.parse_args()

    ec2 = ec2_client()
    data = template_data(
        latest_ubuntu_ami(ec2), ensure_security_group(ec2, args.ssh_cidr), args.key_name
    )

    existing = ec2.describe_launch_templates(
        Filters=[{"Name": "launch-template-name", "Values": [config.TEMPLATE_NAME]}]
    )["LaunchTemplates"]
    if existing:
        version = ec2.create_launch_template_version(
            LaunchTemplateName=config.TEMPLATE_NAME, LaunchTemplateData=data
        )["LaunchTemplateVersion"]
        ec2.modify_launch_template(
            LaunchTemplateName=config.TEMPLATE_NAME,
            DefaultVersion=str(version["VersionNumber"]),
        )
        print(f"Updated {config.TEMPLATE_NAME} to version {version['VersionNumber']}")
    else:
        template = ec2.create_launch_template(
            LaunchTemplateName=config.TEMPLATE_NAME,
            LaunchTemplateData=data,
            TagSpecifications=[
                {"ResourceType": "launch-template", "Tags": [config.PROJECT_TAG]}
            ],
        )["LaunchTemplate"]
        print(f"Created {config.TEMPLATE_NAME} ({template['LaunchTemplateId']})")


if __name__ == "__main__":
    main()
