"""Render the cloud-init user data for one burst slot."""

import re
from pathlib import Path

from services.autoscaler.burst import config


def _lf(text: str) -> str:
    """Use Linux line endings. A Windows checkout can turn the template into CRLF,
    and bash on the EC2 instance would then fail on every line."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


TEMPLATE = _lf(Path(__file__).with_name("user_data.sh.tmpl").read_text())
MAX_USER_DATA_BYTES = 16 * 1024  # EC2 limit

# Both values go into a root shell line, so only allow safe characters
K3S_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+\+k3s\d+$")
K3S_TOKEN_RE = re.compile(r"^[A-Za-z0-9:._-]+$")
INSTANCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9]*\.[a-z0-9]+$")


def _check_wg_conf(slot: int, wg_conf: str) -> str:
    wg_conf = _lf(wg_conf).strip()
    address = re.search(r"^\s*Address\s*=\s*([\d.]+)", wg_conf, re.MULTILINE)
    if not address or address.group(1) != config.node_ip(slot):
        raise ValueError(
            f"wg0.conf for slot {slot} must have Address = {config.node_ip(slot)}/..."
        )
    if re.search(r"^\s*DNS\s*=", wg_conf, re.MULTILINE):
        raise ValueError(
            "remove the DNS line from the slot config, Ubuntu cloud images lack resolvconf"
        )
    if "PersistentKeepalive" not in wg_conf:
        raise ValueError(
            "slot config needs PersistentKeepalive = 25 so the hub can reach the node"
        )
    if "WGEOF" in wg_conf:
        raise ValueError("slot config contains the heredoc marker WGEOF")
    return wg_conf


def render_user_data(
    slot: int,
    wg_conf: str,
    k3s_token: str,
    k3s_version: str | None = None,
    instance_type: str | None = None,
) -> str:
    if slot not in config.SLOTS:
        raise ValueError(f"slot must be one of {list(config.SLOTS)}")
    k3s_version = k3s_version or config.K3S_VERSION
    if not k3s_version:
        raise ValueError("set K3S_VERSION to the server's version (ask member 1)")
    if not K3S_VERSION_RE.match(k3s_version):
        raise ValueError(
            f"K3S_VERSION must look like v1.31.4+k3s1, got {k3s_version!r}"
        )
    k3s_token = (k3s_token or "").strip()
    if not K3S_TOKEN_RE.match(k3s_token):
        raise ValueError(
            "k3s token is empty or has characters other than A-Z a-z 0-9 : . _ -"
        )

    instance_type = instance_type or config.INSTANCE_TYPE
    if not INSTANCE_TYPE_RE.match(instance_type):
        raise ValueError(f"not an EC2 instance type: {instance_type!r}")

    values = {
        "SLOT": str(slot),
        "NODE_IP": config.node_ip(slot),
        "NODE_NAME": config.node_name(slot),
        "HUB_WG_IP": config.HUB_WG_IP,
        "K3S_URL": config.K3S_URL,
        "K3S_VERSION": k3s_version,
        "K3S_TOKEN": k3s_token,
        "MAX_LIFETIME_MIN": str(config.MAX_LIFETIME_MIN),
        "INSTANCE_TYPE": instance_type,
        "WG_CONF": _check_wg_conf(slot, wg_conf),
    }
    script = TEMPLATE
    for name, value in values.items():
        script = script.replace("{{" + name + "}}", value)

    leftover = re.findall(r"\{\{\w+\}\}", script)
    if leftover:
        raise ValueError(f"unfilled placeholders: {leftover}")
    if len(script.encode()) > MAX_USER_DATA_BYTES:
        raise ValueError("user data is over the 16 KB EC2 limit")
    return script
