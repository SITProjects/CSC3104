import hashlib
import json
import re
import unicodedata
from typing import Any

# Zero-width and byte-order characters that OCR and PDFs sometimes emit invisibly
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
# Any run of spaces, tabs or other non-newline whitespace inside a line
_SPACES = re.compile(r"[^\S\n]+")
# Three or more newlines, i.e. two or more blank lines in a row
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """
    Normalizes OCR text so that trivially different readings compare equal.

    Applies NFKC (folds ligatures such as "\ufb01" and full-width forms), removes
    invisible characters and other control characters, turns every line ending
    into "\\n", collapses whitespace inside each line to single spaces, strips
    each line, keeps at most one blank line in a row, and strips blank lines at
    the start and end. Case and punctuation are kept: those are real differences.

    args:
        text: The raw text, e.g. Tesseract's output for one page.
    returns:
        str: The normalized text, "" if it held only whitespace.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_INVISIBLE)
    text = text.translate(dict.fromkeys(map(ord, "\t\v\f"), " "))
    text = "".join(c for c in text if c == "\n" or unicodedata.category(c) != "Cc")
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", "\n".join(lines))
    return text.strip("\n")


def canonical_json(result: Any) -> bytes:
    """
    Serialises a result to the exact bytes that are uploaded and hashed.

    Keys are sorted, there is no extra whitespace, non-ASCII text is kept as
    UTF-8, and NaN or infinity are rejected, so the same result always gives the
    same bytes on every machine.

    args:
        result: JSON-serialisable result, e.g. {"pages": {"pages/0001.png": "..."}}.
    returns:
        bytes: The UTF-8 encoded JSON.
    raises:
        TypeError: If result contains something that is not JSON.
        ValueError: If result contains NaN or infinity.
    """
    return json.dumps(
        result,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def result_hash(result: Any) -> str:
    """
    Returns the hash a replica reports to /result and the verifier votes on.

    args:
        result: The same result passed to canonical_json.
    returns:
        str: SHA-256 hex digest (64 lowercase characters) of canonical_json(result).
    """
    return hashlib.sha256(canonical_json(result)).hexdigest()
