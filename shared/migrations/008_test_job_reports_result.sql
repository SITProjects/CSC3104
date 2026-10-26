-- The test job type used to only echo a line. A replica that exits without
-- reporting a result is marked FAILED (ExitWithoutResult), so test jobs could
-- never pass a vote. Now each copy reports the same fixed result the way real
-- workers do (manifest, upload result.json, report its hash), so a healthy
-- cluster verifies and completes them. Standard library only, on a stock image,
-- so there is nothing to build or load onto the workers.
UPDATE job_types
SET description = 'Simple test workload: each copy reports the same fixed result',
    image = 'python:3.12-slim',
    command = ARRAY['python', '-c', $script$
import hashlib, json, os
from urllib import request

base = os.environ["COORDINATOR_URL"].rstrip("/") + "/internal/replicas/" + os.environ["REPLICA_ID"]
token = os.environ["REPLICA_TOKEN"]


def call(method, path, body=None):
    headers = {"Authorization": "Bearer " + token}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = request.Request(base + "/" + path, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=20) as response:
        return json.loads(response.read() or b"null")


manifest = call("GET", "manifest")
# the same bytes shared.normalized_text.canonical_json would produce
result = json.dumps(
    {"message": "Test job completed"},
    sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
).encode()
upload = request.Request(
    manifest["output_upload_url"], data=result, method="PUT",
    headers={"Content-Type": "application/json"},
)
request.urlopen(upload, timeout=20).close()
call("POST", "result", {"result_hash": hashlib.sha256(result).hexdigest(), "exit_code": 0})
print("Test job completed")
$script$]
WHERE name = 'test';
