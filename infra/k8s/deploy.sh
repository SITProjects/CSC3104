#!/usr/bin/env bash
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

# The tag each manifest asks for, so the build and the manifest always agree
image_in() {
  local tag
  tag=$(grep -oE "idlecloud/$1:[^[:space:]]+" "infra/k8s/base/$2" | head -1)
  [ -n "$tag" ] || die "no idlecloud/$1 image in infra/k8s/base/$2"
  echo "$tag"
}
COORDINATOR_IMAGE=$(image_in coordinator coordinator.yml)
WEB_IMAGE=$(image_in web web.yml)
VERIFIER_IMAGE=$(image_in verifier verifier.yml)

# name, image tag, Dockerfile
for spec in "coordinator $COORDINATOR_IMAGE services/coordinator/Dockerfile" \
            "web $WEB_IMAGE services/web/Dockerfile" \
            "verifier $VERIFIER_IMAGE services/verifier/Dockerfile"; do
  read -r name image dockerfile <<<"$spec"
  if [ "$BUILD" = 1 ]; then
    step "Building $image and loading it into k3s"
    sudo docker build -f "$dockerfile" -t "$image" .
    sudo docker save "$image" | sudo k3s ctr images import -
  fi
  sudo k3s ctr images ls -q | grep -q "docker.io/$image" ||
    die "$image ($name) is not in k3s; run without --no-build"
done

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

step "Web callback Secret"
if [ -n "${HMAC_SECRET:-}" ]; then
  kubectl -n "$NS" create secret generic idlecloud-web \
    --from-literal=hmac-secret="$HMAC_SECRET" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "idlecloud-web set from HMAC_SECRET"
elif kubectl -n "$NS" get secret idlecloud-web -o jsonpath='{.data.hmac-secret}' 2>/dev/null | grep -q .; then
  echo "idlecloud-web already exists; keeping it"
else
  kubectl -n "$NS" create secret generic idlecloud-web \
    --from-literal=hmac-secret="$(openssl rand -hex 32)"
  echo "generated a callback secret. Give the Lambda the same value:"
  echo "  export HMAC_SECRET=\$(kubectl -n $NS get secret idlecloud-web -o jsonpath='{.data.hmac-secret}' | base64 -d)"
fi

step "Enrollment helper"
# Before the coordinator starts, so its socket mount finds the helper's folder
sudo bash infra/enroll/install.sh

step "Applying $OVERLAY"
kubectl apply -k "$OVERLAY"

step "Waiting for Postgres, the coordinator, the web app and the verifier"
kubectl -n "$NS" rollout status statefulset/postgres --timeout=180s
# A rebuilt image under the same tag is not picked up without a restart
[ "$BUILD" = 1 ] && kubectl -n "$NS" rollout restart deployment/coordinator deployment/web deployment/verifier
kubectl -n "$NS" rollout status deployment/coordinator --timeout=180s
kubectl -n "$NS" rollout status deployment/web --timeout=180s
# It serves no HTTP, so a finished rollout (container running) is its health check
kubectl -n "$NS" rollout status deployment/verifier --timeout=180s

# The old separate password Secret is no longer used
kubectl -n "$NS" delete secret idlecloud-postgres --ignore-not-found

step "Health check"
IP=$(kubectl -n "$NS" get svc coordinator -o jsonpath='{.spec.clusterIP}')
curl -sf "http://$IP:8001/healthz" || die "coordinator /healthz failed; see kubectl -n $NS logs deploy/coordinator"
echo
IP=$(kubectl -n "$NS" get svc web -o jsonpath='{.spec.clusterIP}')
curl -sf "http://$IP/healthz" || die "web /healthz failed; see kubectl -n $NS logs deploy/web"
echo
kubectl -n "$NS" get pods
echo "Done."
