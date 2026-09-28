"""``cli_bench`` — the argument surface and the dispatch's exit codes.

The existing ``test_bench_*`` files all enter through ``bench_cmd`` with a
hand-rolled namespace, so nothing has ever built the real parser or driven the
subcommand table. That leaves two things unpinned, and both are the kind of
regression that turns a benchmark run into a wrong number rather than an error:

* ``_positive_int`` — the ONE place every count is validated. ``type=int``
  accepts ``-1``, which reaches ``Corpus.subset`` and silently measures
  all-but-the-last instance while reporting a full run. Its two refusal paths
  (non-integer, non-positive) and its attachment to every counted option are
  covered here.
* ``register_bench_parser`` — the defaults are load-bearing (``--timeline
  anchored``, ``--granularity turn``, MMR and dedup ON), because a flipped
  default changes what was measured without changing what is claimed.

Also covered: the dispatch's exit codes for a missing and an unknown action, the
``list``/``fetch`` routes with ``datasets`` stubbed, and ``bench_cmd``'s OSError
catch site — a report that cannot be written is a failed run, not a traceback.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from kiro_crew.cli_bench import (
    DEFAULT_OUT_DIR,
    _bench_dispatch,
    _load_corpus,
    _positive_int,
    bench_cmd,
    register_bench_parser,
)


class _Args:
    """A stand-in for the argparse namespace the dispatch receives."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def _spec(dataset: str, variant: str) -> Any:
    """A stand-in DatasetSpec — ``_load_corpus`` reads only these two fields."""
    return _Args(dataset=dataset, variant=variant)


@pytest.fixture()
def parser() -> argparse.ArgumentParser:
    """A top-level parser with the real ``bench`` subcommand wired in."""
    root = argparse.ArgumentParser(prog="kirocrew")
    register_bench_parser(root.add_subparsers(dest="command"))
    return root


class TestPositiveInt:
    def test_accepts_a_positive_count(self) -> None:
        assert _positive_int("7") == 7

    def test_refuses_a_non_integer_in_argparses_own_error_path(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError) as excinfo:
            _positive_int("zibble")
        assert "expected an integer" in str(excinfo.value)

    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_refuses_zero_and_negative_counts(self, raw: str) -> None:
        """A negative slice measures the tail and reports it as a full run."""
        with pytest.raises(argparse.ArgumentTypeError) as excinfo:
            _positive_int(raw)
        assert "positive integer" in str(excinfo.value)


class TestRegisterBenchParser:
    def test_retrieval_defaults_are_the_production_shape(
        self, parser: argparse.ArgumentParser
    ) -> None:
        args = parser.parse_args(["bench", "retrieval"])
        assert args.bench_action == "retrieval"
        assert args.corpus == "locomo10"
        assert args.instances is None
        assert args.queries is None
        assert args.granularity == "turn"
        assert args.timeline == "anchored"
        assert args.no_mmr is False
        assert args.no_dedup is False
        assert args.toy_embedder is False
        assert args.out_dir == DEFAULT_OUT_DIR
        assert args.stem is None

    def test_retrieval_flags_are_all_wired(self, parser: argparse.ArgumentParser) -> None:
        args = parser.parse_args(
            [
                "bench",
                "retrieval",
                "longmemeval_s",
                "--instances",
                "3",
                "--queries",
                "2",
                "--granularity",
                "session",
                "--timeline",
                "now",
                "--no-mmr",
                "--no-dedup",
                "--toy-embedder",
                "--out-dir",
                "zibble_out",
                "--stem",
                "zibble_stem",
            ]
        )
        assert (args.corpus, args.instances, args.queries) == ("longmemeval_s", 3, 2)
        assert (args.granularity, args.timeline) == ("session", "now")
        assert args.no_mmr and args.no_dedup and args.toy_embedder
        assert (args.out_dir, args.stem) == ("zibble_out", "zibble_stem")

    @pytest.mark.parametrize("option", ["--instances", "--queries"])
    def test_a_negative_slice_is_refused_before_any_corpus_is_read(
        self, parser: argparse.ArgumentParser, option: str
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["bench", "retrieval", option, "-1"])
        assert excinfo.value.code == 2

    def test_fetch_defaults_to_the_small_corpus(self, parser: argparse.ArgumentParser) -> None:
        assert parser.parse_args(["bench", "fetch"]).corpus == "locomo10"
        assert parser.parse_args(["bench", "fetch", "locomo"]).corpus == "locomo"

    def test_list_takes_no_arguments(self, parser: argparse.ArgumentParser) -> None:
        args = parser.parse_args(["bench", "list"])
        assert args.bench_action == "list"

    def test_compare_requires_both_reports_and_defaults_k_to_five(
        self, parser: argparse.ArgumentParser
    ) -> None:
        args = parser.parse_args(["bench", "compare", "base.json", "cand.json"])
        assert (args.baseline, args.candidate, args.k) == ("base.json", "cand.json", 5)
        assert parser.parse_args(["bench", "compare", "b", "c", "-k", "9"]).k == 9
        with pytest.raises(SystemExit):
            parser.parse_args(["bench", "compare", "only-one.json"])

    def test_compare_k_goes_through_the_positive_gate(
        self, parser: argparse.ArgumentParser
    ) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["bench", "compare", "b", "c", "-k", "0"])

    def test_bench_without_an_action_leaves_bench_action_unset(
        self, parser: argparse.ArgumentParser
    ) -> None:
        assert parser.parse_args(["bench"]).bench_action is None


class TestDispatchRouting:
    def test_no_action_prints_usage_and_returns_two(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _bench_dispatch(_Args()) == 2
        assert "usage: kirocrew bench" in capsys.readouterr().out

    def test_unknown_action_returns_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _bench_dispatch(_Args(bench_action="zibble")) == 2
        assert "unknown bench action: zibble" in capsys.readouterr().out

    def test_list_prints_the_corpus_table(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.eval.bench.datasets.describe", lambda: "zibble-table")
        assert _bench_dispatch(_Args(bench_action="list")) == 0
        assert "zibble-table" in capsys.readouterr().out

    def test_fetch_reports_the_cached_path(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        target = tmp_path / "zibble.json"
        monkeypatch.setattr("kiro_crew.eval.bench.datasets.ensure", lambda key: target)
        assert _bench_dispatch(_Args(bench_action="fetch", corpus="locomo10")) == 0
        assert f"ready: {target}" in capsys.readouterr().out

    def test_fetch_reports_a_download_failure_without_a_traceback(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.eval.bench import datasets

        def _boom(key: str) -> None:
            raise datasets.CorpusFetchError("zibble checksum mismatch")

        monkeypatch.setattr(datasets, "ensure", _boom)
        assert _bench_dispatch(_Args(bench_action="fetch", corpus="locomo10")) == 1
        assert "error: zibble checksum mismatch" in capsys.readouterr().out


class TestLoadCorpus:
    """``_load_corpus`` picks the adapter from the spec, and refuses an unknown key."""

    def test_unknown_key_lists_the_known_ones(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _load_corpus("zibble")
        message = str(excinfo.value)
        assert "unknown corpus 'zibble'" in message
        assert "locomo10" in message

    def test_a_locomo_spec_goes_through_the_locomo_adapter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from kiro_crew.eval.bench import adapters, datasets

        spec = _spec(dataset="locomo", variant="")
        path = tmp_path / "zibble.json"
        monkeypatch.setitem(datasets.SPECS, "zibblecorpus", spec)
        monkeypatch.setattr(datasets, "ensure", lambda s: path)
        monkeypatch.setattr(datasets, "load_json", lambda s: {"raw": 1})
        seen: dict[str, Any] = {}

        def _load_locomo(raw: Any, source_path: str) -> str:
            seen.update(raw=raw, source_path=source_path)
            return "locomo-corpus"

        monkeypatch.setattr(adapters, "load_locomo", _load_locomo)
        assert _load_corpus("zibblecorpus") == "locomo-corpus"
        assert seen == {"raw": {"raw": 1}, "source_path": str(path)}

    def test_any_other_spec_goes_through_the_longmemeval_adapter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from kiro_crew.eval.bench import adapters, datasets

        spec = _spec(dataset="longmemeval", variant="s")
        monkeypatch.setitem(datasets.SPECS, "zibblecorpus", spec)
        monkeypatch.setattr(datasets, "ensure", lambda s: tmp_path / "z.json")
        monkeypatch.setattr(datasets, "load_json", lambda s: [])
        seen: dict[str, Any] = {}

        def _load_lme(raw: Any, variant: str, source_path: str) -> str:
            seen.update(variant=variant)
            return "lme-corpus"

        monkeypatch.setattr(adapters, "load_longmemeval", _load_lme)
        assert _load_corpus("zibblecorpus") == "lme-corpus"
        assert seen == {"variant": "s"}


class TestBenchCmdCatchSites:
    def test_an_oserror_is_reported_rather_than_tracebacked(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-only out-dir is a failed run, not a crashed program."""

        def _boom(args: object) -> int:
            raise OSError("zibble read-only file system")

        monkeypatch.setattr("kiro_crew.cli_bench._bench_dispatch", _boom)
        assert bench_cmd(_Args(bench_action="retrieval")) == 1
        assert "error: zibble read-only file system" in capsys.readouterr().out

    def test_a_clean_dispatch_code_passes_straight_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.cli_bench._bench_dispatch", lambda args: 0)
        assert bench_cmd(_Args(bench_action="list")) == 0
