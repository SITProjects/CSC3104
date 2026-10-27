#!/usr/bin/env bash
# Installs or updates the burst autoscaler and the Spot watcher on the hub.
# Run on the control plane from the repo root, after infra/k8s/deploy.sh:
#
#   bash services/autoscaler/deploy.sh             # build, then deploy
#   bash services/autoscaler/deploy.sh --no-build  # reuse the image in Docker and k3s
#
# Safe to run again. It checks member 1's Secrets first, then refreshes the EC2
# launch template, loads the image into k3s and applies both manifests.
# AWS calls use the hub's own role (LabRole), so no keys are needed here.
set -euo pipefail

NS=idlecloud-system
MANIFEST=services/autoscaler/k8s/autoscaler.yml
SPOTWATCH=services/autoscaler/spotwatch
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
[ -n "$IMAGE" ] && [ -n "$REGION" ] && [ -n "$K3S_VERSION" ] ||
  die "could not read the image, AWS_REGION or K3S_VERSION from $MANIFEST"

step "Checking what member 1 sets up"
secret_key() {
  kubectl -n "$NS" get secret "$1" -o jsonpath="{.data.$2}" 2>/dev/null | grep -q . ||
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
sudo k3s ctr images ls -q | grep -q "docker.io/$IMAGE" ||
  die "$IMAGE is not in k3s; run without --no-build"
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

step "Done"
echo "The autoscaler now launches EC2 workers when campus PCs cannot fit pending tasks."
echo "Follow it with: kubectl -n $NS logs -f deploy/idlecloud-autoscaler"
