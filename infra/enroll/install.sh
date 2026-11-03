#!/usr/bin/env bash
# Installs or updates the enrollment helper on the hub. infra/k8s/deploy.sh
# runs it; by hand, from the repo root:
#
#   sudo bash infra/enroll/install.sh [--endpoint <public-ip>:<port>]
#
# --endpoint is the hub's public WireGuard address that peers connect to. It is
# only needed on the first install, and only if this is not an EC2 instance
# with a public IP (it is read from instance metadata). Running it again
# updates the code and restarts the helper; settings in
# /etc/idlecloud/enroll.env are kept, except --endpoint when given.
set -euo pipefail

die() {
    echo "Error: $*" >&2
    exit 1
}

[[ $EUID -eq 0 ]] || die "Run with sudo."

ENDPOINT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --endpoint) ENDPOINT="${2:-}"; shift 2 || die "--endpoint needs a value." ;;
        *) die "Usage: $0 [--endpoint <public-ip>:<port>]" ;;
    esac
done
[[ -z "$ENDPOINT" || "$ENDPOINT" =~ ^[A-Za-z0-9.-]+:[0-9]+$ ]] ||
    die "The endpoint must look like 1.2.3.4:51820."

SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SRC/../.." && pwd)"
DEST=/opt/idlecloud/enroll
ENV_FILE=/etc/idlecloud/enroll.env
UNIT=idlecloud-enroll.service
SOCKET=/run/idlecloud/enroll.sock
GROUP=idlecloud-enroll
# The coordinator Pod's supplementalGroups in infra/k8s/base/coordinator.yml
GID=10050
HUB_CONFIG=/etc/wireguard/wg0.conf
PEERS_FILE=/etc/wireguard/peers.md
# Where add-peer.sh kept the table before the helper
OLD_PEERS_FILE="$REPO/infra/wireguard/peers.md"

for COMMAND in python3 wg k3s kubectl curl; do
    command -v "$COMMAND" >/dev/null || die "Missing command: $COMMAND"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    die "The helper needs Python 3.11 or newer."
[[ -f "$HUB_CONFIG" ]] || die "Hub configuration is missing: $HUB_CONFIG"

# Reads the public IP from EC2 instance metadata (IMDSv2) and the port from wg0
guess_endpoint() {
    local token ip port
    token=$(curl -sf -m 2 -X PUT http://169.254.169.254/latest/api/token \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60") || return 1
    ip=$(curl -sf -m 2 -H "X-aws-ec2-metadata-token: $token" \
        http://169.254.169.254/latest/meta-data/public-ipv4) || return 1
    port=$(awk -F '=' '/^[[:space:]]*ListenPort[[:space:]]*=/ {
        gsub(/[[:space:]]/, "", $2); print $2 }' "$HUB_CONFIG")
    [[ -n "$ip" && -n "$port" ]] || return 1
    printf '%s:%s\n' "$ip" "$port"
}

set_var() {
    sed -i "/^$1=/d" "$ENV_FILE"
    printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
}

# 1. The group that may use the socket, with the GID the coordinator Pod expects
if getent group "$GROUP" >/dev/null; then
    [[ "$(getent group "$GROUP" | cut -d: -f3)" == "$GID" ]] ||
        die "Group $GROUP exists without GID $GID; the coordinator could not use the socket."
else
    groupadd --system --gid "$GID" "$GROUP"
fi

# 2. Code: replace it, so a file removed from the repo is removed here too
install -d -m 755 "$DEST" "$DEST/enroll"
rm -f "$DEST"/enroll/*.py
install -m 644 "$SRC"/*.py "$DEST/enroll/"
install -m 644 "$REPO/agent/node-agent/node-agent-policy.yml" "$DEST/"

# 3. Settings, kept across updates
install -d -m 755 /etc/idlecloud
if [[ ! -f "$ENV_FILE" ]]; then
    [[ -n "$ENDPOINT" ]] || ENDPOINT=$(guess_endpoint) ||
        die "Could not find the hub's public address. Pass --endpoint <public-ip>:<port>."
    install -m 600 /dev/null "$ENV_FILE"
    cat >"$ENV_FILE" <<EOF
# Enrollment helper settings, one NAME=value per line. infra/enroll/config.py
# lists them all. After a change: sudo systemctl restart $UNIT
HUB_ENDPOINT=$ENDPOINT
SOCKET_GID=$GID
EOF
    echo "Hub endpoint: $ENDPOINT"
elif [[ -n "$ENDPOINT" ]]; then
    set_var HUB_ENDPOINT "$ENDPOINT"
fi

# 4. The peer table moves out of the repo checkout, once (add-peer.sh uses the
# new place too)
if [[ -f "$OLD_PEERS_FILE" && ! -f "$PEERS_FILE" ]]; then
    install -m 644 "$OLD_PEERS_FILE" "$PEERS_FILE"
    echo "Copied $OLD_PEERS_FILE to $PEERS_FILE; it is kept there from now on."
fi

# 5. Service
install -m 644 "$SRC/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"

for _ in $(seq 20); do
    [[ -S "$SOCKET" ]] && break
    sleep 0.5
done
curl -sf --unix-socket "$SOCKET" http://enroll/healthz >/dev/null ||
    die "The helper is not answering. See: journalctl -u $UNIT -n 30"
echo "Enrollment helper installed and answering on $SOCKET."
