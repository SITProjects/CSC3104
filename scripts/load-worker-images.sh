#!/usr/bin/env bash
# Builds the worker images and loads them into the local k3s worker nodes
# (k3-worker-1..3 in docker-compose.yml). They run their own containerd, so they
# can't see images in this machine's Docker. Run from anywhere once the stack is
# up, and again after changing worker/:
#
#   scripts/load-worker-images.sh
#
# Each image is tagged exactly as its job type's seed migration asks for, so the
# image and the database always agree. The test job type's image is pulled from
# Docker Hub by the nodes.
set -euo pipefail

cd "$(dirname "$0")/.."

# Prints the image a seed migration names. $1 file, $2 the image's prefix.
image_in() {
	local image
	image=$(grep -oE "$2[^']+" "$1" | head -1)
	[ -n "$image" ] || { echo "no $2 image in $1" >&2; exit 1; }
	printf '%s\n' "$image"
}

ocr=$(image_in shared/migrations/003_seed_ocr_job_type.sql "idlecloud/ocr:")
python=$(image_in shared/migrations/010_python_job_type.sql "ghcr.io/sitprojects/idlecloud-python:")

echo "== Building $ocr"
docker build -f worker/ocr-task/Dockerfile -t "$ocr" .
echo "== Building $python"
docker build -f worker/python-job/Dockerfile -t "$python" .

workers=$(docker compose ps --services --status running | grep '^k3-worker-' || true)
[ -n "$workers" ] || { echo "no k3-worker containers are running; start the stack first" >&2; exit 1; }

for worker in $workers; do
	for image in "$ocr" "$python"; do
		echo "== Loading $image into $worker"
		docker save "$image" | docker compose exec -T "$worker" ctr images import -
	done
done
echo "Done."
