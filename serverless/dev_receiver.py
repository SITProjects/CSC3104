"""Stand-in for member 5's POST /hooks/pages-ready, for local testing only.

It checks the HMAC signature the way the real endpoint should, then prints each
callback. Run from the repo root in a second terminal:
    python -m serverless.dev_receiver
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from serverless.common.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify

DEFAULT_SECRET = "local-test-secret"
DEFAULT_PORT = 8099


def make_server(
    secret: str = DEFAULT_SECRET, port: int = DEFAULT_PORT, quiet: bool = False
) -> HTTPServer:
    """A receiver that records every request in server.received and server.rejected."""
    seen: set[tuple[str, int]] = set()  # (job_id, page_start), to spot repeats
    pages_done: dict[str, int] = {}

    def say(line: str) -> None:
        if not quiet:
            print(line, flush=True)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/hooks/pages-ready":
                self.send_response(404)
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            timestamp = self.headers.get(TIMESTAMP_HEADER)
            if not verify(secret, body, timestamp, self.headers.get(SIGNATURE_HEADER)):
                server.rejected += 1
                say("REJECTED  bad or missing signature")
                self.send_response(401)
                self.end_headers()
                return

            data = json.loads(body)
            server.received.append(data)
            job_id = data["job_id"]
            if data["status"] == "failed":
                say(f"FAILED    job {job_id}: {data['error']}")
            elif (job_id, data["page_start"]) in seen:
                say(
                    f"REPEAT    job {job_id} pages {data['page_start']}-{data['page_end']}"
                )
            else:
                seen.add((job_id, data["page_start"]))
                count = data["page_end"] - data["page_start"] + 1
                pages_done[job_id] = pages_done.get(job_id, 0) + count
                say(
                    f"OK        job {job_id} pages {data['page_start']}-{data['page_end']} "
                    f"(batch {data['chunk_index'] + 1}/{data['chunk_count']}, "
                    f"{data['render_ms']} ms), "
                    f"{pages_done[job_id]}/{data['page_count']} pages ready"
                )
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass  # keep the console clean

    server = HTTPServer(("0.0.0.0", port), Handler)
    server.received = []
    server.rejected = 0
    return server


def start_in_background(secret: str = DEFAULT_SECRET, port: int = 0) -> HTTPServer:
    """Start a quiet receiver on a free port, for scripted tests. Its URL is server.url."""
    server = make_server(secret, port, quiet=True)
    server.url = f"http://localhost:{server.server_port}/hooks/pages-ready"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    secret = os.environ.get("HMAC_SECRET", DEFAULT_SECRET)
    print(f"Listening on http://localhost:{DEFAULT_PORT}/hooks/pages-ready")
    make_server(secret).serve_forever()
