#!/usr/bin/env bash
set -euo pipefail
umask 077

# Set this once to your EC2 public IP or hostname.
HUB_ENDPOINT="13.215.16.236:51820"

HUB_CONFIG="/etc/wireguard/wg0.conf"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PEERS_FILE="$SCRIPT_DIR/peers.md"

die() {
    echo "Error: $*" >&2
    exit 1
}

[[ $EUID -eq 0 ]] || die "Run with sudo."

[[ $# -eq 2 ]] || die "Usage: $0 <laptop|worker|burst> <name>"

TYPE="$1"
NAME="$2"

[[ "$NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] ||
    die "Invalid peer name."

case "$TYPE" in
    laptop)
        START=10
        END=99
        ;;
    worker)
        START=100
        END=199
        ;;
    burst)
        [[ "$NAME" =~ ^slot-(20[0-4])$ ]] ||
            die "Burst peers must be named slot-200 to slot-204."

        START="${BASH_REMATCH[1]}"
        END="$START"
        ;;
    *)
        die "Unknown peer type: $TYPE"
        ;;
esac

# Reserve slot names exclusively for burst workers.
if [[ "$TYPE" != burst && "$NAME" == slot-* ]]; then
    die "Names starting with slot- are reserved for burst workers."
fi

WORKER_DIR="/etc/wireguard/peers/$NAME"
[[ ! -e "$WORKER_DIR" ]] || die "Peer already exists."

if [[ -f "$PEERS_FILE" ]] &&
   grep -Fq "| $NAME |" "$PEERS_FILE"; then
    die "Peer name is already registered."
fi

IP=""

for (( OCTET=START; OCTET<=END; OCTET++ )); do
    CANDIDATE="10.10.0.$OCTET"
    PATTERN="${CANDIDATE//./\\.}/32([^0-9]|$)"

    if grep -Eq "$PATTERN" "$HUB_CONFIG"; then
        continue
    fi

    if grep -Eq "$PATTERN" <<< "$ACTIVE_PEERS"; then
        continue
    fi

    if [[ -f "$PEERS_FILE" ]] &&
       grep -Fq "| $CANDIDATE |" "$PEERS_FILE"; then
        continue
    fi

    IP="$CANDIDATE"
    break
done

[[ -n "$IP" ]] || die "No available IP for $TYPE / $NAME."

# Keep private keys outside the repository.
install -d -m 700 "$WORKER_DIR"

wg genkey > "$WORKER_DIR/privatekey"
wg pubkey < "$WORKER_DIR/privatekey" > "$WORKER_DIR/publickey"

PRIVATE_KEY="$(cat "$WORKER_DIR/privatekey")"
PUBLIC_KEY="$(cat "$WORKER_DIR/publickey")"

# Generate the configuration that the worker will use.
cat > "$WORKER_DIR/wg0.conf" <<EOF
[Interface]
PrivateKey = $PRIVATE_KEY
Address = $IP/24

[Peer]
PublicKey = $HUB_PUBLIC_KEY
Endpoint = $HUB_ENDPOINT
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
EOF

unset PRIVATE_KEY

# Save the peer so it survives a hub restart.
cat >> "$HUB_CONFIG" <<EOF

# $NAME
[Peer]
PublicKey = $PUBLIC_KEY
AllowedIPs = $IP/32
EOF

# Register the peer immediately.
wg set wg0 peer "$PUBLIC_KEY" allowed-ips "$IP/32"

if [[ ! -f "$PEERS_FILE" ]]; then
    cat > "$PEERS_FILE" <<'EOF'
# WireGuard peers

| Name | VPN IP | Role |
|---|---|---|
| hub | 10.10.0.1 | server |
EOF
fi

printf '| %s | %s | worker |\n' \
    "$NAME" "$IP" >> "$PEERS_FILE"

# Only Kubernetes workers need a join token.
case "$TYPE" in
    worker|burst)
        (
            umask 077
            k3s token create \
                --ttl 10m \
                --description "$NAME" \
                > "$WORKER_DIR/agent-token"
        )
        ;;
esac
