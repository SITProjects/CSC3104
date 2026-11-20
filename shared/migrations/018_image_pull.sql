-- Whether a copy's node is still downloading its image (issue #189).
--
-- A copy is RUNNING from the moment its Kubernetes Job exists. Once its Pod is
-- on a node, the node may first have to pull the job's image, which takes
-- minutes for the several-GB GPU image. The coordinator's pod watcher records
-- that here, so the job page can say "Downloading image" instead of showing a
-- copy that looks stuck.
--
-- pulling_since: when the node started pulling this attempt's image; NULL
-- once the container has started, or if the image was already there.
-- pull_error: the kubelet's message when the pull failed (ErrImagePull,
-- ImagePullBackOff); NULL otherwise. The kubelet keeps retrying, so a later
-- successful pull clears it.
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS pulling_since TIMESTAMPTZ;
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS pull_error TEXT;
