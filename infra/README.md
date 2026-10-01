# Infrastructure

Owner: member 1.

- `aws/`: AWS setup scripts and infrastructure configuration templates.
- `wireguard/`: networking setup and safe peer configuration templates. Generated
  configurations containing keys belong in ignored `wireguard/generated/`.
- `k8s/base/`: shared Kubernetes manifests.
- `k8s/overlays/aws/`: Kustomize configuration for AWS deployment.

Member 1 also owns the future root `docker-compose.yml` application stack and
image/deployment workflows. The existing dev-container Compose file remains the
development environment configuration.
