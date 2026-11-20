# Page renderer

Renders one batch of pages with PyMuPDF (300 DPI grayscale), saves them to
`jobs/{job_id}/pages/{page:04d}.png`, then sends the signed `pages-ready`
callback. See `serverless/README.md` for the contract.

- `handler.py`: the Lambda entry point `render_handler`
