"""Which embedding SOURCES an LTS build's memory may use: ``memory.embedding_sources``.

The enterprise term ``embedding_model_use`` lands on this scope
(``enterprise_policy_translate.MANAGED``). Its members are the three sources, never a
model name: a name cannot tell the bundled model from an Ollama tag, and a custom
file's label is the user's to choose.

* ``bundled``: the in-process llama.cpp runtime with the downloaded Qwen3 GGUF.
* ``custom``: the in-process runtime with the user's GGUF
  (``memory.embed_model_path`` or ``KIROCREW_EMBED_MODEL_PATH``).
* ``ollama``: an Ollama server (``memory.embedding_provider = "ollama_http"``).

The LTS baseline allows ``bundled`` only. A refused source is not swapped for another:
memory falls back to keyword search, as it does for a broken model path, so the stored
vectors keep their space.

The ``oss`` edition never reads the policy here: every function answers "permitted"
before anything is evaluated.
"""

from __future__ import annotations

from kiro_crew import edition

#: Scope key in ``SCOPE_CATALOG``.
SCOPE = "memory.embedding_sources"

BUNDLED = "bundled"
CUSTOM = "custom"
OLLAMA = "ollama"
#: The policy language's closed ``embeddingModelId`` list.
SOURCES = (BUNDLED, CUSTOM, OLLAMA)

#: The error code a refused source reports (the apply route's 409, the status route).
LOCKED_CODE = "locked_by_policy"

#: Said by a refusal, so a log line or a 409 names the policy, not a broken file.
REFUSED_REASON = "the organisation's policy does not allow the {source} embedding source"


def source_permitted(source: str, *, audit: bool = False) -> bool:
    """Whether the policy lets memory embed through *source*. Always ``True`` on oss.

    Pure in-memory work in an LTS build (:func:`edition_locks.policy_permits`), and
    fail-closed there. Pass *audit* from a decision point that builds or applies a
    backend, so the answer reaches SEL; the cached-backend re-check passes nothing,
    because it runs on every embedder read.
    """
    if not edition.is_lts():
        return True
    from kiro_crew.edition_locks import policy_permits

    return policy_permits(SCOPE, source, audit_tool=f"embedding_source:{source}" if audit else "")


def egress_permitted(host: str) -> bool:
    """Whether ``network.egress`` lets memory text reach the non-loopback *host*.

    Always ``True`` on oss. The caller skips loopback hosts: the LTS egress baseline
    allows nothing, so asking it about ``127.0.0.1`` would refuse a local server.
    Not audited: the settings it guards are re-read by every status poll.
    """
    if not edition.is_lts():
        return True
    from kiro_crew.edition_locks import policy_permits

    return policy_permits("network.egress", host)


def refused_reason(source: str) -> str:
    return REFUSED_REASON.format(source=source)
