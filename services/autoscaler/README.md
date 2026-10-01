# Cloud burst autoscaler

Owner: member 6.

Monitor queued work and available campus capacity, launch bounded EC2 burst
capacity, and retire unused workers. Manage burst slots and cost controls, and
coordinate Spot interruption handling with the existing eviction and checkpoint
flow. Infrastructure templates belong in root `infra/`.
