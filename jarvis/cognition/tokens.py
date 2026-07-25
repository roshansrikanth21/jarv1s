"""Token estimation for prompt budgeting.

Uses tiktoken when installed (cl100k_base ≈ GPT/Claude-ish). Otherwise a
whitespace + CJK-aware heuristic that is tighter than naive ``len//4``.
"""
from __future__ import annotations

import re
from functools import lru_cache

_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]"
)
_WORD_RE = re.compile(r"\S+")

_tiktoken_enc = None
_tiktoken_checked = False


def _try_tiktoken():
    global _tiktoken_enc, _tiktoken_checked
    if _tiktoken_checked:
        return _tiktoken_enc
    _tiktoken_checked = True
    try:
        import tiktoken  # type: ignore
        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _tiktoken_enc = None
    return _tiktoken_enc


@lru_cache(maxsize=256)
def estimate_tokens(text: str) -> int:
    """Return a conservative token count for budgeting (never under-count badly)."""
    if not text:
        return 0
    enc = _try_tiktoken()
    if enc is not None:
        try:
            return max(1, len(enc.encode(text)))
        except Exception:
            pass
    return _heuristic_tokens(text)


def _heuristic_tokens(text: str) -> int:
    # CJK scripts are closer to ~1–2 chars/token; Latin closer to ~4 chars or ~0.75/word.
    cjk = len(_CJK_RE.findall(text))
    rest_len = max(0, len(text) - cjk)
    words = len(_WORD_RE.findall(text))
    # Blend word count with char density; pad slightly so we trim early rather than overflow.
    latin_est = max(rest_len / 4.0, words * 1.3)
    cjk_est = cjk * 1.1
    return max(1, int(latin_est + cjk_est + 0.999))


def clear_token_cache() -> None:
    estimate_tokens.cache_clear()
