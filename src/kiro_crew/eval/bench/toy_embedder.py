"""A deterministic stand-in embedder, for tests and plumbing smoke runs ONLY.

**Never use this to produce a reportable number.** It is a hashed bag-of-words
projection, not a language model: it captures lexical overlap and nothing else, so
any retrieval score computed with it measures term matching rather than semantic
recall. :func:`toy_embed_fn` deliberately advertises itself in
:data:`TOY_EMBEDDER_ID` so a run that used it can be recognised after the fact.

It exists for two reasons that are not about convenience.

First, the real embedder is a vendored llama.cpp model whose shared library is not
present on every platform build — on this host ``libllama.so`` is absent from the
Linux payload entirely, so ``get_shared_embedder().wait_ready()`` returns False and
``make_sync_embed_fn()`` returns ``None`` for every input. The ingest guard
correctly refuses to run in that state, which is right for measurement and useless
for verifying that the harness itself works. A deterministic substitute separates
"the harness is broken" from "this host cannot embed".

Second, tests must not depend on a multi-hundred-megabyte model download or on
inference timing. A pure-python embedder makes the ingest, attribution and metric
paths testable in milliseconds.

The projection is fixed by a hash seed, so the same text always yields the same
vector across processes and runs — which is what makes a test assertion about
ranking stable.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Callable

TOY_EMBEDDER_ID = "toy-hashed-bow"

#: Matches the production embedding dimension so the store's ``embedding_dim``
#: default and any dimension assertions behave as they do in production.
TOY_DIM = 1024

_TOKEN = re.compile(r"\w+")


def _token_slots(token: str, dim: int) -> tuple[int, int]:
    """Two slots per token, so a token contributes a stable sparse pattern.

    Two rather than one because a single slot makes near-collisions between
    unrelated tokens indistinguishable from genuine overlap, which would make the
    toy embedder rank pathologically and hide real bugs in the ranker behind noise.
    """
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    a = int.from_bytes(h[:4], "big") % dim
    b = int.from_bytes(h[4:], "big") % dim
    return a, b


def toy_embed(text: str, *, dim: int = TOY_DIM) -> list[float]:
    """L2-normalized hashed bag-of-words. Deterministic across processes.

    Normalized because the store's ranking is cosine-based and its FAISS path uses
    an inner-product index over normalized vectors; an unnormalized vector would
    let document length masquerade as relevance.
    """
    vec = [0.0] * dim
    tokens = _TOKEN.findall(text.lower())
    if not tokens:
        # A zero vector would make cosine similarity undefined and let the ranker
        # order by float noise. One fixed slot keeps empty text comparable and
        # uninteresting rather than random.
        vec[0] = 1.0
        return vec
    for tok in tokens:
        a, b = _token_slots(tok, dim)
        vec[a] += 1.0
        vec[b] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


def toy_embed_fn(*, dim: int = TOY_DIM) -> Callable[[str], list[float]]:
    """A drop-in replacement for ``make_sync_embed_fn()``'s return value."""

    def _fn(text: str) -> list[float]:
        return toy_embed(text, dim=dim)

    return _fn
