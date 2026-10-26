#!/usr/bin/env bash
# Builds the OCR worker image and loads it into the local k3s worker nodes
# (k3-worker-1..3 in docker-compose.yml). They run their own containerd, so they
# can't see images in this machine's Docker, and idlecloud/ocr is not in any
# registry. Run from anywhere once the stack is up, and again after changing
# worker/ocr-task:
#
#   scripts/load-worker-images.sh
#
# The test job type's busybox image is pulled from Docker Hub by the nodes.
set -euo pipefail

cd "$(dirname "$0")/.."

# The tag the OCR job type asks for, so the image and the database always agree
image=$(grep -oE "idlecloud/ocr:[^']+" shared/migrations/003_seed_ocr_job_type.sql | head -1)
[ -n "$image" ] || { echo "no idlecloud/ocr image in 003_seed_ocr_job_type.sql" >&2; exit 1; }

echo "== Building $image"
docker build -f worker/ocr-task/Dockerfile -t "$image" .

workers=$(docker compose ps --services --status running | grep '^k3-worker-' || true)
[ -n "$workers" ] || { echo "no k3-worker containers are running; start the stack first" >&2; exit 1; }

for worker in $workers; do
	echo "== Loading $image into $worker"
	docker save "$image" | docker compose exec -T "$worker" ctr images import -
done
echo "Done."
