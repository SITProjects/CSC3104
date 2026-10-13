"""Settings for burst workers. Values in services/autoscaler/.env.aws override the
defaults. Real environment variables win over the file."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env.aws"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _value = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _value.strip().strip("\"'"))

REGION = os.environ.get("AWS_REGION", "us-east-1")

TEMPLATE_NAME = "idlecloud-burst-worker"
SECURITY_GROUP_NAME = "idlecloud-burst-sg"
INSTANCE_TYPE = os.environ.get("BURST_INSTANCE_TYPE", "t3.small")
DISK_GB = 20

# WireGuard slots reserved on the hub: 10.10.0.200 to 10.10.0.204.
# They match the burst_slots rows in shared/migrations/001_init.sql.
SLOTS = range(200, 205)
HUB_WG_IP = "10.10.0.1"
K3S_URL = f"https://{HUB_WG_IP}:6443"
# Must match the server version, or the agent may refuse to join.
# Member 1 can read it with:  k3s --version
K3S_VERSION = os.environ.get("K3S_VERSION", "")

# Where member 1 stores the slot configs and the join token
SECRETS_NAMESPACE = "idlecloud-system"
WG_SLOTS_SECRET = "burst-wg-slots"  # keys slot-200 ... slot-204, each a full wg0.conf
K3S_TOKEN_SECRET = "k3s-agent-token"  # key token

# Safety net: the instance powers off (and so terminates) after this many minutes
MAX_LIFETIME_MIN = int(os.environ.get("BURST_MAX_LIFETIME_MIN", "120"))

# EC2 tags. The launch template exposes instance tags to the instance metadata
# service (the Spot watcher reads them), and AWS then forbids "/" in tag keys.
# So EC2 tags use "-" while Kubernetes labels keep the "idlecloud/" prefix.
PROJECT_TAG = {"Key": "project", "Value": "idlecloud"}
ROLE_TAG = {"Key": "idlecloud-role", "Value": "burst-worker"}
SLOT_TAG_KEY = "idlecloud-burst-slot"
# Slot tag used before metadata tags were enabled. Still read, so a worker
# launched by older code stays visible until it ends (at most MAX_LIFETIME_MIN).
# Safe to remove once no such worker can be running.
LEGACY_SLOT_TAG_KEY = "idlecloud/burst-slot"

# Injected Spot warning for testing (`launch reclaim`). The value is a UTC
# deadline like 2026-11-01T12:02:00Z. The Spot watcher reads it through instance
# metadata, so the name must match RECLAIM_TAG_KEY in spotwatch/watcher.py.
RECLAIM_TAG_KEY = "reclaim-at"
# A real Spot warning gives two minutes, so the injected one does too
RECLAIM_WARNING_S = 120

# Kubernetes node label with the slot number. user_data.sh.tmpl sets it at join.
SLOT_LABEL_KEY = "idlecloud/burst-slot"


def node_ip(slot: int) -> str:
    return f"10.10.0.{slot}"


def node_name(slot: int) -> str:
    return f"burst-{slot}"


def repo_path(value: str) -> Path:
    """Paths in .env.aws are relative to the repo root."""
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path
