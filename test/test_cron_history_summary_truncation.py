"""Cron-history summary truncation keeps the load-bearing facts.

A cron run's summary is the only short, indexed cross-run record — the trace is
capped separately at 50 KB and is not indexed — so it is what a later session
searches for what a run produced. A cut that drops the run's pull-request URL
drops the whole point of the record: the next session finds nothing and derives
the same pull request again.

Two sites are covered:

* ``truncate_summary`` — the pure function deciding what survives a cut.
* ``CronHistoryStore.append`` and ``CronService``'s run path — that the
  configured cap is applied once, in ``append``, with no caller pre-slicing.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cron_history import (
    _SUMMARY_CAP,
    CronHistoryStore,
    CronRunRecord,
    truncate_summary,
)

_PR_URL = "https://github.com/kirodotdev/KiroCrew/pull/2836"
_ISSUE_URL = "https://github.com/kirodotdev/KiroCrew/issues/2836"


# ── truncate_summary: what survives ──────────────────────────────────────


class TestTruncateSummaryKeepsFacts:
    def test_url_at_the_tail_survives(self) -> None:
        """The common shape: the URL sits past the cap, at the end."""
        text = "Swept the backlog. " + "detail " * 60 + f"\nOpened {_PR_URL}"
        assert len(text) > 200

        out = truncate_summary(text, 200)

        assert len(out) <= 200
        assert _PR_URL in out
        assert "..." in out

    def test_url_at_the_head_survives(self) -> None:
        """A URL in the dropped head is re-stated, not lost."""
        text = f"{_PR_URL} was reopened. " + "noise " * 80 + "\nrun failed"

        out = truncate_summary(text, 120)

        assert len(out) <= 120
        assert _PR_URL in out
        assert out.endswith("run failed")

    def test_both_urls_survive(self) -> None:
        text = f"Filed {_ISSUE_URL} then " + "work " * 80 + f"\nopened {_PR_URL}"

        out = truncate_summary(text, 220)

        assert len(out) <= 220
        assert _ISSUE_URL in out
        assert _PR_URL in out

    def test_no_url_keeps_the_final_outcome_line(self) -> None:
        text = "step one\n" + "middle noise\n" * 60 + "exit code 0, 3 files changed"

        out = truncate_summary(text, 120)

        assert len(out) <= 120
        assert out.endswith("exit code 0, 3 files changed")
        assert out.startswith("step one")
        assert "..." in out

    def test_urls_are_never_split(self) -> None:
        """Every ``http`` in the result is a whole URL, at several caps."""
        text = f"a {_PR_URL} b " + "x" * 400 + f"\ndone {_ISSUE_URL}"

        for cap in range(40, 401, 7):
            out = truncate_summary(text, cap)
            for fragment in out.split():
                if fragment.startswith("http"):
                    assert fragment in (_PR_URL, _ISSUE_URL), (cap, fragment)

    def test_unicode_is_counted_in_characters(self) -> None:
        text = "汇总：" + "任务完成情况说明。" * 40 + f"\n已开 {_PR_URL}"

        out = truncate_summary(text, 150)

        assert len(out) <= 150
        assert _PR_URL in out


# ── truncate_summary: inputs it must leave alone ─────────────────────────


class TestTruncateSummaryLeavesShortInputAlone:
    def test_empty_input_unchanged(self) -> None:
        assert truncate_summary("", 200) == ""

    def test_short_input_unchanged(self) -> None:
        assert truncate_summary("opened a PR", 200) == "opened a PR"

    def test_exactly_at_cap_unchanged(self) -> None:
        text = "y" * 200
        assert truncate_summary(text, 200) == text

    def test_one_over_cap_is_cut(self) -> None:
        text = "y" * 201
        out = truncate_summary(text, 200)
        assert out != text
        assert len(out) <= 200

    def test_idempotent(self) -> None:
        text = f"head {_PR_URL} " + "filler " * 90 + f"\ntail {_ISSUE_URL}"
        once = truncate_summary(text, 200)
        assert truncate_summary(once, 200) == once

    def test_non_positive_cap_yields_empty(self) -> None:
        assert truncate_summary("anything", 0) == ""
        assert truncate_summary("anything", -5) == ""


# ── edges of the cut ─────────────────────────────────────────────────────


class TestTruncateSummaryEdges:
    def test_a_repeated_url_is_listed_once(self) -> None:
        text = f"{_PR_URL} " + "noise " * 60 + f"\nsee {_PR_URL}"

        out = truncate_summary(text, 120)

        assert out.count(_PR_URL) == 1

    def test_the_same_link_twice_in_the_outcome_line_is_counted_once(self) -> None:
        """The fragment's own URL set dedupes, so no line is restated for it."""
        text = "start " + "noise " * 40 + f"\nsee {_PR_URL} and again {_PR_URL}"

        out = truncate_summary(text, 240)

        assert len(out) <= 240
        assert out.count(_PR_URL) == 2  # both are inside the kept fragment
        assert not any(line == _PR_URL for line in out.splitlines())

    def test_a_clip_that_lands_before_a_links_position_keeps_it_whole(self) -> None:
        """The clip only moves for a URL it would cut, not for every URL."""
        line = "finished the sweep with 2 shards red, see " + _PR_URL
        text = "start " + "noise " * 40 + f"\n{line}"

        out = truncate_summary(text, 140)

        assert len(out) <= 140
        assert _PR_URL in out
        assert out.endswith(_PR_URL)

    def test_a_long_outcome_line_without_a_url_is_clipped_from_the_left(self) -> None:
        line = "the sweep finished after 6 reruns and left 2 shards red for the next cycle"
        text = "start " + "noise " * 40 + f"\n{line}"
        assert len(line) > 60

        out = truncate_summary(text, 120)

        assert len(out) <= 120
        assert out.endswith(line[-60:])

    def test_blank_only_text_still_obeys_the_cap(self) -> None:
        """No non-blank line to keep: the marker alone stands for the cut."""
        out = truncate_summary("\n" * 300, 40)

        assert len(out) <= 40
        assert "..." in out

    def test_trailing_blank_lines_are_skipped_for_the_outcome(self) -> None:
        text = "start\n" + "noise\n" * 60 + "exit code 2\n\n   \n"

        out = truncate_summary(text, 100)

        assert len(out) <= 100
        assert out.endswith("exit code 2")

    def test_cap_just_over_the_marker_holds(self) -> None:
        """A budget too small for any fragment still respects the cap."""
        for cap in (4, 5, 6, 7):
            out = truncate_summary("x" * 50 + f" {_PR_URL}", cap)
            assert len(out) <= cap, (cap, out)

    def test_cap_at_or_below_the_marker_holds(self) -> None:
        for cap in (1, 2, 3):
            out = truncate_summary("x" * 50, cap)
            assert len(out) == cap

    def test_an_uppercase_scheme_is_still_a_url(self) -> None:
        """A scheme is case-insensitive, so a shouted link is not prose."""
        shouted = "HTTPS://GITHUB.COM/kirodotdev/KiroCrew/pull/12001"
        text = "start " + "noise " * 40 + f"see {shouted} " + "more " * 40 + "\ndone ok"

        out = truncate_summary(text, 120)

        restated = [line for line in out.splitlines() if line.lower().startswith("http")]
        assert restated == [shouted]

    def test_a_link_whose_address_prefixes_another_is_kept_separately(self) -> None:
        """`/pull/28` is not `/pull/2836`, so neither may absorb the other."""
        short = "https://github.com/kirodotdev/KiroCrew/pull/28"
        long = "https://github.com/kirodotdev/KiroCrew/pull/2836"
        text = f"reopened {short} while sweeping " + "noise " * 40 + f"\nlanded {long}"

        out = truncate_summary(text, 200)

        assert short in out
        assert long in out
        # The short one is restated on its own line, not merely found inside the
        # long one's text.
        assert any(line == short for line in out.splitlines())

    def test_punctuation_inside_an_address_is_never_trimmed(self) -> None:
        """Only a trailing run is prose; interior punctuation belongs to the URL."""
        inner = "https://host.example/a.b,c:d/e!f?q=1&r=2"
        text = "start " + "noise " * 40 + f"at {inner} " + "more " * 40 + "\ndone ok"

        out = truncate_summary(text, 140)

        restated = [line for line in out.splitlines() if line.startswith("http")]
        assert restated == [inner]

    def test_a_url_no_longer_than_its_scheme_is_left_alone(self) -> None:
        """The trim stops at the scheme, so a minimal URL survives intact."""
        text = "start " + "noise " * 40 + "at http://a. " + "more " * 40 + "\ndone ok"

        out = truncate_summary(text, 120)

        restated = [line for line in out.splitlines() if line.startswith("http")]
        assert restated == ["http://a"]

    def test_an_address_may_end_in_an_exclamation_mark(self) -> None:
        """`.../wiki/Yahoo!` is a real address: trimming the `!` kills the link."""
        real = "https://en.wikipedia.org/wiki/Yahoo!"
        text = "start " + "noise " * 40 + f"see {real} " + "more " * 40 + "\ndone ok"

        out = truncate_summary(text, 120)

        restated = [line for line in out.splitlines() if line.startswith("http")]
        assert restated == [real]

    def test_an_address_may_end_in_a_question_mark_or_colon(self) -> None:
        for real in (
            "https://host.example/search?",
            "https://host.example/a:b",
            "https://host.example/a;b",
        ):
            text = "start " + "noise " * 40 + f"see {real} " + "more " * 40 + "\ndone ok"

            out = truncate_summary(text, 120)

            restated = [line for line in out.splitlines() if line.startswith("http")]
            assert restated == [real], real

    def test_a_trailing_full_stop_or_comma_is_still_prose(self) -> None:
        url = "https://github.com/kirodotdev/KiroCrew/pull/12001"
        for suffix in (".", ","):
            text = "start " + "noise " * 40 + f"see {url}{suffix} " + "more " * 40 + "\nok"

            out = truncate_summary(text, 120)

            restated = [line for line in out.splitlines() if line.startswith("http")]
            assert restated == [url], suffix

    def test_a_balanced_bracket_stays_part_of_the_url(self) -> None:
        """Trimming a closing bracket the URL itself opened stores a dead link."""
        wiki = "https://en.wikipedia.org/wiki/Cron_(software)"
        text = "start " + "noise " * 40 + f"see {wiki} " + "more " * 40 + "\ndone ok"

        out = truncate_summary(text, 120)

        restated = [line for line in out.splitlines() if line.startswith("http")]
        assert restated == [wiki]

    def test_a_url_wrapped_in_prose_parentheses_loses_them(self) -> None:
        url = "https://github.com/kirodotdev/KiroCrew/pull/12001"
        text = "start " + "noise " * 40 + f"(see {url}) " + "more " * 40 + "\ndone ok"

        out = truncate_summary(text, 120)

        restated = [line for line in out.splitlines() if line.startswith("http")]
        assert restated == [url]

    def test_a_restated_url_carries_no_sentence_punctuation(self) -> None:
        """A link on its own line is an address, not the end of a sentence."""
        text = "start " + "noise " * 40 + f"see {_PR_URL}. " + "more " * 40 + "\ndone ok"

        out = truncate_summary(text, 120)

        restated = [line for line in out.splitlines() if line.startswith("http")]
        assert restated == [_PR_URL]

    def test_url_longer_than_the_cap_is_dropped_not_split(self) -> None:
        long_url = "https://example.com/" + "s" * 300
        out = truncate_summary(f"opened {long_url}\ndone", 60)

        assert len(out) <= 60
        assert "https://" not in out


# ── the default cap ──────────────────────────────────────────────────────


def test_default_summary_cap_is_200() -> None:
    """The default is left where it was: this change is about the CUT."""
    assert _SUMMARY_CAP == 200


#: A summary of the shape cron agent jobs actually produce: narration, the links
#: the run created, and a closing outcome line.
_SWEEP_SUMMARY = (
    "Nightly flake sweep: reran 6 failing shards and opened one PR per shard. "
    "https://github.com/kirodotdev/KiroCrew/pull/12001 "
    "https://github.com/kirodotdev/KiroCrew/pull/12002 "
    "https://github.com/kirodotdev/KiroCrew/pull/12003 "
    "https://github.com/kirodotdev/KiroCrew/pull/12004\n"
    "4 PRs opened, 2 shards still red"
)


class TestWhatTheCapDecides:
    """The cap decides ROOM: links are paid for before prose, newest first.

    So one link shorter than the cap always survives, and a run that produced
    several keeps as many as the budget holds. Which is why the default is left
    alone: at 200 this summary still keeps its verdict and its freshest links.
    What a wider cap buys is the older links and the NARRATION, and that is the
    operator's call through `cron_summary_cap`.
    """

    def test_the_newest_links_and_the_outcome_survive_at_the_default(self) -> None:
        """At 200 this summary spends its whole budget on links plus the verdict."""
        assert len(_SWEEP_SUMMARY) > _SUMMARY_CAP
        at_default = truncate_summary(_SWEEP_SUMMARY, _SUMMARY_CAP)

        # Three of the four links fit; the OLDEST is the one dropped, because the
        # link a run produced last is the one a later run needs.
        assert at_default.count("https://") == 3
        assert "pull/12001" not in at_default
        assert "pull/12004" in at_default
        assert at_default.endswith("4 PRs opened, 2 shards still red")

    def test_the_narration_is_what_the_default_cannot_hold(self) -> None:
        at_default = truncate_summary(_SWEEP_SUMMARY, _SUMMARY_CAP)

        assert "reran 6 failing shards" in _SWEEP_SUMMARY
        assert "reran 6 failing shards" not in at_default

    def test_raising_the_knob_stores_the_whole_summary(self) -> None:
        assert len(_SWEEP_SUMMARY) <= 500
        assert truncate_summary(_SWEEP_SUMMARY, 500) == _SWEEP_SUMMARY


def test_the_module_spec_states_the_current_default() -> None:
    """The spec paragraph and the code agree on the number and on the reason."""
    spec = (
        pathlib.Path(__file__).resolve().parents[1]
        / "docs"
        / "system-specs"
        / "modules"
        / "learn-cron-dashboard.md"
    ).read_text(encoding="utf-8")

    assert f"The default of {_SUMMARY_CAP} therefore holds" in spec
    assert "URLs are reserved BEFORE the outcome fragment" in spec
    # The claim the paragraph must NOT make: that a cap decides whether a link
    # survives at all, or that every link of a many-link run is kept.
    assert "too small to hold a pull-request URL" not in spec
    assert "kept whatever the cap is" not in spec


def test_config_default_matches_module_default() -> None:
    from kiro_crew.config.sections import CronHistoryConfig

    assert CronHistoryConfig().cron_summary_cap == _SUMMARY_CAP


def test_loader_default_matches_module_default() -> None:
    from kiro_crew.config.loader import _build_cron_history_config

    assert _build_cron_history_config({}).cron_summary_cap == _SUMMARY_CAP


# ── append() is the single truncation site ───────────────────────────────


def _record(**kw) -> CronRunRecord:
    now = time.time()
    return CronRunRecord(
        run_id=kw.get("run_id", "run1"),
        job_id=kw.get("job_id", "job1"),
        started_at=now,
        finished_at=now + 1,
        duration_ms=1000,
        status=kw.get("status", "success"),
        summary=kw.get("summary", "ok"),
        trace=kw.get("trace", ""),
        error=kw.get("error", ""),
    )


def _rows(tmp_path: Path, job_id: str) -> list[dict]:
    job_file = tmp_path / "cron-history" / f"{job_id}.jsonl"
    assert job_file.exists(), "history record was not written"
    return [
        json.loads(line)
        for line in job_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestAppendAppliesTheConfiguredCap:
    @pytest.mark.asyncio
    async def test_append_keeps_the_url_under_the_configured_cap(self, tmp_path: Path) -> None:
        store = CronHistoryStore(base_dir=tmp_path, cron_summary_cap=150)
        long_summary = "Swept the backlog. " + "detail " * 60 + f"\nOpened {_PR_URL}"
        await store.append(_record(summary=long_summary))

        (row,) = _rows(tmp_path, "job1")
        assert len(row["summary"]) <= 150
        assert _PR_URL in row["summary"]

    @pytest.mark.asyncio
    async def test_append_uses_truncate_summary(self, tmp_path: Path) -> None:
        """The cap is applied through the shared function, not a raw slice."""
        store = CronHistoryStore(base_dir=tmp_path, cron_summary_cap=150)
        long_summary = "Swept the backlog. " + "detail " * 60 + f"\nOpened {_PR_URL}"
        await store.append(_record(summary=long_summary))

        (row,) = _rows(tmp_path, "job1")
        assert row["summary"] == truncate_summary(long_summary, 150)

    @pytest.mark.asyncio
    async def test_reconfigured_cap_is_what_append_applies(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        store = CronHistoryStore(base_dir=tmp_path, cron_summary_cap=500)
        cfg = KiroCrewConfig()
        cfg.cron_history.cron_summary_cap = 120
        store.reconfigure(cfg)

        long_summary = "Swept the backlog. " + "detail " * 60 + f"\nOpened {_PR_URL}"
        await store.append(_record(summary=long_summary))

        (row,) = _rows(tmp_path, "job1")
        assert len(row["summary"]) <= 120
        assert _PR_URL in row["summary"]


# ── how the link budget is spent ─────────────────────────────────────────


class TestTheLinkBudget:
    _A = "https://github.com/kirodotdev/KiroCrew/pull/12001"
    _B = "https://github.com/kirodotdev/KiroCrew/pull/900"

    def test_a_link_that_exactly_fills_the_room_is_kept(self) -> None:
        """A separator is charged only when something precedes the link."""
        # 3 for the marker, 1 for the newline under it: a 49-character link
        # exactly fills a cap of 53.
        assert len(self._A) == 49
        text = f"ran a sweep and opened {self._A} " + "noise " * 30 + "\nok"

        out = truncate_summary(text, 53)

        assert len(out) <= 53
        assert self._A in out

    def test_the_marker_yields_rather_than_evict_the_only_link(self) -> None:
        """A URL that fills the cap alone must reach the record, marker or not."""
        assert len(self._A) == 49
        text = f"opened {self._A} while sweeping " + "noise " * 30 + "\ndone"

        out = truncate_summary(text, 49)

        assert len(out) <= 49
        assert out == self._A

    def test_the_marker_stays_whenever_a_link_fits_beside_it(self) -> None:
        """The cut is only left unmarked when marking it would cost the link."""
        text = f"opened {self._A} while sweeping " + "noise " * 30 + "\ndone"

        out = truncate_summary(text, 60)

        assert self._A in out
        assert "..." in out

    def test_priority_follows_a_links_last_appearance(self) -> None:
        """A link named again later outranks one mentioned only in between."""
        text = (
            f"{self._A} started, then {self._B} in the middle, "
            + "noise " * 20
            + f" and {self._A} again, "
            + "noise " * 10
            + "\nfinished ok"
        )

        out = truncate_summary(text, 60)

        assert len(out) <= 60
        assert self._A in out
        assert self._B not in out


# ── URLs claim room before the outcome fragment ──────────────────────────


class TestUrlsAreReservedFirst:
    """A long outcome line must not spend the room a link needed.

    Sizing the fragment first (up to half the cap) and giving links what was
    left drops a link the summary could have held — and the fragment cannot
    stand in for it, because prose is not an address.
    """

    #: 56 characters: wide enough that half of a 120 cap leaves no room for it.
    _LONG_URL = "https://github.com/kirodotdev/KiroCrew/pull/12001/files"
    #: No URL of its own, and long enough to claim half the cap.
    _LONG_OUTCOME = "finished the sweep with 3 shards red and 2 reruns still queued"

    def test_a_link_survives_a_long_outcome_line(self) -> None:
        assert len(self._LONG_URL) >= 54
        assert len(self._LONG_OUTCOME) >= 60
        text = f"ran a sweep, see {self._LONG_URL} " + "noise " * 40 + f"\n{self._LONG_OUTCOME}"

        out = truncate_summary(text, 120)

        assert len(out) <= 120
        assert self._LONG_URL in out

    def test_the_outcome_line_still_gets_what_is_left(self) -> None:
        text = f"ran a sweep, see {self._LONG_URL} " + "noise " * 40 + f"\n{self._LONG_OUTCOME}"

        out = truncate_summary(text, 120)

        # The fragment is clipped rather than dropped: its END, where the verdict
        # sits, is what survives.
        assert out.endswith("queued")


# ── the run path hands the summary over uncut ────────────────────────────


def _job(**kw):
    from kiro_crew.cron import CronJob, CronSchedule

    return CronJob(
        id=kw.pop("id", "j1"),
        name=kw.pop("name", "test"),
        message=kw.pop("message", "go"),
        schedule=kw.pop("schedule", CronSchedule(kind="every", every_secs=60)),
        **kw,
    )


class TestRunPathDoesNotPreTruncate:
    def test_a_300_char_result_reaches_the_record_under_a_configured_500(
        self, tmp_path: Path
    ) -> None:
        """The CONFIGURED cap is what applies, so 300 characters land whole.

        This is the property a slice in ``cron.py`` destroys: it would cut at its
        own literal on the way in, so a wider configured cap could never take
        effect and the trailing URL would never reach history.
        """
        from kiro_crew.cron import CronService

        result = "Reviewed the sweep. " + "x" * (300 - 20 - len(_PR_URL) - 8) + f"\nPR: {_PR_URL}"
        assert 280 <= len(result) <= 320

        async def _produce(job, meta=None):
            job.set_run_result(result)
            job.last_status = "ok"
            job.last_error = None

        svc = CronService(base_dir=tmp_path)
        svc._history._summary_cap = 500
        job = _job()
        svc._jobs = [job]
        svc._save()
        with patch.object(svc, "_execute", side_effect=_produce):
            asyncio.run(svc._run_job_isolated(job))

        (row,) = _rows(tmp_path, job.id)
        assert row["summary"] == result
        assert _PR_URL in row["summary"]

    def test_result_over_the_cap_is_cut_by_append_not_by_the_run_path(self, tmp_path: Path) -> None:
        """A result past the cap keeps its URL — the cut is append's."""
        from kiro_crew.cron import CronService

        result = "Swept the backlog. " + "detail " * 120 + f"\nOpened {_PR_URL}"
        assert len(result) > 500

        async def _produce(job, meta=None):
            job.set_run_result(result)
            job.last_status = "ok"
            job.last_error = None

        svc = CronService(base_dir=tmp_path)
        job = _job()
        svc._jobs = [job]
        svc._save()
        with patch.object(svc, "_execute", side_effect=_produce):
            asyncio.run(svc._run_job_isolated(job))

        (row,) = _rows(tmp_path, job.id)
        assert len(row["summary"]) <= _SUMMARY_CAP
        assert _PR_URL in row["summary"]
        # The trace keeps the whole result; only the summary is cut.
        assert row["trace"] == result
