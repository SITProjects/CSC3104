# PDF splitter

Handles the S3 event for `jobs/{job_id}/input.pdf`, counts the pages, and starts
one renderer Lambda per 20 pages at the same time. A file that is not a usable
PDF gets a `failed` callback instead. See `serverless/README.md` for the contract.

- `handler.py`: the Lambda entry point `split_handler`
- `chunks.py`: splits a page count into 20-page batches, max 500 pages
