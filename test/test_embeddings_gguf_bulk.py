"""The buffered GGUF header walk returns exactly what the per-field walk did.

``_parse_gguf_embedding_metadata`` reads the KV header in bounded chunks and
steps over string arrays in one loop. ``_reference_parse`` below is the
per-field walker it replaced, kept verbatim as the oracle: one ``read()`` per
field and a seek past every skipped value. Every case compares the two
outcomes -- the parsed metadata, or the exception type and message -- on a
synthetic header holding every value type, string arrays, scalar arrays of
every element type and a nested array, at chunk sizes small enough that every
field straddles a chunk boundary somewhere, and at every truncation point.
``TestRealModel`` repeats the comparison on a real GGUF when
``KIROCREW_TEST_EMBED_MODEL`` names one.
"""

from __future__ import annotations

import io
import os
import struct
from pathlib import Path
from typing import BinaryIO

import pytest

import kiro_crew.embeddings_gguf as gguf

# ── The oracle: the per-field walker the buffered one replaced ──


def _reference_parse(source: BinaryIO, file_size: int) -> gguf._GGUFEmbeddingMetadata:
    offset = 0

    def _claim(size: int) -> None:
        nonlocal offset
        if size > file_size - offset:
            raise ValueError("truncated GGUF header")
        offset += size

    def _read_exact(size: int) -> bytes:
        _claim(size)
        chunk = source.read(size)
        if len(chunk) != size:
            raise ValueError("truncated GGUF header")
        return chunk

    def _skip(size: int) -> None:
        _claim(size)
        source.seek(size, os.SEEK_CUR)

    def _unpack(fmt: str) -> int:
        return int(struct.unpack(fmt, _read_exact(struct.calcsize(fmt)))[0])

    def _read_string() -> str:
        length = _unpack("<Q")
        if length > gguf._GGUF_MAX_DECODED_STRING_BYTES:
            raise ValueError("oversized GGUF metadata string")
        return _read_exact(length).decode("utf-8")

    def _skip_string() -> None:
        _skip(_unpack("<Q"))

    def _read_value(value_type: int, *, capture: bool) -> object | None:
        if value_type == gguf._GGUF_TYPE_STRING:
            if capture:
                return _read_string()
            _skip_string()
            return None
        if value_type == gguf._GGUF_TYPE_ARRAY:
            element_type = _unpack("<I")
            count = _unpack("<Q")
            if count > gguf._GGUF_MAX_ARRAY_ITEMS:
                raise ValueError("oversized GGUF metadata array")
            if element_type == gguf._GGUF_TYPE_STRING:
                for _ in range(count):
                    _skip_string()
                return None
            element_size = gguf._GGUF_SCALAR_BYTES.get(element_type)
            if element_size is None:
                raise ValueError(f"unsupported GGUF array type {element_type}")
            _skip(element_size * count)
            return None
        value_size = gguf._GGUF_SCALAR_BYTES.get(value_type)
        if value_size is None:
            raise ValueError(f"unsupported GGUF metadata type {value_type}")
        if capture and value_type == gguf._GGUF_TYPE_UINT32:
            return _unpack("<I")
        if capture and value_type == gguf._GGUF_TYPE_INT32:
            return _unpack("<i")
        if capture and value_type == gguf._GGUF_TYPE_BOOL:
            return _unpack("<B") != 0
        _skip(value_size)
        return None

    if _read_exact(4) != b"GGUF":
        raise ValueError("invalid GGUF magic")
    version = _unpack("<I")
    if version not in {2, 3}:
        raise ValueError(f"unsupported GGUF version {version}")
    _unpack("<Q")
    metadata_count = _unpack("<Q")
    if metadata_count > gguf._GGUF_MAX_METADATA_ITEMS:
        raise ValueError("oversized GGUF metadata table")

    architecture: str | None = None
    causal_by_architecture: dict[str, bool] = {}
    context_length_by_architecture: dict[str, int] = {}
    pooling_by_architecture: dict[str, int] = {}
    for _ in range(metadata_count):
        key = _read_string()
        capture = (
            key == "general.architecture"
            or key.endswith(gguf._GGUF_CAUSAL_SUFFIX)
            or key.endswith(gguf._GGUF_CONTEXT_LENGTH_SUFFIX)
            or key.endswith(gguf._GGUF_POOLING_TYPE_SUFFIX)
        )
        value = _read_value(_unpack("<I"), capture=capture)
        if key == "general.architecture" and isinstance(value, str):
            architecture = value.strip().lower()
        elif key.endswith(gguf._GGUF_CAUSAL_SUFFIX) and isinstance(value, bool):
            causal_by_architecture[key[: -len(gguf._GGUF_CAUSAL_SUFFIX)].lower()] = value
        elif (
            key.endswith(gguf._GGUF_CONTEXT_LENGTH_SUFFIX)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        ):
            context_length_by_architecture[
                key[: -len(gguf._GGUF_CONTEXT_LENGTH_SUFFIX)].lower()
            ] = value
        elif (
            key.endswith(gguf._GGUF_POOLING_TYPE_SUFFIX)
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            pooling_by_architecture[key[: -len(gguf._GGUF_POOLING_TYPE_SUFFIX)].lower()] = value
    return gguf._GGUFEmbeddingMetadata(
        architecture,
        causal_by_architecture.get(architecture or ""),
        context_length_by_architecture.get(architecture or ""),
        pooling_by_architecture.get(architecture or ""),
    )


# ── The synthetic header ──

# GGUF scalar type -> (struct format, one sample value).
_SCALARS = {
    0: ("<B", 0xAB),
    1: ("<b", -5),
    2: ("<H", 0xBEEF),
    3: ("<h", -1234),
    4: ("<I", 0xDEADBEEF),
    5: ("<i", -7),
    6: ("<f", 1.5),
    7: ("<?", True),
    10: ("<Q", 1 << 40),
    11: ("<q", -(1 << 40)),
    12: ("<d", 2.25),
}


def _string(value: str | bytes) -> bytes:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return struct.pack("<Q", len(encoded)) + encoded


def _entry(key: str, value_type: int, value: bytes) -> bytes:
    return _string(key) + struct.pack("<I", value_type) + value


def _string_array(items: list[str]) -> bytes:
    return struct.pack("<IQ", 8, len(items)) + b"".join(_string(item) for item in items)


def _scalar_array(element_type: int, count: int) -> bytes:
    fmt, sample = _SCALARS[element_type]
    return struct.pack("<IQ", element_type, count) + struct.pack(fmt, sample) * count


def _header(entries: list[bytes], *, version: int = 3) -> bytes:
    return struct.pack("<4sIQQ", b"GGUF", version, 7, len(entries)) + b"".join(entries)


# A vocabulary whose strings vary in length: empty, one byte, multi-byte UTF-8,
# and one longer than every small chunk size below, so a body is seeked over.
_VOCAB = ["", "a", "<|endoftext|>", "Ġthe", "日本語", "x" * 300] + [f"tok{i}" for i in range(400)]


def _every_type_entries(*, compact: bool = False) -> list[bytes]:
    # ``compact`` keeps every shape but shortens the long arrays, so a sweep
    # over every truncation point stays fast.
    vocab = _VOCAB[:6] if compact else _VOCAB
    merges = 3 if compact else 250
    token_types = 3 if compact else 500
    entries = [
        # Every scalar type as an uncaptured value.
        *(
            _entry(f"scalar.type{value_type}", value_type, struct.pack(fmt, sample))
            for value_type, (fmt, sample) in _SCALARS.items()
        ),
        _entry("general.name", 8, _string("Qwen3 Embedding 0.6B")),
        _entry("tokenizer.ggml.tokens", 9, _string_array(vocab)),
        _entry("tokenizer.ggml.empty", 9, _string_array([])),
        _entry("tokenizer.ggml.merges", 9, _string_array([f"m {i}" for i in range(merges)])),
        # A scalar array of every element type, one long enough to be seeked over.
        *(
            _entry(f"array.type{value_type}", 9, _scalar_array(value_type, 3 + value_type))
            for value_type in _SCALARS
        ),
        _entry("tokenizer.ggml.token_type", 9, _scalar_array(5, token_types)),
        # Captured keys, each in the shape the result reads and in shapes it ignores.
        _entry("qwen3.attention.causal", 7, struct.pack("<?", False)),
        _entry("qwen3.context_length", 5, struct.pack("<i", 4096)),
        _entry("qwen3.pooling_type", 4, struct.pack("<I", 3)),
        _entry("other.context_length", 4, struct.pack("<I", 512)),
        _entry("other.attention.causal", 0, struct.pack("<B", 1)),  # uint8, not read
        _entry("wide.context_length", 10, struct.pack("<Q", 8192)),  # uint64, not read
        _entry("neg.context_length", 5, struct.pack("<i", -1)),  # not positive
        _entry("arr.pooling_type", 9, _scalar_array(4, 2)),  # an array, not read
        _entry("str.attention.causal", 8, _string("false")),  # a string, not read
        _entry("general.architecture", 8, _string("  Qwen3 ")),
    ]
    return entries


_EVERY_TYPE_HEADER = _header(_every_type_entries())
_COMPACT_HEADER = _header(_every_type_entries(compact=True))
_EVERY_TYPE_EXPECTED = gguf._GGUFEmbeddingMetadata("qwen3", False, 4096, 3)

# Chunk sizes that put every field across a chunk boundary somewhere, and the
# shipped size, under which the whole synthetic header is one read.
_CHUNK_SIZES = [1, 2, 3, 7, 8, 13, 64, 257, gguf._GGUF_READ_CHUNK_BYTES]


def _outcome(parser, data: bytes, file_size: int) -> tuple[object, ...]:
    try:
        return ("ok", parser(io.BytesIO(data), file_size))
    except (OverflowError, UnicodeError, ValueError, struct.error) as exc:
        return ("error", type(exc), str(exc))


def _assert_same(data: bytes, file_size: int | None = None) -> tuple[object, ...]:
    size = len(data) if file_size is None else file_size
    expected = _outcome(_reference_parse, data, size)
    assert _outcome(gguf._parse_gguf_embedding_metadata, data, size) == expected
    return expected


@pytest.fixture(params=_CHUNK_SIZES, ids=lambda size: f"chunk{size}")
def chunk_size(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(gguf, "_GGUF_READ_CHUNK_BYTES", request.param)
    return int(request.param)


class TestMatchesTheReferenceWalker:
    def test_every_value_type_parses_to_the_same_metadata(self, chunk_size: int) -> None:
        # Pinned against a literal as well, so the two walkers cannot agree on
        # a wrong answer.
        assert _assert_same(_EVERY_TYPE_HEADER) == ("ok", _EVERY_TYPE_EXPECTED)

    def test_trailing_bytes_after_the_header_are_not_read_into_the_result(
        self, chunk_size: int
    ) -> None:
        # Tensor data follows the KV section in a real file.
        padded = _EVERY_TYPE_HEADER + bytes(range(256)) * 8
        assert _assert_same(padded) == ("ok", _EVERY_TYPE_EXPECTED)

    def test_every_truncation_point_fails_the_same_way(self, chunk_size: int) -> None:
        # A file cut short, at every byte of the header. The 300-byte vocab
        # string keeps a body longer than every small chunk in the sweep.
        assert _assert_same(_COMPACT_HEADER) == ("ok", _EVERY_TYPE_EXPECTED)
        for cut in range(len(_COMPACT_HEADER)):
            outcome = _assert_same(_COMPACT_HEADER[:cut])
            assert outcome == ("error", ValueError, "truncated GGUF header"), cut

    def test_every_shrink_under_the_parse_fails_the_same_way(self, chunk_size: int) -> None:
        # The size sampled at open says the header is whole; the bytes are gone.
        for cut in range(len(_COMPACT_HEADER)):
            outcome = _assert_same(_COMPACT_HEADER[:cut], len(_COMPACT_HEADER))
            assert outcome == ("error", ValueError, "truncated GGUF header"), cut

    @pytest.mark.parametrize(
        ("entry", "message"),
        [
            pytest.param(
                _entry("nested", 9, struct.pack("<IQ", 9, 1) + _string_array(["a"])),
                "unsupported GGUF array type 9",
                id="nested-array",
            ),
            pytest.param(
                _entry("bad.array", 9, struct.pack("<IQ", 13, 0)),
                "unsupported GGUF array type 13",
                id="unknown-array-type",
            ),
            pytest.param(
                _entry("bad.value", 13, b""),
                "unsupported GGUF metadata type 13",
                id="unknown-value-type",
            ),
            pytest.param(
                _entry("big.array", 9, struct.pack("<IQ", 8, 2_000_001)),
                "oversized GGUF metadata array",
                id="oversized-string-array",
            ),
            pytest.param(
                _string("x" * 1_000_001),
                "oversized GGUF metadata string",
                id="oversized-key",
            ),
            pytest.param(
                _entry("str.array", 9, struct.pack("<IQ", 8, 2) + struct.pack("<Q", 1 << 62)),
                "truncated GGUF header",
                id="string-length-past-the-file",
            ),
        ],
    )
    def test_a_refused_entry_after_every_type_fails_the_same_way(
        self, chunk_size: int, entry: bytes, message: str
    ) -> None:
        data = _header([*_every_type_entries(), entry])
        assert _assert_same(data) == ("error", ValueError, message)

    def test_an_invalid_utf8_key_fails_the_same_way(self, chunk_size: int) -> None:
        data = _header([_string(b"\xff\xfe") + struct.pack("<IB", 0, 0)])
        outcome = _assert_same(data)
        assert outcome[:2] == ("error", UnicodeDecodeError)

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            pytest.param(b"GGUX" + bytes(20), "invalid GGUF magic", id="magic"),
            pytest.param(_header([], version=1), "unsupported GGUF version 1", id="version"),
            pytest.param(
                struct.pack("<4sIQQ", b"GGUF", 3, 0, 100_001),
                "oversized GGUF metadata table",
                id="metadata-count",
            ),
        ],
    )
    def test_a_refused_preamble_fails_the_same_way(
        self, chunk_size: int, data: bytes, message: str
    ) -> None:
        assert _assert_same(data) == ("error", ValueError, message)


class TestBoundedReads:
    def test_no_read_asks_for_more_than_a_chunk_or_past_the_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The walk reads at most one chunk at a time and never asks for a
        # byte past the size sampled at open.
        monkeypatch.setattr(gguf, "_GGUF_READ_CHUNK_BYTES", 64)
        data = _EVERY_TYPE_HEADER + bytes(4096)
        requests: list[tuple[int, int]] = []

        class _Recording(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                assert size is not None and size >= 0
                requests.append((self.tell(), size))
                return super().read(size)

        assert gguf._parse_gguf_embedding_metadata(_Recording(data), len(data)) == (
            _EVERY_TYPE_EXPECTED
        )
        assert requests
        assert all(size <= 64 for _, size in requests)
        assert all(start + size <= len(data) for start, size in requests)
        assert requests[-1][0] + requests[-1][1] <= len(_EVERY_TYPE_HEADER) + 64


_REAL_MODEL = os.environ.get("KIROCREW_TEST_EMBED_MODEL", "")


@pytest.mark.skipif(
    not _REAL_MODEL or not Path(_REAL_MODEL).is_file(),
    reason="set KIROCREW_TEST_EMBED_MODEL to a GGUF embedding model to run",
)
class TestRealModel:
    def test_the_real_header_parses_to_the_same_metadata(self) -> None:
        path = Path(_REAL_MODEL)
        size = path.stat().st_size
        with path.open("rb") as source:
            expected = _reference_parse(source, size)
        with path.open("rb") as source:
            assert gguf._parse_gguf_embedding_metadata(source, size) == expected
        assert gguf._read_gguf_embedding_metadata(path) == expected
        assert expected.architecture is not None
