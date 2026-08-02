"""In-process embedding client with graceful fallback.

Wraps the shared ``LlamaCppEmbedder`` from ``kiro_crew.embeddings``. Returns
None silently if the embedding model is not yet available — no errors, no
degraded UX. Knowledge and vector memory share one loaded model instance
(~700MB RSS).
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import time
from typing import TYPE_CHECKING

from kiro_crew import embeddings as _embeddings
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.knowledge.chunker import CHUNK_OVERLAP, CHUNK_TOKEN_SIZE

if TYPE_CHECKING:
    from kiro_crew.embeddings import EmbeddingBackend

logger = logging.getLogger(__name__)

# Qwen3-Embedding-0.6B (1024d) — dedicated embedding model (not the generative LLM).
# Served in-process via the vendored llama-cpp-python runtime.
DEFAULT_MODEL = "qwen3-embedding:0.6b"
# Retained for config compatibility (knowledge.embed_timeout_secs). The
# in-process backend has no per-request network timeout — the value is stored
# on the embedder but no longer bounds an HTTP call.
TIMEOUT = 10  # seconds
NEGATIVE_CACHE_TTL = 300  # seconds before re-checking failed availability
# Safety bound (chars) on chunk content folded into an item embedding.
_EMBED_CONTENT_BUDGET = (CHUNK_TOKEN_SIZE + CHUNK_OVERLAP) * 10


class InProcessEmbedder:
    """Embed text via the shared EmbeddingBackend. Returns None on any failure."""

    def __init__(
        self,
        model: str | None = None,
        timeout_secs: float = TIMEOUT,
        content_budget: int = _EMBED_CONTENT_BUDGET,
    ):
        # Default the model identity from the active backend so a swapped
        # backend (register_embedding_backend) automatically changes the
        # embed signature and triggers the sig-gated knowledge re-embed.
        self._model_override = model
        # Per-instance content-fold budget (overridable via
        # knowledge.embed_content_budget, see create_embedder_from_config).
        # ``timeout_secs`` is kept for config compatibility only — the
        # in-process runtime has no per-request timeout to apply it to.
        self.timeout_secs = timeout_secs
        self.content_budget = content_budget
        self._available: bool | None = None
        self._last_check: float = 0.0

    @property
    def model(self) -> str:
        if self._model_override:
            return self._model_override
        try:
            return self._get_embedder().model_id
        except Exception:
            return DEFAULT_MODEL

    def _get_embedder(self) -> "EmbeddingBackend":
        # Module-attribute lookup (not a from-import of the function) so test
        # patches of kiro_crew.embeddings.get_shared_embedder stay effective.
        return _embeddings.get_shared_embedder()

    def is_available(self) -> bool:
        """True when the in-process embedder can produce vectors right now.

        NEVER blocks: ``embed()`` on a not-yet-loaded backend kicks a
        background model load and returns ``None`` immediately, so this probe
        is cheap even on the event loop. Negative results are cached with a
        TTL so we don't probe every call; positives are re-validated after
        the TTL too (a broken backend must not report available forever).
        """
        if self._available is not None and (time.time() - self._last_check) < NEGATIVE_CACHE_TTL:
            return self._available
        embedder = self._get_embedder()
        if embedder.is_ready():
            self._available = True
        else:
            # Non-blocking probe: kicks a background load if the model file
            # just landed; returns None (→ unavailable) until the load finishes.
            vec = embedder.embed("_availability_probe")
            self._available = vec is not None
        self._last_check = time.time()
        if not self._available:
            logger.info("Embedding model not yet available — knowledge embeddings disabled")
        return bool(self._available)

    async def is_available_async(self) -> bool:
        """Loop-safe :meth:`is_available` — runs the probe off-loop.

        Single greppable offload point for the availability probe: dashboard
        handlers and the knowledge watcher call this instead of each carrying
        its own ``run_in_executor(None, embedder.is_available)`` boilerplate.
        The in-process probe never blocks on I/O, but a ready backend's probe
        embed still burns CPU — keep it on the ``mc-embed`` bulkhead pool.
        """
        # Fast path: cached result within TTL needs no thread hop.
        if self._available is not None and (time.time() - self._last_check) < NEGATIVE_CACHE_TTL:
            return self._available
        return await run_in_embed_pool(self.is_available)

    def embed(self, text: str) -> list[float] | None:
        """Embed a single text. Returns float list or None on failure."""
        if not text.strip():
            return None
        if not self.is_available():
            return None
        vec = self._get_embedder().embed(text)
        if vec is None:
            # The backend stopped producing vectors (reset/reload failure) —
            # invalidate the cached availability so the next call re-probes
            # instead of reporting available-but-embedding-nothing forever.
            self._available = None
        return vec

    def embed_for_item(
        self, title: str, summary: str | None, content: str | None = None
    ) -> list[float] | None:
        """Embed title + summary + chunk content for knowledge items."""
        parts = [title]
        if summary:
            parts.append(summary)
        if content:
            if len(content) > self.content_budget:
                logger.warning(
                    "Embedding content truncated %d -> %d chars for item %r; chunk "
                    "exceeds the safety bound.",
                    len(content),
                    self.content_budget,
                    title,
                )
                content = content[: self.content_budget]
            parts.append(content)
        return self.embed(" ".join(parts))


# Keep the old name as an alias so references to OllamaEmbedder in type
# annotations and isinstance checks continue to work during the transition.
OllamaEmbedder = InProcessEmbedder


def floats_to_bytes(vec: list[float]) -> bytes:
    """Serialize float list to compact binary for SQLite BLOB storage."""
    return struct.pack(f"{len(vec)}f", *vec)


def bytes_to_floats(data: bytes) -> list[float]:
    """Deserialize an embedding BLOB back to a float list.

    Tolerant of the two shapes that can appear in the ``items.embedding``
    column: the compact binary struct written by :func:`floats_to_bytes`, and
    a legacy JSON-encoded list of floats. Returns ``[]`` for anything it cannot
    decode - empty, a length that is not a positive multiple of 4, or otherwise
    corrupt - rather than raising, so one bad row is skippable instead of
    aborting a whole dedup pass (issue #429). Mirrors the guard already carried
    by the sibling decoder ``knowledge/retrieval.py::_bytes_to_floats``.
    """
    if not data:
        return []
    # Legacy JSON-encoded list of floats (pre-binary storage format).
    try:
        result = json.loads(data)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        pass
    # Binary struct-packed floats. Require >= 16 bytes (>= 4 floats) and a
    # 4-byte-aligned length to avoid misreading a short/garbage blob as floats.
    if isinstance(data, bytes) and len(data) >= 16 and len(data) % 4 == 0:
        try:
            n = len(data) // 4
            return list(struct.unpack(f"{n}f", data))
        except struct.error:
            pass
    return []


def embed_signature(model: str, content_budget: int = _EMBED_CONTENT_BUDGET) -> str:
    """Signature over the embedding inputs a re-embed can actually change.

    Captures the model id and the content budget — change either and a stored
    vector may come from a different vector space, and re-embedding the same
    item content fixes it. Items whose stored ``embedding_sig`` differs from
    the current one are re-embedded by the sig-gated rebuild (manual trigger
    and watcher self-heal both use it). The literal ``inprocess`` token stands
    where the Ollama-era ``base_url`` used to — the in-process runtime has no
    endpoint, and keeping a distinct token forces a one-time re-embed when
    migrating vectors produced by an external server.

    ponytail: does NOT cover edits to ``embed_for_item``'s assembly logic (field
    set / join separator) — a value hash can't see code. Ceiling: such a change
    needs a manual ``force`` rebuild. Upgrade path: add an ast-normalized source
    hash here if that logic starts churning.
    """
    raw = f"{model}|inprocess|{content_budget}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def embedder_signature(embedder: InProcessEmbedder) -> str:
    """Current sig for an embedder. Single source of truth for callsites so the
    set of inputs (model + budget) can't drift between them."""
    return embed_signature(embedder.model, embedder.content_budget)


def _positive_or(value: object, default: float) -> float:
    """Return ``value`` when it is a positive number, else ``default``.

    Guards a config-sourced timeout/budget: a missing key (None), an explicit
    ``0`` sentinel, a negative value, or a non-numeric all fall back to the
    built-in default rather than passing a nonsensical value to the embedder
    (a negative budget mangles slicing and log output). ``bool`` is excluded
    so ``True``/``False`` can't masquerade as ``1``/``0``.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return default


def create_embedder_from_config(config: dict) -> InProcessEmbedder:
    """Create the knowledge embedder (embeddings are always-on).

    Reads the raw ``config.json`` dict so it works in the MCP server process
    without a full config load. Every legacy ``embedding_provider`` value
    ("ollama", "none") is treated as enabled — matching the loader's
    ``_coerce_embedding_provider`` so knowledge and vector memory never
    split-brain on the same config file.
    """
    # The model identity is derived live from the active backend, so a
    # register_embedding_backend swap changes the embed signature and triggers
    # the sig-gated knowledge re-embed. A legacy Ollama-era ``embedding_model``
    # config key is deliberately IGNORED here: honoring it would pin the sig to
    # a name the in-process runtime doesn't serve (e.g. "nomic-embed-text"),
    # mislabeling vectors actually produced by the bundled model AND defeating
    # swap-triggered re-embeds.
    #
    # Knowledge-Library-specific embed tuning survives the in-process move:
    # fall back to the module defaults unless the config supplies a *positive*
    # number (missing key, 0 sentinel, negative, or non-numeric all resolve to
    # the built-in default).
    knowledge_cfg = config.get("knowledge", {}) or {}
    timeout_secs = _positive_or(knowledge_cfg.get("embed_timeout_secs"), TIMEOUT)
    content_budget = int(
        _positive_or(knowledge_cfg.get("embed_content_budget"), _EMBED_CONTENT_BUDGET)
    )
    return InProcessEmbedder(timeout_secs=timeout_secs, content_budget=content_budget)
