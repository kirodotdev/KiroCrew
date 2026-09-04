"""Round-11: the store's text limit, and ordinary OS failures at the CLI boundary.

The text-limit finding is a measurement-validity defect of the worst kind in this
harness. MEASURED on LoCoMo before fixing:

    session granularity: 249 of 272 fragments (91.5%) exceed the 2000-char limit
    turn granularity:      0 of 5882 exceed it (2 fall below the 10-char floor)

`write_episodic` returns False for oversized text, so `--granularity session` was
publishing recall over the 8.5% of the haystack that happened to fit.

The fix refuses OVERSIZED text and keeps reporting UNDERSIZED, and that asymmetry is
the substance: oversize means the ingest strategy generates a shape the store cannot
hold (systematic — the haystack is not the corpus), while undersize is a property of
two individual turns that is already counted, named in `dropped_gold`, and warned
about in the report.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kiro_crew.eval.bench.corpus import BenchInstance, BenchSession, BenchTurn
from kiro_crew.eval.bench.ingest import IngestConfig, IngestError, ingest_instance
from kiro_crew.vector_memory import _EPISODIC_TEXT_MAX


def _instance_with(text: str) -> BenchInstance:
    return BenchInstance(
        instance_id="r11",
        sessions=(
            BenchSession(
                "s1",
                (
                    BenchTurn(turn_id="t1", session_id="s1", speaker="A", text=text),
                    BenchTurn(turn_id="t2", session_id="s1", speaker="B", text="short reply"),
                ),
                "2023/04/10 (Mon) 23:07",
            ),
        ),
        queries=(),
    )


# ── Oversized fragments refuse ───────────────────────────────────────────────


def test_an_oversized_fragment_refuses_the_run(tmp_path: Path) -> None:
    """Silently dropping it would publish recall over an incomplete haystack."""
    oversized = "word " * (_EPISODIC_TEXT_MAX // 2)
    assert len(oversized) > _EPISODIC_TEXT_MAX

    with pytest.raises(IngestError) as excinfo:
        ingest_instance(
            _instance_with(oversized),
            db_path=tmp_path / "big.db",
            embed_fn=lambda _t: [0.1, 0.2],
            config=IngestConfig(granularity="turn", timeline="now"),
        )
    message = str(excinfo.value)
    assert str(_EPISODIC_TEXT_MAX) in message
    assert "granularity session" in message, "the message should name the real cause"


def test_the_refusal_fires_before_the_embed_call(tmp_path: Path) -> None:
    """No point paying for inference on a fragment the store will reject.

    Also proves the check is not merely reacting to `write_episodic` returning False,
    which is what made the drop invisible in the first place.
    """
    calls: list[str] = []

    def embed(text: str) -> list[float]:
        calls.append(text)
        return [0.1, 0.2]

    with pytest.raises(IngestError):
        ingest_instance(
            _instance_with("x " * _EPISODIC_TEXT_MAX),
            db_path=tmp_path / "big2.db",
            embed_fn=embed,
            config=IngestConfig(granularity="turn", timeline="now"),
        )
    # One call for the width probe, and none for the oversized fragment.
    assert len(calls) == 1, f"embedded the oversized fragment: {len(calls)} calls"


def test_a_fragment_exactly_at_the_limit_is_accepted(tmp_path: Path) -> None:
    """Boundary: the refusal is `>`, not `>=`, so the limit itself must still work.

    Note what the limit applies to -- the FRAGMENT, not the raw turn text. With
    `speaker_prefix=True` the stored text is `"A: <turn>"`, so a 2000-character turn
    becomes a 2003-character fragment and is refused. Measured here rather than
    assumed, because that off-by-the-prefix is exactly the kind of thing a hardcoded
    length would get wrong.
    """
    from kiro_crew.eval.bench.ingest import fragment_text

    probe = BenchTurn(turn_id="t1", session_id="s1", speaker="A", text="x")
    overhead = len(fragment_text(probe, speaker_prefix=True)) - 1
    at_limit = "y" * (_EPISODIC_TEXT_MAX - overhead)

    loaded = ingest_instance(
        _instance_with(at_limit),
        db_path=tmp_path / "edge.db",
        embed_fn=lambda _t: [0.1, 0.2],
        config=IngestConfig(granularity="turn", timeline="now"),
    )
    try:
        assert loaded.report.written == 2
    finally:
        loaded.close()


def test_undersized_text_is_reported_not_refused(tmp_path: Path) -> None:
    """The deliberate asymmetry. Two real LoCoMo turns fall below the floor, and
    refusing the whole corpus over 0.03% of it would buy no honesty."""
    loaded = ingest_instance(
        _instance_with("hi"),  # below the store's 10-char floor
        db_path=tmp_path / "small.db",
        embed_fn=lambda _t: [0.1, 0.2],
        config=IngestConfig(granularity="turn", timeline="now"),
    )
    try:
        assert loaded.report.attempted == 2
        assert loaded.report.dropped_fragments >= 1, "the drop must be counted"
        assert loaded.report.written >= 1, "the usable turn must still land"
    finally:
        loaded.close()


# ── Ordinary OS failures are reported, not tracebacked ──────────────────────


def test_an_os_error_is_reported_at_the_cli_boundary(monkeypatch) -> None:
    """A read-only output directory is not a refusal — `BenchRefusal` does not cover
    it — but it is still a failed run rather than a crashed program."""
    from kiro_crew import cli_bench

    def boom(*_a: object, **_k: object):
        raise PermissionError(13, "Permission denied", "/nonwritable/reports")

    monkeypatch.setattr("kiro_crew.eval.bench.datasets.ensure", boom)
    rc = cli_bench.bench_cmd(argparse.Namespace(bench_action="fetch", corpus="locomo10"))
    assert rc == 1

    # The inner dispatch still raises, so this test fails if the handler is removed.
    with pytest.raises(OSError):
        cli_bench._bench_dispatch(argparse.Namespace(bench_action="fetch", corpus="locomo10"))


def test_a_programming_error_is_not_swallowed(monkeypatch) -> None:
    """The boundary catches refusals and OS errors, NOT everything.

    A `TypeError` from a bug in this package must still surface as a traceback --
    swallowing it would turn a defect into a quiet exit code.
    """
    from kiro_crew import cli_bench

    def bug(*_a: object, **_k: object):
        raise TypeError("a real bug, not an environmental failure")

    monkeypatch.setattr("kiro_crew.eval.bench.datasets.ensure", bug)
    with pytest.raises(TypeError):
        cli_bench.bench_cmd(argparse.Namespace(bench_action="fetch", corpus="locomo10"))
