"""Reading a GGUF model's embedding metadata and the context it is loaded with.

A bounded parse of the GGUF header's metadata (architecture, causal flag,
trained context length) decides each model's native context and batch size, so
an encoder is never loaded with a context its weights cannot serve.

:mod:`kiro_crew.embeddings` stays the import path and the patch surface for every
name here; that module's comment lists the owners.
"""

from __future__ import annotations

import logging
import os
import struct
from pathlib import Path
from typing import BinaryIO, NamedTuple

from kiro_crew.embeddings_settings import (
    _ENCODER_ARCHITECTURES,
    _N_CTX,
    _N_UBATCH,
    _POOLING_TYPES,
)

logger = logging.getLogger("kiro_crew.embeddings")


# ── GGUF embedding metadata ──

_GGUF_SCALAR_BYTES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_GGUF_TYPE_UINT32 = 4
_GGUF_TYPE_INT32 = 5
_GGUF_TYPE_BOOL = 7
_GGUF_TYPE_STRING = 8
_GGUF_TYPE_ARRAY = 9
_GGUF_MAX_METADATA_ITEMS = 100_000
_GGUF_MAX_ARRAY_ITEMS = 2_000_000
_GGUF_MAX_DECODED_STRING_BYTES = 1_000_000
# The header is read in chunks of this size: large enough that the shipped
# model's ~6 MiB KV section takes six reads, small enough that the overshoot
# past the last field stays one chunk.
_GGUF_READ_CHUNK_BYTES = 1 << 20
_GGUF_U8 = struct.Struct("<B")
_GGUF_U32 = struct.Struct("<I")
_GGUF_I32 = struct.Struct("<i")
_GGUF_U64 = struct.Struct("<Q")
_GGUF_CAUSAL_SUFFIX = ".attention.causal"
# `<architecture>.context_length`: the position count the model was trained
# with, which llama.cpp reads into ``hparams.n_ctx_train``. For an encoder with
# learned absolute positions it is also the row count of the position table.
_GGUF_CONTEXT_LENGTH_SUFFIX = ".context_length"
# `<architecture>.pooling_type`: the pooling the converter recorded for the model,
# which llama.cpp itself falls back to when no pooling is passed.
_GGUF_POOLING_TYPE_SUFFIX = ".pooling_type"


class _GGUFEmbeddingMetadata(NamedTuple):
    """The KV-header fields that decide a model's llama.cpp load parameters."""

    architecture: str | None
    causal: bool | None
    context_length: int | None
    pooling_type: int | None = None


def _read_gguf_embedding_metadata(path: Path) -> _GGUFEmbeddingMetadata:
    """Read the architecture and its optional causal, context-length and pooling keys.

    All four come from the GGUF KV header. The causal flag is the
    ``<architecture>.attention.causal`` boolean that llama.cpp reads for every
    architecture into ``hparams.causal_attn`` (default true when absent); the
    context length is the ``<architecture>.context_length`` integer it reads
    into ``hparams.n_ctx_train``; the pooling is the ``<architecture>.pooling_type``
    integer it reads into ``hparams.pooling_type`` (see :func:`gguf_declared_pooling`).

    The header is walked with plain bounded reads, never a mapping: a mapping's
    length is fixed when it is built, so a GGUF truncated in place while its
    header is parsed turns the next access past the new end into a SIGBUS the
    gateway process cannot catch. A read that comes up short is a ValueError
    instead, which _model_context_policy turns into the decoder sizes.
    """
    with path.open("rb") as source:
        return _parse_gguf_embedding_metadata(source, os.fstat(source.fileno()).st_size)


def _parse_gguf_embedding_metadata(source: BinaryIO, file_size: int) -> _GGUFEmbeddingMetadata:
    """Parse the KV header from ``source``, a binary stream positioned at its start.

    ``file_size`` is the byte count the file had when it was opened; it bounds
    every field the header declares. The header is read forward in buffered
    chunks of at most _GGUF_READ_CHUNK_BYTES, each also capped at the bytes
    ``file_size`` still allows, and every field is decoded in place with
    ``struct.unpack_from``; a value it does not need that reaches past the
    buffer is seeked over, so the walk reads at most one chunk beyond the
    header. A string array (the tokenizer vocabulary holds ~150,000 of them) is
    stepped over in one loop rather than one call per string. A captured string
    is at most _GGUF_MAX_DECODED_STRING_BYTES. A field reaching past
    ``file_size``, or a read returning fewer bytes than the field needs (the
    file shrank under the parse), is ``truncated GGUF header``.
    """
    # The buffered bytes, the cursor inside them, and the file offset of buf[0].
    buf = b""
    pos = 0
    base = 0

    def _need(size: int) -> None:
        # Make buf[pos : pos + size] present, reading one bounded chunk.
        nonlocal buf, pos, base
        if size > file_size - (base + pos):
            raise ValueError("truncated GGUF header")
        missing = size - (len(buf) - pos)
        if missing <= 0:
            return
        want = min(max(missing, _GGUF_READ_CHUNK_BYTES), file_size - (base + len(buf)))
        chunk = source.read(want)
        if len(chunk) < missing:
            raise ValueError("truncated GGUF header")
        base += pos
        buf = buf[pos:] + chunk
        pos = 0

    def _skip(size: int) -> None:
        nonlocal buf, pos, base
        if size > file_size - (base + pos):
            raise ValueError("truncated GGUF header")
        beyond = size - (len(buf) - pos)
        if beyond <= 0:
            pos += size
            return
        source.seek(beyond, os.SEEK_CUR)
        base += pos + size
        buf = b""
        pos = 0

    def _read_exact(size: int) -> bytes:
        nonlocal pos
        _need(size)
        chunk = buf[pos : pos + size]
        pos += size
        return chunk

    def _unpack(fmt: struct.Struct) -> int:
        nonlocal pos
        _need(fmt.size)
        value = int(fmt.unpack_from(buf, pos)[0])
        pos += fmt.size
        return value

    def _read_string() -> str:
        length = _unpack(_GGUF_U64)
        if length > _GGUF_MAX_DECODED_STRING_BYTES:
            raise ValueError("oversized GGUF metadata string")
        return _read_exact(length).decode("utf-8")

    def _skip_string_array(count: int) -> None:
        # The hot loop of the walk: buf, pos and the buffer end live in locals
        # and are refreshed only when a length prefix or a string body leaves
        # the buffer, so a string inside it costs one unpack_from.
        nonlocal pos
        unpack_length = _GGUF_U64.unpack_from
        data, at, end = buf, pos, len(buf)
        for _ in range(count):
            if end - at < 8:
                pos = at
                _need(8)
                data, at, end = buf, pos, len(buf)
            length = unpack_length(data, at)[0]
            at += 8
            if end - at >= length:
                at += length
            else:
                pos = at
                _skip(length)
                data, at, end = buf, pos, len(buf)
        pos = at

    def _read_value(value_type: int, *, capture: bool) -> object | None:
        if value_type == _GGUF_TYPE_STRING:
            if capture:
                return _read_string()
            _skip(_unpack(_GGUF_U64))
            return None
        if value_type == _GGUF_TYPE_ARRAY:
            element_type = _unpack(_GGUF_U32)
            count = _unpack(_GGUF_U64)
            if count > _GGUF_MAX_ARRAY_ITEMS:
                raise ValueError("oversized GGUF metadata array")
            if element_type == _GGUF_TYPE_STRING:
                _skip_string_array(count)
                return None
            element_size = _GGUF_SCALAR_BYTES.get(element_type)
            if element_size is None:
                raise ValueError(f"unsupported GGUF array type {element_type}")
            _skip(element_size * count)
            return None
        value_size = _GGUF_SCALAR_BYTES.get(value_type)
        if value_size is None:
            raise ValueError(f"unsupported GGUF metadata type {value_type}")
        if capture and value_type == _GGUF_TYPE_UINT32:
            return _unpack(_GGUF_U32)
        if capture and value_type == _GGUF_TYPE_INT32:
            return _unpack(_GGUF_I32)
        if capture and value_type == _GGUF_TYPE_BOOL:
            return _unpack(_GGUF_U8) != 0
        _skip(value_size)
        return None

    if _read_exact(4) != b"GGUF":
        raise ValueError("invalid GGUF magic")
    version = _unpack(_GGUF_U32)
    if version not in {2, 3}:
        raise ValueError(f"unsupported GGUF version {version}")
    _unpack(_GGUF_U64)  # tensor count; only the following KV section is needed
    metadata_count = _unpack(_GGUF_U64)
    if metadata_count > _GGUF_MAX_METADATA_ITEMS:
        raise ValueError("oversized GGUF metadata table")

    architecture: str | None = None
    causal_by_architecture: dict[str, bool] = {}
    context_length_by_architecture: dict[str, int] = {}
    pooling_by_architecture: dict[str, int] = {}
    for _ in range(metadata_count):
        key = _read_string()
        capture = (
            key == "general.architecture"
            or key.endswith(_GGUF_CAUSAL_SUFFIX)
            or key.endswith(_GGUF_CONTEXT_LENGTH_SUFFIX)
            or key.endswith(_GGUF_POOLING_TYPE_SUFFIX)
        )
        value = _read_value(_unpack(_GGUF_U32), capture=capture)
        if key == "general.architecture" and isinstance(value, str):
            architecture = value.strip().lower()
        elif key.endswith(_GGUF_CAUSAL_SUFFIX) and isinstance(value, bool):
            causal_by_architecture[key[: -len(_GGUF_CAUSAL_SUFFIX)].lower()] = value
        elif (
            key.endswith(_GGUF_CONTEXT_LENGTH_SUFFIX)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        ):
            context_length_by_architecture[key[: -len(_GGUF_CONTEXT_LENGTH_SUFFIX)].lower()] = value
        elif (
            key.endswith(_GGUF_POOLING_TYPE_SUFFIX)
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            pooling_by_architecture[key[: -len(_GGUF_POOLING_TYPE_SUFFIX)].lower()] = value
    return _GGUFEmbeddingMetadata(
        architecture,
        causal_by_architecture.get(architecture or ""),
        context_length_by_architecture.get(architecture or ""),
        pooling_by_architecture.get(architecture or ""),
    )


class _ContextPolicy(NamedTuple):
    """The llama.cpp context sizes chosen for one GGUF before it loads."""

    n_ctx: int
    n_batch: int
    n_ubatch: int


# What a decoder (causal) file trained for at least _N_CTX positions gets, the
# bundled Qwen model included, and the fallback when a header cannot be read.
# Kept as one constant so that path stays byte-for-byte the shipped one.
_DECODER_CONTEXT_POLICY = _ContextPolicy(_N_CTX, _N_CTX, _N_UBATCH)


def _model_context_policy(path: Path) -> _ContextPolicy:
    """Choose context sizes before llama.cpp constructs the native context."""
    try:
        metadata = _read_gguf_embedding_metadata(path)
    except (OSError, OverflowError, UnicodeError, ValueError, struct.error):
        logger.warning(
            "Could not read GGUF metadata for %s; using the default context sizes.",
            path.name,
        )
        return _DECODER_CONTEXT_POLICY

    # Every file's context and logical batch are clamped to the trained
    # position count its GGUF declares. A model with learned absolute positions
    # indexes a position table with n_ctx_train rows -- encoder families, but
    # causal gpt2 and starcoder as well -- and llama.cpp aborts the process
    # (`GGML_ASSERT(i01 >= 0 && i01 < ne01)` in ggml's get_rows) when a token
    # sits past its end, whatever the model's causality. With n_batch equal to
    # that count, the vendored binding's create_embedding() -> embed(truncate=True)
    # keeps the first n_batch tokens of a longer input instead (pinned by
    # test_vendored_embed_truncates_to_the_logical_batch_by_default); _load_model
    # states that rule once per model. A file trained for _N_CTX positions or
    # more (the bundled Qwen model declares 32,768) keeps the ceiling.
    window = _N_CTX
    if metadata.context_length is not None:
        window = min(_N_CTX, metadata.context_length)

    # Two llama.cpp paths abort unless one physical micro-batch holds every
    # token: encode(), which every cache-less architecture is routed through,
    # and decode() for a model whose GGUF declares `.attention.causal = false`.
    # Absent that key the runtime defaults to causal attention, so a model
    # outside the cache-less set (llama-embed included) stays on the decoder path.
    non_causal = metadata.causal is False or metadata.architecture in _ENCODER_ARCHITECTURES
    if non_causal:
        return _ContextPolicy(window, window, window)
    # Decoder models keep the lower-RSS micro-batch, which llama.cpp requires
    # to stay within the logical batch.
    return _ContextPolicy(window, window, min(_N_UBATCH, window))


def gguf_declared_pooling(path: Path) -> str:
    """The pooling *path*'s GGUF declares, or ``"last"`` when it declares none usable.

    Every embedding GGUF a current converter writes carries the key: Qwen3
    declares last-token, EmbeddingGemma and Nemotron mean, the ModernBERT family
    CLS. A missing or unreadable header, or a pooling Kiro Crew cannot use (none,
    rank), keeps last-token, the pooling every custom model ran with before.
    """
    try:
        declared = _read_gguf_embedding_metadata(path).pooling_type
    except (OSError, OverflowError, UnicodeError, ValueError, struct.error):
        return "last"
    return next((name for name, value in _POOLING_TYPES.items() if value == declared), "last")
