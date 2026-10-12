"""Splitter Lambda, triggered when jobs/{job_id}/input.pdf lands in S3.

It counts the pages, then starts one renderer Lambda per 20 pages at the same
time (fan-out). With RENDER_FUNCTION_NAME unset, it calls the renderer in-process
instead, which lets the whole flow run without AWS.
"""

import json
import logging
import os
import time

from serverless.common import callback
from serverless.common.errors import BadPdfError
from serverless.common.pdf_pages import count_pages
from serverless.common.storage import (
    input_key,
    job_id_from_key,
    read_object,
    s3_client,
)
from serverless.splitter.chunks import make_chunks

log = logging.getLogger()
log.setLevel(logging.INFO)


def split_handler(event, context=None):
    s3 = s3_client()
    render_function = os.environ.get("RENDER_FUNCTION_NAME", "")
    lambda_client = None
    if render_function:
        import boto3

        lambda_client = boto3.client("lambda")

    results, errors = [], []
    for record in event.get("Records", []):
        # Each record is handled on its own, so one failing job does not stop the others
        try:
            result = _split_one(record, s3, lambda_client, render_function)
        except Exception as error:
            log.exception("record %s failed", record.get("s3", {}).get("object", {}))
            errors.append(str(error))
            continue
        if result:
            results.append(result)

    if errors:
        # Raise so Lambda retries the event. Batches that already started may run
        # twice, which is safe: pages are overwritten and the web app ignores repeats.
        raise RuntimeError(f"{len(errors)} record(s) failed: {errors}")
    return {"jobs": results}


def _split_one(record: dict, s3, lambda_client, render_function: str) -> dict | None:
    bucket = record["s3"]["bucket"]["name"]
    job_id = job_id_from_key(record["s3"]["object"]["key"])
    if job_id is None:
        log.warning("ignoring key %s", record["s3"]["object"]["key"])
        return None
    started = time.monotonic()

    pdf = read_object(s3, bucket, input_key(job_id))
    try:
        page_count = count_pages(pdf)
        chunks = make_chunks(page_count)
    except BadPdfError as error:
        log.warning("job %s rejected: %s", job_id, error)
        callback.post({"status": "failed", "job_id": job_id, "error": str(error)})
        return {"job_id": job_id, "error": str(error)}

    for index, (start, end) in enumerate(chunks):
        payload = {
            "bucket": bucket,
            "job_id": job_id,
            "page_count": page_count,
            "page_start": start,
            "page_end": end,
            "chunk_index": index,
            "chunk_count": len(chunks),
        }
        if lambda_client:
            # "Event" means async, so every renderer starts at the same time
            lambda_client.invoke(
                FunctionName=render_function,
                InvocationType="Event",
                Payload=json.dumps(payload).encode(),
            )
        else:
            from serverless.renderer.handler import render_handler

            render_handler(payload)

    split_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "job %s: %d pages, %d render batches, split took %d ms",
        job_id,
        page_count,
        len(chunks),
        split_ms,
    )
    return {
        "job_id": job_id,
        "page_count": page_count,
        "chunks": len(chunks),
        "split_ms": split_ms,
    }
