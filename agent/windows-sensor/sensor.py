"""
Windows sensor: reports keyboard/mouse idle time and host CPU load to the node agent.

Runs on the Windows host (not inside WSL). Every few seconds it POSTs
{"idle_seconds": float, "cpu_percent": float} to the node agent's /state endpoint.

A tray icon shows the owner whether the PC is lent out.
"""

import ctypes
import logging
import os
import threading
import time
from collections.abc import Callable

import httpx
import psutil

AGENT_URL = os.environ.get("IDLECLOUD_AGENT_URL", "http://localhost:9100/state")
INTERVAL = 5  # seconds between readings
REQUEST_TIMEOUT = 2  # seconds to wait for the node agent before giving up
TRAY_ENABLED = os.environ.get("IDLECLOUD_TRAY", "1") != "0"

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


def send_state(client: httpx.Client, reading: dict[str, float]) -> dict | None:
    """POST one reading to the node agent and return its reply, the agent's
    current decision. Returns None instead of raising if it is down."""
    try:
        response = client.post(AGENT_URL, json=reading)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # The node agent may not be running yet; keep going and try again next tick.
        log.warning("could not reach node agent at %s: %s", AGENT_URL, exc)
        return None


class LendingStatus:
    """What the owner is shown: whether the PC is lent out, and why not."""

    def __init__(self) -> None:
        self.lending: bool | None = None  # None until the first reply
        self.text = ""

    def update(self, reply: dict | None) -> bool:
        """Apply the agent's reply (None if unreachable). Returns True if the
        owner should be told, i.e. lending started or stopped."""
        # Only IDLE means lent. Anything else, including no answer, is not:
        # the agent itself treats a missing sensor as BUSY.
        lending = reply is not None and reply.get("state") == "IDLE"
        if lending:
            self.text = "Lending compute"
        elif reply is None:
            self.text = "Not lending: node agent not running"
        else:
            self.text = f"Not lending: {reply.get('reason') or 'owner active'}"
        changed = lending != self.lending
        self.lending = lending
        return changed


def run(stop: threading.Event, on_change: Callable[[LendingStatus], None]) -> None:
    """Read and send a reading every INTERVAL seconds until `stop` is set.
    Calls on_change whenever lending starts or stops."""
    # The first cpu_percent() call has nothing to compare against and always returns
    # 0.0, so prime it once before the loop.
    psutil.cpu_percent(interval=None)
    status = LendingStatus()

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        while not stop.is_set():
            started = time.monotonic()
            try:
                reading = read_state()
            except OSError as exc:
                # GetLastInputInfo can fail, e.g. on a locked or disconnected session.
                # Skip this tick rather than crash; the node agent treats missing
                # readings as BUSY, which is the safe default.
                log.warning("could not read sensor state: %s", exc)
            else:
                reply = send_state(client, reading)
                if reply is not None:
                    log.info("sent %s", reading)
                if status.update(reply):
                    log.info("%s", status.text)
                    on_change(status)
            # Sleep for whatever is left of the interval so readings stay ~5 s apart
            # even when the POST was slow. wait() returns early on quit.
            stop.wait(max(0.0, INTERVAL - (time.monotonic() - started)))


def run_with_tray(stop: threading.Event) -> bool:
    """Run the sensor loop behind a tray icon. Returns False if the tray
    packages are missing, so the caller can run without it."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        log.warning("no tray icon (%s), showing changes in this window only", exc)
        return False

    def dot(colour: str) -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(image).ellipse((6, 6, 58, 58), fill=colour)
        return image

    green, grey = dot("#2e9d4f"), dot("#8a8f98")

    def show(status: LendingStatus) -> None:
        icon.icon = green if status.lending else grey
        # Windows cuts tooltips at 127 characters
        icon.title = f"idlecloud: {status.text}"[:127]

    def quit_sensor(icon, item) -> None:
        # With no readings the agent goes BUSY within 30 s, so this also stops lending
        stop.set()
        icon.stop()

    icon = pystray.Icon(
        "idlecloud",
        grey,
        "idlecloud: starting",
        menu=pystray.Menu(pystray.MenuItem("Quit (stops lending)", quit_sensor)),
    )

    def setup(icon) -> None:
        # pystray runs this in its own thread, so the icon stays responsive
        # while the loop sleeps or waits on the agent
        icon.visible = True
        run(stop, show)

    icon.run(setup)
    return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log.info("sensor started, reporting to %s every %ss", AGENT_URL, INTERVAL)
    stop = threading.Event()
    try:
        if TRAY_ENABLED and run_with_tray(stop):
            return
        run(stop, lambda status: None)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
