"""``decide`` / ``is_enabled`` / ``timeout_secs``: the four refusals and the call.

The load-bearing test in this file is
:class:`TestDisabledPerformsNoAwait`: it is the reason this seam is safe to place
in a hot path, and it is asserted by making the implementation fail the test if it
is entered at all, rather than by timing anything.

The second load-bearing group is :class:`TestEnablingIsExplicit`. Consent is the
switch that lets conversation state leave the machine and it lives on the KEYSTONE
``decisions_consent.json``, never in ``config.json`` -- so the tests here pin that
only a literal ``true`` on the keystone opens it, and that nothing an operator or
an agent can write into ``config.json`` (``enabled``, the earlier
``preview``/``points``/``arm`` spelling) stands in for it.

:class:`TestGovernanceWithdrawsTheSeam` pins the layer ABOVE that switch: a fleet
that pins ``capabilities.decisions`` off makes an already-consented keystone inert,
at the one keystone read every path funnels through.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from kiro_crew import credential_patterns as _cred
from kiro_crew.config.sections import (
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionProviderConfig,
    DecisionsConfig,
)
from kiro_crew.decisions import consent as consent_mod
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.gate import DECISION_POINT_NAMES, decide, in_bucket, is_enabled
from kiro_crew.decisions.types import Answer, Choice

# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------

#: AWS key-id samples, assembled from the prefix list in
#: ``credential_patterns`` rather than written out. A contiguous key-shaped
#: literal is refused by the repo's own secret scanners -- correctly, since
#: neither the content scan nor Semgrep can tell a test vector from a real
#: leak -- and deriving the samples means a prefix added there is exercised
#: here without this list being edited to match.
_AWS_KEY_BODY = "A2B3C4D5E6F7G8H9"  # 16 of [A-Z0-9], per AWS_KEY_ID_BODY
_AWS_KEY_SAMPLES = tuple(prefix + _AWS_KEY_BODY for prefix in _cred.AWS_KEY_ID_PREFIXES.split("|"))

#: A destination only the URL scanner objects to: the credential scanners clear it
#: (probed above the assertion in ``test_the_two_scanners_are_distinguished``), so
#: it is what separates the two scrub categories rather than doubling the first.
_EXFIL_URL = "see https://collector.example.invalid/x?sess" + "ion=" + "b" * 40

POINT = "skills.select"
QUESTIONS = [Choice(id="verdict", prompt="Which skill?", options=["NONE", "DUP"])]


def _config(
    *, bucket: int = 100, timeout_ms: int = 1000, endpoint: str = "", model: str | None = None
):
    """A config object shaped like the one ``decide`` reads off the live snapshot.

    A real ``KiroCrewConfig`` would work too, but constructing one loads the whole
    config module for a two-field read; the gate only ever touches ``.decisions``,
    so a stand-in with that attribute is the honest surface. Consent is NOT here:
    it is the keystone, patched by the ``consent`` fixture. An empty *endpoint*
    keeps the dataclass default, which is the one the fixture consents to; a
    ``None`` *model* keeps the dataclass default too.
    """
    kwargs: dict = {"timeout_ms": timeout_ms}
    if endpoint:
        kwargs["endpoint"] = endpoint
    if model is not None:
        kwargs["model"] = model
    provider = DecisionProviderConfig(**kwargs)
    return SimpleNamespace(decisions=DecisionsConfig(bucket=bucket, provider=provider))


@pytest.fixture(autouse=True)
def consent(tmp_path, monkeypatch):
    """A real keystone under *tmp_path*, consented by default; returns a setter.

    Real file, not a patched predicate: the gate's read path (fail-soft, strict
    identity) is part of what these tests pin.
    """
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)

    def _set(value, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT):
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({"enabled": value, "endpoint": endpoint}), encoding="utf-8")

    _set(True)
    return _set


class _RecordingOracle:
    """Answers every question with a fixed value and records every call."""

    def __init__(self, value: str = "DUP") -> None:
        self.value = value
        self.calls: list[tuple] = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        return {q.id: Answer(id=q.id, value=self.value, p=0.87, confidence=0.82) for q in questions}


class _ExplodingOracle:
    """Fails the TEST if it is ever entered.

    Not ``raise`` -- a raise would be caught by the call's own guard and logged as
    an error, which is a pass-looking outcome. ``pytest.fail`` inside a coroutine
    that the gate awaits surfaces as a test failure.
    """

    def __init__(self) -> None:
        self.entered = False

    async def ask(self, state, questions):  # pragma: no cover - must never run
        self.entered = True
        pytest.fail("the provider was entered on a path that must perform no await")


class _RaisingOracle:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def ask(self, state, questions):
        raise self.exc


class _SlowOracle:
    """Sleeps *secs* before answering."""

    def __init__(self, secs: float) -> None:
        self.secs = secs
        self.finished = False

    async def ask(self, state, questions):
        await asyncio.sleep(self.secs)
        self.finished = True
        return {q.id: Answer(id=q.id, value="DUP", p=0.5) for q in questions}


@pytest.fixture
def install_impl(monkeypatch):
    """Replace the one implementation ``decide`` constructs.

    ``decide`` imports ``JevOracle`` inside its own body (the import stays off a
    hot path's import graph), so the patch lands on the module the import
    resolves against, not on a name in ``gate``.
    """
    import kiro_crew.decisions.impl_jev as impl_mod

    def _install(oracle):
        monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: oracle)
        return oracle

    return _install


@pytest.fixture
def log_home(tmp_path, monkeypatch):
    """Isolate disk writes and read the real rows built for the writer, in order.

    These assertions pin row CONTENT, not best-effort delivery. ``asyncio.run``
    joins a started append, but cannot recover a job cancelled before its executor
    starts it, or a row dropped when the pinned open spends the append deadline.
    Waiting for the file would wait for a row that may never land. Observe the
    real ``build_row`` result on the loop, before either budget can drop it; keep
    both production budgets and the append itself intact. Disk landing belongs
    to ``test_decisions_log.TestAppend`` and ``test_platform_log_append``; append
    ordering and off-loop execution keep their own assertions below.
    """
    directory = tmp_path / "decisions"
    monkeypatch.setattr(log_mod, "log_dir", lambda: directory)
    built_rows: list[dict] = []
    real_build_row = log_mod.build_row

    def _record_row(**kwargs):
        row = real_build_row(**kwargs)
        built_rows.append(row)
        return row

    monkeypatch.setattr(log_mod, "build_row", _record_row)

    def _rows():
        return list(built_rows)

    return _rows


# ---------------------------------------------------------------------------
# Gate 1 -- enabled
# ---------------------------------------------------------------------------


class TestDisabledPerformsNoAwait:
    """No consent must cost nothing measurable, not merely return None."""

    @pytest.mark.parametrize("state", [False, None], ids=["enabled-false", "no-keystone"])
    def test_refuses_without_entering_the_provider(self, install_impl, log_home, consent, state):
        consent(state)
        oracle = install_impl(_ExplodingOracle())
        assert asyncio.run(decide(POINT, "hello", QUESTIONS, config=_config())) is None
        assert oracle.entered is False
        assert log_home() == [], "a refused decision must not write a log row"

    def test_the_only_await_before_the_refusal_is_the_keystone_read(
        self, install_impl, consent, monkeypatch
    ):
        """Driving the coroutine by hand: one suspension (the off-loop keystone
        read), then ``return None`` -- no provider, no log, no second await."""
        consent(False)
        install_impl(_ExplodingOracle())
        reads = []

        async def _read(fn, *args):
            reads.append(fn)
            return fn(*args)

        monkeypatch.setattr(gate_mod.asyncio, "to_thread", _read)
        assert asyncio.run(decide(POINT, "hello", QUESTIONS, config=_config())) is None
        assert reads == [gate_mod._consented_for]

    def test_a_config_without_the_section_is_off(self, install_impl, log_home):
        install_impl(_ExplodingOracle())
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=SimpleNamespace())) is None
        assert is_enabled(POINT, config=SimpleNamespace()) is False
        assert log_home() == []

    def test_no_snapshot_is_off(self, install_impl, log_home, monkeypatch):
        """An unprimed live watcher fails CLOSED rather than reading from disk."""
        install_impl(_ExplodingOracle())
        monkeypatch.setattr(gate_mod, "_snapshot", lambda: None)
        assert asyncio.run(decide(POINT, "hi", QUESTIONS)) is None
        assert is_enabled(POINT) is False
        assert log_home() == []

    def test_a_raising_config_read_reads_as_off(self, install_impl, monkeypatch):
        """Neither entry point may raise into the turn it was called from."""
        install_impl(_ExplodingOracle())

        def _boom():
            raise RuntimeError("snapshot exploded")

        monkeypatch.setattr(gate_mod, "_snapshot", _boom)
        assert is_enabled(POINT) is False
        assert asyncio.run(decide(POINT, "hi", QUESTIONS)) is None
        assert gate_mod.timeout_secs() > 0.0

    def test_a_config_whose_attributes_raise_reads_as_off(self, install_impl, log_home):
        """``config`` is an arbitrary object; an exploding read is not a turn failure."""
        oracle = install_impl(_ExplodingOracle())

        class _Hostile:
            @property
            def decisions(self):
                raise RuntimeError("attribute exploded")

        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_Hostile())) is None
        assert is_enabled(POINT, config=_Hostile()) is False
        assert oracle.entered is False
        assert log_home() == []


class TestEnablingIsExplicit:
    """Only a literal ``true`` ON THE KEYSTONE opens the seam. Nothing else may."""

    @pytest.mark.parametrize("raw", [1, "true", "yes", "1", [1], {"a": 1}, 0.5])
    def test_a_truthy_non_bool_on_the_keystone_does_not_enable(
        self, install_impl, log_home, consent, raw
    ):
        consent(raw)
        oracle = install_impl(_ExplodingOracle())
        assert consent_mod.is_enabled() is False
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        assert oracle.entered is False
        assert log_home() == []

    @pytest.mark.parametrize("raw", ["not json", "[1]", "", "null"])
    def test_a_corrupt_keystone_reads_as_off(self, install_impl, consent, tmp_path, raw):
        (tmp_path / "decisions_consent.json").write_text(raw, encoding="utf-8")
        oracle = install_impl(_ExplodingOracle())
        assert consent_mod.is_enabled() is False
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        assert oracle.entered is False

    def test_consent_is_bound_to_the_endpoint_it_was_given_for(
        self, install_impl, log_home, consent, monkeypatch, caplog
    ):
        """The other half of the keystone: WHERE lives in config.json too, so a
        redirected `provider.endpoint` is a refusal until the owner re-consents."""
        import logging

        monkeypatch.setattr(gate_mod, "_unconsented_warned", set())
        oracle = install_impl(_ExplodingOracle())
        moved = _config(endpoint="https://attacker.example/v1/systemone")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.decisions.gate"):
            assert is_enabled(POINT, config=moved) is False
            assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=moved)) is None
            assert is_enabled(POINT, config=moved) is False
        assert oracle.entered is False
        assert log_home() == []
        warnings = [r for r in caplog.records if "different provider endpoint" in r.getMessage()]
        assert len(warnings) == 1, "said once per address, not once per message"
        # Re-consenting for the new address opens it; the default is now refused.
        consent(True, "https://attacker.example/v1/systemone")
        assert is_enabled(POINT, config=moved) is True
        assert is_enabled(POINT, config=_config()) is False

    def test_one_snapshot_serves_the_whole_decision(self, install_impl, log_home, monkeypatch):
        """Consent is held against the endpoint the REQUEST goes to: both come from
        one snapshot read at entry, so a config swap during the keystone await
        cannot pass consent on one address and send to another."""
        oracle = install_impl(_RecordingOracle())
        reads: list[int] = []
        snapshot = _config()

        def _one_snapshot():
            reads.append(1)
            return snapshot

        monkeypatch.setattr(gate_mod, "_snapshot", _one_snapshot)
        assert asyncio.run(decide(POINT, "hello", QUESTIONS)) is not None
        assert len(reads) == 1, "decide read the live snapshot more than once"
        assert oracle.calls, "the consented request was sent"
        reads.clear()
        assert is_enabled(POINT) is True
        assert len(reads) == 1, "is_enabled read the live snapshot more than once"

    def test_a_keystone_with_the_flag_but_no_endpoint_permits_nothing(
        self, install_impl, consent, tmp_path
    ):
        (tmp_path / "decisions_consent.json").write_text('{"enabled": true}', encoding="utf-8")
        oracle = install_impl(_ExplodingOracle())
        assert is_enabled(POINT, config=_config()) is False
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        assert oracle.entered is False

    def test_enabled_in_config_json_is_not_consent(self, install_impl, log_home, consent):
        """The whole point of the keystone: config.json is agent-writable, so a
        ``decisions.enabled: true`` there -- however it got there -- opens nothing."""
        consent(None)
        oracle = install_impl(_ExplodingOracle())
        cfg = SimpleNamespace(decisions=DecisionsConfig.from_raw({"enabled": True, "bucket": 100}))
        assert not hasattr(cfg.decisions, "enabled")
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=cfg)) is None
        assert is_enabled(POINT, config=cfg) is False
        assert oracle.entered is False
        assert log_home() == []

    @pytest.mark.parametrize(
        "legacy",
        [
            {"preview": True},
            {"preview": True, "points": {"skills.select": {"arm": "live"}}},
            {"preview": True, "points": {"skills.select": {"arm": "shadow", "bucket": 100}}},
            {"points": {"skills.select": {"arm": "live", "impl": "jev"}}},
        ],
    )
    def test_the_earlier_preview_and_arm_spelling_does_not_enable(
        self, install_impl, log_home, consent, legacy
    ):
        """A config written against the previous contract grants nothing.

        The values are real operator intent, but they were set against a
        different switch. Inferring consent from an arm would turn the seam
        on -- and start sending state -- from a value nobody wrote for it.
        """
        consent(None)
        oracle = install_impl(_ExplodingOracle())
        cfg = SimpleNamespace(decisions=DecisionsConfig.from_raw(legacy))
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=cfg)) is None
        assert oracle.entered is False
        assert log_home() == []


# ---------------------------------------------------------------------------
# Above the owner's switch: the fleet's ceiling
# ---------------------------------------------------------------------------


class TestGovernanceWithdrawsTheSeam:
    """``capabilities.decisions``: the FLEET's switch above the owner's keystone.

    The keystone is the owner's consent; this row is whether the machine may run
    the seam at all. Enforced HERE -- at ``_consented_for``, the one keystone read
    every ``decide`` and ``is_enabled`` path funnels through -- so a keystone
    written before the pin is inert rather than carried over. Without this half, a
    fleet that pinned the row would still send from any machine already consented.
    """

    @pytest.fixture(autouse=True)
    def quiet(self, monkeypatch):
        """The once-per-process warning flag, reset so each case can observe it."""
        monkeypatch.setattr(gate_mod, "_capability_denied_warned", False)

    @staticmethod
    def _deny(monkeypatch, denied: bool) -> list[str]:
        """Stand in for the governed probe; returns the SURFACE KEYS it was asked about.

        Patched at the module the gate imports it FROM, because the import happens
        inside the function -- patching a name on ``gate_mod`` would bind nothing. The
        call log records the key rather than a tally, so a caller that stops passing
        the turn's own surface is visible here instead of merely counted.
        """
        from kiro_crew.decisions import capability

        calls: list[str] = []

        def _probe(surface_key: str = capability.DASHBOARD_SURFACE_KEY) -> bool:
            calls.append(surface_key)
            return denied

        monkeypatch.setattr(capability, "is_decisions_denied", _probe)
        return calls

    def test_a_consented_keystone_is_inert_under_a_denial(
        self, install_impl, log_home, consent, monkeypatch, caplog
    ):
        import logging

        calls = self._deny(monkeypatch, True)
        oracle = install_impl(_ExplodingOracle())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.decisions.gate"):
            assert is_enabled(POINT, config=_config()) is False
            assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
            assert is_enabled(POINT, config=_config()) is False
        assert oracle.entered is False, "a withdrawn seam must not reach the provider"
        assert log_home() == [], "a refused decision writes no row"
        assert len(calls) == 3, "every path through the keystone read asks the ceiling"
        withdrawn = [r for r in caplog.records if "withdrawn by governance" in r.getMessage()]
        assert len(withdrawn) == 1, "said once per process, not once per message"

    def test_a_permitting_ceiling_changes_nothing(self, install_impl, consent, monkeypatch):
        """The control: the same consented keystone still sends when nothing pins it."""
        calls = self._deny(monkeypatch, False)
        oracle = install_impl(_RecordingOracle())
        assert is_enabled(POINT, config=_config()) is True
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is not None
        assert oracle.calls, "the consented request was sent"
        assert calls, "the ceiling was consulted rather than skipped"

    def test_an_unconsented_install_never_asks_the_ceiling(
        self, install_impl, consent, monkeypatch
    ):
        """No consent must keep costing nothing: the governed probe writes an
        audited SEL row per evaluation, and the default install would otherwise pay
        one on every message for an answer that cannot change the refusal."""
        consent(None)
        calls = self._deny(monkeypatch, False)
        install_impl(_ExplodingOracle())
        assert is_enabled(POINT, config=_config()) is False
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        assert calls == []

    def test_the_denial_still_costs_only_the_one_keystone_hop(
        self, install_impl, consent, monkeypatch
    ):
        """The ceiling read rides INSIDE the existing off-loop keystone hop, so the
        no-second-await property :class:`TestDisabledPerformsNoAwait` pins survives
        a denial as well as an absent keystone."""
        self._deny(monkeypatch, True)
        install_impl(_ExplodingOracle())
        reads = []

        async def _read(fn, *args):
            reads.append(fn)
            return fn(*args)

        monkeypatch.setattr(gate_mod.asyncio, "to_thread", _read)
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        assert reads == [gate_mod._consented_for]

    def test_a_profile_bound_to_the_turns_surface_denies_through_the_gate(
        self, install_impl, consent, monkeypatch, tmp_path
    ):
        """The ceiling is evaluated on the TURN's surface, not a pinned dashboard one.

        A profile binds on the surface inferred from the session key. The gate holds a
        trusted session key -- the runtime's own identity for the turn, the same value
        ``in_bucket`` hashes -- so a profile bound to that surface has to be consulted
        on the one path that actually sends. Pinning ``dashboard:ui`` here left such a
        profile unconsulted while a consented owner's turn sent message excerpts to the
        paid endpoint.

        The dashboard callers keep the pin, and this test asserts BOTH halves: the
        non-dashboard turn is denied, and the probe's default -- what the config route
        and the consent PUT use -- still permits. Without the second half the first
        would also pass under a blanket denial, which is the opposite defect.
        """
        from kiro_crew.decisions.capability import is_decisions_denied
        from kiro_crew.platform import context as pc
        from kiro_crew.platform import governance_profiles as gp
        from kiro_crew.platform.governance import parse_policy

        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "slack.json").write_text(
            json.dumps(
                {
                    "name": "slack-narrow",
                    "bind": {"type": "surface", "id": "slack"},
                    "capabilities": {"decisions": {"enabled": False}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
        gp.reset_store()

        class _Ctx:
            governance = parse_policy({"version": 1, "boot": {"fail_closed": True}})

        monkeypatch.setattr(pc, "current_context", lambda: _Ctx())
        try:
            oracle = install_impl(_ExplodingOracle())
            slack_key = "slack:C123"

            # The probe itself, at both surfaces: the control that proves the profile
            # is bound and that the default is NOT caught by it.
            assert is_decisions_denied(slack_key) is True
            assert is_decisions_denied() is False, (
                "the dashboard default must stay permitted, or this test would pass "
                "under a blanket denial instead of a surface-bound one"
            )

            # Through the gate, on the turn's own surface: refused, nothing sent.
            assert is_enabled(POINT, session_key=slack_key, config=_config()) is False
            assert (
                asyncio.run(decide(POINT, "hi", QUESTIONS, session_key=slack_key, config=_config()))
                is None
            )
            assert oracle.entered is False, "a surface-bound denial must not reach the provider"

            # A turn on a surface the profile does not bind still runs.
            assert is_enabled(POINT, session_key="dashboard:ui", config=_config()) is True
        finally:
            gp.reset_store()

    def test_an_unevaluable_ceiling_fails_closed(self, install_impl, consent, monkeypatch):
        """The probe itself is fail-closed; the gate must not re-open it by treating
        a raising probe as "no opinion"."""
        from kiro_crew.decisions import capability

        monkeypatch.setattr(
            capability,
            "vet_and_audit",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        oracle = install_impl(_ExplodingOracle())
        assert is_enabled(POINT, config=_config()) is False
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        assert oracle.entered is False


# ---------------------------------------------------------------------------
# Gate 2 -- the point name
# ---------------------------------------------------------------------------


class TestPointName:
    def test_the_shipped_vocabulary_is_one_point(self):
        assert DECISION_POINT_NAMES == ("skills.select",)

    @pytest.mark.parametrize("unknown", ["skills.dedupe", "cron.novelty", "", "skills.Select"])
    def test_an_unknown_point_is_refused_even_when_enabled(
        self, install_impl, log_home, unknown, caplog
    ):
        """An unknown name must not borrow the section's switch."""
        oracle = install_impl(_ExplodingOracle())
        with caplog.at_level("WARNING"):
            assert asyncio.run(decide(unknown, "hi", QUESTIONS, config=_config())) is None
        assert is_enabled(unknown, config=_config()) is False
        assert oracle.entered is False
        assert log_home() == []
        assert "unknown point" in caplog.text

    def test_every_shipped_name_is_admitted(self, install_impl):
        install_impl(_RecordingOracle())
        for name in DECISION_POINT_NAMES:
            assert is_enabled(name, config=_config()) is True


# ---------------------------------------------------------------------------
# Gate 3 -- the sampling bucket
# ---------------------------------------------------------------------------


class TestBucket:
    def test_bucket_zero_admits_nothing(self):
        assert not any(in_bucket(f"s{i}", 0) for i in range(200))

    def test_bucket_hundred_admits_everything(self):
        assert all(in_bucket(f"s{i}", 100) for i in range(200))

    def test_a_key_is_consistently_in_or_out(self):
        assert {in_bucket("stable-key", 50) for _ in range(20)} in ({True}, {False})

    def test_bucket_is_roughly_the_percentage_it_claims(self):
        hits = sum(in_bucket(f"session-{i}", 25) for i in range(4000))
        assert 800 < hits < 1200, f"25% of 4000 should be ~1000, got {hits}"

    @pytest.mark.parametrize("bucket", [-5, 500, "nonsense", None])
    def test_an_unusable_bucket_is_clamped_not_treated_as_off(self, bucket):
        """A typo must not become a second, undocumented way to disable the seam."""
        admitted = in_bucket("any-key", bucket)  # type: ignore[arg-type]
        assert admitted is (False if bucket == -5 else True)

    def test_bucket_zero_refuses_before_the_provider(self, install_impl, log_home):
        oracle = install_impl(_ExplodingOracle())
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config(bucket=0))) is None
        assert oracle.entered is False
        assert log_home() == []

    def test_the_digest_matches_the_logged_session_field(self, install_impl, log_home):
        """A row's ``session`` is enough to re-derive why it was sampled."""
        install_impl(_RecordingOracle())
        key = "session-abc"
        asyncio.run(decide(POINT, "hi", QUESTIONS, session_key=key, config=_config()))
        assert log_home()[0]["session"] == log_mod.session_digest(key)


# ---------------------------------------------------------------------------
# Gate 4 -- the scrub
# ---------------------------------------------------------------------------


class TestScrub:
    @pytest.mark.parametrize(
        "payload",
        [
            *[f"here is my key {sample}" for sample in _AWS_KEY_SAMPLES],
            "vendor key sk-" + "a" * 32,
            _EXFIL_URL,
        ],
    )
    def test_a_credential_in_the_state_refuses_before_the_network(
        self, install_impl, log_home, payload
    ):
        oracle = install_impl(_ExplodingOracle())
        assert asyncio.run(decide(POINT, payload, QUESTIONS, config=_config())) is None
        assert oracle.entered is False
        row = log_home()[0]
        assert row["scrubbed"] is True
        assert row["error"] in gate_mod.SCRUB_ERRORS
        assert row["answers"] is None

    def test_the_two_scanners_are_distinguished_in_the_row(self, install_impl, log_home):
        """Which scanner refused is the finding; one category for both would lose it."""
        install_impl(_ExplodingOracle())
        asyncio.run(decide(POINT, f"key {_AWS_KEY_SAMPLES[0]}", QUESTIONS, config=_config()))
        asyncio.run(decide(POINT, _EXFIL_URL, QUESTIONS, config=_config()))
        assert [row["error"] for row in log_home()] == [
            gate_mod.ERROR_SCRUBBED_CREDENTIAL,
            gate_mod.ERROR_SCRUBBED_URL,
        ]

    def test_a_credential_nested_in_a_dict_state_is_found(self, install_impl, log_home):
        oracle = install_impl(_ExplodingOracle())
        state = {"messages": [{"text": f"key {_AWS_KEY_SAMPLES[0]}"}]}
        assert asyncio.run(decide(POINT, state, QUESTIONS, config=_config())) is None
        assert oracle.entered is False
        assert log_home()[0]["scrubbed"] is True

    def test_a_credential_in_a_question_prompt_is_found(self, install_impl, log_home):
        """The rubric leaves the machine in the same request as the state."""
        oracle = install_impl(_ExplodingOracle())
        questions = [
            Choice(id="q", prompt=f"is {_AWS_KEY_SAMPLES[0]} the right key?", options=["yes", "no"])
        ]
        assert asyncio.run(decide(POINT, "clean state", questions, config=_config())) is None
        assert oracle.entered is False
        assert log_home()[0]["scrubbed"] is True

    @pytest.mark.parametrize(
        "model",
        ["jev latest", "a" * 65, "-x", "jev\nlatest", "AKIA" + "A" * 16],
        ids=["space", "too-long", "leading-dash", "newline", "aws-key-shaped"],
    )
    def test_a_provider_model_that_is_not_an_id_refuses_before_the_network(
        self, install_impl, log_home, model
    ):
        """``provider.model`` is agent-writable config that goes on the wire, so it
        is bounded to an identifier and scanned like the state. The AWS-shaped case
        passes the shape and is caught by the credential scan instead."""
        oracle = install_impl(_ExplodingOracle())
        assert asyncio.run(decide(POINT, "clean", QUESTIONS, config=_config(model=model))) is None
        assert oracle.entered is False
        row = log_home()[0]
        assert row["scrubbed"] is True
        assert row["error"] in (gate_mod.ERROR_SCRUBBED_MODEL, gate_mod.ERROR_SCRUBBED_CREDENTIAL)
        assert model not in json.dumps(log_home())

    def test_an_empty_provider_model_is_the_default_and_passes(self, install_impl):
        """The gate scrubs the id ``impl_jev`` would send, so "" is the default, not
        a refusal."""
        oracle = install_impl(_RecordingOracle())
        assert asyncio.run(decide(POINT, "clean", QUESTIONS, config=_config(model=""))) is not None
        assert len(oracle.calls) == 1

    def test_the_model_fence_is_a_scrub_category(self):
        assert gate_mod.ERROR_SCRUBBED_MODEL in gate_mod.SCRUB_ERRORS
        assert (
            gate_mod.scrub_reason("clean", QUESTIONS, model="x y") == gate_mod.ERROR_SCRUBBED_MODEL
        )
        assert gate_mod.scrub_reason("clean", QUESTIONS, model="jev-latest") is None

    def test_a_credential_in_a_choice_option_is_found(self, install_impl, log_home):
        oracle = install_impl(_ExplodingOracle())
        questions = [Choice(id="q", prompt="which?", options=["fine", _AWS_KEY_SAMPLES[0]])]
        assert asyncio.run(decide(POINT, "clean state", questions, config=_config())) is None
        assert oracle.entered is False
        assert log_home()[0]["scrubbed"] is True

    def test_a_scanner_that_itself_fails_refuses(self, install_impl, log_home, monkeypatch):
        """An external request cannot be cleared by a scan that did not complete."""
        oracle = install_impl(_ExplodingOracle())
        import kiro_crew.security.redaction as redaction_mod

        def _boom(_text):
            raise RuntimeError("scanner exploded")

        monkeypatch.setattr(redaction_mod, "redact_credentials", _boom)
        assert asyncio.run(decide(POINT, "clean state", QUESTIONS, config=_config())) is None
        assert oracle.entered is False
        assert log_home()[0]["error"] == gate_mod.ERROR_SCRUBBED_SCAN_FAILED

    def test_the_refusal_reason_never_quotes_what_it_found(self, install_impl, log_home):
        """The reason is a category: a quoted match would put the key in the log."""
        install_impl(_ExplodingOracle())
        sample = _AWS_KEY_SAMPLES[0]
        asyncio.run(decide(POINT, f"key {sample}", QUESTIONS, config=_config()))
        assert sample not in json.dumps(log_home())

    def test_the_state_itself_is_never_written_to_the_log(self, install_impl, log_home):
        install_impl(_RecordingOracle())
        private = "the user said something private about their salary"
        asyncio.run(decide(POINT, private, QUESTIONS, config=_config()))
        assert private not in json.dumps(log_home())

    def test_ordinary_state_passes_and_the_answers_reach_the_caller(self, install_impl, log_home):
        oracle = install_impl(_RecordingOracle())
        answers = asyncio.run(decide(POINT, "just words", QUESTIONS, config=_config()))
        assert answers is not None and answers["verdict"].value == "DUP"
        assert len(oracle.calls) == 1
        row = log_home()[0]
        assert row["scrubbed"] is False
        assert row["error"] is None
        assert row["answers"]["verdict"]["value"] == "DUP"

    def test_state_and_questions_pass_through_unchanged(self, install_impl):
        oracle = install_impl(_RecordingOracle())
        state = {"candidate": "x", "existing": ["a", "b"]}
        asyncio.run(decide(POINT, state, QUESTIONS, config=_config()))
        seen_state, seen_questions = oracle.calls[0]
        assert seen_state is state
        assert seen_questions is QUESTIONS


# ---------------------------------------------------------------------------
# The call: its budget, and its failures
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_a_slow_provider_returns_none_within_the_budget(self, install_impl, log_home):
        oracle = install_impl(_SlowOracle(5.0))
        result = asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config(timeout_ms=30)))
        assert result is None
        assert oracle.finished is False, "the call must be cancelled, not merely ignored"
        assert log_home()[0]["error"] == gate_mod.ERROR_TIMEOUT

    @pytest.mark.parametrize("timeout_ms", [0, -1])
    def test_a_nonpositive_timeout_is_floored_not_disabled(self, install_impl, timeout_ms):
        """A typo'd budget must look like a fast timeout, not a broken provider."""
        assert gate_mod.timeout_secs(config=_config(timeout_ms=timeout_ms)) > 0.0

    @pytest.mark.parametrize("raw", [None, "abc", float("inf"), float("nan")])
    def test_an_unusable_timeout_resolves_to_a_real_budget(self, raw):
        """``timeout_secs`` never returns something ``wait_for`` would reject."""
        cfg = SimpleNamespace(
            decisions=SimpleNamespace(bucket=100, provider=SimpleNamespace(timeout_ms=raw))
        )
        value = gate_mod.timeout_secs(config=cfg)
        assert isinstance(value, float) and value > 0.0 and value == value  # not NaN
        assert value != float("inf")

    def test_the_helper_and_the_gate_read_the_same_number(self, install_impl, log_home):
        """One budget, one place it comes from -- an outer wait cannot invent its own."""
        install_impl(_SlowOracle(5.0))
        cfg = _config(timeout_ms=40)
        assert gate_mod.timeout_secs(config=cfg) == pytest.approx(0.04)
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=cfg)) is None
        assert log_home()[0]["error"] == gate_mod.ERROR_TIMEOUT

    def test_no_config_at_all_still_yields_a_budget(self, monkeypatch):
        monkeypatch.setattr(gate_mod, "_snapshot", lambda: None)
        assert gate_mod.timeout_secs() > 0.0


class TestErrors:
    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("connection reset"),
            ValueError("response is not JSON"),
            OSError("network unreachable"),
        ],
    )
    def test_a_provider_failure_returns_none_and_logs_a_category(self, install_impl, log_home, exc):
        install_impl(_RaisingOracle(exc))
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        row = log_home()[0]
        assert row["error"] == gate_mod.ERROR_PROVIDER
        assert row["scrubbed"] is False

    def test_a_provider_message_never_reaches_the_row(self, install_impl, log_home):
        """A provider can quote the request back; the row is on disk, so: category only."""
        install_impl(_RaisingOracle(RuntimeError("rejected body: my-secret-payload")))
        asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert "my-secret-payload" not in json.dumps(log_home())

    def test_every_logged_error_is_one_of_the_named_identifiers(self, install_impl, log_home):
        """A row's ``error`` is a closed vocabulary, so a reader can group on it."""
        known = {
            gate_mod.ERROR_TIMEOUT,
            gate_mod.ERROR_PROVIDER,
            gate_mod.ERROR_INVALID_RESULT,
            *gate_mod.SCRUB_ERRORS,
        }
        install_impl(_RaisingOracle(RuntimeError("boom")))
        asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        install_impl(_SlowOracle(5.0))
        asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config(timeout_ms=20)))
        install_impl(_RecordingOracle())
        asyncio.run(decide(POINT, f"k {_AWS_KEY_SAMPLES[0]}", QUESTIONS, config=_config()))
        errors = [row["error"] for row in log_home()]
        assert len(errors) == 3 and all(err in known for err in errors)

    def test_an_empty_result_is_recorded_as_an_error_not_as_an_answer(self, install_impl, log_home):
        class _Empty:
            async def ask(self, state, questions):
                return {}

        install_impl(_Empty())
        assert asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config())) is None
        row = log_home()[0]
        assert row["error"] == gate_mod.ERROR_INVALID_RESULT
        assert row["answers"] is None

    @pytest.mark.parametrize(
        "question,answer",
        [
            # A Choice answered with a value outside its own option list.
            (Choice(id="q", prompt="?", options=["A", "B"]), Answer(id="q", value="C", p=0.9)),
            # A probability outside 0..1.
            (Choice(id="q", prompt="?", options=["A"]), Answer(id="q", value="A", p=1.4)),
            # A boolean is not a probability.
            (Choice(id="q", prompt="?", options=["A"]), Answer(id="q", value="A", p=True)),
            # An answer keyed to a question that was not asked.
            (Choice(id="q", prompt="?", options=["A"]), Answer(id="other", value="A", p=0.5)),
        ],
    )
    def test_an_out_of_domain_answer_is_recorded_as_an_error(
        self, install_impl, log_home, question, answer
    ):
        class _Fixed:
            async def ask(self, state, questions):
                return {question.id: answer}

        install_impl(_Fixed())
        assert asyncio.run(decide(POINT, "hi", [question], config=_config())) is None
        assert log_home()[0]["error"] == gate_mod.ERROR_INVALID_RESULT

    def test_cancellation_propagates_and_writes_no_row(self, install_impl, log_home):
        """A caller going away is not a provider failure."""
        install_impl(_RaisingOracle(asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert log_home() == []


class TestLoggingCannotCostTheResult:
    """The row is an observation. It may fail; the decision may not."""

    def test_a_broken_log_still_yields_the_answers(self, install_impl, monkeypatch):
        install_impl(_RecordingOracle())

        def _boom(row):
            raise OSError("read-only file system")

        # append() swallows its own errors, so patch it to raise and confirm the
        # gate is not relying on that -- decide must still return the answers.
        monkeypatch.setattr(log_mod, "append", _boom)
        answers = asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert answers is not None and answers["verdict"].value == "DUP"

    def test_an_unbuildable_row_still_yields_the_answers(self, install_impl, monkeypatch):
        """Row construction renders provider-supplied values; that must not escape."""
        install_impl(_RecordingOracle())

        def _boom(**_kwargs):
            raise RuntimeError("row construction exploded")

        monkeypatch.setattr(log_mod, "build_row", _boom)
        answers = asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert answers is not None

    def test_the_row_is_written_after_the_answers_are_in_hand(self, install_impl, log_home):
        """Not inside the provider budget: a log write must not spend the deadline."""
        install_impl(_RecordingOracle())
        order: list[str] = []
        real_append = log_mod.append

        def _tracking(row):
            order.append("append")
            real_append(row)

        log_mod_append = log_mod.append
        try:
            log_mod.append = _tracking  # type: ignore[assignment]
            answers = asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        finally:
            log_mod.append = log_mod_append  # type: ignore[assignment]
        assert answers is not None
        assert order == ["append"]
        assert log_home()[0]["error"] is None

    def test_a_stalled_append_gives_up_on_its_own_budget(self, install_impl, monkeypatch):
        """A hung filesystem may cost the row; it may not hold the caller.

        The write is bounded by ``_LOG_BUDGET_SECS`` on top of the provider
        budget, so an outer wait sized at ``timeout_secs() + _LOG_BUDGET_SECS``
        covers a ``decide`` whose log write never lands.

        Timed INSIDE the loop deliberately. The worker thread is not cancellable,
        so a loop being CLOSED still joins it -- ``asyncio.run`` would pay the
        whole stall at shutdown and hide the property under test. The bound is on
        what ``decide`` holds its caller for, which is what an outer wait sizes
        against; a gateway's loop is long-lived, so the abandoned thread finishes
        its one append in the background.
        """
        install_impl(_RecordingOracle())
        monkeypatch.setattr(gate_mod, "_LOG_BUDGET_SECS", 0.02)
        monkeypatch.setattr(log_mod, "append", lambda row: time.sleep(0.4))

        async def _timed():
            started = time.monotonic()
            answers = await decide(POINT, "hi", QUESTIONS, config=_config())
            return answers, time.monotonic() - started

        answers, elapsed = asyncio.run(_timed())
        assert answers is not None, "a log that never lands must not cost the answers"
        assert elapsed < 0.3, f"the write held the caller for {elapsed:.3f}s"

    def test_no_provider_message_reaches_the_application_log(self, install_impl, log_home, caplog):
        """Not the row and not the logger: a provider can quote the request back."""
        install_impl(_RaisingOracle(RuntimeError("rejected body: my-secret-payload")))
        with caplog.at_level("DEBUG", logger="kiro_crew.decisions.gate"):
            asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        assert "my-secret-payload" not in caplog.text
        assert "RuntimeError" in caplog.text, "the exception class is the diagnostic"

    def test_append_runs_off_the_event_loop(self, install_impl, monkeypatch):
        """The filesystem work is offloaded as one unit, open through close."""
        import threading

        loop_thread = threading.current_thread().ident
        seen: list[int | None] = []

        def _record(row):
            seen.append(threading.current_thread().ident)

        monkeypatch.setattr(log_mod, "append", _record)
        install_impl(_RecordingOracle())

        async def _drive():
            loop_ident = threading.current_thread().ident
            await decide(POINT, "hi", QUESTIONS, config=_config())
            return loop_ident

        ident = asyncio.run(_drive())
        assert seen and seen[0] != ident
        assert loop_thread is not None


# ---------------------------------------------------------------------------
# A point's own row fields
# ---------------------------------------------------------------------------


class TestExtraRowFields:
    """``extra`` is written onto the row and is never sent."""

    @pytest.mark.asyncio
    async def test_it_rides_the_row_of_a_successful_call(self, install_impl, log_home):
        install_impl(_RecordingOracle())
        answers = await decide(
            POINT,
            {"q": "x"},
            QUESTIONS,
            session_key="s",
            config=_config(),
            extra={"turn_id": "abc", "round": 2, "rounds": 3},
        )
        assert answers is not None
        row = log_home()[0]
        assert (row["turn_id"], row["round"], row["rounds"]) == ("abc", 2, 3)

    @pytest.mark.asyncio
    async def test_it_rides_a_failure_row_too(self, install_impl, log_home):
        """Which round failed is the whole question a split menu raises."""
        install_impl(_RaisingOracle(RuntimeError("transport")))
        assert (
            await decide(
                POINT, {"q": "x"}, QUESTIONS, config=_config(), extra={"turn_id": "t", "round": 2}
            )
            is None
        )
        row = log_home()[0]
        assert row["error"] == gate_mod.ERROR_PROVIDER
        assert (row["turn_id"], row["round"]) == ("t", 2)

    @pytest.mark.asyncio
    async def test_it_is_not_part_of_the_request(self, install_impl, log_home):
        """It is a log field, so it is neither scanned nor sent."""
        oracle = install_impl(_RecordingOracle())
        await decide(
            POINT,
            {"q": "x"},
            QUESTIONS,
            config=_config(),
            extra={"turn_id": "abc", "baseline": ["review"]},
        )
        ((state, questions),) = oracle.calls
        assert state == {"q": "x"}, "the state sent is the state given"
        assert "turn_id" not in json.dumps(state)

    @pytest.mark.asyncio
    async def test_a_refusal_that_writes_no_row_writes_no_extra(self, install_impl, log_home):
        """The three cheap refusals still touch no disk."""
        install_impl(_ExplodingOracle())
        assert (
            await decide(
                POINT,
                {"q": "x"},
                QUESTIONS,
                session_key="s",
                config=_config(bucket=0),
                extra={"turn_id": "abc"},
            )
            is None
        )
        assert log_home() == []
