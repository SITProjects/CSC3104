# Linux node agent

Owner: member 2.

Receive Windows sensor readings, decide whether the worker is idle or busy,
apply or remove the owner-active Kubernetes taint, and send coordinator
heartbeats. Run inside the worker's Linux environment.
