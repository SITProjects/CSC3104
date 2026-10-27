#!/usr/bin/env bash
# Installs or updates the burst autoscaler and the Spot watcher on the hub.
# Run on the control plane from the repo root, after infra/k8s/deploy.sh:
#
#   bash services/autoscaler/deploy.sh             # build, then deploy
#   bash services/autoscaler/deploy.sh --no-build  # reuse the image in Docker and k3s
#
# Safe to run again. It checks member 1's Secrets first, then refreshes the EC2
# launch template, loads the image into k3s and applies both manifests. Last, a
# one-off Job points OCR at the newest image published from main, so workers
# can download it. AWS calls use the hub's own role (LabRole), so no keys are
# needed here.
set -euo pipefail

NS=idlecloud-system
MANIFEST=services/autoscaler/k8s/autoscaler.yml
SPOTWATCH=services/autoscaler/spotwatch
OCR_JOB_DIR=services/autoscaler/ocrimage
OCR_JOB=select-ocr-image
BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

die() { echo "deploy: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

[ -f "$MANIFEST" ] || die "run from the repo root"
kubectl get nodes >/dev/null 2>&1 || die "kubectl cannot reach the cluster (check ~/.kube/config)"

# The manifest is the one place for these, so the template and the pod agree
env_in_manifest() {
  grep -A1 -E "name: $1\$" "$MANIFEST" | grep -oE 'value: *[^ #]+' | head -1 | sed 's/value: *//; s/"//g'
}
IMAGE=$(grep -oE "idlecloud/autoscaler:[^[:space:]]+" "$MANIFEST" | head -1)
REGION=$(env_in_manifest AWS_REGION)
K3S_VERSION=$(env_in_manifest K3S_VERSION)
# The OCR Job reuses member 1's coordinator image
COORDINATOR_IMAGE=$(grep -oE "idlecloud/coordinator:[^[:space:]]+" infra/k8s/base/coordinator.yml | head -1)
[ -n "$IMAGE" ] && [ -n "$REGION" ] && [ -n "$K3S_VERSION" ] ||
  die "could not read the image, AWS_REGION or K3S_VERSION from $MANIFEST"
[ -n "$COORDINATOR_IMAGE" ] || die "could not read the coordinator image from infra/k8s/base/coordinator.yml"

step "Checking what member 1 sets up"
secret_key() {
  [ -n "$(kubectl -n "$NS" get secret "$1" -o jsonpath="{.data.$2}" 2>/dev/null)" ] ||
    die "Secret $1 has no key $2. $3"
}
secret_key idlecloud-db DATABASE_URL "Run infra/k8s/deploy.sh first."
secret_key k3s-agent-token token "Create it from the server's token (see services/autoscaler/README.md)."
for slot in 200 201 202 203 204; do
  secret_key burst-wg-slots "slot-$slot" "Each burst slot needs its full wg0.conf (see services/autoscaler/README.md)."
done
CONTROL=$(kubectl get nodes -l idlecloud/role=control -o jsonpath='{.items[*].metadata.name}' || true)
CONTROL=${CONTROL%% *}
[ -n "$CONTROL" ] || die "no node has the label idlecloud/role=control, so the autoscaler cannot be placed"
SERVER_VERSION=$(kubectl get node "$CONTROL" -o jsonpath='{.status.nodeInfo.kubeletVersion}')
[ "$SERVER_VERSION" = "$K3S_VERSION" ] ||
  die "K3S_VERSION in $MANIFEST is $K3S_VERSION but the server runs $SERVER_VERSION. Burst workers would fail to join. Update the manifest."
echo "Secrets present, control node $CONTROL, K3s $SERVER_VERSION, region $REGION"

if [ "$BUILD" = 1 ]; then
  step "Building $IMAGE and loading it into k3s"
  sudo docker build -f services/autoscaler/Dockerfile -t "$IMAGE" .
  sudo docker save "$IMAGE" | sudo k3s ctr images import -
fi
# grep reads the whole list (no -q), so ctr is never cut off mid-write. Under
# pipefail that SIGPIPE would fail the check even when the image is there.
sudo k3s ctr images ls -q | grep -Fx "docker.io/$IMAGE" >/dev/null ||
  die "$IMAGE is not in k3s; run without --no-build"
sudo k3s ctr images ls -q | grep -Fx "docker.io/$COORDINATOR_IMAGE" >/dev/null ||
  die "$COORDINATOR_IMAGE is not in k3s; run infra/k8s/deploy.sh first"
sudo docker image inspect "$IMAGE" >/dev/null 2>&1 ||
  die "$IMAGE is not in Docker; run without --no-build to rebuild it for the launch-template step"

step "EC2 launch template in $REGION"
# The image already has the code and boto3. Host networking lets it use the
# hub's instance role, like the autoscaler pod does.
sudo docker run --rm --network host \
  -e AWS_USE_DEFAULT_CREDENTIALS=1 -e AWS_REGION="$REGION" \
  "$IMAGE" python -m services.autoscaler.burst.launch_template ||
  die "could not create the launch template. Check that LabRole allows EC2 launch templates and security groups in $REGION"

step "Applying the autoscaler and the Spot watcher"
kubectl apply -f "$MANIFEST"
kubectl apply -k "$SPOTWATCH"
# A rebuilt image under the same tag is not picked up without a restart
[ "$BUILD" = 1 ] && kubectl -n "$NS" rollout restart deployment/idlecloud-autoscaler
kubectl -n "$NS" rollout status deployment/idlecloud-autoscaler --timeout=180s ||
  die "the autoscaler did not start; see kubectl -n $NS logs deploy/idlecloud-autoscaler"

step "Selecting the newest published OCR image"
# A Job cannot be changed once created, so remove the last run first
kubectl -n "$NS" delete job "$OCR_JOB" --ignore-not-found >/dev/null
kubectl kustomize "$OCR_JOB_DIR" | sed "s#COORDINATOR_IMAGE#$COORDINATOR_IMAGE#" | kubectl apply -f -
result=""
for _ in $(seq 75); do  # the Job gives up after 120 s
  result=$(kubectl -n "$NS" get job "$OCR_JOB" -o jsonpath='{.status.succeeded}/{.status.failed}')
  [ "$result" != "/" ] && break
  sleep 2
done
kubectl -n "$NS" logs "job/$OCR_JOB" 2>/dev/null || true
[ "$result" = "1/" ] ||
  die "the OCR image was not selected (see the message above). The autoscaler itself is installed. Fix it and run this script again."

step "Done"
echo "The autoscaler now launches EC2 workers when campus PCs cannot fit pending tasks."
echo "Follow it with: kubectl -n $NS logs -f deploy/idlecloud-autoscaler"
