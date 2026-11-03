"""Runs wg, k3s and kubectl. Arguments are passed as a list, never through a
shell. stdout can hold keys and tokens, so it is returned but never logged."""

import logging
import subprocess
from collections.abc import Sequence
from typing import Protocol

from enroll.errors import CommandFailed

log = logging.getLogger("enroll")

TIMEOUT_S = 30


class Runner(Protocol):
    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str: ...


def run(args: Sequence[str], stdin: str | None = None) -> str:
    """stdout of the command. Raises CommandFailed on a non-zero exit."""
    try:
        done = subprocess.run(
            list(args),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        raise CommandFailed(args[0], 127) from None
    except subprocess.TimeoutExpired:
        raise CommandFailed(args[0], -1) from None
    if done.returncode != 0:
        # stderr names the problem (no such peer, bad flag); it carries no secrets
        log.warning(
            "%s %s exited %d: %s",
            args[0],
            args[1] if len(args) > 1 else "",
            done.returncode,
            done.stderr.strip()[:500],
        )
        raise CommandFailed(args[0], done.returncode)
    return done.stdout
