"""Run the four-job fallback batch from a cold start using a new normal account."""
import getpass
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import requests

import burst_trial


def main():
    site = os.environ["BURST_SITE_URL"]
    admin_email = os.environ["BURST_ADMIN_EMAIL"]
    password = getpass.getpass("Admin password (hidden): ")
    admin = burst_trial.login(site, {"email": admin_email, "password": password})
    limit = time.monotonic() + 900
    while any(m["active"] for m in burst_trial.scan(admin, site).values()):
        if time.monotonic() >= limit:
            raise RuntimeError("No clean cold start observed within 15 minutes")
        time.sleep(10)
    label = "fallback-batch-" + uuid.uuid4().hex[:12]
    creds = {"email": "group17-" + label + "@example.test", "password": secrets.token_urlsafe(24)}
    reply = requests.post(site + "/signup", data={**creds, "confirm_password": creds["password"]},
                          allow_redirects=False, timeout=30)
    if reply.status_code != 303:
        raise RuntimeError(f"Signup returned HTTP {reply.status_code}")
    os.environ["BURST_ADMIN_PASSWORD"] = password
    os.environ["BURST_USERS_JSON"] = json.dumps([creds])
    args = SimpleNamespace(label=label, jobs=4, timeout_minutes=30, match_label=None, fallback_only=True,
                           out=Path(__file__).resolve().parent/"results"/label)
    print("Starting cold batch", label, flush=True)
    burst_trial.run(args)


if __name__ == "__main__":
    main()

