#!/usr/bin/env bash
set -euo pipefail
umask 077

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

if [[ "$TYPE" != burst && "$NAME" == slot-* ]]; then
    die "Names starting with slot- are reserved for burst workers."
fi

# Worker names also become Kubernetes node names in join-worker.sh.
if [[ "$TYPE" != laptop ]]; then
    [[ "$NAME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ &&
       ${#NAME} -le 63 ]] || die "Use a lowercase Kubernetes node name."
    command -v k3s >/dev/null || die "K3s is not installed on this hub."
fi

for COMMAND in wg flock; do
    command -v "$COMMAND" >/dev/null || die "Missing command: $COMMAND"
done

[[ -f "$HUB_CONFIG" ]] || die "Hub configuration is missing."

# Lock before reading allocations or changing peer state.
exec 9>/run/wireguard-add-peer.lock
flock -x 9

HUB_PUBLIC_KEY="$(wg show wg0 public-key)" ||
    die "Cannot read the hub public key. Is wg0 running?"
ACTIVE_PEERS="$(wg show wg0 allowed-ips)" ||
    die "Cannot read the hub peers. Is wg0 running?"

[[ -n "$HUB_PUBLIC_KEY" && "$HUB_PUBLIC_KEY" != "(none)" ]] ||
    die "The hub's wg0 interface has no key configured."

if grep -Eiq \
    '^[[:space:]]*SaveConfig[[:space:]]*=[[:space:]]*true' \
    "$HUB_CONFIG"; then
    die "Remove SaveConfig=true from the hub configuration first."
fi

WORKER_DIR="/etc/wireguard/peers/$NAME"
[[ ! -e "$WORKER_DIR" ]] || die "Peer already exists: $WORKER_DIR"

if [[ -f "$PEERS_FILE" ]] && grep -Fq "| $NAME |" "$PEERS_FILE"; then
    die "Peer name is already registered."
fi

# Assumes one 10.10.0.X/32 allocation per peer on the hub.
IP=""
for (( OCTET=START; OCTET<=END; OCTET++ )); do
    CANDIDATE="10.10.0.$OCTET"
    PATTERN="(^|[^0-9.])${CANDIDATE//./\\.}/32([^0-9]|$)"

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

# Prepare credentials before registering the peer. Clean up if this fails.
install -d -m 700 /etc/wireguard/peers
STAGING_DIR="$(mktemp -d "/etc/wireguard/peers/.${NAME}.XXXXXX")"
trap 'if [[ -n "${STAGING_DIR:-}" ]]; then rm -rf -- "$STAGING_DIR"; fi' EXIT

wg genkey > "$STAGING_DIR/privatekey"
wg pubkey < "$STAGING_DIR/privatekey" > "$STAGING_DIR/publickey"
PRIVATE_KEY="$(cat "$STAGING_DIR/privatekey")"
PUBLIC_KEY="$(cat "$STAGING_DIR/publickey")"

cat > "$STAGING_DIR/wg0.conf" <<EOF
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

case "$TYPE" in
    worker|burst)
        k3s token create --ttl 10m --description "$NAME" \
            > "$STAGING_DIR/agent-token"
        ;;
esac

# Preserve the credentials once registration begins, even if a later step fails.
mv -- "$STAGING_DIR" "$WORKER_DIR"
STAGING_DIR=""

cat >> "$HUB_CONFIG" <<EOF

# $NAME
[Peer]
PublicKey = $PUBLIC_KEY
AllowedIPs = $IP/32
EOF

if [[ ! -f "$PEERS_FILE" ]]; then
    cat > "$PEERS_FILE" <<'EOF'
# WireGuard peers

| Name | VPN IP | Role |
|---|---|---|
| hub | 10.10.0.1 | server |
EOF
fi

printf '| %s | %s | %s |\n' "$NAME" "$IP" "$TYPE" >> "$PEERS_FILE"

wg set wg0 peer "$PUBLIC_KEY" allowed-ips "$IP/32" ||
    die "Peer saved, but live registration failed. Inspect wg0 before retrying."
