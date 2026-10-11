class BadPdfError(Exception):
    """The uploaded file cannot be processed (not a PDF, locked, too many pages).

    It is the user's problem, not ours, so the splitter reports it to the web app
    and does not retry.
    """
