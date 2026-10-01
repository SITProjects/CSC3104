# Cloud and Distributed Computing

> [!NOTE]
> WIP, remove this note when project is ready.

project_description

---

## Project Scope

- [link_to_source](link_to_source)
  - scope_description

## Usage

### runtime_executable

```sh
./runtime_executable <args> [optional]
```

- runtime_executable_description

## Getting Started

### Prerequisites

- [Git](https://github.com/git-guides/install-git) (fully set-up)
- [Docker/Podman](https://docs.docker.com/engine/install/)
- [VS Code](https://code.visualstudio.com/download)
  - [Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) extension

> [!WARNING]
> Do not use GitHub Desktop! All interactions (files, git, runtime, etc.) should be done through the Dev Container within VS Code.

### Installation

1. Clone the repo

   ```sh
   git clone git@github.com:10x-Slackers/repo_name.git
   ```

2. Open the repository in VS Code

   ```sh
   code repo_name/
   ```

3. Click on the "Re-open in Dev Container" prompt
4. Install the pre-commit hooks

   ```sh
   pre-commit install
   ```

5. Start working!

## Developer Tooling

- Dev Containers
  - Standardised developer environment
- Pre-Commit
  - Run linting and formatting for all files during git commit

## Repository Structure

The repository root corresponds to `idlecloud/` in the Campus Idle-Compute Cloud
implementation plan. These folders establish component ownership; service
implementations will be added separately.

```text
shared/                  # Shared contracts and Python helpers
  migrations/            # SQL migrations
infra/                   # Infrastructure
  aws/
  wireguard/
  k8s/
    base/
    overlays/aws/
agent/                   # Worker management
  windows-sensor/
  node-agent/
services/
  coordinator/           # Scheduling and checkpoint recovery
  verifier/              # Voting and trust scores
  web/                   # Website and public API
    templates/
    static/
  autoscaler/            # Cloud bursting
worker/
  ocr-task/              # OCR container
serverless/              # Lambda page pipeline
  splitter/
  renderer/
eval/
  data/                  # Reusable sample PDFs
  results/               # Experiment CSVs
  graphs/                # Report figures
docs/                    # Reports, diagrams, testing instructions
tests/
  unit/
  integration/
```

| Owner | Responsibilities | Folders |
| --- | --- | --- |
| Member 1 | AWS, WireGuard, K3s, deployment, and local application stack | `infra/` |
| Member 2 | Windows idle detection and Linux node management | `agent/` |
| Member 3 | Scheduling, checkpoints, and recovery | `services/coordinator/` |
| Member 4 | Majority voting, trust scores, and verified output | `services/verifier/` |
| Member 5 | Website, public API, and OCR workload | `services/web/`, `worker/ocr-task/` |
| Member 6 | Burst capacity and serverless page splitting | `services/autoscaler/`, `serverless/` |
| All members | Contracts, evaluation evidence, documentation, and tests | `shared/`, `eval/`, `docs/`, `tests/` |

Member 3 will author shared SQL migrations, with review by the whole team.
Component READMEs describe the responsibilities for each folder.

The existing `src/csc3104/` package, `pyproject.toml`, and `uv.lock` remain in place.
Python package migration and service entry points will be addressed when services
are implemented; this scaffold does not change imports or executable behavior.

The existing `.devcontainer/docker-compose.yml` provides the development
environment. Member 1 will add a separate root `docker-compose.yml` for the local
application stack: PostgreSQL, MinIO with bucket initialization, web, and
coordinator. That application stack is not implemented by this scaffold.

Keep local environment settings in ignored `.env` files and commit only safe
`.env.example` or `.env.*.example` templates. Put generated WireGuard configs in
`infra/wireguard/generated/` and transient runtime files in root `runtime/`.
Evaluation sample PDFs, result CSVs, graphs, and documentation remain trackable.
