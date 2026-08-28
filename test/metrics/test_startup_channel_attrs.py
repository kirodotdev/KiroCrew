"""Channel + construction-path dimensions on the session-startup telemetry.

A startup duration with no conversation-source and no path dimension cannot be
attributed: it says a rebuild took N ms without saying who paid for it or whether
``session/load`` or ``session/new`` ran. These tests pin the dimensions that make
the attribution a group-by, and pin their value sets closed so a future edit
cannot turn a metric attribute into an unbounded label.
"""

from unittest.mock import patch

from kiro_crew.messaging.link import TELEMETRY_CHANNELS, telemetry_channel_of
from kiro_crew.session import POOL_DECISIONS


class _Rec:
    """Recorder stand-in capturing histogram + counter attributes."""

    def __init__(self) -> None:
        self.hist: list = []
        self.counters: list = []

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.hist.append((name, dict(attrs or {})))

    def counter(self, name, value=1, *, unit="1", attrs=None, **kwargs) -> None:
        self.counters.append((name, dict(attrs or {})))


class TestTelemetryChannelOf:
    """Classification of a session key into a bounded channel label."""

    def test_channel_namespaces_map_to_themselves(self):
        assert telemetry_channel_of("telegram:123:456") == "telegram"
        assert telemetry_channel_of("slack:1785370133.085469") == "slack"
        assert telemetry_channel_of("discord:99") == "discord"

    def test_persisted_filename_stem_form_is_accepted(self):
        """history._safe_key folds ':' to '_', so both spellings must classify."""
        assert telemetry_channel_of("telegram_123") == "telegram"

    def test_local_surfaces_get_their_own_labels(self):
        assert telemetry_channel_of("dashboard:chat-60-1785802461") == "dashboard"
        assert telemetry_channel_of("cron:job-1") == "cron"
        assert telemetry_channel_of("subagent:abc123") == "subagent"
        assert telemetry_channel_of("taskrunner:spec") == "taskrunner"

    def test_singleton_sessions(self):
        assert telemetry_channel_of("_bg") == "background"
        assert telemetry_channel_of("_hb") == "heartbeat"

    def test_missing_key_is_unknown(self):
        assert telemetry_channel_of(None) == "unknown"
        assert telemetry_channel_of("") == "unknown"

    def test_unrecognised_key_never_leaks_into_the_label(self):
        """Cardinality guard: the key itself must never become the label."""
        weird = "some-unmodelled-prefix:0xdeadbeef"
        assert telemetry_channel_of(weird) == "other"
        assert weird not in TELEMETRY_CHANNELS

    def test_every_return_value_is_in_the_closed_set(self):
        keys = [
            "telegram:1",
            "slack:1",
            "dashboard:chat-1",
            "cron:c",
            "subagent:s",
            "channel:x",
            "_bg",
            "_hb",
            "",
            "nonsense:1",
        ]
        assert all(telemetry_channel_of(k) in TELEMETRY_CHANNELS for k in keys)


class TestStartupAttrs:
    """The kiro startup emit carries channel + resumed on every datapoint."""

    def _emit(self, *, session_key=None, meta=None, outcome="ready"):
        from kiro_crew.providers.acp import AcpProvider

        rec = _Rec()
        provider = AcpProvider.__new__(AcpProvider)  # emitter only, no spawn
        if session_key is not None:
            client = type("_C", (), {"_session_key": session_key})()
            provider._client = client
        phases = {"spawn_init": 1400.0, "session_load": 2100.0}
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
            provider._emit_kiro_startup_metric(0.0, phases, outcome, meta)
        return rec

    def test_channel_is_attached_to_every_datapoint(self):
        rec = self._emit(session_key="telegram:42:1")
        assert rec.hist, "startup histogram must be emitted"
        assert all(a["channel"] == "telegram" for _, a in rec.hist)

    def test_dashboard_and_telegram_are_distinguishable(self):
        tg = self._emit(session_key="telegram:42:1")
        dash = self._emit(session_key="dashboard:chat-7-1785802461")
        assert {a["channel"] for _, a in tg.hist} == {"telegram"}
        assert {a["channel"] for _, a in dash.hist} == {"dashboard"}

    def test_absent_client_does_not_suppress_the_emit(self):
        """The emitter runs in a finally, sometimes before the client exists."""
        rec = self._emit()
        assert len(rec.hist) == 3  # total + 2 phases
        assert all(a["channel"] == "unknown" for _, a in rec.hist)

    def test_resumed_defaults_false_without_meta(self):
        rec = self._emit(session_key="dashboard:chat-1")
        assert all(a["resumed"] is False for _, a in rec.hist)

    def test_resumed_true_propagates(self):
        rec = self._emit(session_key="telegram:1", meta={"resumed": True})
        assert all(a["resumed"] is True for _, a in rec.hist)

    def test_session_load_phase_is_carried_through(self):
        """The resume cost must be its own phase, not folded into session_new."""
        rec = self._emit(session_key="telegram:1", meta={"resumed": True})
        assert "session_load" in {a["phase"] for _, a in rec.hist}

    def test_resume_outcome_counter_emitted_when_present(self):
        rec = self._emit(
            session_key="telegram:1",
            meta={"resumed": False, "resume_outcome": "fallback_replay"},
        )
        assert (
            "kirocrew.session.resume.outcome",
            {
                "outcome": "fallback_replay",
                "channel": "telegram",
            },
        ) in rec.counters

    def test_no_resume_counter_on_a_fresh_session(self):
        rec = self._emit(session_key="dashboard:chat-1")
        assert rec.counters == []

    def test_attribute_set_is_pinned(self):
        """Cardinality guard.

        Per-phase datapoints carry exactly these keys. Crossing another
        dimension in here multiplies every phase series, so a new dimension
        belongs on a single phase or on its own counter instead.
        """
        rec = self._emit(session_key="telegram:1", meta={"resumed": True})
        for _, attrs in rec.hist:
            assert set(attrs) == {
                "backend",
                "outcome",
                "spawned",
                "channel",
                "resumed",
                "phase",
            }


class TestChannelSurvivesClientSwap:
    """The channel must come from the key captured BEFORE startup runs.

    A successful cold start replaces ``self._client`` with an
    ``AcpSessionProvider`` whose ``_session_key`` starts empty, and the metric is
    emitted from a ``finally`` that runs after that swap. Reading the live client
    there would file every successful cold start under ``unknown`` — the exact
    population this instrument exists to measure.
    """

    def _emit_after_swap(self, original_key, meta):
        from kiro_crew.providers.acp import AcpProvider

        rec = _Rec()
        provider = AcpProvider.__new__(AcpProvider)
        # Stand in for the post-startup provider: key present but empty.
        provider._client = type("_Swapped", (), {"_session_key": ""})()
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=rec):
            provider._emit_kiro_startup_metric(0.0, {"spawn_init": 1.0}, "ready", meta)
        return rec

    def test_captured_key_wins_over_the_swapped_client(self):
        rec = self._emit_after_swap("telegram:9:1", {"session_key": "telegram:9:1"})
        assert {a["channel"] for _, a in rec.hist} == {"telegram"}

    def test_without_the_capture_it_would_degrade_to_unknown(self):
        """Pins WHY the capture exists: the swapped client alone yields unknown."""
        rec = self._emit_after_swap("telegram:9:1", {})
        assert {a["channel"] for _, a in rec.hist} == {"unknown"}


class TestPrefixDrift:
    """`_TELEMETRY_LOCAL_PREFIXES` mirrors the prefixes SessionManager mints.

    It is a hand-maintained list, so a new session-key prefix added to session.py
    would silently fold into `other` and quietly degrade the dashboard. This
    cross-check fails at PR time instead.
    """

    def test_every_session_manager_prefix_has_a_label(self):
        from kiro_crew import session as session_mod
        from kiro_crew.messaging.link import telemetry_channel_of

        minted = [
            session_mod._SUBAGENT_PREFIX,
            session_mod._CHANNEL_PREFIX,
            session_mod._SIDE_PREFIX,
            *[p for p in session_mod._STATELESS_PREFIXES],
        ]
        unlabelled = [p for p in minted if telemetry_channel_of(f"{p}x") == "other"]
        assert not unlabelled, (
            "session.py mints prefixes with no telemetry label; add them to "
            f"_TELEMETRY_LOCAL_PREFIXES: {unlabelled}"
        )

    def test_singleton_keys_have_labels(self):
        from kiro_crew import session as session_mod
        from kiro_crew.messaging.link import telemetry_channel_of

        for key in (session_mod.BACKGROUND_KEY, session_mod.HEARTBEAT_KEY):
            assert telemetry_channel_of(key) != "other"


class TestPoolDecisions:
    """The warm-pool decision counter and its closed outcome set."""

    def test_bypass_resume_is_a_modelled_outcome(self):
        """The reason a resumable session can never use the pool."""
        assert "bypass_resume" in POOL_DECISIONS

    def test_every_gate_has_a_reason(self):
        assert {
            "hit",
            "miss_empty",
            "bypass_resume",
            "bypass_stateless",
            "bypass_cwd",
            "bypass_env",
            "bypass_backend",
            "disabled",
        } <= POOL_DECISIONS

    def test_unmodelled_decision_folds_to_other(self):
        from kiro_crew.session import SessionManager

        mgr = SessionManager.__new__(SessionManager)
        rec = _Rec()
        # session.py binds get_recorder at module import, so patch it there.
        with patch("kiro_crew.session.get_recorder", return_value=rec):
            mgr._record_pool_decision("something_new", "telegram:1")
        assert rec.counters == [
            (
                "kirocrew.session.pool.decision",
                {"outcome": "other", "channel": "telegram"},
            )
        ]

    def test_decision_is_tagged_with_the_channel(self):
        from kiro_crew.session import SessionManager

        mgr = SessionManager.__new__(SessionManager)
        rec = _Rec()
        with patch("kiro_crew.session.get_recorder", return_value=rec):
            mgr._record_pool_decision("bypass_resume", "telegram:99:1")
            mgr._record_pool_decision("hit", "dashboard:chat-3")
        assert [a["channel"] for _, a in rec.counters] == ["telegram", "dashboard"]
        assert [a["outcome"] for _, a in rec.counters] == ["bypass_resume", "hit"]
