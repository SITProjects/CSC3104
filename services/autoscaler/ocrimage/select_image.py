"""Point the OCR job type at the newest OCR image published from main.

Runs as a one-off Kubernetes Job at the end of services/autoscaler/deploy.sh,
in the coordinator image (it already has psycopg and DATABASE_URL).

It asks GHCR for the digest behind the main tag without logging in. That is the
same anonymous download every campus and EC2 worker does, so success proves
they can pull the image. It then pins that exact digest in the database, so
every replica of a task runs identical code for voting. It leaves the image
alone while OCR jobs are still pending or running.

Exit 0: selected, already selected, or kept because OCR jobs are active.
Exit 1: the image cannot be downloaded without a login, or the database failed.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

import psycopg

REPO = os.environ.get("OCR_IMAGE_REPO", "sitprojects/idlecloud-ocr")
TAG = "main"
REGISTRY = "https://ghcr.io"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# An image any worker can download: a GHCR digest, or a GHCR tag such as
# migration 013's ghcr.io/sitprojects/idlecloud-ocr:main. Anything else, such as
# the seed's idlecloud/ocr:0.1.0, only exists where it was loaded.
PUBLISHED = re.compile(r"ghcr\.io/[a-z0-9._/-]+(@sha256:[0-9a-f]{64}|:[\w][\w.-]*)")
# An image index (several platforms) or a single image, in OCI or Docker format
ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


def fetch(path: str, token: str | None = None, accept: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(REGISTRY + path, headers=headers)
    return urllib.request.urlopen(request, timeout=20)


def runs_on_amd64(manifest: dict, token: str) -> bool:
    """Workers are linux/amd64 PCs and EC2 instances."""
    if "manifests" in manifest:
        return any(
            entry.get("platform", {}).get("os") == "linux"
            and entry.get("platform", {}).get("architecture") == "amd64"
            for entry in manifest["manifests"]
        )
    config = manifest["config"]["digest"]
    with fetch(f"/v2/{REPO}/blobs/{config}", token) as response:
        image = json.load(response)
    return image.get("os") == "linux" and image.get("architecture") == "amd64"


def published_image() -> str:
    """ghcr.io/<repo>@sha256:..., found exactly as a worker without a login would."""
    try:
        with fetch(f"/token?scope=repository:{REPO}:pull") as response:
            token = json.load(response)["token"]
        with fetch(f"/v2/{REPO}/manifests/{TAG}", token, ACCEPT) as response:
            digest = response.headers.get("Docker-Content-Digest", "")
            manifest = json.load(response)
        if not runs_on_amd64(manifest, token):
            sys.exit(f"ghcr.io/{REPO}:{TAG} has no linux/amd64 image. Nothing changed.")
    except urllib.error.HTTPError as error:
        sys.exit(
            f"Workers cannot download ghcr.io/{REPO}:{TAG} without a login "
            f"(HTTP {error.code}). Check that the Publish worker images workflow "
            "passed and that the package is Public on GitHub. Nothing changed."
        )
    except (urllib.error.URLError, OSError) as error:
        sys.exit(f"Could not reach ghcr.io ({error}). Nothing changed.")
    if not DIGEST.fullmatch(digest):
        sys.exit(f"GHCR returned no valid digest for {TAG}. Nothing changed.")
    return f"ghcr.io/{REPO}@{digest}"


def select(image: str) -> str:
    """Pin the image unless OCR jobs are active. Returns what happened."""
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
        conn.execute("SET LOCAL lock_timeout = '10s'")
        # Wait for in-flight job inserts, then hold off new ones for this short
        # transaction, so no job can start between the check and the update.
        conn.execute("LOCK TABLE jobs IN SHARE MODE")
        row = conn.execute(
            "SELECT image FROM job_types WHERE name = 'ocr' FOR UPDATE"
        ).fetchone()
        if row is None:
            sys.exit("The ocr job type is missing. Run infra/k8s/deploy.sh first.")
        if row[0] == image:
            return f"OCR already uses {image}"
        active = conn.execute(
            "SELECT count(*) FROM jobs WHERE job_type = 'ocr'"
            " AND status IN ('PENDING', 'RUNNING')"
        ).fetchone()[0]
        if active and not PUBLISHED.fullmatch(row[0]):
            # Keeping it would report success while fresh workers cannot start
            # these jobs. Switching mid-job could mix builds within one vote.
            sys.exit(
                f"{active} OCR job(s) are queued on {row[0]}, which fresh workers "
                "cannot download. Cancel them, then run deploy.sh again. "
                "Nothing changed."
            )
        if active:
            return (
                f"Kept {row[0]}: {active} OCR job(s) are still active. "
                "Run deploy.sh again once they finish to switch to the new image."
            )
        conn.execute("UPDATE job_types SET image = %s WHERE name = 'ocr'", (image,))
        return f"OCR now uses {image} (was {row[0]})"


def main() -> None:
    image = published_image()
    try:
        print(select(image))
    except psycopg.Error as error:
        sys.exit(f"Database update failed: {error}. Nothing changed.")


if __name__ == "__main__":
    main()
