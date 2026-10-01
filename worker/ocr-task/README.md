# OCR task worker

Owner: member 5, collaborating with members 3 and 4 on shared helpers.

Run the OCR container: fetch a replica manifest, resume from its checkpoint,
process pages with Tesseract, save progress after each page, and upload results
and report hashes. Handle termination through the shared checkpoint flow.
Workers use presigned URLs and do not hold AWS credentials.
