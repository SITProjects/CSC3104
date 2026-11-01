"""Deploy the page pipeline: build the image, push it to ECR, create or update
both Lambdas, and make uploads of jobs/{job_id}/input.pdf start the splitter.
Also set seven-day S3 expiry for jobs/ and pending-index/ and one-day cleanup
of incomplete multipart uploads, keeping other lifecycle rules.

Run from the repo root, with Docker running. Point the callback at the web
app's public HTTPS address, such as its CloudFront domain:

    python -m serverless.deploy --bucket csc3104-idle-compute \
        --web-url https://<web app domain>

Safe to run again after a code change. It updates what already exists and
keeps the current callback settings unless new ones are given.

Credentials come from runtime/aws-credentials (the same file as the autoscaler's
launch command). Paste the AWS Academy "AWS CLI" block there. Without that file
boto3's normal lookup is used. The bucket must already exist (member 1 owns it).
Its region decides where the Lambdas go, because S3 only triggers a Lambda in
its own region.

The callback goes to <web-url>/hooks/pages-ready, signed with HMAC_SECRET. Without
HMAC_SECRET in the environment, the secret is read from the cluster's
idlecloud-web Secret with kubectl, the one member 1's deploy.sh gives the web app,
so the two always match. The URL must be https:// unless --allow-http-webhook is
given (HMAC signs the body but does not encrypt it). Without --web-url each
function keeps the callback settings it already has, and a new function only
logs its callbacks.
"""

import argparse
import base64
import configparser
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BUILD_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BUILD_DIR.parent / "runtime" / "aws-credentials"

REPO_NAME = "idlecloud-serverless"
SPLIT_FN = "idlecloud-split"
RENDER_FN = "idlecloud-render"
TRIGGER_ID = "idlecloud-split-on-upload"
EXPIRY_RULE_PREFIX = "idlecloud-expire-"
EXPIRING_PREFIXES = ("jobs/", "pending-index/")
# A backstop for leftover files, measured from upload, not job completion.
LEFTOVER_DAYS = 7
OWN_ROLE = "idlecloud-lambda-role"
# Lambda settings this script writes only when --web-url is given
CALLBACK_KEYS = ("WEBHOOK_URL", "HMAC_SECRET", "ALLOW_NO_WEBHOOK")
LOG_ONLY = {"ALLOW_NO_WEBHOOK": "1"}
# create_function fails with this while a new IAM role is still spreading
ROLE_NOT_READY = "cannot be assumed"
ROLE_RETRY_DELAYS_S = (5, 10, 15, 20, 25)
# The web app's callback route, and where member 1's deploy.sh keeps its secret
HOOK_PATH = "/hooks/pages-ready"
WEB_SECRET = ("idlecloud-system", "idlecloud-web", "hmac-secret")

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
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code not in ("AccessDenied", "AccessDeniedException"):
            raise  # expired keys and the like must stop the deploy
        # AWS Academy denies reading roles but always has LabRole. This is an
        # assumption, so say so. A wrong guess fails at create_function.
        arn = f"arn:aws:iam::{account}:role/LabRole"
        print(f"Cannot read IAM roles ({code}), assuming AWS Academy's {arn}")
        return arn
    try:
        arn = iam.get_role(RoleName=OWN_ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = _create_role(iam)
        print(f"Created IAM role {OWN_ROLE}")
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


def create_function(lam, **params) -> None:
    """create_function, retried while a new role is not yet usable by Lambda.
    IAM changes take a while to spread, and no fixed wait is long enough."""
    for delay in (*ROLE_RETRY_DELAYS_S, None):
        try:
            lam.create_function(**params)
            return
        except lam.exceptions.InvalidParameterValueException as error:
            if delay is None or ROLE_NOT_READY not in str(error):
                raise
            print(f"Role not usable by Lambda yet, retrying in {delay} s")
            time.sleep(delay)


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
        create_function(
            lam,
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


def with_expiry_rules(rules: list[dict]) -> list[dict]:
    """The bucket's lifecycle rules with our expiry rules added or replaced.
    put_bucket_lifecycle_configuration replaces every rule, so other rules
    are kept as they were."""
    others = [
        rule for rule in rules if not rule.get("ID", "").startswith(EXPIRY_RULE_PREFIX)
    ]
    return others + [
        {
            "ID": EXPIRY_RULE_PREFIX + prefix.rstrip("/"),
            "Status": "Enabled",
            "Filter": {"Prefix": prefix},
            "Expiration": {"Days": LEFTOVER_DAYS},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
        }
        for prefix in EXPIRING_PREFIXES
    ]


def expire_leftover_files(s3, bucket: str) -> None:
    try:
        try:
            rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
        except ClientError as error:
            if error.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
                raise
            rules = []
        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration={"Rules": with_expiry_rules(rules)},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "AccessDenied":
            raise
        print(
            "WARNING: S3 lifecycle rules were not set (AccessDenied). "
            "Member 1 can add them in the S3 console."
        )
        return
    print(
        f"Objects under jobs/ and pending-index/ now expire after {LEFTOVER_DAYS} "
        "days. Incomplete multipart uploads are aborted after 1 day."
    )


def cluster_secret() -> str:
    """The web app's callback secret, read from the cluster with kubectl."""
    namespace, name, key = WEB_SECRET
    try:
        found = subprocess.run(
            ["kubectl", "-n", namespace, "get", "secret", name,
             "-o", f"jsonpath={{.data.{key}}}"],
            capture_output=True, text=True, check=True, timeout=20,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit(
            f"Could not read the {name} Secret with kubectl ({error}). Run this on "
            "the control plane, or set HMAC_SECRET to the web app's value."
        ) from error
    if not found:
        raise SystemExit(
            f"The {name} Secret has no {key}. Run infra/k8s/deploy.sh first."
        )
    return base64.b64decode(found).decode()


def callback_env(web_url: str | None, allow_http: bool = False) -> dict:
    """The callback settings to write, or {} to keep each function's own."""
    if not web_url:
        if os.environ.get("WEBHOOK_URL"):
            # Older instructions exported WEBHOOK_URL. Ignoring it would quietly
            # deploy with the wrong callback, so stop and point to the flag.
            raise SystemExit(
                "WEBHOOK_URL is no longer read. Pass the web app's address with "
                "--web-url instead, or unset WEBHOOK_URL to keep the current settings."
            )
        print("--web-url not given: keeping the current callback settings")
        return {}
    url = web_url.rstrip("/")
    if not url.endswith(HOOK_PATH):
        url += HOOK_PATH
    if not url.startswith("https://") and not allow_http:
        raise SystemExit(
            "--web-url must start with https://. Page details would travel "
            "unencrypted. Add --allow-http-webhook only for a test endpoint."
        )
    secret = os.environ.get("HMAC_SECRET") or cluster_secret()
    return {"WEBHOOK_URL": url, "HMAC_SECRET": secret, "ALLOW_NO_WEBHOOK": "0"}


def warn_if_unreachable(webhook_url: str) -> None:
    """A quick look at the web app's /healthz from here. Only a warning: this
    machine and AWS Lambda may see the network differently."""
    health = webhook_url.removesuffix(HOOK_PATH) + "/healthz"
    try:
        with urllib.request.urlopen(health, timeout=5) as response:
            print(f"Web app answers at {health} ({response.status})")
    except (urllib.error.URLError, OSError) as error:
        print(
            f"WARNING: {health} did not answer ({error}). The Lambda must reach "
            "this address from the internet: check the ingress and that the "
            "security group allows the port."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # Required on purpose: the dev container's S3_BUCKET names the local MinIO
    # bucket, which must never be used as a real AWS bucket name
    parser.add_argument(
        "--bucket", required=True, help="existing bucket the web app uploads PDFs to"
    )
    parser.add_argument(
        "--web-url",
        help="the web app's public address, e.g. https://<cloudfront domain>. "
        "Omit to keep the current callback settings",
    )
    parser.add_argument(
        "--allow-http-webhook",
        action="store_true",
        help="accept an http:// --web-url (test endpoints only)",
    )
    args = parser.parse_args()
    env = callback_env(args.web_url, args.allow_http_webhook)
    if env:
        warn_if_unreachable(env["WEBHOOK_URL"])

    s3 = aws_session().client("s3")
    region = bucket_region(s3, args.bucket)
    session = aws_session(region)
    account = session.client("sts").get_caller_identity()["Account"]
    s3, lam = session.client("s3"), session.client("lambda")
    print(f"Deploying to {region} for bucket {args.bucket}")

    role = lambda_role(session.client("iam"), account, region, args.bucket)
    print(f"Lambdas run as {role}")
    image_uri = build_and_push(session.client("ecr"))
    upsert_function(lam, RENDER_FN, image_uri, role, {**env, "RENDER_DPI": "300"})
    # The splitter also sends a callback when it rejects a PDF
    split_arn = upsert_function(
        lam, SPLIT_FN, image_uri, role, {**env, "RENDER_FUNCTION_NAME": RENDER_FN}
    )
    connect_trigger(lam, s3, args.bucket, account, split_arn)
    expire_leftover_files(s3, args.bucket)
    print(f"Done. Image {image_uri}")


if __name__ == "__main__":
    try:
        main()
    except ClientError as error:
        raise SystemExit(f"AWS error: {error}") from error
