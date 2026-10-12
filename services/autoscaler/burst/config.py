"""Settings for burst workers. Values in services/autoscaler/.env.aws override the defaults."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env.aws"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _value = _line.split("=", 1)
            os.environ[_key.strip()] = _value.strip()

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

PROJECT_TAG = {"Key": "project", "Value": "idlecloud"}
SLOT_TAG_KEY = "idlecloud/burst-slot"


def node_ip(slot: int) -> str:
    return f"10.10.0.{slot}"


def node_name(slot: int) -> str:
    return f"burst-{slot}"


def repo_path(value: str) -> Path:
    """Paths in .env.aws are relative to the repo root."""
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path
