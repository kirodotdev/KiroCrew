"""The embedder bounds the tokens one native decode handles, so llama.cpp's output buffer never grows.

In embedding mode llama.cpp makes every token an output and reserves
``n_vocab + n_embd`` floats per output in a buffer sized to the largest batch
ever decoded and never shrunk (1.19 GB after one 2,048-token input for the
shipped model). ``_install_bounded_decode`` sets ``kiro_crew_max_decode_tokens``
to ``n_ubatch`` on a causal, cached, last-token-pooled context, and the vendored
``Llama.embed`` then decodes a longer batch in slices of at most that many
tokens -- the micro-batches llama.cpp splits a single call into -- reading each
sequence's vector after the slice holding its last token.

Four groups:

* ``TestInstall`` pins which contexts get the bound, with fakes.
* ``TestVendoredEmbed`` runs the SHIPPED ``Llama.embed`` source (extracted from
  ``src/kiro_crew/_vendor/llama_cpp/llama.py``) against a fake causal runtime
  that clears its pooled outputs on every decode, as llama.cpp does, so a
  vector read after the wrong slice is a different vector. It fails if a
  re-vendor drops the divergence.
* ``TestRealModel`` runs only when ``KIROCREW_TEST_EMBED_MODEL`` names a GGUF
  file and the vendored runtime loads; it compares vectors byte for byte.
* ``test_the_byte_identity_proof_covers_the_vendored_runtime`` runs in CI and
  pins the runtime ``TestRealModel`` last passed on, so a vendored llama.cpp or
  shipped-model bump fails until that proof is run again.
"""

from __future__ import annotations

import ast
import ctypes
import hashlib
import os
import pathlib
import struct
import sys
import types
from types import SimpleNamespace
from typing import List, Optional, Union

import pytest

from kiro_crew import embeddings as embeddings_mod
from kiro_crew.embeddings import LlamaCppEmbedder

_LAST = embeddings_mod._POOLING_TYPE_LAST
_MEAN = 1
_NONE = 0
_VENDORED_LLAMA = (
    pathlib.Path(embeddings_mod.__file__).resolve().parent / "_vendor" / "llama_cpp" / "llama.py"
)


def _fake_llm(
    *,
    memory: object | None = "mem-handle",
    metadata: object = None,
    n_ubatch: object = 512,
) -> SimpleNamespace:
    if metadata is None:
        metadata = {"general.architecture": "qwen3"}
    return SimpleNamespace(
        _ctx=SimpleNamespace(memory=memory),
        metadata=metadata,
        context_params=SimpleNamespace(n_seq_max=256, n_ubatch=n_ubatch),
    )


class TestInstall:
    def test_a_causal_cached_context_gets_its_micro_batch(self) -> None:
        llm = _fake_llm()
        assert embeddings_mod._install_bounded_decode(llm) is True
        assert llm.kiro_crew_max_decode_tokens == 512

    def test_an_explicitly_causal_gguf_gets_it(self) -> None:
        llm = _fake_llm(
            metadata={"general.architecture": "qwen3", "qwen3.attention.causal": "true"}
        )
        assert embeddings_mod._install_bounded_decode(llm) is True

    @pytest.mark.parametrize(
        "llm",
        [
            pytest.param(_fake_llm(memory=None), id="cache-less-context"),
            pytest.param(
                _fake_llm(metadata={"general.architecture": "bert"}), id="encoder-architecture"
            ),
            pytest.param(
                _fake_llm(
                    metadata={"general.architecture": "qwen3", "qwen3.attention.causal": "false"}
                ),
                id="declared-non-causal",
            ),
            pytest.param(_fake_llm(metadata={}), id="no-architecture"),
            pytest.param(_fake_llm(metadata="not-a-dict"), id="unreadable-metadata"),
            pytest.param(_fake_llm(n_ubatch=0), id="no-micro-batch"),
            pytest.param(_fake_llm(n_ubatch=True), id="bool-micro-batch"),
        ],
    )
    def test_any_other_context_keeps_the_single_decode(self, llm: SimpleNamespace) -> None:
        assert embeddings_mod._install_bounded_decode(llm) is False
        assert not hasattr(llm, "kiro_crew_max_decode_tokens")

    def test_an_object_without_a_context_is_left_alone(self) -> None:
        assert embeddings_mod._install_bounded_decode(object()) is False

    def test_the_loaded_model_gets_the_bound(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        class _LoadedLlama:
            instances: list = []

            def __init__(self, **kwargs) -> None:
                self._ctx = SimpleNamespace(memory="mem-handle", kv_cache_clear=lambda: None)
                self.metadata = {"general.architecture": "qwen3"}
                self.context_params = SimpleNamespace(n_seq_max=256, n_ubatch=kwargs["n_ubatch"])
                type(self).instances.append(self)

            def create_embedding(self, texts):
                return {"data": [{"embedding": [0.1] * 1024} for _ in texts]}

        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: _LoadedLlama)
        model = tmp_path / "model.gguf"
        model.write_bytes(b"g" * (embeddings_mod._GGUF_MIN_BYTES + 100_000))
        embedder = LlamaCppEmbedder(model_path=model)
        try:
            assert embedder.wait_ready(timeout=30)
            assert _LoadedLlama.instances[0].kiro_crew_max_decode_tokens == embeddings_mod._N_UBATCH
        finally:
            embedder.close()


# ═══════════════════════════════════════════════════════════════════════════
# The shipped Llama.embed against a fake causal runtime
# ═══════════════════════════════════════════════════════════════════════════

_N_EMBD = 4


class _Batch(ctypes.Structure):
    """Same field layout as the vendored ``llama_cpp.llama_batch``."""

    _fields_ = [
        ("n_tokens", ctypes.c_int32),
        ("token", ctypes.POINTER(ctypes.c_int32)),
        ("embd", ctypes.POINTER(ctypes.c_float)),
        ("pos", ctypes.POINTER(ctypes.c_int32)),
        ("n_seq_id", ctypes.POINTER(ctypes.c_int32)),
        ("seq_id", ctypes.POINTER(ctypes.POINTER(ctypes.c_int32))),
        ("logits", ctypes.POINTER(ctypes.c_int8)),
    ]


class _HostBatch:
    """Mirror of ``_internals.LlamaBatch`` with its arrays allocated by ctypes."""

    def __init__(self, n_tokens: int) -> None:
        self._token = (ctypes.c_int32 * n_tokens)()
        self._pos = (ctypes.c_int32 * n_tokens)()
        self._n_seq_id = (ctypes.c_int32 * n_tokens)()
        self._seq_cells = [(ctypes.c_int32 * 1)() for _ in range(n_tokens)]
        self._seq_id = (ctypes.POINTER(ctypes.c_int32) * n_tokens)(
            *[ctypes.cast(c, ctypes.POINTER(ctypes.c_int32)) for c in self._seq_cells]
        )
        self._logits = (ctypes.c_int8 * n_tokens)()
        self.batch = _Batch(
            0,
            ctypes.cast(self._token, ctypes.POINTER(ctypes.c_int32)),
            ctypes.POINTER(ctypes.c_float)(),
            ctypes.cast(self._pos, ctypes.POINTER(ctypes.c_int32)),
            ctypes.cast(self._n_seq_id, ctypes.POINTER(ctypes.c_int32)),
            ctypes.cast(self._seq_id, ctypes.POINTER(ctypes.POINTER(ctypes.c_int32))),
            ctypes.cast(self._logits, ctypes.POINTER(ctypes.c_int8)),
        )

    def n_tokens(self) -> int:
        return self.batch.n_tokens

    def reset(self) -> None:
        self.batch.n_tokens = 0

    def add_sequence(self, tokens, seq_id: int, logits_all: bool) -> None:
        n0 = self.batch.n_tokens
        for i, tok in enumerate(tokens):
            j = n0 + i
            self._token[j] = tok
            self._pos[j] = i
            self._seq_cells[j][0] = seq_id
            self._n_seq_id[j] = 1
            self._logits[j] = logits_all
        self.batch.n_tokens = n0 + len(tokens)


class _CausalRuntime:
    """A causal model behind a KV cache, with llama.cpp's per-decode pooled outputs.

    A sequence's vector is a function of every token it has in the cache, so a
    prefix read gives a different vector. Pooled outputs are cleared at the
    start of every decode and set for each sequence present in that decode.
    """

    def __init__(self) -> None:
        self.kv: dict[int, list[int]] = {}
        self.pooled: dict[int, ctypes.Array] = {}
        self.decode_sizes: list[int] = []

    def clear(self) -> None:
        self.kv.clear()

    def decode(self, batch: _Batch) -> int:
        self.decode_sizes.append(batch.n_tokens)
        self.pooled = {}
        present = []
        for i in range(batch.n_tokens):
            assert batch.n_seq_id[i] == 1 and batch.logits[i] == 1
            seq, pos = batch.seq_id[i][0], batch.pos[i]
            cells = self.kv.setdefault(seq, [])
            assert pos == len(cells), "positions must continue the cached sequence"
            cells.append(batch.token[i])
            if seq not in present:
                present.append(seq)
        for seq in present:
            cells = self.kv[seq]
            values = [
                float(len(cells)),
                float(sum(cells)),
                float(cells[-1]),
                float(sum(t * (k + 1) for k, t in enumerate(cells))),
            ]
            self.pooled[seq] = (ctypes.c_float * _N_EMBD)(*values)
        return 0

    def embeddings_seq(self, ctx: object, seq: int):
        found = self.pooled.get(seq)
        return ctypes.cast(found, ctypes.POINTER(ctypes.c_float)) if found is not None else None


def _vendored_embed(runtime: _CausalRuntime):
    """Compile the shipped ``Llama.embed`` and its batch-offset helper against fakes."""
    tree = ast.parse(_VENDORED_LLAMA.read_text(encoding="utf-8"))
    helper = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_kiro_crew_batch_offset"
    )
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Llama")
    embed = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "embed")
    native = types.SimpleNamespace(
        LLAMA_POOLING_TYPE_NONE=_NONE,
        LLAMA_POOLING_TYPE_LAST=_LAST,
        llama_batch=_Batch,
        llama_decode=lambda ctx, batch: runtime.decode(batch),
        llama_get_embeddings_seq=runtime.embeddings_seq,
        llama_perf_context_reset=lambda ctx: None,
        llama_perf_context_print=lambda ctx: None,
    )
    namespace = {
        "ctypes": ctypes,
        "llama_cpp": native,
        "internals": types.SimpleNamespace(normalize_embedding=lambda v: v),
        "Union": Union,
        "List": List,
        "Optional": Optional,
    }
    module = ast.Module(body=[helper, embed], type_ignores=[])
    code = compile(module, str(_VENDORED_LLAMA), "exec")
    # The code object is two definitions from the repository's own vendored
    # llama.py, read from disk: no external input reaches it, which is what the
    # audit rule guards against.
    exec(code, namespace)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    return namespace["embed"]


class _FakeLlama:
    def __init__(
        self, runtime: _CausalRuntime, *, n_batch: int, pooling: int, bound: int | None
    ) -> None:
        self.n_batch = n_batch
        self.verbose = False
        self.context_params = SimpleNamespace(n_seq_max=256, embeddings=True)
        self._pooling = pooling
        self._batch = _HostBatch(n_batch)
        self._ctx = SimpleNamespace(
            ctx="ctx-handle",
            kv_cache_clear=runtime.clear,
            decode=lambda host_batch: runtime.decode(host_batch.batch),
        )
        if bound is not None:
            self.kiro_crew_max_decode_tokens = bound

    def n_embd(self) -> int:
        return _N_EMBD

    def pooling_type(self) -> int:
        return self._pooling

    def tokenize(self, raw: bytes) -> list[int]:
        return list(raw)

    def reset(self) -> None:
        pass


def _run(texts: list[str], *, bound: int | None, pooling: int = _LAST, n_batch: int = 2048):
    runtime = _CausalRuntime()
    embed = _vendored_embed(runtime)
    llm = _FakeLlama(runtime, n_batch=n_batch, pooling=pooling, bound=bound)
    return embed(llm, texts), runtime.decode_sizes


def _text(n: int, seed: int) -> str:
    return "".join(chr(97 + (seed * 7 + i * 13) % 26) for i in range(n))


class TestVendoredEmbed:
    # Lengths straddle the bound and the logical batch: one pack mixes them.
    PACK = [_text(n, s) for s, n in enumerate((3, 255, 256, 257, 40, 600, 1, 511, 300))]

    def test_a_bounded_decode_returns_the_single_decode_vectors(self) -> None:
        single, single_sizes = _run(self.PACK, bound=None)
        bounded, bounded_sizes = _run(self.PACK, bound=256)
        assert bounded == single
        assert max(single_sizes) > 256
        assert max(bounded_sizes) <= 256
        assert sum(bounded_sizes) == sum(single_sizes)

    @pytest.mark.parametrize("bound", [1, 7, 100, 256, 1024])
    def test_every_bound_returns_the_same_vectors(self, bound: int) -> None:
        single, _ = _run(self.PACK, bound=None)
        bounded, sizes = _run(self.PACK, bound=bound)
        assert bounded == single
        assert max(sizes) <= bound

    def test_a_micro_batch_bound_decodes_llama_cpps_own_micro_batches(self) -> None:
        # A unified-cache decode splits each pack into consecutive n_ubatch slices
        # in batch order (split_simple); bounded at n_ubatch, each call is one of them.
        n_ubatch = 512
        single, single_sizes = _run(self.PACK, bound=None)
        bounded, sizes = _run(self.PACK, bound=n_ubatch)
        expected: list[int] = []
        for total in single_sizes:
            expected += [n_ubatch] * (total // n_ubatch) + (
                [total % n_ubatch] if total % n_ubatch else []
            )
        assert single_sizes == [1923, 300]
        assert sizes == expected == [512, 512, 512, 387, 300]
        assert bounded == single

    def test_one_long_input_is_sliced(self) -> None:
        single, _ = _run([_text(2000, 9)], bound=None)
        bounded, sizes = _run([_text(2000, 9)], bound=256)
        assert bounded == single
        assert sizes == [256] * 7 + [208]

    def test_a_batch_within_the_bound_keeps_one_decode(self) -> None:
        _, sizes = _run([_text(100, 1), _text(150, 2)], bound=256)
        assert sizes == [250]

    def test_packs_split_at_the_logical_batch_still_match(self) -> None:
        texts = [_text(n, s) for s, n in enumerate((900, 900, 900, 30))]
        single, single_sizes = _run(texts, bound=None, n_batch=2048)
        bounded, _ = _run(texts, bound=256, n_batch=2048)
        assert len(single_sizes) == 2
        assert bounded == single

    def test_other_pooling_is_never_sliced(self) -> None:
        _, sizes = _run([_text(700, 3)], bound=256, pooling=_MEAN)
        assert sizes == [700]

    def test_an_unset_bound_is_upstream_behaviour(self) -> None:
        _, sizes = _run([_text(700, 3)], bound=None)
        assert sizes == [700]


def _gguf_header_file(
    path: pathlib.Path, architecture: str, *, causal: bool | None = None, extra_items: int = 0
) -> pathlib.Path:
    """A minimal GGUF v3 KV header; ``extra_items`` inflates the metadata count it declares."""

    def gguf_string(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return struct.pack("<Q", len(encoded)) + encoded

    items = [(gguf_string("general.architecture"), 8, gguf_string(architecture))]
    if causal is not None:
        key = gguf_string(f"{architecture}.attention.causal")
        items.append((key, 7, struct.pack("<?", causal)))
    payload = bytearray(struct.pack("<4sIQQ", b"GGUF", 3, 0, len(items) + extra_items))
    for key, value_type, value in items:
        payload += key + struct.pack("<I", value_type) + value
    path.write_bytes(bytes(payload))
    return path


class TestGateAgainstThePolicy:
    """Why the gate reads the loaded runtime and not only ``_model_context_policy``'s sizes."""

    @pytest.mark.parametrize(
        "architecture, causal",
        [
            pytest.param("bert", None, id="cache-less-encoder"),
            pytest.param("qwen3", False, id="declared-non-causal"),
        ],
    )
    def test_a_readable_non_causal_header_gets_one_micro_batch_and_is_never_sliced(
        self, tmp_path: pathlib.Path, architecture: str, causal: bool | None
    ) -> None:
        path = _gguf_header_file(tmp_path / "model.gguf", architecture, causal=causal)
        policy = embeddings_mod._model_context_policy(path)
        assert policy.n_ubatch == policy.n_batch
        texts = [_text(900, 1), _text(900, 2), _text(policy.n_batch - 1800, 3)]
        _, sizes = _run(texts, bound=policy.n_ubatch, n_batch=policy.n_batch)
        assert sizes == [policy.n_batch]

    @pytest.mark.parametrize(
        "metadata, memory",
        [
            pytest.param({"general.architecture": "bert"}, None, id="cache-less-encoder"),
            pytest.param(
                {"general.architecture": "qwen3", "qwen3.attention.causal": "false"},
                "mem-handle",
                id="declared-non-causal",
            ),
        ],
    )
    def test_a_header_the_policy_cannot_read_keeps_a_non_causal_model_whole(
        self, tmp_path: pathlib.Path, caplog, metadata: dict, memory: object | None
    ) -> None:
        # The policy's bounded parser refuses a metadata table longer than
        # _GGUF_MAX_METADATA_ITEMS, which llama.cpp loads, and falls back to the
        # decoder sizes whatever the model is.
        path = _gguf_header_file(
            tmp_path / "model.gguf",
            metadata["general.architecture"],
            extra_items=embeddings_mod._GGUF_MAX_METADATA_ITEMS,
        )
        with caplog.at_level("WARNING", logger=embeddings_mod.__name__):
            policy = embeddings_mod._model_context_policy(path)
        assert "Could not read GGUF metadata" in caplog.text
        assert policy.n_ubatch < policy.n_batch
        # A bound set from those sizes alone would cut an input between one and
        # two micro-batches long in two. Its length comes from the shipped
        # micro-batch, so the shape holds whatever _N_UBATCH is.
        assert policy.n_ubatch == embeddings_mod._N_UBATCH
        tokens = policy.n_ubatch + policy.n_ubatch // 2
        assert tokens <= policy.n_batch
        _, sizes = _run([_text(tokens, 4)], bound=policy.n_ubatch, n_batch=policy.n_batch)
        assert sizes == [policy.n_ubatch, tokens - policy.n_ubatch]
        # The loaded runtime still knows what the model is, so no bound is set.
        llm = _fake_llm(memory=memory, metadata=metadata, n_ubatch=policy.n_ubatch)
        assert embeddings_mod._install_bounded_decode(llm) is False
        assert not hasattr(llm, "kiro_crew_max_decode_tokens")


_REAL_MODEL = os.environ.get("KIROCREW_TEST_EMBED_MODEL", "")

# What TestRealModel last passed on: every vendored native llama.cpp library and
# the three binding files the embed path runs, as their vendor-manifest digests,
# plus the shipped model's pin. Proven on linux_x86_64 with the shipped
# qwen3-embedding-0.6b.gguf, sha256
# 06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439.
_PROVEN_RUNTIME_DIGEST = "51809aa578037b401030db5cbe2886543411d556d863af9e40b751ae33f12e95"
_EMBED_PATH_BINDINGS = ("llama_cpp/llama.py", "llama_cpp/_internals.py", "llama_cpp/llama_cpp.py")
_VENDOR_MANIFEST = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "vendor_manifest.sha256"
)


def _vendored_runtime_digest() -> str:
    rows = []
    for line in _VENDOR_MANIFEST.read_text(encoding="utf-8").splitlines():
        digest, _, path = line.partition("  ")
        name = path.removeprefix("src/kiro_crew/_vendor/")
        if name.startswith("llama_cpp_libs/") or name in _EMBED_PATH_BINDINGS:
            rows.append(f"{digest}  {name}")
    rows.append(f"{embeddings_mod._GGUF_SHA256}  shipped-model")
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


def test_the_byte_identity_proof_covers_the_vendored_runtime() -> None:
    current = _vendored_runtime_digest()
    assert current == _PROVEN_RUNTIME_DIGEST, (
        "The vendored llama.cpp runtime, its embed-path binding or the shipped model "
        "changed since TestRealModel last proved the bounded decode byte-identical to "
        "the single decode, and CI has no model to prove it again. Run "
        "`KIROCREW_TEST_EMBED_MODEL=<the shipped GGUF, sha256 _GGUF_SHA256> python -m "
        "pytest -n0 test/test_embed_bounded_decode.py::TestRealModel` on this tree; when "
        f"it passes set _PROVEN_RUNTIME_DIGEST = {current!r} (src/kiro_crew/_vendor/"
        "README.md, 'Updating the vendored tree')."
    )


def _anon_mib() -> int:
    with open("/proc/self/status", encoding="ascii") as fh:
        line = next(line for line in fh if line.startswith("RssAnon:"))
    return int(line.split()[1]) // 1024


@pytest.mark.skipif(
    not (_REAL_MODEL and pathlib.Path(_REAL_MODEL).is_file()),
    reason="set KIROCREW_TEST_EMBED_MODEL to a GGUF embedding model to run",
)
class TestRealModel:
    def test_vectors_match_and_the_output_buffer_stays_within_one_micro_batch(self) -> None:
        llama_cls = embeddings_mod._load_llama_class()
        if llama_cls is None:
            pytest.skip("vendored llama.cpp runtime unavailable on this host")
        path = pathlib.Path(_REAL_MODEL)
        policy = embeddings_mod._model_context_policy(path)
        llm = llama_cls(
            model_path=str(path),
            embedding=True,
            pooling_type=_LAST,
            n_ctx=policy.n_ctx,
            n_batch=policy.n_batch,
            n_ubatch=policy.n_ubatch,
            n_threads=4,
            n_threads_batch=4,
            verbose=False,
        )
        embeddings_mod._install_metadata_kv_clear(llm)
        assert embeddings_mod._install_bounded_decode(llm)
        bound = llm.kiro_crew_max_decode_tokens
        words = "memory gateway session vector index transcript slot crew".split()
        texts = [
            " ".join(words[(i * 3 + k) % len(words)] for k in range(n))
            for i, n in enumerate((900, 3, 420, 60, 1300))
        ]

        def vectors(limit: int | None) -> list[bytes]:
            llm.kiro_crew_max_decode_tokens = limit
            data = llm.create_embedding(texts)["data"]
            return [struct.pack(f"{len(d['embedding'])}f", *d["embedding"]) for d in data]

        assert max(len(llm.tokenize(t.encode())) for t in texts) > bound
        before = _anon_mib() if sys.platform.startswith("linux") else None
        bounded = vectors(bound)
        grown_bounded = _anon_mib() - before if before is not None else None
        single = vectors(None)
        grown_single = _anon_mib() - before - grown_bounded if before is not None else None
        assert bounded == single
        if grown_bounded is not None and grown_single is not None:
            # Each output row is 610,772 bytes for the shipped model. The bounded pass
            # holds at most n_ubatch rows; the single one re-reserves the whole pack.
            assert grown_bounded < 600, grown_bounded
            assert grown_single > 400, grown_single
        llm.close()
