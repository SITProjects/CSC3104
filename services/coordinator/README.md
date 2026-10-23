# Coordinator

Owner: member 3.

Provide the shared internal API, split jobs into tasks and replicas, and schedule
Kubernetes Jobs. Issue presigned storage URLs, track progress and node
heartbeats, and recover interrupted work from checkpoints. Shared contracts and
SQL migrations belong in root `shared/`.

`burst.py` holds member 6's read-only `GET /admin/burst` (admins only). It
reads the burst cost views for the web admin page. See the autoscaler README,
section "Admin page".
