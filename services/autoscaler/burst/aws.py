"""AWS Academy credentials for the burst scripts.

Uses the same runtime/aws-credentials file as serverless/ (see its README).
Paste the block from the Learner Lab's "AWS Details" > "AWS CLI: Show" there.
Passing the keys explicitly beats the fake MinIO keys the dev container sets.

Inside the cluster there is no such file. The autoscaler Deployment sets
AWS_USE_DEFAULT_CREDENTIALS=1, so boto3 uses its normal lookup instead, such as
the control-plane EC2 instance's LabRole.
"""

import configparser
import os

import boto3

from services.autoscaler.burst import config

CREDENTIALS_FILE = config.REPO_ROOT / "runtime" / "aws-credentials"


def ec2_client():
    if (
        not CREDENTIALS_FILE.exists()
        and os.environ.get("AWS_USE_DEFAULT_CREDENTIALS") == "1"
    ):
        return boto3.Session(region_name=config.REGION).client("ec2")
    if not CREDENTIALS_FILE.exists():
        raise SystemExit(
            "Missing runtime/aws-credentials. Start the AWS Academy lab, open AWS Details, "
            "click Show next to AWS CLI, and paste the whole block there."
        )
    parser = configparser.ConfigParser()
    parser.read(CREDENTIALS_FILE)
    if not parser.sections():
        raise SystemExit(
            "runtime/aws-credentials is empty or not in the [default] format"
        )
    section = parser[parser.sections()[0]]
    session = boto3.Session(
        aws_access_key_id=section.get("aws_access_key_id"),
        aws_secret_access_key=section.get("aws_secret_access_key"),
        aws_session_token=section.get("aws_session_token"),
        region_name=config.REGION,
    )
    return session.client("ec2")
