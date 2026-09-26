"""Approximate token counting.

Jev's exact tokenizer is not published, so we use tiktoken's cl100k_base as a
proxy for *pre-run* estimates. The authoritative count is always the
`usage.input_tokens` value that comes back in each response.
"""

from __future__ import annotations

from functools import lru_cache

from . import config


@lru_cache(maxsize=1)
def _encoder():
    import tiktoken

    return tiktoken.get_encoding(config.TOKENIZER_ENCODING)


def count_tokens(text: str) -> int:
    """Approximate number of input tokens for `text`."""
    if not text:
        return 0
    return len(_encoder().encode(text))
