"""The ``otel_usage`` fold, and the OTel export subscriber that reads it.

Three groups. ``TestTheFold`` drives ``_otel_usage_start/_step/_render`` directly, the
same way ``test_crew_log_outline_fold.py`` drives its own fold, and pins the render's
field set so a later edit that adds a field must touch this test rather than silently
widening what the export subscriber (and anything else reading this fold) is handed.

``TestTheClosedVocabularies`` is the attribute boundary: an agent name or an actor value
outside the closed set the export subscriber knows must fold to ``other`` rather than
reach an attribute, which is the whole reason this fold and this export exist as a
separate module from the raw entry.

``TestTheSubscriberInstallAndIsolation`` drives the installed subscriber through the
real crew-log bus: the telemetry-off gate, one session's delta-over-time accounting, and
the bus's own promise that one subscriber raising does not silence another.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import bus as crew_log_bus
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.projection_otel_usage import (
    OTEL_USAGE_FOLD_NAME,
    SESSION_OPENED_TYPE,
    TURN_STARTED_TYPE,
    _agent_kind,
    _check_agent_kind_table_current,
    _otel_usage_copy,
    _otel_usage_render,
    _otel_usage_start,
    _otel_usage_step,
)
from kiro_crew.crew_log.schema import Entry
from kiro_crew.metrics import crew_log_export as export
from kiro_crew.metrics.crew_log_export import Metric

GATEWAY = "gateway"
SESSION = "s-otel-usage-eager"


def _entry(seq: int, entry_type: str, data: dict[str, Any]) -> Entry:
    return Entry(seq=seq, time=1_000 + seq, type=entry_type, src=GATEWAY, data=data)


def _opened(agent: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "slot": "",
        "model": "",
        "cwd": "",
        "owner": "raymond",
        "resumed": False,
    }


def _turn(actor: str, depth: int = 0) -> dict[str, Any]:
    return {"actor": actor, "depth": depth}


class TestTheDashboardTypeDeclaration:
    """``test_dashboard_types.py::TestTheThreeStates::test_no_object_passes_as_having_no_keys``
    is the gate this class exists to keep green: every object-shaped leaf a fold
    renders must be declared either as a known set of properties or as OPAQUE
    (``dashboard_types._OPAQUE``). ``turns`` is keyed by an actor value with no bound
    on which actors appear first, so it is opaque -- this pins the declaration rather
    than relying on catching its absence only in the cross-cutting dashboard-types
    suite, which S1 missed once already (see the PR history for why this class
    exists).
    """

    def test_otel_usage_turns_is_declared_opaque(self) -> None:
        from kiro_crew import dashboard_types as dt

        assert dt._OPAQUE.get("otel_usage.turns") == "an actor value"


class TestTheFold:
    def test_render_starts_unopened_with_no_turns(self) -> None:
        state = _otel_usage_start()
        assert _otel_usage_render(state) == {
            "opened": False,
            "opened_agent_kind": "",
            "opened_ts": 0,
            "turns": {},
        }

    def test_render_field_set_is_pinned(self) -> None:
        """A drift gate: a later edit that adds or renames a rendered field must update
        this assertion, so the export subscriber's contract moves on purpose and not by
        accident. See the module's own docstring for why nothing here is free text.
        """
        state = _otel_usage_start()
        _otel_usage_step(state, _entry(1, SESSION_OPENED_TYPE, _opened("kirocrew-worker")))
        rendered = _otel_usage_render(state)
        assert set(rendered) == {"opened", "opened_agent_kind", "opened_ts", "turns"}
        assert rendered["opened"] is True
        # The raw name was folded to the closed kind in step; the render carries the
        # enum, never "kirocrew-worker".
        assert rendered["opened_agent_kind"] == "worker"
        assert rendered["opened_ts"] == 1_001
        assert rendered["turns"] == {}

    def test_session_opened_is_counted_once_even_on_a_resumed_reattach(self) -> None:
        state = _otel_usage_start()
        _otel_usage_step(state, _entry(1, SESSION_OPENED_TYPE, _opened("kirocrew-worker")))
        # A re-attach to the same crew log carries another session/opened with
        # resumed=True; this fold's own opening fact must not flip or re-fire.
        _otel_usage_step(
            state, _entry(2, SESSION_OPENED_TYPE, {**_opened("kirocrew-lite"), "resumed": True})
        )
        rendered = _otel_usage_render(state)
        assert rendered["opened"] is True
        assert rendered["opened_agent_kind"] == "worker"

    def test_turns_are_counted_per_actor_and_accumulate(self) -> None:
        state = _otel_usage_start()
        _otel_usage_step(state, _entry(1, TURN_STARTED_TYPE, _turn("user")))
        _otel_usage_step(state, _entry(2, TURN_STARTED_TYPE, _turn("cron")))
        _otel_usage_step(state, _entry(3, TURN_STARTED_TYPE, _turn("user")))
        rendered = _otel_usage_render(state)
        assert rendered["turns"] == {"user": 2, "cron": 1}

    def test_a_turn_started_with_no_actor_string_is_skipped(self) -> None:
        state = _otel_usage_start()
        _otel_usage_step(state, _entry(1, TURN_STARTED_TYPE, {"depth": 0}))  # no actor
        rendered = _otel_usage_render(state)
        assert rendered == {
            "opened": False,
            "opened_agent_kind": "",
            "opened_ts": 0,
            "turns": {},
        }

    def test_a_session_opened_with_no_agent_string_still_opens_as_other(self) -> None:
        """``session/opened`` firing at all is the whole fact this fold counts as the
        unit opening -- ``agent`` is a required field on the real type, so a line
        missing it is already damaged. The fold folds a missing or non-string agent to
        ``other`` (never a raw value) rather than deciding the unit never opened.
        """
        state = _otel_usage_start()
        _otel_usage_step(state, _entry(1, SESSION_OPENED_TYPE, {"slot": "", "owner": "x"}))
        rendered = _otel_usage_render(state)
        # opened_ts is the entry's own time (1_000 + seq); the open fact still holds,
        # with the agent folded to the closed ``other`` bucket.
        assert rendered == {
            "opened": True,
            "opened_agent_kind": "other",
            "opened_ts": 1_001,
            "turns": {},
        }

    def test_a_member_dm_session_lands_in_the_crewmate_bucket(self) -> None:
        """A crewmate's pinned DM session as the WRITERS actually spell it: the agent is
        the crewmate's own crew name and the ``member-<slug>`` shape is in ``slot``
        (``handlers/members.py`` mints the slot with ``agent=member_name``, and
        ``chat_runner`` writes ``agent=slot.agent``, ``slot=slot.key``). Classifying off
        the agent name alone can only answer ``other`` for this entry, which empties the
        ``crewmate`` value of ``agent_kind`` -- so the step reads the slot key.
        """
        state = _otel_usage_start()
        _otel_usage_step(
            state,
            _entry(
                1,
                SESSION_OPENED_TYPE,
                {**_opened("Some Crewmate"), "slot": "member-some-crewmate"},
            ),
        )
        assert _otel_usage_render(state)["opened_agent_kind"] == "crewmate"

    def test_the_slot_key_is_read_for_its_shape_and_never_kept(self) -> None:
        """The slug inside the slot key is as private as the agent name beside it: the
        step reads the key to answer one closed enum and keeps neither string, so a
        distinctive slug reaches no state, no copy and no render.
        """
        unique_slug = "member-zzq-private-slug-6b1e4d02"
        state = _otel_usage_start()
        _otel_usage_step(
            state,
            _entry(1, SESSION_OPENED_TYPE, {**_opened("Some Crewmate"), "slot": unique_slug}),
        )
        assert state["opened_agent_kind"] == "crewmate"
        assert unique_slug not in repr(state)
        assert unique_slug not in repr(_otel_usage_render(state))
        assert unique_slug not in repr(_otel_usage_copy(state))

    def test_a_non_string_slot_classifies_by_agent_name_instead_of_raising(self) -> None:
        state = _otel_usage_start()
        _otel_usage_step(
            state,
            _entry(1, SESSION_OPENED_TYPE, {**_opened("kirocrew-worker"), "slot": 7}),
        )
        assert _otel_usage_render(state)["opened_agent_kind"] == "worker"

    def test_an_entry_of_another_type_does_not_move_this_fold(self) -> None:
        state = _otel_usage_start()
        before = _otel_usage_render(state)
        _otel_usage_step(state, _entry(1, "tool/called", {"name": "whatever"}))
        assert _otel_usage_render(state) == before


class TestTheClosedVocabularies:
    @pytest.mark.parametrize(
        "raw_agent,expected",
        [
            ("kirocrew-worker", "worker"),
            ("kirocrew-conductor", "conductor"),
            ("kirocrew-pipeline-conductor", "pipeline_conductor"),
            ("kirocrew-ledger-conductor", "ledger_conductor"),
            ("kirocrew-security-conductor", "security_conductor"),
            ("kirocrew-lite", "lite"),
            ("kirocrew-guest", "guest"),
            ("kirocrew-dashboard-author", "dashboard_author"),
            ("kirocrew-knowledge", "knowledge"),
            ("kirocrew-research", "research"),
            ("kirocrew-team-lead", "team_lead"),
            ("kirocrew-heartbeat", "heartbeat"),
            ("kirocrew-dashboard-manager", "dashboard_manager"),
            ("kirocrew", "default"),
            ("", "other"),
            ("a-third-party-agent", "other"),
            ("my-custom-template", "other"),
            # A slot-key SHAPE in the agent field is NOT what a crewmate session looks
            # like on the wire, so it classifies by name like any other unknown string.
            # The crewmate bucket is reached from the slot field; see the class below.
            ("member-some-crewmate", "other"),
        ],
    )
    def test_agent_kind_is_a_closed_fold_with_an_other_bucket(
        self, raw_agent: str, expected: str
    ) -> None:
        assert _agent_kind(raw_agent, "") == expected

    @pytest.mark.parametrize(
        "raw_slot",
        [
            "member-some-crewmate",
            "dashboard_member-some-crewmate",
            "dashboard:member-some-crewmate",
        ],
    )
    def test_the_crewmate_bucket_is_read_from_the_slot_key_under_every_prefix(
        self, raw_slot: str
    ) -> None:
        """The slot key travels under three prefixes depending on the layer
        (``members.is_member_session_key``), and a member DM session's ``agent`` is the
        crewmate's own crew name, so the key is the only field that carries the shape.
        The crewmate answer wins over whatever the agent name is, including an owned
        stem a crewmate happens to be named after.
        """
        assert _agent_kind("Some Crewmate", raw_slot) == "crewmate"
        assert _agent_kind("kirocrew-worker", raw_slot) == "crewmate"

    def test_an_ordinary_slot_key_leaves_the_agent_name_to_answer(self) -> None:
        assert _agent_kind("kirocrew-worker", "dashboard_main") == "worker"
        assert _agent_kind("kirocrew", "chat-2883") == "default"
        assert _agent_kind("a-third-party-agent", "dashboard_main") == "other"

    def test_agent_kind_table_matches_owned_kiro_agent_files(self) -> None:
        """Re-assert the drift guard so a future drift between ``_AGENT_KIND_BY_STEM``
        and ``OWNED_KIRO_AGENT_FILES`` fails a normal test run. The table and the check
        live with the fold now, since the fold is what folds the raw agent.
        """
        _check_agent_kind_table_current()  # raises on drift

    def test_the_classifier_failure_log_never_carries_the_agent_name(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the crewmate-slot check cannot be imported, the classifier logs the
        failure, folds to ``other`` and keeps going -- but the raw agent name is a local
        in that frame, so the log must carry neither the name nor an ``exc_info``
        traceback that would reach back into the frame. Design-review 1.4: the raw name
        leaves no trace, the failure path included.
        """
        import sys

        unique = "zzq-private-agent-name-in-log-5e1d"
        unique_slot = "member-zzq-private-slug-in-log-5e1d"
        # Force the ``kiro_crew.members`` import inside _agent_kind to raise, driving the
        # except branch. Both raw strings are locals in that frame.
        monkeypatch.setitem(sys.modules, "kiro_crew.members", None)
        with caplog.at_level("DEBUG", logger="kiro_crew.crew_log.projection"):
            # The slot shape cannot be asked without the import, so the name answers --
            # a crewmate session folds to ``other`` rather than guessing at the shape.
            assert _agent_kind(unique, unique_slot) == "other"
        # The branch ran (a record was emitted), and no record carries either raw string.
        assert any("members import failed" in record.getMessage() for record in caplog.records)
        for record in caplog.records:
            assert unique not in record.getMessage()
            assert unique_slot not in record.getMessage()
            assert unique not in str(record.exc_info or "")
            assert unique_slot not in str(record.exc_info or "")
            assert record.exc_info is None

    def test_agent_kind_reads_no_filesystem_or_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """agent_kind classification stays a pure function of the raw agent string plus
        code constants -- it never reads a file or crew config.

        The fold itself only folds ``session/opened.agent`` (a log field), so its replay
        is deterministic; this pins the one place the raw name is turned into the closed
        ``agent_kind`` enum. If classification ever started reading a config or spec file,
        the same log would classify differently depending on what was on disk at read
        time, which is exactly the replay-nondeterminism the design review flags. Here
        ``open`` raises, so any classification that opened a file would fail the test.
        """

        def _no_open(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("agent_kind classification opened a file")

        monkeypatch.setattr("builtins.open", _no_open)
        # One input per bucket kind: an owned stem, the bare default, a crewmate DM
        # slot key, and an unknown name -- the full set of branches _agent_kind takes.
        assert _agent_kind("kirocrew-worker", "") == "worker"
        assert _agent_kind("kirocrew", "") == "default"
        assert _agent_kind("Some Crewmate", "dashboard:member-acme") == "crewmate"
        assert _agent_kind("some-third-party-agent", "") == "other"

    @pytest.mark.parametrize(
        "actor,expected_attr",
        [
            ("user", "user"),
            ("app", "app"),
            ("crew", "crew"),
            ("cron", "cron"),
            ("autonudge", "autonudge"),
            ("subagent", "subagent"),
            ("gateway", "gateway"),
            ("other", "other"),
            ("some-unknown-future-actor", "other"),
        ],
    )
    def test_turn_actor_diff_folds_unknown_values_to_other(
        self, actor: str, expected_attr: str
    ) -> None:
        adds = export._otel_usage_diff(
            {}, {"opened": False, "opened_agent_kind": "", "turns": {actor: 1}}
        )
        # A session whose opening is not folded yet has no kind: the turn still counts,
        # under ``other``, never under an empty or raw value.
        assert adds == [
            Metric(
                export.TURN_STARTED_METRIC,
                1,
                {"actor": expected_attr, "agent_kind": "other"},
            )
        ]

    def test_the_actor_set_is_the_writers_own_closed_set(self) -> None:
        # One definition: the export reads ACTOR_VALUES rather than a second spelling,
        # so a value the writer adds can never be silently folded to other here.
        from kiro_crew.crew_log.entry_types_lifecycle import ACTOR_VALUES

        assert export._known_actors() == frozenset(ACTOR_VALUES)
        assert "other" in export._known_actors()

    @pytest.mark.parametrize(
        "rendered_kind,expected_attr",
        [
            ("conductor", "conductor"),
            ("worker", "worker"),
            ("crewmate", "crewmate"),
            ("default", "default"),
            ("", "other"),
            ("kirocrew-worker", "other"),
            ("some-raw-agent-name", "other"),
        ],
    )
    def test_turn_agent_kind_is_the_sessions_closed_kind(
        self, rendered_kind: str, expected_attr: str
    ) -> None:
        # turn.started carries the agent_kind of the session the turn ran in, read from
        # the fold's already-closed opened_agent_kind. Anything outside the closed set
        # -- including a raw agent filename a damaged render might carry -- is other.
        adds = export._otel_usage_diff(
            {"opened": True, "opened_agent_kind": rendered_kind, "turns": {}},
            {"opened": True, "opened_agent_kind": rendered_kind, "turns": {"crew": 2}},
        )
        assert adds == [
            Metric(
                export.TURN_STARTED_METRIC,
                2,
                {"actor": "crew", "agent_kind": expected_attr},
            )
        ]

    def test_session_started_fires_once_on_first_sight_of_a_new_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # session.started fires on first sight (previous is None) of a session whose
        # opened_ts is at or after install -- new to this process. The old "opened
        # transition" trigger could never fire under the baseline gate, so the trigger
        # is first-sight + timestamp.
        monkeypatch.setattr(export, "_install_ms", 1_000)
        monkeypatch.setattr(export, "_evicted_max_opened_ts", 1_000)
        current = {
            "opened": True,
            "opened_agent_kind": "worker",
            "opened_ts": 1_500,  # strictly after the evicted-max anchor -> new
            "turns": {"user": 1},
        }
        # A new session's whole first render counts against zero: session.started AND
        # the first turn already in that first batch (the F2 fix).
        adds = export._otel_usage_diff(None, current)
        assert adds == [
            Metric(export.SESSION_STARTED_METRIC, 1, {"agent_kind": "worker"}),
            Metric(
                export.TURN_STARTED_METRIC,
                1,
                {"actor": "user", "agent_kind": "worker"},
            ),
        ]
        # A later delivery (previous not None) never re-fires session.started; it diffs
        # turns against the stored floor.
        assert export._otel_usage_diff(current, current) == []

    def test_session_started_does_not_fire_for_a_session_opened_before_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A resumed session: opened_ts predates install, so its first sight baselines
        # without counting the open.
        monkeypatch.setattr(export, "_install_ms", 2_000)
        monkeypatch.setattr(export, "_evicted_max_opened_ts", 2_000)
        current = {
            "opened": True,
            "opened_agent_kind": "worker",
            "opened_ts": 1_000,  # not after the anchor -> resumed, baselined
            "turns": {},
        }
        assert export._otel_usage_diff(None, current) == []

    def test_turn_diff_is_the_delta_not_the_total(self) -> None:
        previous = {"opened": True, "opened_agent_kind": "worker", "turns": {"user": 2}}
        current = {"opened": True, "opened_agent_kind": "worker", "turns": {"user": 5}}
        adds = export._otel_usage_diff(previous, current)
        assert adds == [
            Metric(
                export.TURN_STARTED_METRIC,
                3,
                {"actor": "user", "agent_kind": "worker"},
            )
        ]

    def test_a_total_that_does_not_grow_emits_no_add(self) -> None:
        previous = {"opened": True, "opened_agent_kind": "worker", "turns": {"user": 5}}
        current = {"opened": True, "opened_agent_kind": "worker", "turns": {"user": 5}}
        assert export._otel_usage_diff(previous, current) == []


class TestTheSubscriberInstallAndIsolation:
    def setup_method(self) -> None:
        crew_log_bus.reset_for_tests()
        export.reset_for_tests()

    def teardown_method(self) -> None:
        export.reset_for_tests()
        crew_log_bus.reset_for_tests()

    def test_install_does_not_subscribe_when_telemetry_is_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.metrics.recorder import MetricsRecorder

        monkeypatch.setattr(
            "kiro_crew.metrics.provider.get_recorder", lambda: MetricsRecorder(None)
        )
        installed = export.install_otel_crew_log_subscriber()
        assert installed is False
        assert crew_log_bus.subscriber_count(crew_log_bus.FOLD_ADVANCED) == 0

    def test_install_subscribes_when_telemetry_is_enabled_and_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorder = MetricsRecorder(object())  # any non-None meter marks it enabled
        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
        assert export.install_otel_crew_log_subscriber() is True
        assert crew_log_bus.subscriber_count(crew_log_bus.FOLD_ADVANCED) == 1
        # A second install call replaces rather than adds a second subscription.
        assert export.install_otel_crew_log_subscriber() is True
        assert crew_log_bus.subscriber_count(crew_log_bus.FOLD_ADVANCED) == 1

    def test_a_delivery_accumulates_the_right_counter_adds_over_two_publishes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorded: list[tuple[str, Any, dict[str, Any]]] = []

        class _Recorder(MetricsRecorder):
            def __init__(self) -> None:
                super().__init__(object())

            def counter(self, name: str, value: Any = 1, *, attrs: Any = None, **_: Any) -> None:
                recorded.append((name, value, dict(attrs or {})))

            def histogram(self, *args: Any, **kwargs: Any) -> None:
                pass

        recorder = _Recorder()
        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
        assert export.install_otel_crew_log_subscriber() is True
        # opened_ts BEFORE install -> a resumed session: session.started does not fire,
        # so this test isolates the turn-delta accounting.
        old_ts = export._install_ms - 10_000

        def _publish(rev: int, turns: dict[str, int]) -> None:
            crew_log_bus.publish(
                crew_log_bus.FOLD_ADVANCED,
                crew_log_bus.FoldAdvanced(
                    scope=crew_log_bus.SCOPE_SESSION,
                    key="sess-a",
                    fold=OTEL_USAGE_FOLD_NAME,
                    revision=rev,
                    value={
                        "opened": True,
                        "opened_agent_kind": "worker",
                        "opened_ts": old_ts,
                        "turns": turns,
                    },
                    seq=rev,
                ),
            )

        # First sight BASELINES the cumulative turn totals (the floor); nothing emitted.
        _publish(1, {"user": 1})
        assert recorded == []
        # Later deliveries diff against that floor and count only what grew since.
        _publish(2, {"user": 3})
        _publish(3, {"user": 3, "cron": 1})
        assert recorded == [
            (export.TURN_STARTED_METRIC, 2, {"actor": "user", "agent_kind": "worker"}),
            (export.TURN_STARTED_METRIC, 1, {"actor": "cron", "agent_kind": "worker"}),
        ]

    def test_export_lag_is_sampled_only_for_a_fold_this_module_exports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bus subscription filters on ``scope=SCOPE_SESSION`` alone, so every
        session-keyed fold the eager worker advances reaches the callback -- ``status``,
        ``class``, ``usage``, ``timeline``. Those are dropped by the ``_EXPORTERS``
        lookup, and timing the drop would fill this histogram with the cost of one dict
        lookup. The population it is read for is the diff-and-record work, so one
        sample per EXPORTED delivery and none for the rest.
        """
        from kiro_crew.metrics.recorder import MetricsRecorder

        lags: list[tuple[str, Any]] = []

        class _Recorder(MetricsRecorder):
            def __init__(self) -> None:
                super().__init__(object())

            def counter(self, *args: Any, **kwargs: Any) -> None:
                pass

            def histogram(self, name: str, value: Any = 0, **_: Any) -> None:
                lags.append((name, value))

        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: _Recorder())
        assert export.install_otel_crew_log_subscriber() is True

        def _publish(fold: str, rev: int) -> None:
            crew_log_bus.publish(
                crew_log_bus.FOLD_ADVANCED,
                crew_log_bus.FoldAdvanced(
                    scope=crew_log_bus.SCOPE_SESSION,
                    key="sess-lag",
                    fold=fold,
                    revision=rev,
                    value={
                        "opened": True,
                        "opened_agent_kind": "worker",
                        "opened_ts": export._install_ms - 10_000,
                        "turns": {"user": rev},
                    },
                    seq=rev,
                ),
            )

        # Three deliveries for folds this module does not export: no observation.
        _publish("status", 1)
        _publish("class", 2)
        _publish("timeline", 3)
        assert lags == []

        # A malformed event for a fold it DOES export is also not export work.
        crew_log_bus.publish(
            crew_log_bus.FOLD_ADVANCED,
            crew_log_bus.FoldAdvanced(
                scope=crew_log_bus.SCOPE_SESSION,
                key="",
                fold=OTEL_USAGE_FOLD_NAME,
                revision=4,
                value={},
                seq=4,
            ),
        )
        assert lags == []

        # Its own fold: exactly one observation per delivery, under the lag name.
        _publish(OTEL_USAGE_FOLD_NAME, 5)
        _publish(OTEL_USAGE_FOLD_NAME, 6)
        assert [name for name, _ in lags] == [export.EXPORT_LAG_METRIC] * 2
        assert all(isinstance(value, float) and value >= 0.0 for _, value in lags)

    def test_a_new_session_opened_in_this_process_counts_session_started_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session whose opened_ts is at or after install is new to THIS process, so
        its first sight counts session.started exactly once, attributed by agent_kind.
        A second delivery does not re-count it.
        """
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorded: list[tuple[str, Any, dict[str, Any]]] = []

        class _Recorder(MetricsRecorder):
            def __init__(self) -> None:
                super().__init__(object())

            def counter(self, name: str, value: Any = 1, *, attrs: Any = None, **_: Any) -> None:
                recorded.append((name, value, dict(attrs or {})))

            def histogram(self, *args: Any, **kwargs: Any) -> None:
                pass

        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: _Recorder())
        assert export.install_otel_crew_log_subscriber() is True
        new_ts = export._install_ms + 1

        def _publish(rev: int, turns: dict[str, int]) -> None:
            crew_log_bus.publish(
                crew_log_bus.FOLD_ADVANCED,
                crew_log_bus.FoldAdvanced(
                    scope=crew_log_bus.SCOPE_SESSION,
                    key="brand-new",
                    fold=OTEL_USAGE_FOLD_NAME,
                    revision=rev,
                    value={
                        "opened": True,
                        "opened_agent_kind": "worker",
                        "opened_ts": new_ts,
                        "turns": turns,
                    },
                    seq=rev,
                ),
            )

        _publish(1, {})
        assert recorded == [(export.SESSION_STARTED_METRIC, 1, {"agent_kind": "worker"})]
        # A second delivery (a turn) counts the turn, not a second session.started.
        _publish(2, {"user": 1})
        assert recorded == [
            (export.SESSION_STARTED_METRIC, 1, {"agent_kind": "worker"}),
            (export.TURN_STARTED_METRIC, 1, {"actor": "user", "agent_kind": "worker"}),
        ]

    def test_a_resumed_session_after_restart_is_not_recounted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The finding: a gateway restart leaves _last_values empty, so the first
        delivery for a resumed session carries its whole cumulative render. Baselining
        the first sight means that render is the floor, not a re-add.
        """
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorded: list[tuple[str, Any, dict[str, Any]]] = []

        class _Recorder(MetricsRecorder):
            def __init__(self) -> None:
                super().__init__(object())

            def counter(self, name: str, value: Any = 1, *, attrs: Any = None, **_: Any) -> None:
                recorded.append((name, value, dict(attrs or {})))

            def histogram(self, *args: Any, **kwargs: Any) -> None:
                pass

        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: _Recorder())
        assert export.install_otel_crew_log_subscriber() is True
        # opened_ts BEFORE install -> a session opened by a previous process. Its
        # session.started must not fire and its pre-restart turns must not be re-added.
        old_ts = export._install_ms - 10_000
        crew_log_bus.publish(
            crew_log_bus.FOLD_ADVANCED,
            crew_log_bus.FoldAdvanced(
                scope=crew_log_bus.SCOPE_SESSION,
                key="resumed",
                fold=OTEL_USAGE_FOLD_NAME,
                revision=1,
                value={
                    "opened": True,
                    "opened_agent_kind": "worker",
                    "opened_ts": old_ts,
                    "turns": {"user": 3},
                },
                seq=3,
            ),
        )
        assert recorded == [], "a resumed session's open or pre-restart total was counted"
        # One new turn after the restart: only that one counts, still no session.started.
        crew_log_bus.publish(
            crew_log_bus.FOLD_ADVANCED,
            crew_log_bus.FoldAdvanced(
                scope=crew_log_bus.SCOPE_SESSION,
                key="resumed",
                fold=OTEL_USAGE_FOLD_NAME,
                revision=2,
                value={
                    "opened": True,
                    "opened_agent_kind": "worker",
                    "opened_ts": old_ts,
                    "turns": {"user": 4},
                },
                seq=4,
            ),
        )
        assert recorded == [
            (export.TURN_STARTED_METRIC, 1, {"actor": "user", "agent_kind": "worker"})
        ]

    def test_an_evicted_new_session_re_seen_is_not_recounted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The F1 case: a NEW session (counted on first sight) is evicted, then seen
        again. Its re-sight must NOT re-fire session.started or re-add its turns. The
        evicted-max anchor rises to the victim's opened_ts on eviction, so its re-sight
        reads as not-new and baselines.
        """
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorded: list[tuple[str, Any, dict[str, Any]]] = []

        class _Recorder(MetricsRecorder):
            def __init__(self) -> None:
                super().__init__(object())

            def counter(self, name: str, value: Any = 1, *, attrs: Any = None, **_: Any) -> None:
                recorded.append((name, value, dict(attrs or {})))

            def histogram(self, *args: Any, **kwargs: Any) -> None:
                pass

        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: _Recorder())
        assert export.install_otel_crew_log_subscriber() is True
        base = export._install_ms

        def _publish(key: str, rev: int, opened_ts: int, turns: dict[str, int]) -> None:
            crew_log_bus.publish(
                crew_log_bus.FOLD_ADVANCED,
                crew_log_bus.FoldAdvanced(
                    scope=crew_log_bus.SCOPE_SESSION,
                    key=key,
                    fold=OTEL_USAGE_FOLD_NAME,
                    revision=rev,
                    value={
                        "opened": True,
                        "opened_agent_kind": "worker",
                        "opened_ts": opened_ts,
                        "turns": turns,
                    },
                    seq=rev,
                ),
            )

        # A new session (opened after install) -- first sight counts it whole.
        _publish("victim", 1, base + 5, {"user": 2})
        assert recorded == [
            (export.SESSION_STARTED_METRIC, 1, {"agent_kind": "worker"}),
            (export.TURN_STARTED_METRIC, 2, {"actor": "user", "agent_kind": "worker"}),
        ]
        recorded.clear()
        # Evict "victim" with filler sessions opened AFTER it (so the evicted-max rises
        # to at least victim's opened_ts).
        for i in range(export._MAX_TRACKED_SESSIONS):
            _publish(f"filler-{i}", 1, base + 10 + i, {})
        recorded.clear()
        # "victim" re-seen: evicted-max is now >= its opened_ts, so it is not-new and
        # baselines -- no re-fire of session.started, no re-add of its turns.
        _publish("victim", 2, base + 5, {"user": 2})
        assert recorded == [], "an evicted new session was re-counted on re-sight"

    def test_a_new_session_opened_after_an_eviction_still_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other side of the evicted-max rule: a session opened AFTER the newest
        evicted session is still provably new and is counted whole.
        """
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorded: list[tuple[str, Any, dict[str, Any]]] = []

        class _Recorder(MetricsRecorder):
            def __init__(self) -> None:
                super().__init__(object())

            def counter(self, name: str, value: Any = 1, *, attrs: Any = None, **_: Any) -> None:
                recorded.append((name, value, dict(attrs or {})))

            def histogram(self, *args: Any, **kwargs: Any) -> None:
                pass

        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: _Recorder())
        assert export.install_otel_crew_log_subscriber() is True
        base = export._install_ms

        def _publish(key: str, opened_ts: int, turns: dict[str, int]) -> None:
            crew_log_bus.publish(
                crew_log_bus.FOLD_ADVANCED,
                crew_log_bus.FoldAdvanced(
                    scope=crew_log_bus.SCOPE_SESSION,
                    key=key,
                    fold=OTEL_USAGE_FOLD_NAME,
                    revision=1,
                    value={
                        "opened": True,
                        "opened_agent_kind": "worker",
                        "opened_ts": opened_ts,
                        "turns": turns,
                    },
                    seq=1,
                ),
            )

        for i in range(export._MAX_TRACKED_SESSIONS + 1):
            _publish(f"filler-{i}", base + 10 + i, {})
        recorded.clear()
        # A fresh session opened AFTER every filler -> still new, counted whole.
        _publish("fresh", base + 10 + export._MAX_TRACKED_SESSIONS + 100, {"user": 1})
        assert recorded == [
            (export.SESSION_STARTED_METRIC, 1, {"agent_kind": "worker"}),
            (export.TURN_STARTED_METRIC, 1, {"actor": "user", "agent_kind": "worker"}),
        ]

    def test_a_raising_subscriber_does_not_silence_this_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bus's own isolation contract (``bus.publish`` logs and continues), proved
        with this module's real callback as the SURVIVING subscriber.
        """
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorded: list[tuple[str, Any, dict[str, Any]]] = []

        class _Recorder(MetricsRecorder):
            def __init__(self) -> None:
                super().__init__(object())

            def counter(self, name: str, value: Any = 1, *, attrs: Any = None, **_: Any) -> None:
                recorded.append((name, value, dict(attrs or {})))

            def histogram(self, *args: Any, **kwargs: Any) -> None:
                pass

        recorder = _Recorder()
        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
        assert export.install_otel_crew_log_subscriber() is True

        def _boom(_event: Any) -> None:
            raise RuntimeError("a sibling subscriber exploding")

        crew_log_bus.subscribe(crew_log_bus.FOLD_ADVANCED, _boom, scope=crew_log_bus.SCOPE_SESSION)

        def _publish(rev: int, turns: dict[str, int]) -> None:
            crew_log_bus.publish(
                crew_log_bus.FOLD_ADVANCED,
                crew_log_bus.FoldAdvanced(
                    scope=crew_log_bus.SCOPE_SESSION,
                    key="sess-b",
                    fold=OTEL_USAGE_FOLD_NAME,
                    revision=rev,
                    value={"opened": True, "opened_agent_kind": "worker", "turns": turns},
                    seq=rev,
                ),
            )

        # First sight baselines; the second delivery's turn is what proves this
        # subscriber still ran AFTER the sibling raised.
        _publish(1, {})
        _publish(2, {"user": 1})
        # Only this module's instruments: the bus counts the sibling's raise through the
        # same recorder (``kirocrew.crew_log.bus.subscriber_error``), which is the bus's
        # contract, not this subscriber's.
        ours = (export.SESSION_STARTED_METRIC, export.TURN_STARTED_METRIC)
        assert [item for item in recorded if item[0] in ours] == [
            (export.TURN_STARTED_METRIC, 1, {"actor": "user", "agent_kind": "worker"})
        ]

    def test_this_subscriber_raising_on_a_malformed_event_does_not_raise_to_the_bus(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorder = MetricsRecorder(object())
        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
        assert export.install_otel_crew_log_subscriber() is True

        class _NotAFoldAdvanced:
            scope = crew_log_bus.SCOPE_SESSION
            fold = OTEL_USAGE_FOLD_NAME
            key = "sess-c"
            value = "not-a-mapping"  # malformed on purpose
            revision = 1

        # Must not raise out of publish.
        crew_log_bus.publish(crew_log_bus.FOLD_ADVANCED, _NotAFoldAdvanced())


class TestTheContractExtensions:
    """The extension a later row needs: the one-subscription-per-session-row shape,
    with dispatch by the event's fold name. Exercised by
    installing a TEMPORARY second row in :data:`export._EXPORTERS` (restored in
    ``teardown_method``) rather than by adding a real second fold here -- S1 ships one,
    and this file's job is to prove the CONTRACT, not to anticipate S2's own fold.
    """

    def setup_method(self) -> None:
        crew_log_bus.reset_for_tests()
        export.reset_for_tests()
        self._original_exporters = export._EXPORTERS

    def teardown_method(self) -> None:
        export._EXPORTERS = self._original_exporters
        export.reset_for_tests()
        crew_log_bus.reset_for_tests()

    @staticmethod
    def _recorder() -> Any:
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorded: list[tuple[str, str, Any, dict[str, Any]]] = []

        class _Recorder(MetricsRecorder):
            def __init__(self) -> None:
                super().__init__(object())

            def counter(self, name: str, value: Any = 1, *, attrs: Any = None, **_: Any) -> None:
                recorded.append(("counter", name, value, dict(attrs or {})))

            def histogram(self, name: str, value: Any, *, attrs: Any = None, **_: Any) -> None:
                recorded.append(("histogram", name, value, dict(attrs or {})))

        return _Recorder(), recorded

    def test_a_second_session_row_shares_the_one_subscription_and_dispatches_by_fold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two session-keyed rows -> still ONE bus subscription; each delivery runs
        only the row whose fold name the event carries.
        """

        def _other_diff(previous: Any, current: Any) -> "list[Metric]":
            if previous is None:  # first sight baselines, like every real diff
                return []
            return [Metric("kirocrew.crew.other.count", current.get("n", 0), {})]

        export._EXPORTERS = (
            export.UsageExporter("otel_usage", export._otel_usage_diff),
            export.UsageExporter("other_test", _other_diff),
        )
        recorder, recorded = self._recorder()
        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
        assert export.install_otel_crew_log_subscriber() is True
        # One subscription serves both session-keyed rows.
        assert crew_log_bus.subscriber_count(crew_log_bus.FOLD_ADVANCED) == 1

        def _pub(key: str, fold: str, rev: int, value: dict[str, Any]) -> None:
            crew_log_bus.publish(
                crew_log_bus.FOLD_ADVANCED,
                crew_log_bus.FoldAdvanced(
                    scope=crew_log_bus.SCOPE_SESSION,
                    key=key,
                    fold=fold,
                    revision=rev,
                    value=value,
                    seq=rev,
                ),
            )

        # Each session's first sight baselines; the second delivery per session is what
        # each row's diff actually runs on, which is what proves the by-fold dispatch.
        _pub("sess-other", "other_test", 1, {"n": 0})
        _pub("sess-other", "other_test", 2, {"n": 5})
        _pub(
            "sess-x",
            "otel_usage",
            1,
            {"opened": True, "opened_agent_kind": "worker", "turns": {}},
        )
        _pub(
            "sess-x",
            "otel_usage",
            2,
            {"opened": True, "opened_agent_kind": "worker", "turns": {"user": 1}},
        )
        assert ("counter", "kirocrew.crew.other.count", 5, {}) in recorded
        assert (
            "counter",
            export.TURN_STARTED_METRIC,
            1,
            {"actor": "user", "agent_kind": "worker"},
        ) in recorded
        counters = [row for row in recorded if row[0] == "counter"]
        assert len(counters) == 2


class TestTheRealAppendPath:
    """``otel_usage`` driven through a REAL ``CrewLog``, the eager worker and the real
    bus -- the path every other fold in ``test_crew_log_session_eager.py`` is proved on.
    Proves this fold is actually woken by a real commit (not just by calling ``_step``
    directly), that it is published on the bus like its siblings, and that a second
    process (simulated by dropping every in-memory cell and the bus subscription)
    resumes from the ON-DISK savepoint instead of folding cold.
    """

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path: Any, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
        # Patches through _floor_monkeypatch, not the shared monkeypatch, so a test that
        # calls monkeypatch.undo() cannot lift this fixture's KIROCREW_HOME pin (D11).
        _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        eager.stop_for_tests()
        crew_log.forget_slot_folds()
        crew_log.forget_session_folds()
        crew_log_bus.reset_for_tests()
        emit.reset_caches()
        export.reset_for_tests()
        yield
        eager.stop_for_tests()
        crew_log.forget_slot_folds()
        crew_log.forget_session_folds()
        crew_log_bus.reset_for_tests()
        emit.reset_caches()
        export.reset_for_tests()

    def _log(self, unit_id: str = SESSION) -> CrewLog:
        return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew-worker")

    def _wake(self, unit_id: str, entry_type: str, seq: int) -> None:
        eager.note_commit(unit_id, entry_type, seq)
        assert eager.drain(timeout=10.0)

    def test_a_real_commit_wakes_the_fold_and_publishes_it_on_the_bus(self) -> None:
        seen: list[Any] = []
        crew_log_bus.subscribe(
            crew_log_bus.FOLD_ADVANCED, seen.append, scope=crew_log_bus.SCOPE_SESSION
        )
        handle = self._log()
        done = handle.append(
            "session/opened",
            {
                "agent": "kirocrew-worker",
                "slot": "",
                "model": "",
                "cwd": "",
                "owner": "raymond",
                "resumed": False,
            },
            src=GATEWAY,
        )
        self._wake(SESSION, "session/opened", int(done.seq))

        otel_events = [event for event in seen if event.fold == OTEL_USAGE_FOLD_NAME]
        assert len(otel_events) == 1
        value = otel_events[0].value
        assert value["opened"] is True
        # The raw "kirocrew-worker" was folded to the closed kind in the fold's step;
        # the render the subscriber sees carries only the enum.
        assert value["opened_agent_kind"] == "worker"
        assert value["turns"] == {}
        # opened_ts is the opening entry's own real timestamp -- a positive epoch ms,
        # not a fixed number, so it is asserted by shape rather than by value.
        assert isinstance(value["opened_ts"], int) and value["opened_ts"] > 0
        assert value == crew_log.read_projection(SESSION, OTEL_USAGE_FOLD_NAME).value

    def test_the_real_path_end_to_end_reaches_a_counter_add(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full chain this story ships: an append, the eager wake, the bus publish,
        this module's subscriber, and a real (stubbed-transport) recorder's ``.add()``.
        """
        from kiro_crew.metrics.crew_log_health import FOLD_APPLY, FOLD_START
        from kiro_crew.metrics.recorder import MetricsRecorder

        recorded: list[tuple[str, Any, dict[str, Any]]] = []

        class _Recorder(MetricsRecorder):
            def __init__(self) -> None:
                super().__init__(object())

            def counter(self, name: str, value: Any = 1, *, attrs: Any = None, **_: Any) -> None:
                recorded.append((name, value, dict(attrs or {})))

            def histogram(self, *args: Any, **kwargs: Any) -> None:
                pass

        recorder = _Recorder()
        monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
        # The eager fold worker records its own crew log fold counters through the
        # same recorder. Freeze their clock so every pass lands in one bucket and the
        # whole stream below stays an exact equality.
        monkeypatch.setattr("kiro_crew.metrics.crew_log_health._now", lambda: 0.0)
        assert export.install_otel_crew_log_subscriber() is True
        # Pin the "new" anchors BELOW any real now_ms() the appends below will stamp, so
        # the opening's opened_ts is deterministically > the anchor -- otherwise install
        # and the open landing in the same millisecond would make opened_ts == anchor,
        # which the strict ``>`` rejects, and session.started would flake out.
        monkeypatch.setattr(export, "_install_ms", 1)
        monkeypatch.setattr(export, "_evicted_max_opened_ts", 1)

        handle = self._log()
        opened = handle.append(
            "session/opened",
            {
                "agent": "kirocrew-worker",
                "slot": "",
                "model": "",
                "cwd": "",
                "owner": "raymond",
                "resumed": False,
            },
            src=GATEWAY,
        )
        self._wake(SESSION, "session/opened", int(opened.seq))
        started = handle.append(
            "turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY
        )
        self._wake(SESSION, "turn/started", int(started.seq))
        # The session was opened AFTER export install (its opened_ts is a fresh now_ms),
        # so its first sight counts session.started once; the next delivery (the turn)
        # diffs against the baselined floor and counts the turn. Around each sits the
        # eager fold pass that delivered it: one ``fold.start`` (cold on the first
        # pass, warm after) and one ``fold.apply`` per session fold that moved.

        def fold_pass(origin: str, folds: tuple[str, ...]) -> list[Any]:
            return [(FOLD_START, 1, {"scope": "session", "origin": origin})] + [
                (FOLD_APPLY, 1, {"scope": "session", "fold": f, "bucket": "lt_10ms"}) for f in folds
            ]

        assert recorded == [
            *fold_pass(
                "cold",
                (
                    "status",
                    "usage",
                    "timeline",
                    "tools",
                    "approvals",
                    "subagents",
                    "class",
                    "outline",
                    "otel_usage",
                    "otel_session_tree",
                ),
            ),
            (export.SESSION_STARTED_METRIC, 1, {"agent_kind": "worker"}),
            *fold_pass("warm", ("status", "timeline", "class", "outline", "otel_usage")),
            (export.TURN_STARTED_METRIC, 1, {"actor": "user", "agent_kind": "worker"}),
        ]

    def test_restart_resumes_from_the_savepoint_instead_of_folding_cold(self) -> None:
        """MUTATION-SENSITIVE: drop every in-memory cell (what a process restart loses)
        and prove the next read comes back from the ON-DISK savepoint written beside the
        unit's own log, not from a cold walk of the file -- the same proof
        ``test_crew_log_checkpoint.py`` runs for the advertised folds, run here for this
        one. If this regresses to a cold fold every time, the answer stays correct but
        the cost guarantee this story's dev plan asks for silently stops holding.
        """
        handle = self._log()
        for turn in range(1, 300):
            done = handle.append(
                "turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY
            )
            self._wake(SESSION, "turn/started", int(done.seq))

        on_disk_before = savepoints.load(handle, [OTEL_USAGE_FOLD_NAME])
        assert on_disk_before is not None, "no savepoint was written after 299 turns"
        assert OTEL_USAGE_FOLD_NAME in on_disk_before.checkpoints

        # Simulate a process restart: forget every warm cell, but the file and its
        # savepoint on disk are untouched.
        crew_log.forget_session_folds(SESSION)

        resumed = crew_log.read_projection(SESSION, OTEL_USAGE_FOLD_NAME)
        assert resumed.value["turns"] == {"user": 299}

        # And the resumed value is BYTE IDENTICAL to a genuine cold fold of the same
        # file, which is the equality the savepoint exists to preserve.
        cold_state = _otel_usage_start()
        for entry in handle.iter_from(1, known=crew_log.KNOWN_TYPES):
            _otel_usage_step(cold_state, entry)
        assert resumed.value == _otel_usage_render(cold_state)

    def test_a_raw_agent_name_never_reaches_state_render_or_savepoint(self) -> None:
        """Design-review 1.4 (privacy): the raw ``session/opened.agent`` is folded to the
        closed ``agent_kind`` in ``step`` and only the enum is kept, so a distinctive
        agent string appears nowhere this fold writes -- not its live state, not its
        render, and not the savepoint persisted to disk.

        The same holds for the opening entry's ``slot``, which the step reads for the
        crewmate shape: the real append path below opens with a distinctive member slug
        in that field, and the slug has to be as absent from the savepoint as the agent
        name is.
        """
        unique = "zzq-private-agent-name-7f3a2b9c"
        unique_slot = "member-zzq-private-slug-7f3a2b9c"

        # Live state and render, driven directly.
        state = _otel_usage_start()
        _otel_usage_step(state, _entry(1, SESSION_OPENED_TYPE, _opened(unique)))
        assert unique not in repr(state)
        assert unique not in repr(_otel_usage_render(state))
        assert unique not in repr(_otel_usage_copy(state))
        # An unowned third-party name folds to ``other``.
        assert state["opened_agent_kind"] == "other"

        # The real append path and the on-disk savepoint.
        handle = CrewLog.create(lg.KIND_SESSION, SESSION, owner="raymond", agent=unique)
        opened = handle.append(
            "session/opened",
            {
                "agent": unique,
                "slot": unique_slot,
                "model": "",
                "cwd": "",
                "owner": "raymond",
                "resumed": False,
            },
            src=GATEWAY,
        )
        self._wake(SESSION, "session/opened", int(opened.seq))
        for turn in range(1, 300):  # cross the savepoint threshold
            done = handle.append(
                "turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY
            )
            self._wake(SESSION, "turn/started", int(done.seq))

        rendered = crew_log.read_projection(SESSION, OTEL_USAGE_FOLD_NAME).value
        # The slot key carried the member shape, so the closed answer is ``crewmate``
        # -- and neither raw string is in the render that produced it.
        assert rendered["opened_agent_kind"] == "crewmate"
        assert unique not in repr(rendered)
        assert unique_slot not in repr(rendered)

        on_disk = savepoints.load(handle, [OTEL_USAGE_FOLD_NAME])
        assert on_disk is not None and OTEL_USAGE_FOLD_NAME in on_disk.checkpoints
        assert unique not in repr(on_disk.checkpoints[OTEL_USAGE_FOLD_NAME])
        assert unique_slot not in repr(on_disk.checkpoints[OTEL_USAGE_FOLD_NAME])


class TestTheBootKickRunsOffTheLoop:
    """``start_dashboard`` installs the subscriber through a tracked task, off the loop.

    On an opted-in host the first ``get_recorder()`` the install reaches builds the
    recorder (SDK import plus the install-id write), so the install must run on a worker
    thread rather than on the event loop between the bind and the ready publish.
    """

    def test_the_install_runs_on_a_worker_thread_as_a_tracked_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import threading
        from types import SimpleNamespace

        # Through the composed ``server`` facade: the owner module's functions run on
        # its globals (``logger`` among them), bound only once ``server`` is imported.
        from kiro_crew.dashboard import server

        ran_on: list[int] = []

        def _fake_install() -> bool:
            ran_on.append(threading.get_ident())
            return True

        monkeypatch.setattr(export, "install_otel_crew_log_subscriber", _fake_install)
        state = SimpleNamespace(_background_tasks=set())

        async def _drive() -> int:
            loop_thread = threading.get_ident()
            server._kick_otel_crew_log_subscriber(state)  # type: ignore[arg-type]
            # The kick returns without running the install: nothing is awaited inline.
            assert ran_on == []
            assert len(state._background_tasks) == 1
            await asyncio.gather(*state._background_tasks)
            return loop_thread

        loop_thread = asyncio.run(_drive())
        assert len(ran_on) == 1
        assert ran_on[0] != loop_thread
        assert state._background_tasks == set()

    def test_an_install_failure_is_logged_and_does_not_escape(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import asyncio
        from types import SimpleNamespace

        # Through the composed ``server`` facade: the owner module's functions run on
        # its globals (``logger`` among them), bound only once ``server`` is imported.
        from kiro_crew.dashboard import server

        def _boom() -> bool:
            raise RuntimeError("install failed")

        monkeypatch.setattr(export, "install_otel_crew_log_subscriber", _boom)
        state = SimpleNamespace(_background_tasks=set())

        async def _drive() -> None:
            server._kick_otel_crew_log_subscriber(state)  # type: ignore[arg-type]
            await asyncio.gather(*state._background_tasks)

        with caplog.at_level("WARNING"):
            asyncio.run(_drive())
        assert "OTel crew-log subscriber install failed" in caplog.text
