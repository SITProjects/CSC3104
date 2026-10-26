#!/usr/bin/env bash
set -euo pipefail

# Installs the node agent as a systemd service, so it starts at boot and is
# restarted if it stops. install-worker.ps1 runs it; by hand, from this folder:
#   sudo bash install-service.sh <node-name> [coordinator-url]
# Running it again updates the code and restarts the agent. Settings in
# /etc/idlecloud/node-agent.env are kept, except the ones passed here.

die() {
    echo "Error: $*" >&2
    exit 1
}

[[ $EUID -eq 0 ]] || die "Run with sudo."
[[ $# -ge 1 && $# -le 2 ]] ||
    die "Usage: $0 <node-name> [coordinator-url]"

NAME="$1"
URL="${2:-}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/idlecloud/node-agent
ENV_FILE=/etc/idlecloud/node-agent.env
UNIT=idlecloud-node-agent.service

[[ "$NAME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] ||
    die "Use a valid lowercase node name."
[[ -z "$URL" || "$URL" =~ ^https?://[^[:space:]]+$ ]] ||
    die "Coordinator URL must look like http://<cluster-ip>:8001."

# Ubuntu 24.04 has no venv module by default.
if ! dpkg -s python3-venv >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
fi

# Replace the code, so a file removed from the repo is removed here too.
install -d -m 755 "$DEST"
rm -f "$DEST"/*.py
install -m 644 "$SRC"/*.py "$SRC/requirements.txt" "$DEST"/
[[ -x "$DEST/.venv/bin/python" ]] || python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --quiet --disable-pip-version-check \
    -r "$DEST/requirements.txt"

set_var() {
    sed -i "/^$1=/d" "$ENV_FILE"
    printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
}

install -d -m 700 /etc/idlecloud
if [[ ! -f "$ENV_FILE" ]]; then
    install -m 600 /dev/null "$ENV_FILE"
    cat >"$ENV_FILE" <<'EOF'
# Node agent settings, one NAME=value per line. config.py lists them all, for
# example NODE_AGENT_DEMO=1 or NODE_AGENT_BLOCKED=08:00-09:00.
# After a change: sudo systemctl restart idlecloud-node-agent
COORDINATOR_URL=
EOF
fi
set_var NODE_NAME "$NAME"
[[ -z "$URL" ]] || set_var COORDINATOR_URL "$URL"

install -m 644 "$SRC/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT"
systemctl restart "$UNIT"

echo "Node agent installed as $UNIT."
if [[ ! -s /etc/idlecloud/node-agent.kubeconfig ]]; then
    echo "It retries every 10 seconds until /etc/idlecloud/node-agent.kubeconfig"
    echo "is in place (agent/node-agent/make-kubeconfig.sh), then starts by itself."
fi
