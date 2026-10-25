"""Tell the coordinator this node is alive, and whether it can take work.

With no heartbeat for 30 seconds the coordinator marks the node OFFLINE and
requeues its running replicas, so a PC that crashes or drops off WireGuard is
noticed even though nobody tainted it.

The coordinator checks the token with a TokenReview. make-kubeconfig.sh binds it
to this node, so it can only report for this node. Its audience is the
coordinator's, so the API server itself does not accept it.
"""

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("node-agent")

# Straight to the coordinator, never through an HTTP proxy from the environment
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Heartbeat:
    def __init__(
        self, coordinator_url: str, node_name: str, token_file: str, timeout_s: float
    ) -> None:
        self.url = f"{coordinator_url}/internal/nodes/{node_name}/heartbeat"
        self.token_file = token_file
        self.timeout_s = timeout_s
        self.last_problem: str | None = None

    def send(self, state: str, cpu_percent: float) -> bool:
        """Send one heartbeat. Blocks for up to timeout_s. Never raises: a
        failure is logged once per kind and the next heartbeat tries again.
        Returns True if the coordinator accepted it."""
        # Read every time, so a renewed token works without a restart
        try:
            with open(self.token_file) as file:
                token = file.read().strip()
        except OSError as error:
            return self._problem(
                f"no coordinator token in {self.token_file} ({error.strerror})"
            )
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"state": state, "cpu_percent": cpu_percent}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with _direct.open(request, timeout=self.timeout_s):
                pass
        except urllib.error.HTTPError as error:
            return self._problem(f"HTTP {error.code}")
        except OSError as error:  # URLError, timeouts, refused connections
            return self._problem(str(getattr(error, "reason", error)))
        if self.last_problem:
            log.info("coordinator heartbeats working again")
        self.last_problem = None
        return True

    def _problem(self, problem: str) -> bool:
        if problem != self.last_problem:
            log.warning(
                "heartbeat to coordinator failed (%s), will keep trying", problem
            )
            self.last_problem = problem
        return False
