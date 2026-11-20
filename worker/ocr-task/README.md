# OCR task worker

Run the OCR container: fetch a replica manifest, resume from its checkpoint,
process pages with Tesseract, save progress after each page, and upload results
and report hashes. Handle termination through the shared checkpoint flow.
Workers use presigned URLs and do not hold AWS credentials.
