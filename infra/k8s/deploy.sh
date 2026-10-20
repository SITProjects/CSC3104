#!/usr/bin/env bash
# Deploy or update everything that runs on the AWS control plane: namespaces,
# the database Secret, Postgres and the coordinator. Run on the EC2 hub from the
# repo root, after k3s is installed (runbook steps 0 to 2):
#
#   infra/k8s/deploy.sh            build the coordinator image, then apply
#   infra/k8s/deploy.sh --no-build apply only (image already imported)
#
# Safe to run again. It never replaces an existing database password: Postgres
# only reads it when the data folder is first created, so a new one would lock
# the coordinator out.
set -euo pipefail

NS=idlecloud-system
OVERLAY=infra/k8s/overlays/aws
BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

die() { echo "deploy: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

[ -f "$OVERLAY/kustomization.yaml" ] || die "run from the repo root"
kubectl get nodes >/dev/null 2>&1 || die "kubectl cannot reach the cluster (check ~/.kube/config)"
if grep -q REPLACE_WITH_BUCKET_NAME "$OVERLAY/coordinator-s3.yml"; then
  die "set S3_BUCKET in $OVERLAY/coordinator-s3.yml first"
fi

# The tag coordinator.yml asks for, so the build and the manifest always agree
IMAGE=$(grep -oE 'idlecloud/coordinator:[^[:space:]]+' infra/k8s/base/coordinator.yml | head -1)
[ -n "$IMAGE" ] || die "no idlecloud/coordinator image in infra/k8s/base/coordinator.yml"

if [ "$BUILD" = 1 ]; then
  step "Building $IMAGE and loading it into k3s"
  sudo docker build -f services/coordinator/Dockerfile -t "$IMAGE" .
  sudo docker save "$IMAGE" | sudo k3s ctr images import -
fi
sudo k3s ctr images ls -q | grep -q "docker.io/$IMAGE" ||
  die "$IMAGE is not in k3s; run without --no-build"

step "Namespaces"
kubectl apply -f infra/k8s/base/namespaces.yml

step "Database Secret"
if kubectl -n "$NS" get secret idlecloud-db -o jsonpath='{.data.POSTGRES_PASSWORD}' 2>/dev/null | grep -q . &&
   kubectl -n "$NS" get secret idlecloud-db -o jsonpath='{.data.DATABASE_URL}' 2>/dev/null | grep -q .; then
  echo "idlecloud-db already has both keys; keeping it"
else
  # Reuse a password Postgres already knows: the old separate Secret, then the
  # URL in an older idlecloud-db. Only a brand-new cluster gets a fresh one.
  PW=$(kubectl -n "$NS" get secret idlecloud-postgres -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)
  if [ -z "$PW" ]; then
    URL=$(kubectl -n "$NS" get secret idlecloud-db -o jsonpath='{.data.DATABASE_URL}' 2>/dev/null | base64 -d || true)
    PW=$(printf '%s' "$URL" | sed -nE 's#^postgresql://[^:]+:([^@]+)@.*#\1#p')
  fi
  if [ -z "$PW" ]; then
    if kubectl -n "$NS" get pvc data-postgres-0 >/dev/null 2>&1; then
      die "Postgres data exists but no password was found in any Secret; recreate idlecloud-db by hand"
    fi
    PW=$(openssl rand -hex 16)
    echo "new cluster: generated a database password"
  else
    echo "reusing the existing database password"
  fi
  kubectl -n "$NS" create secret generic idlecloud-db \
    --from-literal=POSTGRES_PASSWORD="$PW" \
    --from-literal=DATABASE_URL="postgresql://admin:$PW@postgres.$NS.svc.cluster.local:5432/csc3104" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

step "Applying $OVERLAY"
kubectl apply -k "$OVERLAY"

step "Waiting for Postgres and the coordinator"
kubectl -n "$NS" rollout status statefulset/postgres --timeout=180s
# A rebuilt image under the same tag is not picked up without a restart
[ "$BUILD" = 1 ] && kubectl -n "$NS" rollout restart deployment/coordinator
kubectl -n "$NS" rollout status deployment/coordinator --timeout=180s

# The old separate password Secret is no longer used
kubectl -n "$NS" delete secret idlecloud-postgres --ignore-not-found

step "Health check"
IP=$(kubectl -n "$NS" get svc coordinator -o jsonpath='{.spec.clusterIP}')
curl -sf "http://$IP:8001/healthz" || die "coordinator /healthz failed; see kubectl -n $NS logs deploy/coordinator"
echo
kubectl -n "$NS" get pods
echo "Done."
