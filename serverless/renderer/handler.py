"""Renderer Lambda: renders one batch of pages, saves the PNGs, calls back the web app."""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from serverless.common import callback
from serverless.common.pdf_pages import render_pages
from serverless.common.storage import input_key, page_key, read_object, s3_client

log = logging.getLogger()
log.setLevel(logging.INFO)


def render_handler(event, context=None):
    s3 = s3_client()
    bucket, job_id = event["bucket"], event["job_id"]
    start, end = event["page_start"], event["page_end"]
    dpi = int(os.environ.get("RENDER_DPI", "300"))
    started = time.monotonic()

    pdf = read_object(s3, bucket, input_key(job_id))
    # Render on this thread and upload on others, so CPU and network overlap
    with ThreadPoolExecutor(max_workers=4) as pool:
        uploads = [
            pool.submit(
                s3.put_object,
                Bucket=bucket,
                Key=page_key(job_id, number),
                Body=png,
                ContentType="image/png",
            )
            for number, png in render_pages(pdf, start, end, dpi)
        ]
        for upload in uploads:
            upload.result()  # raises if any upload failed

    render_ms = int((time.monotonic() - started) * 1000)
    log.info("job %s: pages %d-%d rendered in %d ms", job_id, start, end, render_ms)

    result = {
        "status": "ok",
        "job_id": job_id,
        "page_count": event["page_count"],
        "page_start": start,
        "page_end": end,
        "chunk_index": event["chunk_index"],
        "chunk_count": event["chunk_count"],
        "render_ms": render_ms,
    }
    callback.post(result)
    return result
