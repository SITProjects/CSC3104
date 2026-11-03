"""Errors the server turns into HTTP answers. Messages are safe to return: they
never hold keys, tokens or command output."""


class HelperError(Exception):
    status = 500


class BadRequest(HelperError):
    status = 400


class NotFound(HelperError):
    status = 404


class Conflict(HelperError):
    status = 409


class CommandFailed(HelperError):
    """A wg, k3s or kubectl command failed. Only the program's name and exit
    code are kept; its output may hold secrets, so it is never returned."""

    status = 502

    def __init__(self, program: str, returncode: int):
        super().__init__(f"{program} failed (exit {returncode})")
        self.program = program
        self.returncode = returncode
