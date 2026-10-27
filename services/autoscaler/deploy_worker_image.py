"""Select a published OCR image on the hub, after the application is deployed.

Copy the digest reference from the Publish worker images workflow:
    python3 -m services.autoscaler.deploy_worker_image --sudo-docker --image ghcr.io/OWNER/idlecloud-ocr@sha256:DIGEST

Docker verifies an anonymous linux/amd64 pull, then kubectl updates the existing
OCR job type through the coordinator container. Workers need no registry keys.
This command is independent of infra/k8s/deploy.sh and never runs on its own.
"""

import argparse
import re
import subprocess
import tempfile

PUBLIC_IMAGE = re.compile(
    r"ghcr\.io/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}"
)

# The container already has psycopg and DATABASE_URL. Keep the value out of the
# host's environment and pass the image as a SQL parameter, never shell text.
DATABASE_SCRIPT = """
import os
import sys
import psycopg

image = sys.argv[1]
try:
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as conn:
        conn.execute("SET LOCAL lock_timeout = '10s'")
        # Wait for in-flight job inserts, then block new ones for this short
        # transaction. Otherwise a new job could race the active-job check.
        conn.execute('LOCK TABLE jobs IN SHARE MODE')
        row = conn.execute(
            "SELECT image FROM job_types WHERE name = 'ocr' FOR UPDATE"
        ).fetchone()
        if row is None:
            raise SystemExit('OCR job type is missing. Deploy the coordinator and its migrations first.')
        if row[0] == image:
            message = 'OCR already uses ' + image
        else:
            active = conn.execute(
                "SELECT count(*) FROM jobs WHERE job_type = 'ocr' AND status IN ('PENDING', 'RUNNING')"
            ).fetchone()[0]
            if active:
                raise SystemExit(
                    f'{active} OCR job(s) are still active. Finish or cancel them before changing the worker image.'
                )
            conn.execute("UPDATE job_types SET image = %s WHERE name = 'ocr'", (image,))
            message = 'OCR worker image updated: ' + image
    print(message)
except psycopg.Error as error:
    raise SystemExit('Database operation failed: ' + str(error)) from error
"""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--image", required=True, help="published ghcr.io/...@sha256:... reference"
    )
    parser.add_argument(
        "--sudo-docker",
        action="store_true",
        help="use sudo for Docker while kubectl keeps your user configuration",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="verify the image without updating the database",
    )
    args = parser.parse_args(argv)
    if not PUBLIC_IMAGE.fullmatch(args.image):
        parser.error(
            "--image must be a ghcr.io/...@sha256:<64 hex characters> reference from the publishing workflow"
        )

    # A fresh configuration prevents a hub registry login from masking a private
    # package: campus and EC2 workers must be able to download without a login.
    with tempfile.TemporaryDirectory(prefix="idlecloud-anonymous-pull-") as config:
        docker = (["sudo"] if args.sudo_docker else []) + ["docker", "--config", config]
        try:
            subprocess.run(
                [*docker, "pull", "--platform", "linux/amd64", args.image],
                check=True,
                timeout=300,
            )
            platform = subprocess.run(
                [
                    *docker,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Os}}/{{.Architecture}}",
                    args.image,
                ],
                check=True,
                text=True,
                capture_output=True,
                timeout=30,
            ).stdout.strip()
        except subprocess.CalledProcessError as error:
            raise SystemExit(
                "Anonymous image pull failed. Check the workflow digest and ensure "
                "the GHCR package is Public. The database was not changed."
            ) from error
        if platform != "linux/amd64":
            raise SystemExit(
                "The image must support linux/amd64. The database was not changed."
            )
    if args.check_only:
        print("Anonymous linux/amd64 pull passed. The database was not changed.")
        return
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            "idlecloud-system",
            "exec",
            "-i",
            "deployment/coordinator",
            "-c",
            "coordinator",
            "--",
            "python",
            "-",
            args.image,
        ],
        input=DATABASE_SCRIPT,
        text=True,
        capture_output=True,
        check=True,
        timeout=45,
    )
    print(result.stdout.strip())


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as error:
        raise SystemExit(f"Required command is missing: {error.filename}") from error
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            error.stderr or f"Command failed with exit code {error.returncode}."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise SystemExit(
            "Command timed out. Check Docker and the cluster connection, then retry."
        ) from error
