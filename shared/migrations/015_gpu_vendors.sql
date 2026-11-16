-- GPU job types run on NVIDIA and AMD workers alike.
--
-- gpu_vendors lists the GPU vendors a GPU job type's copies may run on. The
-- coordinator keeps the copies on worker nodes whose idlecloud/gpu label is one
-- of them (infra/join-worker.sh sets nvidia or amd). It is NULL for CPU types.
--
-- A job type still names one image. Each GPU worker pulls the build for its
-- own GPU under that name: an AMD worker's K3s rewrites the pytorch-gpu image
-- to its ROCm build (idlecloud-pytorch-rocm, same tag) when it pulls, and an
-- NVIDIA worker pulls it as it is. So a GPU job type's image must be a tag,
-- never a digest: a digest names one vendor's build only.
ALTER TABLE job_types ADD COLUMN IF NOT EXISTS gpu_vendors TEXT[];

-- GPU types from before this migration ran on NVIDIA only
UPDATE job_types SET gpu_vendors = ARRAY['nvidia'] WHERE gpu AND gpu_vendors IS NULL;

ALTER TABLE job_types DROP CONSTRAINT IF EXISTS job_types_gpu_vendors_check;
ALTER TABLE job_types ADD CONSTRAINT job_types_gpu_vendors_check
    CHECK (
        (NOT gpu AND gpu_vendors IS NULL)
        OR (
            gpu
            AND cardinality(gpu_vendors) > 0
            AND gpu_vendors <@ ARRAY['nvidia', 'amd']::TEXT[]
        )
    );

ALTER TABLE job_types DROP CONSTRAINT IF EXISTS job_types_gpu_image_tag_check;
ALTER TABLE job_types ADD CONSTRAINT job_types_gpu_image_tag_check
    CHECK (NOT gpu OR position('@' IN image) = 0);

-- The one PyTorch GPU type now takes either vendor's GPU
UPDATE job_types
SET gpu_vendors = ARRAY['nvidia', 'amd'],
    description = 'Your own Python script with PyTorch on an NVIDIA (CUDA) or AMD (ROCm) GPU, numpy, pandas and scikit-learn, run once on an idle PC'
WHERE name = 'pytorch-gpu';
