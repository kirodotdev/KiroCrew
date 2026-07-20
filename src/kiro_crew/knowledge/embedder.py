"""Ollama embedding client with graceful fallback.

Calls Ollama's /api/embeddings endpoint. Returns None silently if Ollama
is not running or the model isn't available — no errors, no degraded UX.
"""
from __future__ import annotations

import hashlib
import json
import logging
import socket
import struct
import time
import urllib.error
import urllib.request

from kiro_crew.executors import run_in_embed_pool
from kiro_crew.knowledge.chunker import CHUNK_OVERLAP, CHUNK_TOKEN_SIZE

logger = logging.getLogger(__name__)

# Qwen3-Embedding-0.6B (1024d) — dedicated embedding model, not the generative LLM.
# Pulled from the public Ollama registry: `ollama pull qwen3-embedding:0.6b`.
# Documented fallback for smaller installs: `nomic-embed-text` (768d).
DEFAULT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_BASE_URL = "http://localhost:11434"
TIMEOUT = 10  # seconds
NEGATIVE_CACHE_TTL = 300  # seconds before re-checking failed availability
# Safety bound (chars) on chunk content folded into an item embedding. This is a
# backstop for a pathological un-chunked blob (e.g. a separator-less minified file or
# base64 string that the chunker's whitespace-based splitter cannot break), NOT a limit
# on normal chunks. The HeadingAwareChunker already bounds every chunk to
# ~CHUNK_TOKEN_SIZE + CHUNK_OVERLAP tokens; we size this above that maximum (using a
# deliberately generous 10 chars/token — real text runs ~4-6, dense code/paths/URLs
# higher) so a normally-chunked passage is always embedded whole. Configured embed
# models have ample context (snowflake-arctic-embed2 8192 tok, qwen3-embedding ~32K),
# so this only ever fires for inputs that bypassed normal chunk bounding — and when it
# does, the truncation is logged rather than silent.
_EMBED_CONTENT_BUDGET = (CHUNK_TOKEN_SIZE + CHUNK_OVERLAP) * 10


class OllamaEmbedder:
    """Embed text via Ollama. Returns None on any failure (graceful degradation)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_secs: float = TIMEOUT,
        content_budget: int = _EMBED_CONTENT_BUDGET,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        # Per-request embed timeout and content-fold budget. Both default to the
        # module constants (preserving prior behavior) but are overridable via
        # config so operators can raise the timeout when a large chunk times out
        # on a cold model load (the embed then never completes and the item is
        # retried every maintenance pass). See create_embedder_from_config.
        self.timeout_secs = timeout_secs
        self.content_budget = content_budget
        self._available: bool | None = None  # cached availability check
        self._last_check: float = 0.0

    def is_available(self) -> bool:
        """Check if Ollama is reachable. Caches positive result; negative cached with TTL.

        Blocking (urllib probe, 3s timeout on a hung connection) — coroutines on
        the gateway event loop MUST use :meth:`is_available_async` instead.
        """
        if self._available is True:
            return True
        if self._available is False and (time.time() - self._last_check) < NEGATIVE_CACHE_TTL:
            return False
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                self._available = resp.status == 200
        except Exception:
            self._available = False
        self._last_check = time.time()
        if not self._available:
            logger.info("Ollama not available at %s — embeddings disabled", self.base_url)
        return bool(self._available)

    async def is_available_async(self) -> bool:
        """Loop-safe :meth:`is_available` — runs the blocking probe off-loop.

        Single greppable offload point for the availability probe: dashboard
        handlers and the knowledge watcher call this instead of each carrying
        its own ``run_in_executor(None, embedder.is_available)`` boilerplate
        (the copy-paste drift that let inline probes stall the gateway loop).
        """
        # Fast path: cached-positive needs no thread hop.
        if self._available is True:
            return True
        return await run_in_embed_pool(self.is_available)

    def embed(self, text: str) -> list[float] | None:
        """Embed a single text. Returns float list or None on failure."""
        if not text.strip():
            return None
        if not self.is_available():
            return None
        try:
            payload = json.dumps({"model": self.model, "prompt": text}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/api/embeddings",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_secs) as resp:  # nosemgrep
                data = json.loads(resp.read())
            return data.get("embedding")
        except (socket.timeout, TimeoutError):
            # A timeout is operator-actionable (slow/overloaded Ollama, or a chunk
            # near the content budget on CPU-only inference) -- surface it at
            # WARNING so repeated misses aren't silent at default log levels.
            logger.warning(
                "Ollama embed timeout (%ss, content=%d chars) at %s; consider raising "
                "knowledge.embed_timeout_secs or lowering knowledge.embed_content_budget.",
                self.timeout_secs,
                len(text),
                self.base_url,
            )
            self._available = None  # invalidate so next call re-checks
            return None
        except Exception as e:
            if isinstance(e, urllib.error.URLError) and isinstance(
                getattr(e, "reason", None), (socket.timeout, TimeoutError)
            ):
                logger.warning(
                    "Ollama embed timeout (%ss, content=%d chars) at %s; consider raising "
                    "knowledge.embed_timeout_secs or lowering knowledge.embed_content_budget.",
                    self.timeout_secs,
                    len(text),
                    self.base_url,
                )
            else:
                logger.debug("Ollama embed failed: %s", e)
            self._available = None  # invalidate so next call re-checks
            return None

    def embed_for_item(
        self, title: str, summary: str | None, content: str | None = None
    ) -> list[float] | None:
        """Embed title + summary + chunk content for knowledge items.

        Content is included so vector search matches on body text, not just the
        title/summary, and is appended after them. A normally-chunked passage is
        embedded whole; ``_EMBED_CONTENT_BUDGET`` is only a backstop for a pathological
        un-chunked blob, and truncation is logged (never silent) when it fires so the
        dropped-tail recall gap is observable rather than hidden.
        """
        parts = [title]
        if summary:
            parts.append(summary)
        if content:
            if len(content) > self.content_budget:
                logger.warning(
                    "Embedding content truncated %d -> %d chars for item %r; chunk "
                    "exceeds the safety bound (likely un-chunked/separator-less input). "
                    "Tail is excluded from the vector.",
                    len(content), self.content_budget, title,
                )
                content = content[:self.content_budget]
            parts.append(content)
        return self.embed(" ".join(parts))


def floats_to_bytes(vec: list[float]) -> bytes:
    """Serialize float list to compact binary for SQLite BLOB storage."""
    return struct.pack(f"{len(vec)}f", *vec)


def bytes_to_floats(data: bytes) -> list[float]:
    """Deserialize binary BLOB back to float list."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def embed_signature(
    model: str, content_budget: int = _EMBED_CONTENT_BUDGET, *, base_url: str = ""
) -> str:
    """Signature over the embedding inputs a re-embed can actually change.

    Captures the model name, the inference endpoint (``base_url``), and the content
    budget — change any one and a stored vector may come from a different vector
    space, and re-embedding the same item content fixes it. ``base_url`` matters
    because the same model name served by a different backend (different host, or a
    different model variant behind the same name) can produce vectors that no longer
    align with query embeddings. Items whose stored ``embedding_sig`` differs from
    the current one are re-embedded by the sig-gated rebuild (manual trigger and
    watcher self-heal both use it).

    ``content_budget`` is the SECOND positional parameter (preserving the original
    ``embed_signature(model, content_budget)`` contract); ``base_url`` is
    KEYWORD-ONLY so adding it can never silently rebind a caller's positional
    ``content_budget`` to a URL (which would mint a wrong-but-valid signature and
    trigger spurious rebuilds). The hash string order is unchanged, so existing
    stored signatures stay valid regardless of how the args are passed.

    ponytail: does NOT cover edits to ``embed_for_item``'s assembly logic (field
    set / join separator) — a value hash can't see code. Ceiling: such a change
    needs a manual ``force`` rebuild. Upgrade path: add an ast-normalized source
    hash here if that logic starts churning.
    """
    raw = f"{model}|{base_url}|{content_budget}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def embedder_signature(embedder: OllamaEmbedder) -> str:
    """Current sig for an embedder. Single source of truth for callsites so the
    set of inputs (model + base_url + budget) can't drift between them."""
    return embed_signature(
        embedder.model, embedder.content_budget, base_url=embedder.base_url
    )


def _positive_or(value: object, default: float) -> float:
    """Return ``value`` when it is a positive number, else ``default``.

    Guards a config-sourced timeout/budget: a missing key (None), an explicit
    ``0`` sentinel, a negative value, or a non-numeric all fall back to the
    built-in default rather than passing a nonsensical value to the embedder
    (a negative timeout makes every embed raise/return None; a negative budget
    mangles slicing and log output). ``bool`` is excluded so ``True``/``False``
    can't masquerade as ``1``/``0``.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return default


def create_embedder_from_config(config: dict) -> OllamaEmbedder | None:
    """Create embedder from shared memory embedding config. Returns None if disabled.

    Uses the same config as Vector Memory (memory.embedding_provider/model/url)
    so knowledge and memory share one embedding setup.
    """
    memory_cfg = config.get("memory", {})
    if memory_cfg.get("embedding_provider") != "ollama":
        return None
    model = memory_cfg.get("embedding_model", DEFAULT_MODEL)
    base_url = memory_cfg.get("embedding_url", DEFAULT_BASE_URL)
    # Knowledge-Library-specific embed tuning. Fall back to the module defaults
    # unless the config supplies a *positive* number: a missing key, an explicit
    # 0 sentinel, a negative value, or a non-numeric all resolve to the built-in
    # default rather than passing a nonsensical timeout/budget to the embedder.
    knowledge_cfg = config.get("knowledge", {}) or {}
    timeout_secs = _positive_or(knowledge_cfg.get("embed_timeout_secs"), TIMEOUT)
    content_budget = int(_positive_or(knowledge_cfg.get("embed_content_budget"), _EMBED_CONTENT_BUDGET))
    return OllamaEmbedder(
        model=model,
        base_url=base_url,
        timeout_secs=timeout_secs,
        content_budget=content_budget,
    )
