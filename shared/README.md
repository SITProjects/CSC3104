# Shared contracts and helpers

Owner: all members. Member 3 authors SQL migrations for team review.

Keep agreed database, API, status, object-storage, and Kubernetes contracts here,
alongside common helpers such as checkpoint handling and text normalization.
Put ordered SQL migrations in `migrations/`. Coordinate contract changes with
the whole team before changing component implementations.

## Result normalization

`normalized_text.py` makes replicas that agree produce the same `result_hash`.
It only needs the standard library, so a worker image can copy that one file.

| Function | Used by | What it does |
| --- | --- | --- |
| `normalize_text(text)` | worker, per page | NFKC, `\n` line endings, single spaces, stripped lines, at most one blank line in a row; case and punctuation kept |
| `canonical_json(result)` | worker, before upload | sorted keys, no extra spaces, UTF-8, no NaN: the exact bytes of `result.json` |
| `result_hash(result)` | worker for `/result`, verifier for voting | SHA-256 hex of `canonical_json(result)` |

Upload `canonical_json(result)` itself, not a re-serialised copy, so the stored
file matches the reported hash. Tests: `python -m pytest tests/unit/test_normalized_text.py`.
