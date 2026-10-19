"""Where a wake judge brief travels outside the tick.

Covers the gateway and ``monitor_update`` hops that carry the brief, the reader's subject
checks against what a monitor stores, transcript age parsing, the arm path, and a localized
word for every outcome. The shared autouse fixture comes from ``autonudge_wake_judge_helpers.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
from typing import Any

import pytest
from autonudge_wake_judge_helpers import _restore_judge_process_globals  # noqa: F401 - fixture

from kiro_crew import autonudge_judge as judge
from kiro_crew.autonudge import AutoNudgeService, infer_monitor
from kiro_crew.irq import Outcome


def _gateway_source() -> str:
    """The Slack gateway facade's text followed by its composed owners' text."""
    from kiro_crew.slack import gateway

    facade = pathlib.Path(gateway.__file__)
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (facade, *sorted((facade.parent / "gateway_runtime").glob("*.py")))
    )


class TestUpdatePathCarriesTheBrief:
    """``monitor_update`` must MOVE the brief, not answer success and drop it.

    Each hop is asserted separately because the defect this replaces was silent: the
    tool validated the field, returned success, and no layer beneath it ever received
    the value. A test against the service alone passed the whole time.
    """

    def test_the_tool_copies_a_validated_brief_into_the_patch(self) -> None:
        from kiro_crew.mcp_tools.control_parts import monitor

        assert 'patch["judge"] = validate_judge_spec(args["judge"])' in inspect.getsource(monitor)

    def test_the_reader_hands_over_the_observation_time(self) -> None:
        """``last_observed_at`` is a sibling field, not a key inside the observation.

        The collector ages its row from ``observed_at`` on the mapping it is given,
        so a reader that copies only the canonical dict makes every pull-request row
        age 0.0 -- which is silent, and is exactly what defeats the drop-oldest-first
        bound the recency sort exists to serve.
        """
        source = _gateway_source()
        assert 'payload["observed_at"] = float(at)' in source
        assert 'getattr(monitor, "last_observed_at", 0.0)' in source

    def test_the_session_read_is_bounded_to_what_the_collector_retains(self) -> None:
        """A wider page advances the cursor across rows that are then discarded.

        ``next_since`` follows the returned window and ``session_evidence`` keeps
        only the last ``MAX_ROWS_PER_TARGET``, so an unbounded read loses the oldest
        rows of any burst larger than that and never reads them again.
        """
        source = _gateway_source()
        assert "limit=_judge.MAX_ROWS_PER_TARGET," in source

    def test_the_reader_checks_the_subject_before_answering(self) -> None:
        """One monitor answers every target, so the subject must be verified."""
        source = _gateway_source()
        assert "_judge.pr_observation_is_about(" in source


class TestTheReaderOnlyAnswersForItsOwnSubject:
    """A brief may name a pull request the loop does not watch.

    The loop holds ONE monitor, so an unchecked reader returns the watched
    subject's state labelled with whatever target it was asked for -- and a judge
    could then rule quiet on a different pull request's facts.
    """

    WATCHED = "https://github.com/kirodotdev/KiroCrew/pull/12787"
    OTHER = "https://github.com/kirodotdev/KiroCrew/pull/12788"

    def _about(self, target: str, **kwargs: Any) -> bool:
        return judge.pr_observation_is_about(
            target,
            monitor_kind=kwargs.pop("monitor_kind", "github_pull_request"),
            monitor_target=kwargs.pop("monitor_target", self.WATCHED),
            **kwargs,
        )

    def test_the_watched_subject_is_accepted(self) -> None:
        assert self._about(self.WATCHED) is True

    def test_a_different_pull_request_is_refused(self) -> None:
        assert self._about(self.OTHER) is False

    def test_the_same_number_on_another_repository_is_refused(self) -> None:
        assert self._about("https://github.com/other/repo/pull/12787") is False

    def test_the_same_slug_on_another_host_is_refused(self) -> None:
        """One slug on two servers is two pull requests, so the host is identity."""
        assert self._about("https://github.example.com/kirodotdev/KiroCrew/pull/12787") is False

    def test_another_spelling_of_the_watched_subject_is_accepted(self) -> None:
        """Owner and monitor are spelled by different writers, so both are inferred."""
        assert self._about("https://github.com/kirodotdev/KiroCrew/pull/12787/files") is True

    def test_an_observation_labelled_with_another_subject_is_refused(self) -> None:
        """The reading's own label disagreeing with its record is not a guess to make."""
        assert (
            self._about(
                self.WATCHED,
                observation={
                    "target": "https://github.com/other/repo/pull/1",
                    "kind": "github_pull_request",
                },
            )
            is False
        )

    def test_an_observation_of_another_kind_is_refused(self) -> None:
        assert self._about(self.WATCHED, observation={"kind": "gitlab_merge_request"}) is False

    def test_a_matching_observation_label_is_accepted(self) -> None:
        assert (
            self._about(
                self.WATCHED,
                observation={"target": self.WATCHED, "kind": "github_pull_request"},
            )
            is True
        )

    def test_a_monitor_with_no_subject_is_refused(self) -> None:
        assert self._about(self.WATCHED, monitor_target="") is False
        assert self._about(self.WATCHED, monitor_kind="") is False

    def test_an_unparseable_request_that_does_not_match_exactly_is_refused(self) -> None:
        assert self._about("not a pull request") is False

    def test_an_empty_object_survives_as_a_clear(self) -> None:
        """``{}`` is falsy, so an ``if args.get(...)`` guard would silently eat it."""
        from kiro_crew.mcp_tools.control_parts import monitor

        source = inspect.getsource(monitor)
        guard = source[: source.index('patch["judge"] = validate_judge_spec')].rsplit("if ", 1)[1]
        assert "is not None" in guard, "a truthiness guard would drop the clear"

    def test_every_hop_between_the_tool_and_the_loop_names_it(self) -> None:
        from kiro_crew import autonudge_authz
        from kiro_crew.autonudge import AutoNudgeService
        from kiro_crew.dashboard import session_directive_apply_monitor

        applier = inspect.getsource(session_directive_apply_monitor)
        assert 'judge=patch.get("judge")' in applier, "this applier names every kwarg"
        assert "judge" in inspect.signature(autonudge_authz.authorize_and_update_nudge).parameters
        assert "judge=judge" in inspect.getsource(autonudge_authz.authorize_and_update_nudge)
        assert "judge" in inspect.signature(AutoNudgeService.update).parameters


class TestTheReaderAnswersForTheShapeAMonitorActuallyStores:
    """The watched side is read from a real ``MonitorState``, not a hand-written string.

    ``infer_monitor`` stores the CANONICAL subject (``owner/name#123``) and the
    inferred kind, not the URL the owner armed with. A check that puts that stored
    subject back through inference gets nothing -- the shorthand carries no host by
    design -- so every judged pull-request watch had its target dropped and answered
    FALLBACK on every tick, which fires and spends the turn the judge exists to save.

    Hand-written monitor fields hid that: they spelled the watched side as the URL
    and the kind as the wire name, so the comparison ran on two values neither
    producer emits.
    """

    MESSAGE = "Watch https://github.com/kirodotdev/KiroCrew/pull/12787 until CI is green."

    def _stored(self) -> tuple[str, str]:
        monitor = infer_monitor(self.MESSAGE, now=1_000.0)
        assert monitor is not None
        return monitor.kind, monitor.target

    def test_the_stored_subject_is_the_canonical_shorthand(self) -> None:
        """The premise: what is stored is not what the owner typed."""
        kind, watched = self._stored()
        assert watched == "kirodotdev/KiroCrew#12787"
        assert kind == "gh-pr"
        assert judge._pr_identity(watched) is None

    def test_the_target_the_message_yields_is_accepted(self) -> None:
        """The whole point: a URL-armed loop's own target reaches its own observation."""
        kind, watched = self._stored()
        targets = judge.parse_targets(None, self.MESSAGE)
        assert targets, "the message names a pull request"
        for target in targets:
            assert (
                judge.pr_observation_is_about(
                    target,
                    monitor_kind=kind,
                    monitor_target=watched,
                    observation={},
                )
                is True
            )

    def test_another_pull_request_is_still_refused_against_a_stored_subject(self) -> None:
        kind, watched = self._stored()
        assert (
            judge.pr_observation_is_about(
                "https://github.com/kirodotdev/KiroCrew/pull/12788",
                monitor_kind=kind,
                monitor_target=watched,
                observation={},
            )
            is False
        )

    def test_the_same_number_on_another_repository_is_still_refused(self) -> None:
        kind, watched = self._stored()
        assert (
            judge.pr_observation_is_about(
                "https://github.com/other/repo/pull/12787",
                monitor_kind=kind,
                monitor_target=watched,
                observation={},
            )
            is False
        )

    def test_a_kind_the_monitor_is_not_bound_to_is_refused(self) -> None:
        _kind, watched = self._stored()
        targets = judge.parse_targets(None, self.MESSAGE)
        assert all(
            judge.pr_observation_is_about(
                target,
                monitor_kind="gitlab-mr",
                monitor_target=watched,
                observation={},
            )
            is False
            for target in targets
        )


class TestAgeParsing:
    """Ages decide what gets DROPPED at the cap, so the real row shape must parse.

    Transcript rows carry an ISO 8601 string (verified against a live history file),
    while a probe observation carries an epoch float. Reading only the float made
    every session row age 0.0 -- and with all ages equal, which item lost its place at
    the cap was arbitrary, so a newer actionable row could be discarded before an
    older one.
    """

    def test_an_iso_string_is_a_real_age(self) -> None:
        from datetime import datetime, timedelta, timezone

        clock = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
        row = (clock - timedelta(seconds=90)).isoformat()
        assert judge._age_from_ts(row, clock.timestamp()) == pytest.approx(90.0, abs=0.01)

    def test_a_trailing_z_and_a_naive_stamp_both_read_as_utc(self) -> None:
        clock = 1_790_000_000.0
        zulu = judge._age_from_ts("2026-09-22T07:25:47+00:00", clock)
        assert judge._age_from_ts("2026-09-22T07:25:47Z", clock) == zulu
        assert judge._age_from_ts("2026-09-22T07:25:47", clock) == zulu

    def test_an_epoch_float_still_parses(self) -> None:
        assert judge._age_from_ts(1_000.0, 1_060.0) == pytest.approx(60.0)

    @pytest.mark.parametrize(
        "value", [None, True, False, "", "   ", "not a time", "2026-13-45T99:99:99", [], {}]
    )
    def test_an_unreadable_clock_keeps_the_item_rather_than_hiding_it(self, value: object) -> None:
        assert judge._age_from_ts(value, 1_000.0) == 0.0

    def test_a_future_stamp_is_zero_not_negative(self) -> None:
        assert judge._age_from_ts(2_000.0, 1_000.0) == 0.0


class TestArmPath:
    """A brief survives arming, revision, clearing and a store reload."""

    def test_round_trip(self, tmp_path: pathlib.Path) -> None:
        async def main() -> None:
            base = tmp_path / "nudges"
            base.mkdir()
            svc = AutoNudgeService(base_dir=base)
            brief = {"targets": ["chat-2-2"], "wake_when": "RULING"}
            loop = await svc.add("chat-1-1", "watch chat-2-2", idle_secs=60, judge=brief)
            assert loop.judge == brief

            loop.judge_quiet_streak = 3
            loop.judge_cursors = {"chat-2-2": 5}
            revised = await svc.update(loop.id, judge={"wake_when": "BLOCKED"})
            assert revised is not None
            assert revised.judge == {"wake_when": "BLOCKED"}
            assert revised.judge_quiet_streak == 0, "a new brief starts a new streak"
            assert revised.judge_cursors == {}

            cleared = await svc.update(loop.id, judge={})
            assert cleared is not None and cleared.judge == {}

            await svc.update(loop.id, judge=brief)
            untouched = await svc.update(loop.id, idle_secs=120)
            assert untouched is not None and untouched.judge == brief

            plain = await svc.add("chat-9-9", "no judge here", idle_secs=60)
            assert plain.judge == {}

        asyncio.run(main())


class TestEveryOutcomeHasALocalizedWord:
    """The popover renders a word per outcome, so the enum owes the catalog one.

    The verdict line is otherwise localized, and an outcome with no entry renders
    the point's own identifier inside it. Asserted against the enum rather than a
    hand-written list, so adding a fifth outcome fails here instead of shipping an
    English token into thirteen catalogs.
    """

    _EN = pathlib.Path(__file__).resolve().parents[1] / "website/src/i18n/locales/en.manual.json"

    def _words(self) -> dict[str, str]:
        import json

        section = json.loads(self._EN.read_text(encoding="utf-8"))["components"]["autoNudgePopover"]
        prefix = "judge_outcome_"
        return {
            key[len(prefix) :]: value for key, value in section.items() if key.startswith(prefix)
        }

    def test_every_outcome_value_has_a_word(self) -> None:
        words = self._words()
        missing = sorted(member.value for member in Outcome if member.value not in words)
        assert not missing, f"no localized word for outcome(s): {missing}"

    def test_an_unmapped_token_still_reads_as_a_word(self) -> None:
        """The verdict record can store ``unknown``, so that token needs a word too."""
        assert "unknown" in self._words()
