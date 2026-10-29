"""The auto-improvement ``CrPipeline``: verify -> REPRODUCE -> draft -> ledger.

A kept winner becomes exactly one draft pull request, or one terminal ledger row
saying why not. These pin each outcome:

* the dedup guard: a terminal row short-circuits to ``duplicate``, an in-flight
  ``seen`` row does not;
* perf: a reproduce error, the repository-retired exits, a fluke that does not
  reproduce, and a reproduced win that carries the REPRODUCE numbers back;
* ``_reproduces``: same direction, beating the band, and a ``0.0`` band kept as is;
* the shared draft step: direct-commit mode, the ``parent_ref`` signature probe, a
  draft exception, a ``QUEUED:`` reference, and a filed PR whose ledger write fails.

A real :class:`Ledger` writes JSONL under ``tmp_path``. The measurer and PR recipe are
fakes, and the description builders are stubbed (``test_pr_description.py`` covers
them), so nothing is built, measured or filed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import ledger as L
from kiro_crew.apps.builtins.auto_improvement.spine import pr_pipeline as pp
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    BugGateResult,
    Candidate,
    Measurement,
    Proposal,
)
from kiro_crew.apps.builtins.auto_improvement.spine.pr_pipeline import CrPipeline

TARGET = "src/pkg/mod.py::fn"


@pytest.fixture(autouse=True)
def _stub_descriptions(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed titles and bodies, so a test reads exactly what reached the recipe."""
    _floor_monkeypatch.setattr(pp, "perf_pr_description", lambda **kw: "# Perf title\nperf body")
    _floor_monkeypatch.setattr(pp, "perf_cr_title", lambda *a, **kw: "perf summary")
    _floor_monkeypatch.setattr(pp, "bug_pr_description", lambda **kw: "bug body")
    _floor_monkeypatch.setattr(pp, "bug_cr_title", lambda cand: "bug summary")


class _Recipe:
    """A PR recipe that records each draft and returns a scripted reference."""

    def __init__(self, *, cr: str = "https://example.test/pr/1", fail: bool = False) -> None:
        self.cr, self.fail = cr, fail
        self.calls: list[dict[str, Any]] = []
        self.pr_queue_dir: Path | None = None

    def draft(self, *, summary, description, diff, fingerprint, parent_ref=None) -> str:
        self.calls.append(
            {
                "summary": summary,
                "description": description,
                "diff": diff,
                "fingerprint": fingerprint,
                "parent_ref": parent_ref,
            }
        )
        if self.fail:
            raise RuntimeError("gh exploded")
        return self.cr


class _OldRecipe(_Recipe):
    """A recipe that predates ``parent_ref``."""

    def draft(self, *, summary, description, diff, fingerprint) -> str:  # type: ignore[override]
        return super().draft(
            summary=summary, description=description, diff=diff, fingerprint=fingerprint
        )


class _Measurer:
    def __init__(self, result: Measurement | Exception) -> None:
        self.result = result
        self.calls: list[str] = []

    def reproduce(self, *, profile, proposal, gated_commit_sha) -> Measurement:
        self.calls.append(gated_commit_sha)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _profile(recipe: _Recipe) -> Any:
    return SimpleNamespace(pr_recipe=recipe, ruler=SimpleNamespace(primary_name="ms", unit="ms"))


def _winner(kind: str = "perf", tmp_path: Path | None = None) -> Proposal:
    return Proposal(
        cand_id="c1",
        candidate=Candidate(kind=kind, target=TARGET),
        worktree=tmp_path or Path("."),
        branch="cand/c1",
        description="d",
        diff="--- a\n+++ b\n",
    )


def _m(delta: float | None, band: float | None = 1.0, ok: bool = True) -> Measurement:
    return Measurement(ok=ok, primary_delta=delta, noise_band=band)


def _rows(ledger: L.Ledger) -> list[dict]:
    if not ledger.path.exists():
        return []
    return [json.loads(line) for line in ledger.path.read_text().splitlines() if line.strip()]


@pytest.fixture()
def ledger(tmp_path: Path) -> L.Ledger:
    return L.Ledger(tmp_path / "ledger" / "ledger.jsonl")


def _pipeline(ledger: L.Ledger, measurer: _Measurer | None = None, **kw: Any) -> CrPipeline:
    return CrPipeline(ledger=ledger, measurer=measurer or _Measurer(_m(-5.0)), **kw)


def _emit_perf(p: CrPipeline, recipe: _Recipe, verify: Measurement | None = None, **kw: Any):
    return p.emit_perf(
        profile=_profile(recipe),
        winner=_winner(),
        verify=verify or _m(-4.0),
        cycle=3,
        gated_commit_sha="abc123",
        diff_ref="d1",
        base_anchor="base",
        **kw,
    )


def _emit_bug(p: CrPipeline, recipe: _Recipe, **kw: Any):
    return p.emit_bug(
        profile=_profile(recipe),
        winner=_winner("bug"),
        bug_res=BugGateResult(passed=True, reason="bug_filed"),
        cycle=3,
        diff_ref="d1",
        base_anchor="base",
        **kw,
    )


FP_PERF = L.fingerprint(kind="perf", target=TARGET)
FP_BUG = L.fingerprint(kind="bug", target=TARGET)


class TestDedupGuard:
    @pytest.mark.parametrize("prior", [L.STATUS_FILED, L.STATUS_FAILED_VERIFY, L.STATUS_ERROR])
    def test_a_terminal_row_short_circuits_to_duplicate(self, ledger, prior) -> None:
        ledger.record(L.LedgerEntry(fp=FP_PERF, kind="perf", target=TARGET, status=prior))
        measurer, recipe = _Measurer(_m(-5.0)), _Recipe()
        out = _emit_perf(_pipeline(ledger, measurer), recipe)
        assert (out.status, out.note, out.filed) == (L.STATUS_DUPLICATE, f"already {prior}", False)
        assert (measurer.calls, recipe.calls) == ([], [])  # no reproduce, no draft
        assert _rows(ledger)[-1]["status"] == L.STATUS_DUPLICATE

    def test_a_filed_bug_fix_is_never_re_filed(self, ledger) -> None:
        recipe = _Recipe()
        assert _emit_bug(_pipeline(ledger), recipe).status == L.STATUS_FILED
        # Same locus again: also after a restart, since the ledger reloads its JSONL.
        again = _emit_bug(_pipeline(L.Ledger(ledger.path)), recipe)
        assert again.status == L.STATUS_DUPLICATE
        assert len(recipe.calls) == 1

    def test_an_in_flight_seen_row_is_not_a_duplicate(self, ledger) -> None:
        ledger.record(L.LedgerEntry(fp=FP_BUG, kind="bug", target=TARGET, status=L.STATUS_SEEN))
        recipe = _Recipe()
        out = _emit_bug(_pipeline(ledger), recipe)
        assert out.status == L.STATUS_FILED
        assert len(recipe.calls) == 1


class TestPerf:
    def test_a_reproduced_win_files_one_draft_and_carries_the_reproduce_back(self, ledger) -> None:
        reproduce = _m(-5.0)
        measurer, recipe = _Measurer(reproduce), _Recipe()
        out = _emit_perf(_pipeline(ledger, measurer), recipe, parent_ref="parent-1")
        assert measurer.calls == ["abc123"]
        assert (out.status, out.filed, out.cr) == (L.STATUS_FILED, True, recipe.cr)
        assert out.reproduce is reproduce  # the second A/B, not a re-use of VERIFY
        assert recipe.calls == [
            {
                "summary": "perf summary",
                "description": "# Perf title\nperf body",
                "diff": "--- a\n+++ b\n",
                "fingerprint": FP_PERF,
                "parent_ref": "parent-1",
            }
        ]
        rows = _rows(ledger)
        assert [(r["status"], r["cr"]) for r in rows] == [(L.STATUS_FILED, recipe.cr)]
        assert ledger.filed_crs() == [recipe.cr]

    def test_a_win_that_does_not_reproduce_is_a_fluke(self, ledger) -> None:
        recipe = _Recipe()
        out = _emit_perf(_pipeline(ledger, _Measurer(_m(+3.0))), recipe)
        assert (out.status, out.note, out.filed) == (
            L.STATUS_FAILED_VERIFY,
            "not reproduced",
            False,
        )
        assert recipe.calls == []
        assert _rows(ledger)[-1]["note"] == "first-run fluke: v1Δ=-4.0 v2Δ=3.0"

    def test_a_reproduce_error_is_recorded_not_raised(self, ledger) -> None:
        recipe = _Recipe()
        out = _emit_perf(_pipeline(ledger, _Measurer(RuntimeError("harness died"))), recipe)
        assert (out.status, out.note) == (L.STATUS_ERROR, "reproduce error")
        assert recipe.calls == []
        assert _rows(ledger)[-1]["note"] == "reproduce error: harness died"

    @pytest.mark.parametrize(
        "result", [_m(-5.0), RuntimeError("harness died")], ids=["after-success", "after-error"]
    )
    def test_a_retired_repository_stops_without_a_row_or_a_draft(self, ledger, result) -> None:
        stages: list[str] = []

        def _retire(stage: str) -> bool:
            stages.append(stage)
            return True

        recipe = _Recipe()
        out = _emit_perf(_pipeline(ledger, _Measurer(result), retire_if_unsafe=_retire), recipe)
        assert (out.status, out.repository_retired) == (L.STATUS_ERROR, True)
        assert stages == ["reproduce"]
        assert (_rows(ledger), recipe.calls) == ([], [])


class TestReproduces:
    @pytest.mark.parametrize(
        ("verify", "reproduce", "expected"),
        [
            (_m(-4.0), _m(-1.5, band=1.0), True),
            (_m(+4.0), _m(+1.5, band=1.0), True),  # same direction either way
            (_m(-4.0), _m(+1.5, band=1.0), False),  # flipped sign
            (_m(-4.0), _m(-1.0, band=1.0), False),  # inside the band
            (_m(-4.0), _m(-1.5, ok=False), False),  # reproduce did not run cleanly
            (_m(-4.0), _m(None), False),
            (_m(None), _m(-1.5), False),
            # A 0.0 band is a real calibration: it must not fall back to verify's band.
            (_m(-4.0, band=5.0), _m(-0.1, band=0.0), True),
            (_m(-4.0, band=5.0), _m(-0.1, band=None), False),  # falls back to verify's 5.0
            (_m(-4.0, band=None), _m(-0.1, band=None), True),  # no band at all: 0.0
        ],
    )
    def test_reproduce_must_match_direction_and_beat_the_band(
        self, verify, reproduce, expected
    ) -> None:
        assert CrPipeline._reproduces(verify, reproduce) is expected


class TestDraftStep:
    def test_a_bug_fix_files_one_draft(self, ledger) -> None:
        recipe = _Recipe()
        out = _emit_bug(_pipeline(ledger), recipe)
        assert (out.status, out.filed, out.reproduce) == (L.STATUS_FILED, True, None)
        assert recipe.calls[0]["summary"] == "bug summary"
        assert recipe.calls[0]["parent_ref"] is None  # an empty parent_ref is sent as None
        assert _rows(ledger)[-1]["note"] == "bug RED→GREEN→STAYGREEN (bug_filed)"

    def test_a_recipe_without_parent_ref_is_called_on_its_own_arity(self, ledger) -> None:
        recipe = _OldRecipe()
        out = _emit_bug(_pipeline(ledger), recipe, parent_ref="parent-1")
        assert out.status == L.STATUS_FILED
        assert len(recipe.calls) == 1  # once, with no retry on a different arity

    def test_a_kwargs_recipe_receives_parent_ref(self) -> None:
        def _draft(**kwargs: Any) -> str:
            return ""

        assert CrPipeline._draft_accepts_parent_ref(_draft) is True
        assert CrPipeline._draft_accepts_parent_ref(len) is False  # not introspectable

    def test_a_draft_exception_is_recorded_as_error_and_drafted_once(self, ledger) -> None:
        recipe = _Recipe(fail=True)
        out = _emit_bug(_pipeline(ledger), recipe)
        assert (out.status, out.filed, out.note) == (
            L.STATUS_ERROR,
            False,
            "cr draft error: gh exploded",
        )
        assert len(recipe.calls) == 1
        assert _rows(ledger)[-1]["status"] == L.STATUS_ERROR

    def test_a_queued_reference_is_retryable_but_still_a_realized_win(self, ledger) -> None:
        recipe = _Recipe(cr=f"QUEUED:{FP_BUG}")
        out = _emit_bug(_pipeline(ledger), recipe)
        assert (out.status, out.cr, out.filed) == (L.STATUS_ERROR, f"QUEUED:{FP_BUG}", True)
        row = _rows(ledger)[-1]
        assert (row["status"], row["cr"]) == (L.STATUS_ERROR, f"QUEUED:{FP_BUG}")
        assert ledger.filed_crs() == []  # never handed to the PR watchers as a PR

    def test_a_filed_pr_survives_a_failed_ledger_write(self, ledger, monkeypatch, caplog) -> None:
        recipe = _Recipe()
        p = _pipeline(ledger)

        def _full_disk(entry: L.LedgerEntry) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(ledger, "record", _full_disk)
        caplog.set_level(logging.ERROR, logger=p.log.name)
        out = _emit_bug(p, recipe)
        assert (out.status, out.cr, out.filed) == (L.STATUS_FILED, recipe.cr, True)
        assert any("could NOT be written" in r.getMessage() for r in caplog.records)


class TestDirectCommit:
    def test_a_verified_win_is_handed_back_with_a_queue_copy_and_no_row(
        self, ledger, tmp_path: Path
    ) -> None:
        recipe = _Recipe()
        recipe.pr_queue_dir = tmp_path / "pr_queue"
        reproduce = _m(-5.0)
        out = _emit_perf(_pipeline(ledger, _Measurer(reproduce), direct_commit=True), recipe)
        assert (out.status, out.committed_ready, out.filed) == (L.STATUS_SEEN, True, False)
        assert out.reproduce is reproduce
        assert (recipe.calls, _rows(ledger)) == ([], [])  # the driver records `committed`
        assert (recipe.pr_queue_dir / f"{FP_PERF}.diff").read_text() == "--- a\n+++ b\n"
        # One title: the description's own leading H1 is dropped.
        assert (recipe.pr_queue_dir / f"{FP_PERF}.pr.md").read_text() == (
            "# perf summary\n\nperf body\n"
        )

    def test_no_queue_dir_writes_nothing(self, ledger, tmp_path: Path) -> None:
        recipe = _Recipe()
        out = _emit_bug(_pipeline(ledger, direct_commit=True), recipe)
        assert out.committed_ready is True
        assert not any(tmp_path.rglob("*.pr.md"))

    def test_a_failed_queue_copy_never_blocks_the_commit(self, ledger, tmp_path: Path) -> None:
        recipe = _Recipe()
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        recipe.pr_queue_dir = blocker  # mkdir under a file fails
        out = _emit_bug(_pipeline(ledger, direct_commit=True), recipe)
        assert out.committed_ready is True
