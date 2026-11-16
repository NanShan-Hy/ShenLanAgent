"""Offline token estimates; financial reservation retains the UTF-8 upper bound.

Encoding patterns and vocabulary hashes are from OpenAI tiktoken 0.14.0
(MIT, see token_data/LICENSE.tiktoken). No requests or token arrays are cached.
"""
from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
from pathlib import Path

import tiktoken

from .core import dumps
from .security import fail


DATA = Path(__file__).with_name('token_data')
FRAMING = 2048
CHUNK_CHARS = 1024
ASSETS = {
    'cl100k_base': '223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7',
    'o200k_base': '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d',
}
PATTERNS = {
    'cl100k_base': r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s",
    'o200k_base': '|'.join([
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}", r" ?[^\s\p{L}\p{N}]+[\r\n/]*", r"\s*[\r\n]+", r"\s+(?!\S)", r"\s+",
    ]),
}


@lru_cache(maxsize=2)
def encoding(name):
    # Local-only loader: a missing/corrupt asset never triggers a network download.
    raw = (DATA / (name + '.tiktoken')).read_bytes()
    if hashlib.sha256(raw).hexdigest() != ASSETS[name]:
        raise ValueError('tokenizer vocabulary checksum mismatch')
    ranks = {base64.b64decode(token, validate=True): int(rank)
             for token, rank in (line.split() for line in raw.splitlines() if line)}
    return tiktoken.Encoding(name='aigateway_' + name, pat_str=PATTERNS[name],
                            mergeable_ranks=ranks, special_tokens={})


def warm_tokenizers():
    for name in ASSETS:
        encoding(name)


def reservation_input_bound(payload):
    """Existing worst-case reservation, independent of admission token estimates."""
    text,media=budget_material(payload)
    return len(text.encode('utf-8')) + FRAMING + media


def budget_material(payload):
    media=getattr(payload,'media_tokens',0)
    def scrub(value):
        if isinstance(value,dict):
            if value.get('type') in ('input_image','image_url'):return {'type':'image','budgeted':True}
            return {k:scrub(v) for k,v in value.items()}
        if isinstance(value,list):return [scrub(v) for v in value]
        return value
    return dumps(scrub(payload) if media else payload),media


def estimate_input(payload, provider):
    text,media = budget_material(payload)
    byte_bound = len(text.encode('utf-8')) + FRAMING
    if provider not in ('openai', 'account_pool'):
        # DeepSeek and arbitrary compatible vendors have different tokenizers.
        return byte_bound+media
    try:
        # Both common OpenAI vocabularies, plus 10% and framing allowance. Chunking
        # bounds repeated-string BPE work and errs upwards at chunk boundaries.
        counts = [sum(len(encoding(name).encode_ordinary(text[i:i + CHUNK_CHARS]))
                      for i in range(0, len(text), CHUNK_CHARS)) for name in ASSETS]
    except (OSError, ValueError):
        fail(503, 'tokenizer_unavailable', '本地Token计算资源不可用，请联系管理员检查部署')
    return min(byte_bound, (max(counts) * 110 + 99) // 100 + FRAMING)+media
