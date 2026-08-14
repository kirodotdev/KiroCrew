"""Every caller-influenced filesystem path in the harness, and every raising guard.

Written as a sweep rather than two tests for two reported findings. Review found the
read gate missing in round one and the *write* gate missing in round two — the same
class, in mirror image — so the third round audited all five entry points and all
three refusal-to-catch pairs at once. Three of the five holes below were never
reported by any reviewer; they were found by enumerating the surface.

Every test re-anchors HOME at the fixture, because `is_sensitive_path` resolves
against the real home: without that the assertions pass for the wrong reason (the
fake `.aws` is not sensitive, so the guard never fires and the write succeeds).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.eval.bench import datasets
from kiro_crew.eval.bench.corpus import (
    CAT_SINGLE_HOP,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
    Corpus,
)
from kiro_crew.eval.bench.ingest import IngestConfig
from kiro_crew.eval.bench.retrieval import RetrievalConfig
from kiro_crew.eval.bench.run import RunResult, run_retrieval, write_report
from kiro_crew.eval.bench.safepath import (
    UnsafePathError,
    guard_output_dir,
    guard_read_path,
    guard_write_path,
)
from kiro_crew.eval.bench.toy_embedder import TOY_EMBEDDER_ID, toy_embed_fn


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-anchor home so the production gate actually fires on fixture paths."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".ssh").mkdir()
    return tmp_path


# ── The guard itself ─────────────────────────────────────────────────────────


def test_a_protected_file_is_refused_for_read_and_write(fake_home: Path) -> None:
    target = fake_home / ".aws" / "credentials"
    target.write_text("[default]\n")
    with pytest.raises(UnsafePathError):
        guard_read_path(target, what="corpus file")
    with pytest.raises(UnsafePathError):
        guard_write_path(target, what="report")


def test_a_symlink_cannot_launder_the_target(fake_home: Path, tmp_path: Path) -> None:
    """Resolution happens before the check, so the link is followed."""
    target = fake_home / ".ssh" / "id_rsa"
    target.write_text("key\n")
    link = tmp_path / "innocent.json"
    if not hasattr(link, "symlink_to"):
        pytest.skip("no symlink support")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating a symlink needs privilege on this host")
    with pytest.raises(UnsafePathError):
        guard_read_path(link, what="corpus file")


def test_an_output_dir_that_merely_contains_a_protected_tree_is_refused(
    fake_home: Path,
) -> None:
    """This is why the dir guard is not just the write guard.

    Home is not itself a sensitive path, but `~/.ssh` lies under it, and a command
    that creates directories and files there is doing something no benchmark needs.
    """
    with pytest.raises(UnsafePathError) as exc:
        guard_output_dir(fake_home, what="report output directory")
    assert "lies under it" in str(exc.value)


def test_an_ordinary_directory_is_allowed(tmp_path: Path) -> None:
    """The guard must not be so broad that it refuses the normal case."""
    out = tmp_path / "bench_results"
    assert guard_output_dir(out, what="report output directory") == out.resolve()
    assert guard_write_path(out / "a.json", what="report") == (out / "a.json").resolve()


# ── Entry point 1: the report WRITE (reported) ───────────────────────────────


def _result() -> RunResult:
    return RunResult(
        corpus_name="toy",
        corpus_variant="v0",
        corpus_fingerprint="abc",
        instances=1,
        sessions=1,
        turns=1,
        queries=1,
        # format_report reads these, so a bare {} would fail for a reason unrelated
        # to what the test is about.
        ingest={"granularity": "turn", "timeline": "now"},
        retrieval={"mmr": True},
        backend="sqlite_cosine",
        embedder=TOY_EMBEDDER_ID,
        metrics=__import__(
            "kiro_crew.eval.bench.retrieval", fromlist=["RetrievalAggregate"]
        ).RetrievalAggregate(),
    )


def test_write_report_refuses_a_protected_out_dir(fake_home: Path) -> None:
    with pytest.raises(UnsafePathError):
        write_report(_result(), fake_home / ".aws")


def test_write_report_refuses_a_stem_that_traverses_out_of_a_safe_dir(
    fake_home: Path, tmp_path: Path
) -> None:
    """A safe --out-dir plus a traversing --stem must not slip through.

    Both composed paths are checked individually for exactly this reason; checking
    only the directory would let `--stem ../.aws/credentials` through.
    """
    safe = tmp_path / "out"
    with pytest.raises(UnsafePathError):
        write_report(_result(), safe, stem="../.aws/credentials")


def test_write_report_does_not_create_the_directory_when_it_refuses(
    fake_home: Path,
) -> None:
    """Gate BEFORE mkdir: creating a tree under a protected root is already damage."""
    victim = fake_home / ".ssh" / "nested"
    with pytest.raises(UnsafePathError):
        write_report(_result(), victim)
    assert not victim.exists()


def test_write_report_still_works_for_an_ordinary_directory(tmp_path: Path) -> None:
    md, js = write_report(_result(), tmp_path / "results", stem="run1")
    assert md.exists() and js.exists()
    assert json.loads(js.read_text())["config"]["embedder"] == TOY_EMBEDDER_ID


# ── Entry point 2: the corpus cache root, from the environment (not reported) ─


def test_the_corpus_cache_root_is_gated_too(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KIROCREW_BENCH_CACHE can point anywhere, so it needs the same gate as argv.

    Not reported by any reviewer — found by enumerating the entry points after the
    write gate was reported. A cache root inside the trust root would drop corpus
    files and `.sha256` sidecars there.
    """
    monkeypatch.setenv("KIROCREW_BENCH_CACHE", str(fake_home / ".aws"))
    with pytest.raises(UnsafePathError):
        datasets.ensure("locomo10", allow_download=False)


# ── Entry point 3: the public *_file adapters (not reported) ─────────────────


def test_the_file_adapters_gate_their_caller_supplied_path(fake_home: Path) -> None:
    from kiro_crew.eval.bench.adapters import load_locomo_file, load_longmemeval_file

    victim = fake_home / ".aws" / "credentials"
    victim.write_text("[default]\n")
    with pytest.raises(UnsafePathError):
        load_locomo_file(victim)
    with pytest.raises(UnsafePathError):
        load_longmemeval_file(victim, variant="oracle")


# ── Every raising guard has a catch site ─────────────────────────────────────


class _Args:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def _retrieval_args(**over: object) -> _Args:
    base: dict[str, object] = {
        "bench_action": "retrieval",
        "corpus": "locomo10",
        "instances": 1,
        "queries": 1,
        "granularity": "turn",
        "timeline": "now",
        "no_mmr": False,
        "no_dedup": False,
        "toy_embedder": False,
        "out_dir": "bench_results",
        "stem": None,
    }
    base.update(over)
    return _Args(**base)


def test_a_non_resident_embedder_prints_a_refusal_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """IngestError is the guard that fires on any host with an incomplete payload.

    It existed to print a good message and was not caught, so the normal state of
    such a host produced a traceback from the CLI.
    """
    from kiro_crew import cli_bench
    from kiro_crew.eval.bench.ingest import IngestError

    def _boom(*_a: object, **_k: object) -> None:
        raise IngestError("the embedding model is not resident")

    monkeypatch.setattr("kiro_crew.eval.bench.run.prepare_embedder", _boom)
    monkeypatch.setattr(
        cli_bench, "_load_corpus", lambda key: _tiny_corpus()  # noqa: ARG005
    )
    rc = cli_bench.bench_cmd(_retrieval_args(out_dir=str(tmp_path)))
    out = capsys.readouterr().out
    assert rc == 1
    assert "refusing to run" in out
    assert "not resident" in out
    assert "Traceback" not in out


def test_a_refused_report_write_does_not_discard_the_measurement(
    fake_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report is printed before it is saved, so a refused write loses only the file.

    Reported as such rather than as a failed run — the numbers are on stdout.
    """
    from kiro_crew import cli_bench

    monkeypatch.setattr(
        cli_bench, "_load_corpus", lambda key: _tiny_corpus()  # noqa: ARG005
    )
    rc = cli_bench.bench_cmd(
        _retrieval_args(toy_embedder=True, out_dir=str(fake_home / ".aws"))
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "not saved" in out
    # The measurement itself still reached the user.
    assert "session-level" in out or "retrieval" in out


def _tiny_corpus() -> Corpus:
    return Corpus(
        "toy",
        "v0",
        (
            BenchInstance(
                "i1",
                (
                    BenchSession("s1", (BenchTurn("s1#t0", "s1", "Alice", "the blue mat"),)),
                    BenchSession("s2", (BenchTurn("s2#t0", "s2", "Alice", "a green park"),)),
                ),
                (
                    BenchQuery(
                        query_id="q1",
                        question="blue mat",
                        category=CAT_SINGLE_HOP,
                        gold_session_ids=("s1",),
                    ),
                ),
            ),
        ),
    )


def test_the_tiny_corpus_actually_runs_when_nothing_is_refused(tmp_path: Path) -> None:
    """Guard against a vacuous suite: the happy path must still work end to end."""
    result = run_retrieval(
        _tiny_corpus(),
        ingest_config=IngestConfig(timeline="now"),
        retrieval_config=RetrievalConfig(k_values=(1,)),
        embed_fn=toy_embed_fn(),
        embedder_id=TOY_EMBEDDER_ID,
        store_root=tmp_path,
    )
    assert result.metrics.scored_queries == 1
