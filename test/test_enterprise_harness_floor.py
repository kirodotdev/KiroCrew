"""Enterprise edition: KAS is the default harness and the ``agent_backend`` floor.

RFC ``docs/request-for-change/rfc-crew-enterprise-edition.md`` D6. In an enterprise
build the floor no policy can remove is KAS, the baseline minimal set is ``kas``, the
Kiro CLI harness needs a remote rule, and a KAS that cannot start refuses the start
instead of falling back to the Kiro CLI harness. The ``oss`` edition is unchanged.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from kiro_crew import agent_backend_governance as abg
from kiro_crew import enterprise_boot
from kiro_crew.agent_sdk import backends as acp_backends
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import apply_kas_cli_floor, build_default_context
from kiro_crew.platform.governance import parse_policy
from kiro_crew.platform.governance_parts.baseline import BASELINE_CONTROLS

KIRO = acp_backends.ACP_BACKEND_KIRO
KAS = acp_backends.ACP_BACKEND_KAS
CLAUDE = acp_backends.ACP_BACKEND_CLAUDE


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    baseline = set(acp_backends._baseline)
    selectable = set(acp_backends._selectable)
    unservable = dict(acp_backends._host_unservable)
    yield
    acp_backends._baseline.clear()
    acp_backends._baseline.update(baseline)
    acp_backends._selectable.clear()
    acp_backends._selectable.update(selectable)
    acp_backends._host_unservable.clear()
    acp_backends._host_unservable.update(unservable)
    gp.reset_store()
    ctx_mod.reset_context()


@pytest.fixture
def enterprise(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


def _registered(monkeypatch, *backends):
    monkeypatch.setattr(acp_backends, "_baseline", set(backends))
    monkeypatch.setattr(acp_backends, "_selectable", set(backends))
    monkeypatch.setattr(acp_backends, "_host_unservable", {})


def _install(policy_body):
    """Install *policy_body* as the ceiling. The context is composed as ``oss``.

    An enterprise build's boot fetches its policy (RFC D2); a test composes the
    default context on the ``oss`` path and installs the ceiling itself, then
    restores the edition the test asked for, which is what the evaluator reads.
    """
    from kiro_crew import edition
    from kiro_crew.config.loader import KiroCrewConfig

    asked = edition.EDITION
    edition.EDITION = "oss"
    try:
        base = build_default_context(KiroCrewConfig.load())
    finally:
        edition.EDITION = asked
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


def _policy(rule=None):
    body = {"version": 1, "boot": {"fail_closed": True}}
    if rule is not None:
        body["agent_backend"] = rule
    return body


class TestFloorByEdition:
    def test_oss_floor_is_kiro_cli(self):
        assert acp_backends.governance_floor_backend() == KIRO

    def test_enterprise_floor_is_kas(self, enterprise):
        assert acp_backends.governance_floor_backend() == KAS

    def test_enterprise_baseline_minimal_set_is_kas(self):
        assert BASELINE_CONTROLS["agent_backend"] == {"mode": "allow", "allow": ["kas"]}


class TestEnterpriseSelectableSet:
    def test_default_is_kas_only(self, enterprise, monkeypatch):
        # A remote policy that does not name agent_backend: the baseline answers.
        _registered(monkeypatch, KIRO, KAS, CLAUDE)
        _install(_policy())
        abg.narrow_selectable_backends()
        assert acp_backends.selectable_backends() == frozenset({KAS})

    def test_policy_allowing_kiro_adds_it(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS, CLAUDE)
        _install(_policy({"mode": "allow", "allow": ["kiro"]}))
        abg.narrow_selectable_backends()
        assert acp_backends.selectable_backends() == frozenset({KIRO, KAS})

    def test_policy_denying_kas_leaves_kas(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS)
        _install(_policy({"mode": "deny", "deny": ["kas"]}))
        abg.narrow_selectable_backends()
        assert KAS in acp_backends.selectable_backends()

    def test_an_empty_allow_list_still_leaves_kas(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS, CLAUDE)
        _install(_policy({"mode": "allow", "allow": []}))
        abg.narrow_selectable_backends()
        assert acp_backends.selectable_backends() == frozenset({KAS})

    def test_denials_force_keep_kas(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS)
        acp_backends.apply_selectable_denials({KIRO, KAS})
        assert acp_backends.selectable_backends() == frozenset({KAS})

    def test_kas_is_never_marked_unservable(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS)
        acp_backends.mark_backend_unservable(KAS, "too old")
        assert KAS in acp_backends.selectable_backends()
        assert acp_backends.host_unservable_reason(KAS) is None


class TestEnterpriseResolve:
    @pytest.mark.parametrize("value", [None, KIRO, CLAUDE, "nonsense", 7])
    def test_every_unselectable_value_resolves_to_kas(self, enterprise, monkeypatch, value):
        _registered(monkeypatch, KIRO, KAS, CLAUDE)
        acp_backends.apply_selectable_denials({KIRO, CLAUDE})
        assert acp_backends.resolve_selected_backend(value) == KAS

    def test_the_warning_names_the_kas_floor(self, enterprise, monkeypatch, caplog):
        _registered(monkeypatch, KIRO, KAS)
        acp_backends.apply_selectable_denials({KIRO})
        acp_backends.resolve_selected_backend(KIRO)
        assert "using the KAS floor" in caplog.text


class TestOssUnchanged:
    def test_oss_default_policy_keeps_kiro_cli_floor(self, monkeypatch):
        _registered(monkeypatch, KIRO, KAS, CLAUDE)
        _install(_policy({"mode": "allow", "allow": []}))
        abg.narrow_selectable_backends()
        assert acp_backends.selectable_backends() == frozenset({KIRO})

    def test_oss_unselectable_value_takes_kiro_cli(self, monkeypatch, caplog):
        _registered(monkeypatch, KIRO, KAS)
        acp_backends.apply_selectable_denials({KAS})
        assert acp_backends.resolve_selected_backend(CLAUDE) == KIRO
        assert "using the Kiro CLI floor" in caplog.text

    def test_oss_marks_an_unservable_kas(self, monkeypatch):
        _registered(monkeypatch, KIRO, KAS)
        acp_backends.mark_backend_unservable(KAS, "too old")
        assert acp_backends.selectable_backends() == frozenset({KIRO})

    def test_oss_boot_check_does_nothing(self, monkeypatch):
        def _boom(**_kw):
            raise AssertionError("must not run in oss")

        monkeypatch.setattr(enterprise_boot, "kas_unrunnable", _boom)
        enterprise_boot.require_kas_harness(_cfg(KAS))


def _cfg(main, member=None, *, kas_bundle=False):
    """A loaded config: an unset member backend holds its field default, KAS."""
    return SimpleNamespace(
        agent=SimpleNamespace(
            acp_backend=main, member_acp_backend=member or KAS, kas_bundle=kas_bundle
        )
    )


def _verdict(monkeypatch, offers, reason):
    from kiro_crew import agent_sdk

    monkeypatch.setattr(agent_sdk, "kas_engine_verdict", lambda: (offers, reason))


def _bundle(monkeypatch, *, installed, installable):
    from kiro_crew.agent_sdk import backend_install, kas_bundle

    monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: object() if installed else None)
    monkeypatch.setattr(backend_install, "_engine_installable_here", lambda: installable)


_RELAY = SimpleNamespace(bundle=False, reason="", automatic=False)
_AUTO_BUNDLE = SimpleNamespace(bundle=True, reason="no_kiro_cli", automatic=True)
_CONFIGURED_BUNDLE = SimpleNamespace(bundle=True, reason="configured", automatic=False)


class TestKasUnrunnable:
    def test_relay_gap_is_refused(self, monkeypatch):
        _verdict(monkeypatch, False, "this kiro-cli has no --agent-engine flag")
        _bundle(monkeypatch, installed=False, installable=False)
        failed, fix = enterprise_boot.kas_unrunnable(kas_route=_RELAY)
        assert "--agent-engine" in failed
        assert fix == enterprise_boot.KAS_FIX_UPDATE_KIRO_CLI

    def test_relay_gap_names_the_bundle_when_it_could_run(self, monkeypatch):
        _verdict(monkeypatch, False, "this kiro-cli has no --agent-engine flag")
        _bundle(monkeypatch, installed=False, installable=True)
        _failed, fix = enterprise_boot.kas_unrunnable(kas_route=_RELAY)
        assert "kiro-cli update" in fix
        assert "`agent.kas_bundle` to true" in fix

    def test_relay_unknown_is_not_proof(self, monkeypatch):
        _verdict(monkeypatch, None, None)
        assert enterprise_boot.kas_unrunnable(kas_route=_RELAY) is None

    def test_relay_that_offers_kas_runs(self, monkeypatch):
        _verdict(monkeypatch, True, None)
        assert enterprise_boot.kas_unrunnable(kas_route=_RELAY) is None

    def test_bundle_installed_runs(self, monkeypatch):
        _bundle(monkeypatch, installed=True, installable=False)
        assert enterprise_boot.kas_unrunnable(kas_route=_AUTO_BUNDLE) is None

    def test_bundle_installable_runs(self, monkeypatch):
        _bundle(monkeypatch, installed=False, installable=True)
        assert enterprise_boot.kas_unrunnable(kas_route=_AUTO_BUNDLE) is None

    def test_automatic_bundle_that_cannot_run_names_the_relay(self, monkeypatch):
        _bundle(monkeypatch, installed=False, installable=False)
        failed, fix = enterprise_boot.kas_unrunnable(kas_route=_AUTO_BUNDLE)
        assert "engine is not installed" in failed
        assert fix == enterprise_boot.KAS_FIX_ENGINE_UNAVAILABLE
        assert "kiro-cli login" in fix

    def test_configured_bundle_that_cannot_run_says_to_turn_it_off(self, monkeypatch):
        _bundle(monkeypatch, installed=False, installable=False)
        _failed, fix = enterprise_boot.kas_unrunnable(kas_route=_CONFIGURED_BUNDLE)
        assert fix == enterprise_boot.KAS_FIX_ENGINE_UNAVAILABLE_CONFIGURED
        assert "`agent.kas_bundle` to false" in fix


class TestRequireKasHarness:
    """Fix 1: only the MAIN backend decides the gateway start."""

    @pytest.fixture(autouse=True)
    def _clean(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(enterprise_boot, "_kas_refusal", None)

    def test_main_on_kas_is_refused_with_the_fix(self, enterprise, monkeypatch, capsys):
        _registered(monkeypatch, KIRO, KAS)
        monkeypatch.setattr(
            enterprise_boot, "kas_unrunnable", lambda **_kw: ("KAS cannot start: x", "do y")
        )
        with pytest.raises(SystemExit) as exc:
            enterprise_boot.require_kas_harness(_cfg(KAS))
        assert exc.value.code == enterprise_boot.EXIT_CODE
        err = capsys.readouterr().err
        assert "KAS cannot start: x" in err and "do y" in err

    def test_a_policy_denied_main_backend_runs_kas_and_is_refused(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS, CLAUDE)
        acp_backends.apply_selectable_denials({KIRO, CLAUDE})
        monkeypatch.setattr(enterprise_boot, "kas_unrunnable", lambda **_kw: ("f", "x"))
        with pytest.raises(SystemExit):
            enterprise_boot.require_kas_harness(_cfg(CLAUDE))

    def test_a_runnable_kas_starts(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS)
        monkeypatch.setattr(enterprise_boot, "kas_unrunnable", lambda **_kw: None)
        enterprise_boot.require_kas_harness(_cfg(KAS))
        assert enterprise_boot.kas_refusal() is None

    @pytest.mark.parametrize("member", [None, KAS], ids=["member-unset", "member-declared-kas"])
    def test_a_kiro_cli_main_starts_and_records_the_verdict(self, enterprise, monkeypatch, member):
        # The member backend never decides the start, declared or not.
        _registered(monkeypatch, KIRO, KAS)
        monkeypatch.setattr(
            enterprise_boot, "kas_unrunnable", lambda **_kw: ("KAS cannot start: x", "do y")
        )
        enterprise_boot.require_kas_harness(_cfg(KIRO, member))
        assert enterprise_boot.kas_refusal() == ("KAS cannot start: x", "do y")

    def test_a_kiro_cli_main_with_an_unknown_verdict_records_nothing(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS)
        monkeypatch.setattr(enterprise_boot, "kas_unrunnable", lambda **_kw: None)
        enterprise_boot.require_kas_harness(_cfg(KIRO, KAS))
        assert enterprise_boot.kas_refusal() is None

    def test_a_check_that_raises_refuses_a_kas_main(self, enterprise, monkeypatch, capsys):
        _registered(monkeypatch, KIRO, KAS)

        def _raise(**_kw):
            raise RuntimeError("probe broke")

        monkeypatch.setattr(enterprise_boot, "kas_unrunnable", _raise)
        with pytest.raises(SystemExit):
            enterprise_boot.require_kas_harness(_cfg(KAS))
        assert "kirocrew doctor" in capsys.readouterr().err

    def test_a_check_that_raises_records_nothing_for_a_kiro_cli_main(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS)

        def _raise(**_kw):
            raise RuntimeError("probe broke")

        monkeypatch.setattr(enterprise_boot, "kas_unrunnable", _raise)
        enterprise_boot.require_kas_harness(_cfg(KIRO))
        assert enterprise_boot.kas_refusal() is None


class TestEachBootJudgesAfresh:
    """Fix 3: the record is cleared at every boot check, and test mode skips it."""

    def test_the_record_is_cleared_first(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS)
        monkeypatch.setattr(enterprise_boot, "_kas_refusal", ("stale", "stale"))

        def _raise(**_kw):
            raise RuntimeError("probe broke")

        monkeypatch.setattr(enterprise_boot, "kas_unrunnable", _raise)
        enterprise_boot.require_kas_harness(_cfg(KIRO))
        assert enterprise_boot.kas_refusal() is None

    def test_test_mode_probes_nothing_and_starts(self, enterprise, monkeypatch):
        _registered(monkeypatch, KIRO, KAS)
        monkeypatch.setattr(enterprise_boot, "_kas_refusal", ("stale", "stale"))

        def _boom(**_kw):
            raise AssertionError("test mode must not probe kiro-cli")

        monkeypatch.setattr(enterprise_boot, "kas_unrunnable", _boom)
        enterprise_boot.require_kas_harness(_cfg(KAS), test_mode=True)
        assert enterprise_boot.kas_refusal() is None


REFUSAL = (
    "KAS cannot start: this kiro-cli has no --agent-engine flag",
    enterprise_boot.KAS_FIX_UPDATE_KIRO_CLI,
)


class TestASessionOnAnUnstartableKasIsRefused:
    """Fix 2: every session whose backend is KAS, refused before any process."""

    def test_the_refusal_names_the_fix(self, enterprise, monkeypatch):
        from kiro_crew.crewmate_harness import (
            HARNESS_UNAVAILABLE_CODE,
            CrewmateHarnessUnavailable,
            KasUnavailable,
        )

        monkeypatch.setattr(enterprise_boot, "_kas_refusal", REFUSAL)
        with pytest.raises(KasUnavailable) as exc:
            enterprise_boot.refuse_unstartable_kas()
        # The same refusal every caller already handles for an unstartable pin.
        assert isinstance(exc.value, CrewmateHarnessUnavailable)
        assert exc.value.code == HARNESS_UNAVAILABLE_CODE
        message = str(exc.value)
        assert "--agent-engine v3" in message
        assert "kiro-cli update" in message
        assert "Nothing runs on another harness" in message

    def test_nothing_recorded_refuses_nothing(self, enterprise, monkeypatch):
        monkeypatch.setattr(enterprise_boot, "_kas_refusal", None)
        enterprise_boot.refuse_unstartable_kas()

    def test_oss_refuses_nothing(self, monkeypatch):
        monkeypatch.setattr(enterprise_boot, "_kas_refusal", REFUSAL)
        enterprise_boot.refuse_unstartable_kas()

    def _factory(self, monkeypatch, selected):
        from kiro_crew import members
        from kiro_crew.agent_sdk.drivers import acp
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr(members, "select_provider_backend", lambda *a, **k: selected)
        built: list[dict] = []
        monkeypatch.setattr(acp, "build_acp_provider", lambda **kw: built.append(kw) or kw)
        return KiroCrewConfig().create_provider_factory(), built

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"session_key": "dashboard:chat-1"},
            {"session_key": "dashboard:member-atlas-0123456789ab"},
            {"session_key": "dashboard:sub-1", "backend_override": "kas"},
        ],
        ids=["main-switched-to-kas", "crewmate-dm", "spawn-run-override"],
    )
    def test_the_factory_refuses_a_kas_session_before_building_it(
        self, enterprise, monkeypatch, kwargs
    ):
        from kiro_crew.crewmate_harness import KasUnavailable

        monkeypatch.setattr(enterprise_boot, "_kas_refusal", REFUSAL)
        factory, built = self._factory(monkeypatch, KAS)
        with pytest.raises(KasUnavailable):
            factory(agent="kirocrew", **kwargs)
        assert built == [], "a provider was built for a KAS that cannot start"

    @pytest.mark.parametrize("selected", [KIRO, CLAUDE], ids=["kiro-cli", "claude"])
    def test_the_factory_never_checks_a_non_kas_session(self, enterprise, monkeypatch, selected):
        monkeypatch.setattr(enterprise_boot, "_kas_refusal", REFUSAL)

        def _boom():
            raise AssertionError("the check must run only for a KAS session (H13)")

        monkeypatch.setattr(enterprise_boot, "refuse_unstartable_kas", _boom)
        factory, built = self._factory(monkeypatch, selected)
        factory(session_key="dashboard:chat-1", agent="kirocrew")
        assert built and built[0]["acp_backend"] == selected


class TestBackgroundFloorInEnterprise:
    def test_a_relay_gap_never_moves_kas_to_kiro_cli(self, enterprise, monkeypatch, caplog):
        _registered(monkeypatch, KIRO, KAS)
        _verdict(monkeypatch, False, "this kiro-cli has no --agent-engine flag")
        cfg = _cfg(KAS)
        assert apply_kas_cli_floor(cfg) is None
        assert cfg.agent.acp_backend == KAS
        assert KAS in acp_backends.selectable_backends()
        assert "using kiro-cli instead" not in caplog.text


class TestBackgroundRuntimeFloor:
    """The background runtime's backend for an unreadable config is the build floor."""

    @pytest.mark.parametrize("edition_name, expected", [("enterprise", KAS), ("oss", KIRO)])
    def test_the_unreadable_backend_fallback(self, monkeypatch, edition_name, expected):
        from types import SimpleNamespace

        from kiro_crew.session_background import BackgroundSessionRuntime

        monkeypatch.setattr("kiro_crew.edition.EDITION", edition_name)
        runtime = object.__new__(BackgroundSessionRuntime)
        runtime._deps = SimpleNamespace(acp_backend_kiro=KIRO)
        assert runtime._unreadable_backend() == expected

    def test_the_background_spawn_refuses_an_unstartable_kas(self):
        # Pinned on the source: the background runtime builds its AcpRuntime directly,
        # so the KAS refusal must sit right before that build, for KAS only.
        import inspect

        from kiro_crew import session_background

        src = inspect.getsource(session_background)
        check = src.index("if configured_backend == ACP_BACKEND_KAS:")
        refuse = src.index("refuse_unstartable_kas()", check)
        build = src.index("replacement = build_runtime(", check)
        assert check < refuse < build


class TestFailoverNeverPicksKiroCliInEnterprise:
    """``agent.acp_failover`` never hands a run to the Kiro CLI harness in enterprise."""

    def _run(self, monkeypatch, chain, available):
        import asyncio

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.dashboard.messaging_api import spawn as spawn_mod

        cfg = SimpleNamespace(agent=SimpleNamespace(acp_failover={"enabled": True, "chain": chain}))
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
        return asyncio.run(
            spawn_mod._failover_backend("codex", "backend_not_installed", frozenset(available))
        )

    def test_enterprise_skips_kiro_cli_as_a_successor(self, enterprise, monkeypatch):
        backend, tried = self._run(monkeypatch, ["codex", "kiro"], {KIRO, KAS})
        assert backend is None
        assert tried == ("kiro",)

    def test_enterprise_takes_a_later_non_kiro_successor(self, enterprise, monkeypatch):
        backend, _tried = self._run(monkeypatch, ["codex", "kiro", "claude"], {KIRO, CLAUDE})
        assert backend == CLAUDE

    def test_oss_still_takes_kiro_cli(self, monkeypatch):
        backend, _tried = self._run(monkeypatch, ["codex", "kiro"], {KIRO, KAS})
        assert backend == KIRO
