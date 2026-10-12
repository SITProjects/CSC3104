#!/usr/bin/env bash
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-/tmp/dev-kubeconfig}"
# Make a private, editable copy of the generated connection settings.
install -m 644 /k3s-config/config "$KUBECONFIG"

# Inside Compose, reach the server using its service name.
kubectl config set-cluster default --server=https://k3-server:6443
kubectl config rename-context default idleCloud-cluster

kubectl --context=idleCloud-cluster get nodes
kubectl --context=idleCloud-cluster apply \
  -f infra/k8s/base/namespaces.yml
