"""Regression guard for the embedder's eager per-token scores buffer (#6827).

Upstream ``llama_cpp.Llama.__init__`` unconditionally allocates
``self.scores = np.ndarray((n_batch, n_vocab), dtype=np.single)`` at model load
(the shape is ``(n_batch, n_vocab)`` because ``logits_all`` defaults to False at
construction). Nothing on the embedding path ever reads it: vectors come from
``llama_get_embeddings_seq`` / ``llama_get_embeddings``, and ``eval()`` only
writes scores when ``self._logits_all`` is set. For Qwen3-Embedding-0.6B
(n_vocab 151,936) at the embedder's ``n_batch == n_ctx == 2048`` that was
~1.24 GB of float32 allocated once and never touched.

The vendored copy therefore diverges from upstream: when the model is opened in
embedding mode with ``logits_all`` off, the row count collapses to ZERO, so the
buffer costs nothing and the full ~1.24 GB is reclaimed. ``n_batch`` stays equal
to ``n_ctx``, so the logical batch still covers a whole input in one go and no
input length can be silently truncated.

Two groups of tests below:

* Call-site tests fake the vendored Llama class exactly as
  ``test/test_embeddings.py`` does (capturing constructor kwargs) and pin
  ``embedding=True`` plus ``n_batch == n_ctx``. They fail if a future change
  reintroduces a reduced ``n_batch``, which would recreate a truncation band for
  unspaced input (CJK / hex / minified text) that the whitespace-based chunker's
  word count does not bound.
* Vendored-constructor tests parse the SHIPPED
  ``src/kiro_crew/_vendor/llama_cpp/llama.py`` and evaluate its real row-count
  expression under controlled inputs. They fail if a re-vendor from upstream
  drops the divergence, or if the guard stops feeding the allocation.

No test loads a real model or hits the network.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import numpy as np

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.embeddings import LlamaCppEmbedder

# Qwen3-Embedding-0.6B, the shipped embedding model.
_N_VOCAB = 151_936
_VENDORED_LLAMA = (
    pathlib.Path(embeddings_mod.__file__).resolve().parent / "_vendor" / "llama_cpp" / "llama.py"
)


# ═══════════════════════════════════════════════════════════════════════════
# Call site: the embedder must keep the full logical batch
# ═══════════════════════════════════════════════════════════════════════════


def _make_recording_llama_class(dim: int = embeddings_mod._DEFAULT_DIM):
    """Fake Llama class that records the kwargs its constructor was handed.

    Mirrors ``_make_fake_llama_class`` in test/test_embeddings.py: it never loads
    a real model and returns a fixed-width embedding for ``create_embedding``.
    """

    class _RecordingLlama:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            type(self).instances.append(self)

        def create_embedding(self, texts):
            return {"data": [{"embedding": [0.1] * dim} for _ in texts]}

    return _RecordingLlama


def _write_model_file(path: pathlib.Path) -> pathlib.Path:
    """Write a stand-in GGUF wide enough that model_file_present() accepts it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"g" * (embeddings_mod._GGUF_MIN_BYTES + 100_000))
    return path


def _load_embedder(tmp_path: pathlib.Path, monkeypatch):
    """Build an embedder wired to a recording fake Llama and load it."""
    fake_cls = _make_recording_llama_class()
    monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
    model = _write_model_file(tmp_path / "model.gguf")
    emb = LlamaCppEmbedder(model_path=model)
    assert emb.wait_ready(timeout=5), "fake model failed to load"
    assert fake_cls.instances, "Llama constructor was never called"
    return fake_cls.instances[0].kwargs


def test_embedder_opens_the_model_in_embedding_mode(tmp_path: pathlib.Path, monkeypatch) -> None:
    """``embedding=True`` is what triggers the vendored zero-row allocation.

    If the embedder ever stopped passing it, the scores buffer would silently
    come back at full size, so this is the load-bearing precondition of the fix.
    """
    kwargs = _load_embedder(tmp_path, monkeypatch)
    assert kwargs["embedding"] is True
    # logits_all is not passed, so it keeps its False default -- the other half
    # of the guard's condition.
    assert "logits_all" not in kwargs


def test_embedder_keeps_the_full_logical_batch(tmp_path: pathlib.Path, monkeypatch) -> None:
    """n_batch must stay equal to n_ctx now that the buffer is skipped outright.

    ``Llama.embed(truncate=True)`` clips each input to ``n_batch`` tokens, so any
    ``n_batch < n_ctx`` opens a band of token counts that are accepted by the
    context window but silently truncated -- and the pre-Llama guard is a
    CHARACTER clip plus a whitespace-based word count, neither of which bounds
    tokens for unspaced input (CJK, hex, minified JS). Skipping the buffer in the
    constructor removes the reason to shrink n_batch at all, so this test fails
    if a reduced n_batch is reintroduced here.
    """
    kwargs = _load_embedder(tmp_path, monkeypatch)
    assert kwargs["n_ctx"] == embeddings_mod._N_CTX
    assert kwargs["n_batch"] == embeddings_mod._N_CTX
    assert kwargs["n_batch"] == kwargs["n_ctx"]
    # n_ubatch is a separate, already-tuned lever and stays below n_batch.
    assert kwargs["n_ubatch"] == embeddings_mod._N_UBATCH
    assert kwargs["n_ubatch"] < kwargs["n_batch"]


# ═══════════════════════════════════════════════════════════════════════════
# Vendored constructor: the divergence itself
# ═══════════════════════════════════════════════════════════════════════════


def _llama_init_body() -> list[ast.stmt]:
    """AST statements of the shipped ``Llama.__init__``."""
    tree = ast.parse(_VENDORED_LLAMA.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Llama":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "__init__":
                    return sub.body
    raise AssertionError("Llama.__init__ not found in the vendored llama.py")


def _row_count_expression() -> ast.expr:
    """The real ``n_score_rows = ...`` expression from the shipped file."""
    for stmt in _llama_init_body():
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "n_score_rows" for t in stmt.targets
        ):
            return stmt.value
    raise AssertionError(
        "the vendored Llama.__init__ no longer computes `n_score_rows`; the "
        "kiro_crew scores-buffer divergence (#6827) was lost, probably by a "
        "re-vendor from upstream"
    )


def _eval_row_count(*, embedding: bool, logits_all: bool) -> int:
    """Evaluate the shipped row-count expression under controlled inputs.

    Interprets the tiny expression grammar directly (conditional expressions,
    ``and``/``not``, comparisons, name lookups, constants) instead of ``eval``,
    so no code object from the vendored file is ever executed here. Anything
    outside that grammar fails loudly — which is the point: the divergence
    changing shape is exactly what this suite must surface, not paper over.
    """
    namespace: dict[str, object] = {
        "embedding": embedding,
        "logits_all": logits_all,
        "n_ctx": embeddings_mod._N_CTX,
        "n_batch": embeddings_mod._N_CTX,
        # self._logits_all is derived from logits_all (or forced True by a draft
        # model) before the allocation; mirror the no-draft-model case.
        "self": SimpleNamespace(_logits_all=logits_all),
    }

    def interp(node: ast.expr) -> object:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return namespace[node.id]
        if isinstance(node, ast.Attribute):
            return getattr(interp(node.value), node.attr)
        if isinstance(node, ast.IfExp):
            return interp(node.body) if interp(node.test) else interp(node.orelse)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            result: object = True
            for value in node.values:
                result = interp(value)
                if not result:
                    return result
            return result
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not interp(node.operand)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = interp(node.left), interp(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError(
            "the vendored `n_score_rows` expression uses syntax this test's "
            f"interpreter does not model ({ast.dump(node)}); if the divergence "
            "changed shape, extend the interpreter to match the new expression"
        )

    value = interp(_row_count_expression())
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def test_embedding_mode_allocates_zero_score_rows() -> None:
    """The embedder's exact construction must produce a zero-row buffer."""
    assert _eval_row_count(embedding=True, logits_all=False) == 0


def test_non_embedding_modes_keep_upstream_row_counts() -> None:
    """The divergence must not change generation behaviour.

    Outside embedding mode the shipped expression must still yield upstream's
    ``n_ctx if logits_all else n_batch``, so a completion model is unaffected.
    """
    assert _eval_row_count(embedding=False, logits_all=False) == embeddings_mod._N_CTX
    assert _eval_row_count(embedding=False, logits_all=True) == embeddings_mod._N_CTX
    # An embedding model that explicitly asks for all logits still gets them.
    assert _eval_row_count(embedding=True, logits_all=True) == embeddings_mod._N_CTX


def test_scores_allocation_consumes_the_guarded_row_count() -> None:
    """The guard must actually feed ``self.scores``, not sit beside it.

    Without this, the row-count expression could be computed and then ignored by
    an allocation that hard-codes ``n_batch`` again.
    """
    for stmt in _llama_init_body():
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        if not any(
            isinstance(t, ast.Attribute)
            and t.attr == "scores"
            and isinstance(t.value, ast.Name)
            and t.value.id == "self"
            for t in targets
        ):
            continue
        call = stmt.value
        assert isinstance(call, ast.Call), "self.scores is no longer an ndarray call"
        shape = call.args[0]
        assert isinstance(shape, ast.Tuple), "self.scores shape is not a tuple literal"
        rows = shape.elts[0]
        assert isinstance(rows, ast.Name) and rows.id == "n_score_rows", (
            "self.scores is allocated from something other than the guarded "
            "n_score_rows row count"
        )
        return
    raise AssertionError("no `self.scores = ...` allocation in Llama.__init__")


def test_zero_rows_reclaims_the_whole_logits_allocation() -> None:
    """Quantify the win the fix claims, in the shipped model's terms."""
    upstream_bytes = embeddings_mod._N_CTX * _N_VOCAB * 4
    fixed_bytes = _eval_row_count(embedding=True, logits_all=False) * _N_VOCAB * 4
    assert fixed_bytes == 0
    # ~1.24 GB reclaimed in full, not merely reduced.
    assert upstream_bytes > 1_200_000_000
    assert upstream_bytes - fixed_bytes == upstream_bytes


def test_slice_based_scores_readers_survive_a_zero_row_buffer() -> None:
    """Pin why zero rows is safe for every audited reader of ``self.scores``.

    The readers that remain reachable (or trivially reachable) in embedding mode
    all SLICE rather than index: the ``_scores`` and ``eval_logits`` properties do
    ``scores[: n_tokens, :]``, ``save_state`` copies that slice, and
    ``load_state`` assigns into it. Numpy slicing clamps out of range, so each
    yields an empty result instead of raising, and the vocab width and dtype are
    preserved for anything inspecting the shape.
    """
    scores = np.ndarray((0, _N_VOCAB), dtype=np.single)
    assert scores.shape == (0, _N_VOCAB)
    assert scores.dtype == np.single
    assert scores.nbytes == 0
    # _scores / eval_logits, at n_tokens 0 and at an out-of-range n_tokens.
    assert scores[:0, :].shape == (0, _N_VOCAB)
    assert scores[:512, :].shape == (0, _N_VOCAB)
    assert scores[:512, :].tolist() == []
    # save_state -> load_state round trip stays shape-compatible.
    saved = scores[:0, :].copy()
    scores[:0, :] = saved
    rest = scores[0:, :]
    rest[rest > 0] = 0.0
