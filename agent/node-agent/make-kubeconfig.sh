#!/usr/bin/env bash
# Give one worker's node agent its own Kubernetes identity, allowed only to get
# and patch its own Node (resourceNames), and write a kubeconfig for it.
#
# Run where your kubectl has admin access to the cluster, after the worker has
# joined (the node must show in `kubectl get nodes`):
#   ./make-kubeconfig.sh <node-name> [output-file]
# Then copy the file to the worker as /etc/idlecloud/node-agent.kubeconfig
# (mode 600). It holds a secret token: never commit it.
set -euo pipefail

NODE="${1:?usage: $0 <node-name> [output-file]}"
OUT="${2:-node-agent-$NODE.kubeconfig}"
NS=idlecloud-system
SA="node-agent-$NODE"
# The K3s API through WireGuard, not the public IP (6443 is closed to the internet)
SERVER="${SERVER:-https://10.10.0.1:6443}"

kubectl get node "$NODE" >/dev/null # stop early on a typo

# RBAC below says which node; this policy says what the agent may change on it
kubectl apply -f "$(dirname "$0")/node-agent-policy.yml"

kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: $SA
  namespace: $NS
---
# Nodes are cluster-wide, so this must be a ClusterRole. resourceNames limits it
# to this one node: the agent cannot taint or read any other PC.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: $SA
rules:
  - apiGroups: [""]
    resources: ["nodes"]
    resourceNames: ["$NODE"]
    verbs: ["get", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: $SA
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: $SA
subjects:
  - kind: ServiceAccount
    name: $SA
    namespace: $NS
---
# A token that does not expire, so lab PCs keep working without renewal.
# Delete this Secret to revoke the PC's access.
apiVersion: v1
kind: Secret
metadata:
  name: $SA-token
  namespace: $NS
  annotations:
    kubernetes.io/service-account.name: $SA
type: kubernetes.io/service-account-token
EOF

# The token controller fills the Secret in a moment
for _ in $(seq 20); do
  TOKEN=$(kubectl -n "$NS" get secret "$SA-token" -o jsonpath='{.data.token}' | base64 -d)
  [ -n "$TOKEN" ] && break
  sleep 0.5
done
[ -n "$TOKEN" ] || { echo "token for $SA was never created" >&2; exit 1; }
CA=$(kubectl -n "$NS" get secret "$SA-token" -o jsonpath='{.data.ca\.crt}')

umask 077
cat >"$OUT" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: idlecloud
    cluster:
      server: $SERVER
      certificate-authority-data: $CA
users:
  - name: $SA
    user:
      token: $TOKEN
contexts:
  - name: $SA
    context:
      cluster: idlecloud
      user: $SA
current-context: $SA
EOF

echo "wrote $OUT"
echo "check: kubectl auth can-i patch nodes/$NODE --as=system:serviceaccount:$NS:$SA  (yes)"
echo "       kubectl auth can-i patch nodes --as=system:serviceaccount:$NS:$SA  (no)"
