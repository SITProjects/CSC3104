-- Point the OCR job type at the image the Publish worker images workflow pushes
-- to GHCR, like the python and pytorch types. The old idlecloud/ocr:0.1.0 only
-- existed where it was loaded by hand, so fresh workers could not start OCR
-- copies. scripts/load-worker-images.sh builds and loads this tag locally.
--
-- Only the seed's local tag is replaced: on the hub, services/autoscaler/ocrimage
-- may already have pinned an exact ghcr.io/...@sha256 digest, which must stay.
UPDATE job_types
SET image = 'ghcr.io/sitprojects/idlecloud-ocr:main'
WHERE name = 'ocr' AND image = 'idlecloud/ocr:0.1.0';
