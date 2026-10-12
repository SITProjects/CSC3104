"""
Windows sensor: reports keyboard/mouse idle time and host CPU load to the node agent.

Runs on the Windows host (not inside WSL). Every few seconds it POSTs
{"idle_seconds": float, "cpu_percent": float} to the node agent's /state endpoint.
"""

import ctypes
import logging
import os
import time

import httpx
import psutil

AGENT_URL = os.environ.get("IDLECLOUD_AGENT_URL", "http://localhost:9100/state")
INTERVAL = 5  # seconds between readings
REQUEST_TIMEOUT = 2  # seconds to wait for the node agent before giving up

log = logging.getLogger("windows-sensor")


class LASTINPUTINFO(ctypes.Structure):
    """Mirror of the Windows LASTINPUTINFO struct that GetLastInputInfo fills in."""

    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


def get_idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input on this Windows session."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # GetTickCount returns an unsigned 32-bit value; the default (signed int) would
    # turn negative after ~25 days of uptime.
    kernel32.GetTickCount.restype = ctypes.c_uint32

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise ctypes.WinError()

    # Both values are milliseconds since boot and wrap every ~49.7 days, so mask the
    # difference to keep it correct across the wrap.
    idle_ms = (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return idle_ms / 1000


def read_state() -> dict[str, float]:
    """Take one reading of idle time and CPU load."""
    # cpu_percent() with no interval is the average since the previous call,
    # i.e. the last INTERVAL seconds once the loop is running.
    return {
        "idle_seconds": round(get_idle_seconds(), 1),
        "cpu_percent": psutil.cpu_percent(interval=None),
    }


def send_state(client: httpx.Client, reading: dict[str, float]) -> bool:
    """POST one reading to the node agent. Returns False instead of raising if it is down."""
    try:
        client.post(AGENT_URL, json=reading).raise_for_status()
        return True
    except httpx.HTTPError as exc:
        # The node agent may not be running yet; keep going and try again next tick.
        log.warning("could not reach node agent at %s: %s", AGENT_URL, exc)
        return False


def main() -> None:
    """Read and send a reading every INTERVAL seconds, forever."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log.info("sensor started, reporting to %s every %ss", AGENT_URL, INTERVAL)

    # The first cpu_percent() call has nothing to compare against and always returns
    # 0.0, so prime it once before the loop.
    psutil.cpu_percent(interval=None)

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        while True:
            started = time.monotonic()
            reading = read_state()
            if send_state(client, reading):
                log.info("sent %s", reading)
            # Sleep for whatever is left of the interval so readings stay ~5 s apart
            # even when the POST was slow.
            time.sleep(max(0.0, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
