"""Deploy the page pipeline: build the image, push it to ECR, create or update
both Lambdas, and make uploads of jobs/{job_id}/input.pdf start the splitter.

Run from the repo root, with Docker running:

    python -m serverless.deploy --bucket csc3104-idle-compute

Safe to run again after a code change. It updates what already exists and
keeps the current callback settings unless new ones are given.

Credentials come from runtime/aws-credentials (the same file as the autoscaler's
launch command). Paste the AWS Academy "AWS CLI" block there. Without that file
boto3's normal lookup is used. The bucket must already exist (member 1 owns it).
Its region decides where the Lambdas go, because S3 only triggers a Lambda in
its own region.

The callback goes to WEBHOOK_URL, signed with HMAC_SECRET, both read from the
environment. Set them to change the callback. Without WEBHOOK_URL each function
keeps the callback settings it already has, and a new function only logs its
callbacks.
"""

import argparse
import base64
import configparser
import json
import os
import subprocess
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BUILD_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BUILD_DIR.parent / "runtime" / "aws-credentials"

REPO_NAME = "idlecloud-serverless"
SPLIT_FN = "idlecloud-split"
RENDER_FN = "idlecloud-render"
TRIGGER_ID = "idlecloud-split-on-upload"
OWN_ROLE = "idlecloud-lambda-role"
# Lambda settings this script owns only when WEBHOOK_URL is given
CALLBACK_KEYS = ("WEBHOOK_URL", "HMAC_SECRET", "ALLOW_NO_WEBHOOK")
LOG_ONLY = {"ALLOW_NO_WEBHOOK": "1"}

# name: (handler, memory MB, timeout s, /tmp MB). More memory also means more
# CPU, which speeds up rendering. The splitter only counts pages and fans out.
FUNCTIONS = {
    SPLIT_FN: ("serverless.splitter.handler.split_handler", 1024, 60, 512),
    RENDER_FN: ("serverless.renderer.handler.render_handler", 2048, 180, 1024),
}


def aws_session(region: str | None = None) -> boto3.Session:
    """Keys from runtime/aws-credentials when it exists. Passing them explicitly
    beats the fake MinIO keys the dev container puts in the environment."""
    if not CREDENTIALS_FILE.exists():
        return boto3.Session(region_name=region)
    parser = configparser.ConfigParser()
    parser.read(CREDENTIALS_FILE)
    if not parser.sections():
        raise SystemExit("runtime/aws-credentials is empty or not in [default] format")
    keys = parser[parser.sections()[0]]
    return boto3.Session(
        aws_access_key_id=keys.get("aws_access_key_id"),
        aws_secret_access_key=keys.get("aws_secret_access_key"),
        aws_session_token=keys.get("aws_session_token"),
        region_name=region,
    )


def bucket_region(s3, bucket: str) -> str:
    try:
        location = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
    except ClientError as error:
        raise SystemExit(
            f"Cannot read bucket {bucket}: {error}\n"
            "Check runtime/aws-credentials (lab keys expire with the session)."
        ) from error
    return location or "us-east-1"  # AWS reports us-east-1 as no constraint


def run(*cmd: str, stdin: str | None = None) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True, input=stdin, text=True)


def build_and_push(ecr) -> str:
    try:
        ecr.create_repository(repositoryName=REPO_NAME)
    except ecr.exceptions.RepositoryAlreadyExistsException:
        pass
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    registry = auth["proxyEndpoint"].removeprefix("https://")
    run(
        "docker",
        "login",
        "--username",
        user,
        "--password-stdin",
        registry,
        stdin=password,
    )

    tag = time.strftime("%Y%m%d-%H%M%S")  # a new tag per deploy, so Lambda pulls it
    uri = f"{registry}/{REPO_NAME}:{tag}"
    # Lambda rejects the multi-arch manifest buildx adds by default
    run(
        "docker", "build", "--platform", "linux/amd64", "--provenance=false",
        "-t", uri, str(BUILD_DIR),
    )  # fmt: skip
    run("docker", "push", uri)
    return uri


def lambda_role(iam, account: str, region: str, bucket: str) -> str:
    """AWS Academy blocks creating roles but gives LabRole, so use it. A normal
    account gets a small role that can only log, use this bucket's jobs/ and
    start the renderer. Its policies are applied on every run, so a new bucket
    or region, or a run cut short, is put right next time."""
    try:
        return iam.get_role(RoleName="LabRole")["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        pass
    except ClientError:
        # AWS Academy can deny reading roles. LabRole is always there.
        return f"arn:aws:iam::{account}:role/LabRole"
    created = False
    try:
        arn = iam.get_role(RoleName=OWN_ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = _create_role(iam)
        created = True
    # Both calls replace what is there, so running them again is harmless
    iam.attach_role_policy(
        RoleName=OWN_ROLE,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    iam.put_role_policy(
        RoleName=OWN_ROLE,
        PolicyName="idlecloud-pipeline",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:PutObject"],
                        "Resource": f"arn:aws:s3:::{bucket}/jobs/*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": "lambda:InvokeFunction",
                        "Resource": f"arn:aws:lambda:{region}:{account}:function:{RENDER_FN}",
                    },
                ],
            }
        ),
    )
    if created:
        print(f"Created IAM role {OWN_ROLE}, waiting for AWS to pick it up")
        time.sleep(15)  # a new role takes a few seconds before Lambda accepts it
    return arn


def _create_role(iam) -> str:
    return iam.create_role(
        RoleName=OWN_ROLE,
        AssumeRolePolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        ),
    )["Role"]["Arn"]


def upsert_function(lam, name: str, image_uri: str, role: str, env: dict) -> str:
    """env replaces all the function's variables (that is how Lambda works), so
    callback settings not given in env are carried over from the function."""
    handler, memory, timeout, tmp_mb = FUNCTIONS[name]
    config = {
        "Role": role,
        "MemorySize": memory,
        "Timeout": timeout,
        "ImageConfig": {"Command": [handler]},
        "EphemeralStorage": {"Size": tmp_mb},
    }
    try:
        current = lam.get_function(FunctionName=name)["Configuration"]
    except lam.exceptions.ResourceNotFoundException:
        config["Environment"] = {"Variables": {**LOG_ONLY, **env}}
        lam.create_function(
            FunctionName=name,
            PackageType="Image",
            Code={"ImageUri": image_uri},
            Architectures=["x86_64"],
            **config,
        )
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
        print(f"Created {name}")
    else:
        old = current.get("Environment", {}).get("Variables", {})
        kept = {k: v for k, v in old.items() if k in CALLBACK_KEYS}
        config["Environment"] = {"Variables": {**kept, **env}}
        lam.update_function_code(FunctionName=name, ImageUri=image_uri)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_configuration(FunctionName=name, **config)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        print(f"Updated {name}")
    # Async calls (S3 events, the splitter's fan-out) keep Lambda's default of
    # two retries. Not set here, so settings added in the console stay.
    return lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]


def with_split_trigger(current: dict, split_arn: str) -> dict:
    """The bucket's notification settings with our trigger added or replaced.
    put_bucket_notification_configuration replaces everything, so every other
    setting (other members' triggers, queues, topics) is kept as it was."""
    config = {k: v for k, v in current.items() if k != "ResponseMetadata"}
    others = [
        c
        for c in config.get("LambdaFunctionConfigurations", [])
        if c.get("Id") != TRIGGER_ID
    ]
    config["LambdaFunctionConfigurations"] = others + [
        {
            "Id": TRIGGER_ID,
            "LambdaFunctionArn": split_arn,
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {
                "Key": {
                    "FilterRules": [
                        {"Name": "prefix", "Value": "jobs/"},
                        {"Name": "suffix", "Value": "/input.pdf"},
                    ]
                }
            },
        }
    ]
    return config


def connect_trigger(lam, s3, bucket: str, account: str, split_arn: str) -> None:
    try:
        lam.add_permission(
            FunctionName=SPLIT_FN,
            StatementId=f"allow-s3-{bucket}",
            Action="lambda:InvokeFunction",
            Principal="s3.amazonaws.com",
            SourceArn=f"arn:aws:s3:::{bucket}",
            SourceAccount=account,
        )
    except lam.exceptions.ResourceConflictException:
        pass  # already allowed
    current = s3.get_bucket_notification_configuration(Bucket=bucket)
    s3.put_bucket_notification_configuration(
        Bucket=bucket, NotificationConfiguration=with_split_trigger(current, split_arn)
    )
    print(f"Uploads to s3://{bucket}/jobs/<job_id>/input.pdf now start {SPLIT_FN}")


def callback_env() -> dict:
    """The callback settings to write, or {} to keep each function's own."""
    url = os.environ.get("WEBHOOK_URL", "")
    secret = os.environ.get("HMAC_SECRET", "")
    if not url:
        print("WEBHOOK_URL not set: keeping the current callback settings")
        return {}
    if not secret:
        raise SystemExit("Set HMAC_SECRET too when WEBHOOK_URL is set")
    return {"WEBHOOK_URL": url, "HMAC_SECRET": secret, "ALLOW_NO_WEBHOOK": "0"}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # Required on purpose: the dev container's S3_BUCKET names the local MinIO
    # bucket, which must never be used as a real AWS bucket name
    parser.add_argument(
        "--bucket", required=True, help="existing bucket the web app uploads PDFs to"
    )
    args = parser.parse_args()
    env = callback_env()

    s3 = aws_session().client("s3")
    region = bucket_region(s3, args.bucket)
    session = aws_session(region)
    account = session.client("sts").get_caller_identity()["Account"]
    s3, lam = session.client("s3"), session.client("lambda")
    print(f"Deploying to {region} for bucket {args.bucket}")

    role = lambda_role(session.client("iam"), account, region, args.bucket)
    image_uri = build_and_push(session.client("ecr"))
    upsert_function(lam, RENDER_FN, image_uri, role, {**env, "RENDER_DPI": "300"})
    # The splitter also sends a callback when it rejects a PDF
    split_arn = upsert_function(
        lam, SPLIT_FN, image_uri, role, {**env, "RENDER_FUNCTION_NAME": RENDER_FN}
    )
    connect_trigger(lam, s3, args.bucket, account, split_arn)
    print(f"Done. Image {image_uri}")


if __name__ == "__main__":
    try:
        main()
    except ClientError as error:
        raise SystemExit(f"AWS error: {error}") from error
