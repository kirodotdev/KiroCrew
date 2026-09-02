"""Round-5 review findings: five blocking defects, one test group each.

All five were real. Four share one shape -- the harness publishes a number that
is arithmetically well-formed and semantically wrong -- which is the same class
rounds 1-4 kept finding, so these tests assert the MECHANISM that makes the
number wrong rather than only a happy-path outcome:

* the recorded embedder identity must describe the LIVE embedder, or a custom
  model is stamped with the bundled identity and ``compare_reports`` -- which
  refuses only when identities DIFFER -- diffs two vector spaces and calls the
  delta exact;
* a mid-run embedding failure must refuse: blank text is filtered upstream, so a
  ``None`` there is an inference failure whose NULL row is reachable only by
  keyword match while the report still presents itself as vector retrieval;
* session granularity must OMIT the turn block rather than score session ids
  against gold turn ids, and must count dropped gold in the id space it actually
  ingested;
* all three corpus readers must use the nofollow helper, closing the same
  check-to-use window the sidecar and staging-file reads already close.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from kiro_crew.eval.bench.adapters import locomo as locomo_adapter
from kiro_crew.eval.bench.corpus import (
    CAT_SINGLE_HOP,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
)
from kiro_crew.eval.bench.ingest import (
    IngestConfig,
    IngestedInstance,
    IngestError,
    ingest_instance,
)
from kiro_crew.eval.bench.retrieval import QueryRetrieval, aggregate
from kiro_crew.eval.bench.safepath import UnsafePathError

LME_TS = "2023/04/10 (Mon) 23:07"

# Symlink behaviour is POSIX-specific twice over here: creating a link needs
# elevation or developer mode on Windows, and `O_NOFOLLOW` resolves to 0 there so
# the refusal these tests assert cannot happen. Matches the guards used by the
# other symlink tests in this suite.
posix_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="POSIX symlink and O_NOFOLLOW semantics"
)


def _turn(turn_id: str, session_id: str, text: str, speaker: str = "Alice") -> BenchTurn:
    return BenchTurn(turn_id=turn_id, session_id=session_id, speaker=speaker, text=text)


def _instance() -> BenchInstance:
    return BenchInstance(
        instance_id="r5",
        sessions=(
            BenchSession(
                "s1",
                (
                    _turn("t1", "s1", "we booked the scuba diving lessons in Malta"),
                    _turn("t2", "s1", "then argued about the espresso machine", "Bob"),
                ),
                LME_TS,
            ),
        ),
        queries=(),
    )


def _duplicate_session_instance() -> BenchInstance:
    """Two byte-identical sessions, so the second fragment is deduped and DROPPED.

    This exercises the drop path without monkeypatching the store: identical text
    makes ``first_seen`` true, which is the harness's own dedup branch.
    """
    turns_a = (
        _turn("t1", "s1", "we booked the scuba diving lessons in Malta"),
        _turn("t2", "s1", "then argued about the espresso machine", "Bob"),
    )
    turns_b = (
        _turn("t3", "s2", "we booked the scuba diving lessons in Malta"),
        _turn("t4", "s2", "then argued about the espresso machine", "Bob"),
    )
    return BenchInstance(
        instance_id="r5-dup",
        sessions=(
            BenchSession("s1", turns_a, LME_TS),
            BenchSession("s2", turns_b, LME_TS),
        ),
        queries=(
            BenchQuery(
                query_id="q1",
                question="where did they book lessons?",
                gold_answer="Malta",
                category=CAT_SINGLE_HOP,
                raw_category="1",
                gold_session_ids=("s2",),
                # Gold evidence is TURN-level, and t3 lives in the session whose
                # fragment gets deduped away.
                gold_turn_ids=("t3",),
            ),
        ),
    )


def _embed(text: str) -> list[float]:
    return [float(len(text) % 7), 0.5]


# ── Finding 2: the identity must describe what actually ran ──────────────────


def test_recorded_identity_comes_from_the_live_embedder(monkeypatch) -> None:
    """`KIROCREW_EMBED_MODEL_PATH` runs a different model, and the width can be
    adopted from the file, so the module constants do not describe the system."""
    from kiro_crew.eval.bench import run as run_mod

    class FakeEmbedder:
        model_id = "some-other-model:1.5b"
        dim = 768

    monkeypatch.setattr(
        "kiro_crew.embeddings.get_shared_embedder", lambda: FakeEmbedder(), raising=True
    )
    assert run_mod._production_embedder_id() == "some-other-model:1.5b@768"


def test_an_unreadable_identity_refuses_rather_than_claiming_the_bundled_one(
    monkeypatch,
) -> None:
    """Falling back to the constants would reintroduce the mislabelling, silently."""
    from kiro_crew.eval.bench import run as run_mod

    class Mute:
        pass

    monkeypatch.setattr(
        "kiro_crew.embeddings.get_shared_embedder", lambda: Mute(), raising=True
    )
    with pytest.raises(IngestError) as excinfo:
        run_mod._production_embedder_id()
    assert "does not report its identity" in str(excinfo.value)


# ── Finding 3: a mid-run inference failure must refuse ───────────────────────


def test_a_null_embedding_after_readiness_aborts_the_run(tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(text: str) -> list[float] | None:
        calls["n"] += 1
        # Succeed once, so the failure is provably mid-run rather than a startup
        # refusal that `prepare_embedder` would already have caught.
        return [0.1, 0.2] if calls["n"] == 1 else None

    with pytest.raises(IngestError) as excinfo:
        ingest_instance(
            _instance(),
            db_path=tmp_path / "null.db",
            embed_fn=flaky,
            config=IngestConfig(granularity="turn", timeline="now"),
        )
    msg = str(excinfo.value)
    assert "no vector" in msg
    assert "keyword" in msg
    assert calls["n"] >= 2, "the run should have got past the first fragment"


def test_a_healthy_run_is_unaffected(tmp_path: Path) -> None:
    """Otherwise the suite could pass by making the harness refuse everything."""
    loaded = ingest_instance(
        _instance(),
        db_path=tmp_path / "ok.db",
        embed_fn=_embed,
        config=IngestConfig(granularity="turn", timeline="now"),
    )
    try:
        assert loaded.report.written == 2
        assert loaded.report.null_embeddings == 0
    finally:
        loaded.close()


# ── Finding 4: session granularity has no turn level ────────────────────────


def _one_result() -> list[QueryRetrieval]:
    return [
        QueryRetrieval(
            query_id="q1",
            category=CAT_SINGLE_HOP,
            raw_category="1",
            unanswerable=False,
            gold_session_ids=("s1",),
            gold_turn_ids=("t1",),
            retrieved_session_ids=("s1",),
            retrieved_turn_ids=("t1",),
            unattributed_hits=0,
        )
    ]


def test_session_granularity_omits_the_turn_block_entirely() -> None:
    """Absent, never 0.0 -- a zero reads as a real and very bad score.

    Scored at k=1: a cut-off counts only for queries whose observed ranked list
    has at least k entries, so @5 would be legitimately unmeasurable here and the
    test would pass for the wrong reason.
    """
    agg = aggregate(_one_result(), k_values=(1,), turn_attribution=False)
    assert agg.turn == {}
    assert agg.turn_measurable == {}
    assert agg.session, "session metrics must still be published"


def test_turn_granularity_still_publishes_the_turn_block() -> None:
    agg = aggregate(_one_result(), k_values=(1,), turn_attribution=True)
    assert agg.turn_measurable, "turn mode must still report the turn block"


def test_ingest_marks_whether_turn_attribution_exists(tmp_path: Path) -> None:
    assert "turn_attribution" in IngestedInstance.__dataclass_fields__
    for granularity, expected in (("turn", True), ("session", False)):
        loaded = ingest_instance(
            _instance(),
            db_path=tmp_path / f"{granularity}.db",
            embed_fn=_embed,
            config=IngestConfig(granularity=granularity, timeline="now"),
        )
        try:
            assert loaded.turn_attribution is expected
        finally:
            loaded.close()


def test_session_mode_records_dropped_gold_as_turn_ids(tmp_path: Path) -> None:
    """`gold_turns` holds TURN ids, so testing a SESSION id never matches.

    Before the fix, session mode reported no dropped gold no matter what happened
    -- worse than a wrong number, because it is a clean bill of health that cannot
    fail.
    """
    loaded = ingest_instance(
        _duplicate_session_instance(),
        db_path=tmp_path / "dropgold.db",
        embed_fn=_embed,
        config=IngestConfig(granularity="session", timeline="now"),
    )
    try:
        assert loaded.report.dropped_fragments >= 1, "the duplicate should be deduped"
        assert loaded.report.dropped_gold == ("t3",), (
            "the dropped session contained gold turn t3, which is what must be "
            f"named; got {loaded.report.dropped_gold!r}"
        )
    finally:
        loaded.close()


def test_turn_mode_dropped_gold_is_unchanged(tmp_path: Path) -> None:
    """The session-mode branch must not alter turn-mode accounting."""
    loaded = ingest_instance(
        _duplicate_session_instance(),
        db_path=tmp_path / "dropgold_turn.db",
        embed_fn=_embed,
        config=IngestConfig(granularity="turn", timeline="now"),
    )
    try:
        assert loaded.report.dropped_gold == ("t3",)
    finally:
        loaded.close()


# ── Finding 5: the corpus readers must not follow a final symlink ────────────


@posix_symlinks
def test_a_symlink_into_a_protected_path_is_refused_through_the_link(
    tmp_path: Path, monkeypatch
) -> None:
    """The disclosure case: the RESOLVED target is what gets re-checked.

    Note the read path's contract differs from the write path's on purpose. A
    write through a link destroys the target, so `open_write_nofollow` refuses any
    final-component link. A read through a link discloses only what the
    resolved-target check already approved, and refusing all links would break
    legitimate setups such as a corpus cache symlinked to another disk. So the
    protection here is the re-check of the resolved target, plus O_NOFOLLOW on the
    canonical path to close the check-to-use window.
    """
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    secret = home / ".aws" / "credentials"
    secret.write_text("[default]\naws_secret_access_key = nope\n", encoding="utf-8")
    # `is_sensitive_path` resolves against the real home, so the fixture has to
    # become the home for the assertion to hold for the right reason.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    link = tmp_path / "corpus.json"
    link.symlink_to(secret)

    with pytest.raises(UnsafePathError):
        locomo_adapter.load_locomo_file(link)


@posix_symlinks
def test_an_ordinary_symlink_is_still_readable(tmp_path: Path) -> None:
    """Documents the deliberate read/write asymmetry above.

    If this ever starts failing, the read path has silently adopted the write
    path's stricter contract and legitimate corpus layouts will break.
    """
    real = tmp_path / "ordinary.json"
    real.write_text(json.dumps([]), encoding="utf-8")
    link = tmp_path / "corpus.json"
    link.symlink_to(real)
    corpus = locomo_adapter.load_locomo_file(link)
    assert corpus.source_path


def test_a_plain_corpus_file_still_loads(tmp_path: Path) -> None:
    """The suite must not be able to pass by refusing every read."""
    payload = tmp_path / "corpus.json"
    payload.write_text(json.dumps([]), encoding="utf-8")
    corpus = locomo_adapter.load_locomo_file(payload)
    assert corpus.source_path


def test_all_three_corpus_readers_route_through_the_nofollow_helper() -> None:
    """Point-wise patching is how the previous sweep left these three behind."""
    from kiro_crew.eval.bench import datasets
    from kiro_crew.eval.bench.adapters import longmemeval

    for mod, fn_name in (
        (datasets, "load_json"),
        (locomo_adapter, "load_locomo_file"),
        (longmemeval, "load_longmemeval_file"),
    ):
        src = inspect.getsource(getattr(mod, fn_name))
        assert "read_text_nofollow" in src, f"{fn_name} does not use the helper"
        # Comment lines in these functions name `path.open()` to explain why it is
        # NOT used, so a raw substring search matches the documentation.
        effective = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )
        assert ".open(" not in effective, f"{fn_name} still opens the path directly"
