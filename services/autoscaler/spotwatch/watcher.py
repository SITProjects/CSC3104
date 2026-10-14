"""Spot watcher. Runs on every burst node as a DaemonSet pod.

Every 5 seconds it asks the EC2 instance metadata service (IMDSv2) whether the
machine is about to be taken back. It checks two sources:

  spot      the real AWS Spot interruption notice (spot/instance-action)
  injected  the EC2 tag "reclaim-at", written by `launch reclaim` to test the
            same path on AWS Academy, where Spot is not available

Both feed one handler. It runs once per warning and puts the owner-active taint
on this node, so Kubernetes evicts the task pods and member 3's checkpoint and
requeue logic takes over. It also writes annotations so the autoscaler and the
admin page can see why, and logs one JSON line with the timings for evaluation.

Every 10 seconds it also sends the coordinator a heartbeat for this node:
IDLE (available for work) or BUSY (reclaim warning seen, take no new work).

A reclaim is final. Once the node is marked, the watcher stops checking and
never touches the taint again. Removing the taint by hand does not cancel the
reclaim: the machine still goes at the deadline.
Burst nodes have no campus node agent, so without this the coordinator would
mark them OFFLINE.

Standard library only. It is loaded from a ConfigMap into a small Python image,
so new burst nodes pull no project image. The pod uses the host network,
because the metadata service only answers one network hop away.
"""

import http.client
import json
import logging
import os
import signal
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

# Names shared with the autoscaler (controller/settings.py, burst/config.py)
OWNER_TAINT = "idlecloud/owner-active"
RECLAIM_AT_ANNOTATION = "idlecloud/reclaim-at"
RECLAIM_SOURCE_ANNOTATION = "idlecloud/reclaim-source"
RECLAIM_SEEN_ANNOTATION = "idlecloud/reclaim-seen-at"
RECLAIM_TAG_KEY = "reclaim-at"

IMDS_URL = os.environ.get("IMDS_URL", "http://169.254.169.254")
POLL_S = float(os.environ.get("POLL_SECONDS", "5"))
HEARTBEAT_S = float(os.environ.get("HEARTBEAT_SECONDS", "10"))
NODE_NAME = os.environ.get("NODE_NAME", "")
COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "").rstrip("/")
COORDINATOR_TOKEN_FILE = os.environ.get(
    "COORDINATOR_TOKEN_FILE", "/var/run/secrets/idlecloud/token"
)
SA_DIR = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
# AWS gives two minutes. Used when a real notice cannot be parsed.
DEFAULT_WARNING_S = 120
HTTP_TIMEOUT_S = 2

log = logging.getLogger("spot-watcher")

# Everything a plain HTTP call can raise: refused or dropped connections and
# timeouts (OSError, which includes URLError), malformed replies
# (http.client.HTTPException, which is not an OSError) and bad text (ValueError)
HTTP_ERRORS = (OSError, http.client.HTTPException, ValueError)

# Never send metadata or cluster calls through a proxy
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime | None:
    """ISO 8601 time from AWS or the reclaim tag. A time without a zone is UTC."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except (ValueError, AttributeError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class Notice:
    def __init__(self, source: str, action: str, deadline: datetime):
        self.source = source  # "spot" or "injected"
        self.action = action
        self.deadline = deadline


def parse_spot_notice(body: str, now: datetime) -> Notice:
    """AWS sends {"action": "terminate", "time": "2026-11-01T12:02:00Z"}.
    The notice being there at all means the machine is going, so a body we cannot
    read still counts, with the usual two minutes."""
    try:
        data = json.loads(body)
        action = str(data.get("action", "unknown"))
        deadline = parse_time(str(data.get("time", "")))
    except (ValueError, AttributeError):
        action, deadline = "unknown", None
    return Notice(
        "spot", action, deadline or now + timedelta(seconds=DEFAULT_WARNING_S)
    )


class Imds:
    """Minimal IMDSv2 client: a session token, then GETs with that token."""

    TOKEN_TTL_S = 21600

    def __init__(self, base: str = IMDS_URL):
        self.base = base
        self.token = None
        self.token_at = 0.0

    def _token(self) -> str:
        if (
            self.token is None
            or time.monotonic() - self.token_at > self.TOKEN_TTL_S - 300
        ):
            request = urllib.request.Request(
                f"{self.base}/latest/api/token",
                method="PUT",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": str(self.TOKEN_TTL_S)},
            )
            with _direct.open(request, timeout=HTTP_TIMEOUT_S) as response:
                self.token = response.read().decode()
            self.token_at = time.monotonic()
        return self.token

    def get(self, path: str) -> str | None:
        """Body of a metadata path, or None when it does not exist (404)."""
        for attempt in (1, 2):
            request = urllib.request.Request(
                f"{self.base}/latest/meta-data/{path}",
                headers={"X-aws-ec2-metadata-token": self._token()},
            )
            try:
                with _direct.open(request, timeout=HTTP_TIMEOUT_S) as response:
                    return response.read().decode()
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                if error.code == 401 and attempt == 1:
                    self.token = None  # expired token, get a new one once
                    continue
                raise
        return None


class Watcher:
    def __init__(self, imds: Imds, now=lambda: datetime.now(UTC)):
        self.imds = imds
        self.now = now
        self.bad_tag_logged = None

    def check(self) -> Notice | None:
        """The real Spot notice wins. The injected tag is only for testing."""
        body = self.imds.get("spot/instance-action")
        if body is not None:
            return parse_spot_notice(body, self.now())
        value = self.imds.get(f"tags/instance/{RECLAIM_TAG_KEY}")
        if value is None:
            return None
        deadline = parse_time(value)
        if deadline is None:
            if value != self.bad_tag_logged:  # log a bad tag once, not every 5 s
                log.warning(
                    "ignoring %s tag with unreadable time %r", RECLAIM_TAG_KEY, value
                )
                self.bad_tag_logged = value
            return None
        return Notice("injected", "terminate", deadline)


class KubeApi:
    """Just enough of the Kubernetes API to read and patch one node."""

    def __init__(self, sa_dir: str = SA_DIR):
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.base = f"https://{host}:{port}"
        self.sa_dir = sa_dir
        self.ssl = ssl.create_default_context(cafile=f"{sa_dir}/ca.crt")

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        # Re-read every call: the kubelet rotates the projected token
        with open(f"{self.sa_dir}/token") as file:
            token = file.read().strip()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/merge-patch+json"
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        handler = urllib.request.HTTPSHandler(context=self.ssl)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), handler)
        with opener.open(request, timeout=10) as response:
            return json.loads(response.read())

    def get_node(self, name: str) -> dict:
        return self._call("GET", f"/api/v1/nodes/{name}")

    def patch_node(self, name: str, body: dict) -> dict:
        return self._call("PATCH", f"/api/v1/nodes/{name}", body)


def already_marked(node: dict) -> bool:
    """True when an earlier run (or a restart of this pod) already handled it."""
    taints = node.get("spec", {}).get("taints") or []
    notes = node.get("metadata", {}).get("annotations") or {}
    return (
        any(t.get("key") == OWNER_TAINT for t in taints)
        and RECLAIM_AT_ANNOTATION in notes
    )


def mark_node(kube: KubeApi, name: str, notice: Notice, seen_at: datetime) -> bool:
    """Taint and annotate the node. Returns False if it was already marked.

    The taint list is replaced as a whole, so the patch carries the node's
    resourceVersion. If the kubelet changed the node in between, the API answers
    409 and we read it again, so no other taint is ever lost."""
    for _ in range(5):
        node = kube.get_node(name)
        if already_marked(node):
            return False
        taints = [
            t
            for t in node.get("spec", {}).get("taints") or []
            if t.get("key") != OWNER_TAINT
        ]
        taints.append({"key": OWNER_TAINT, "value": "true", "effect": "NoExecute"})
        body = {
            "metadata": {
                "resourceVersion": node["metadata"]["resourceVersion"],
                "annotations": {
                    RECLAIM_AT_ANNOTATION: utc_iso(notice.deadline),
                    RECLAIM_SOURCE_ANNOTATION: notice.source,
                    RECLAIM_SEEN_ANNOTATION: utc_iso(seen_at),
                },
            },
            "spec": {"taints": taints},
        }
        try:
            kube.patch_node(name, body)
            return True
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
    raise RuntimeError(f"node {name} kept changing, could not taint it")


class Heartbeat:
    """Tells the coordinator this burst node is alive. Heartbeat failures must
    never stop reclaim handling, so every error is caught, logged once per kind
    and retried quietly. A send is synchronous with a 2 s timeout, so a slow
    coordinator can delay the next poll by up to that."""

    def __init__(self, url: str = COORDINATOR_URL, node: str = NODE_NAME):
        self.url = f"{url}/internal/nodes/{node}/heartbeat" if url else ""
        self.last_problem = None
        self.cpu = _CpuMeter()

    def send(self, state: str) -> None:
        if not self.url:
            return
        # Projected service account token meant only for the coordinator. The
        # coordinator can check it with a TokenReview to learn the node.
        # Without it, send nothing rather than an unauthenticated heartbeat.
        try:
            with open(COORDINATOR_TOKEN_FILE) as file:
                token = file.read().strip()
        except OSError as error:
            self._problem(f"no coordinator token ({error.strerror})")
            return
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        body = json.dumps({"state": state, "cpu_percent": self.cpu.percent()}).encode()
        request = urllib.request.Request(
            self.url, data=body, method="POST", headers=headers
        )
        try:
            with _direct.open(request, timeout=HTTP_TIMEOUT_S):
                pass
            if self.last_problem:
                log.info("coordinator heartbeats working again")
            self.last_problem = None
        except HTTP_ERRORS as error:
            self._problem(getattr(error, "code", None) or type(error).__name__)

    def _problem(self, problem) -> None:
        if problem != self.last_problem:
            log.warning(
                "heartbeat to coordinator failed (%s), will keep trying", problem
            )
            self.last_problem = problem


class _CpuMeter:
    """Whole-machine CPU use between two calls, from /proc/stat."""

    def __init__(self, path: str = "/proc/stat"):
        self.path = path
        self.last = self._read()

    def _read(self):
        try:
            with open(self.path) as file:
                values = [int(v) for v in file.readline().split()[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
            return sum(values), idle
        except (OSError, ValueError, IndexError):
            return None

    def percent(self) -> float:
        now, last = self._read(), self.last
        self.last = now
        if not now or not last or now[0] <= last[0]:
            return 0.0
        busy = (now[0] - last[0]) - (now[1] - last[1])
        return round(100.0 * busy / (now[0] - last[0]), 1)


def handle(kube: KubeApi, notice: Notice, seen_at: datetime) -> None:
    started = time.monotonic()
    changed = mark_node(kube, NODE_NAME, notice, seen_at)
    if not changed:
        log.info("node %s was already marked for reclaim", NODE_NAME)
        return
    tainted_at = datetime.now(UTC)
    # One JSON line per reclaim, easy to collect for the evaluation
    log.info(
        json.dumps(
            {
                "event": "reclaim",
                "node": NODE_NAME,
                "source": notice.source,
                "action": notice.action,
                "deadline": utc_iso(notice.deadline),
                "seen_at": utc_iso(seen_at),
                "tainted_at": utc_iso(tainted_at),
                "taint_ms": round((time.monotonic() - started) * 1000),
                # Time left before the machine goes. AWS gives 120 s, so
                # 120 minus this is how late the warning was noticed.
                "lead_s": round((notice.deadline - seen_at).total_seconds(), 1),
            }
        )
    )


def log_tag_access(imds: Imds) -> None:
    """Say at start whether injected warnings can work on this machine. A worker
    from an old launch template version cannot read its tags, and the injected
    tag would then be ignored without any other sign."""
    try:
        readable = imds.get("tags/instance") is not None
    except HTTP_ERRORS:
        log.warning(
            "could not check instance tag access, metadata service not reachable yet"
        )
        return
    if readable:
        log.info("instance tags are readable, injected reclaim warnings will work")
    else:
        log.warning(
            "instance tags are NOT readable through metadata (old launch template "
            "version?). Injected reclaim warnings will be ignored. Real Spot "
            "notices still work."
        )


def run(stop: threading.Event) -> None:
    if not NODE_NAME:
        raise SystemExit("NODE_NAME is not set (use the downward API)")
    imds, kube, beat = Imds(), KubeApi(), Heartbeat()
    watcher = Watcher(imds)
    try:
        marked = already_marked(kube.get_node(NODE_NAME))
    except Exception:  # API not reachable yet. mark_node checks again later.
        log.exception("could not read node %s, carrying on", NODE_NAME)
        marked = False
    if marked:
        log.info("node %s is already marked for reclaim", NODE_NAME)
    log_tag_access(imds)
    log.info("watching %s every %.0f s", NODE_NAME, POLL_S)
    # The first warning is kept, so a failed taint is retried with the same
    # deadline. The taint goes first because it is what moves the work off;
    # the BUSY heartbeat follows in the same tick, even if the taint failed.
    notice, seen_at = None, None
    next_beat, last_error = 0.0, None
    while not stop.is_set():
        try:
            if notice is None and not marked:
                notice = watcher.check()
                if notice:
                    seen_at = datetime.now(UTC)
                    next_beat = 0.0  # send BUSY this tick, after the taint attempt
            if notice and not marked:
                handle(kube, notice, seen_at)
                marked = True
            last_error = None
        except Exception as error:  # keep watching whatever goes wrong
            # Full details the first time, then one line, so a long outage
            # does not fill the log every 5 seconds
            if repr(error) != last_error:
                log.exception("reclaim handling failed, retrying every %.0f s", POLL_S)
                last_error = repr(error)
        if time.monotonic() >= next_beat:
            try:
                beat.send("BUSY" if notice or marked else "IDLE")
            except Exception:  # second guard, a heartbeat must never end the loop
                log.exception("unexpected heartbeat error, carrying on")
            next_beat = time.monotonic() + HEARTBEAT_S
        stop.wait(POLL_S)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(stop)


if __name__ == "__main__":
    main()
