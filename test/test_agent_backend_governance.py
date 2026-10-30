"""The ``agent_backend`` governance scope — additive over a floor, applied at boot.

Two questions decide whether a harness can be selected, and these tests pin the seam
between them: ``agent_sdk.backends.selectable_backends`` answers "can this BUILD serve it"
(a capability fact, not governable) and this scope answers "may THIS DEPLOYMENT
select it".

The semantics under test: an ``allow`` list ADDS to the floor rather than replacing
the set, so no policy can leave an install with no startable harness.

The ENFORCEMENT POSITION is equally load-bearing and is pinned here too. Policy
narrows the registry once at boot; nothing downstream gains a check. That is what
keeps selectability at one gate (harness-parity H4), keeps the single gate free of a
platform-context read that would re-enter the config load (H3), and keeps the Kiro
construction path free of an adapter-driven conditional (H13).
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew import agent_backend_governance as abg
from kiro_crew.agent_sdk import backends as acp_backends
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import SCOPE_CATALOG, parse_policy

FLOOR = acp_backends.GOVERNANCE_FLOOR_BACKEND


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    # Both registry sets are process-global module state; restore them so one test's
    # recompute cannot leak into the next. Reached through ``agent_sdk.backends``,
    # the module that DEFINES the pair: the ``kiro_crew.acp_backends`` shim
    # re-exports the public names only, because a second binding to a mutable set
    # is how two views of one registry start disagreeing.
    baseline = set(acp_backends._baseline)
    selectable = set(acp_backends._selectable)
    yield
    acp_backends._baseline.clear()
    acp_backends._baseline.update(baseline)
    acp_backends._selectable.clear()
    acp_backends._selectable.update(selectable)
    gp.reset_store()
    ctx_mod.reset_context()


def _install(policy_body):
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


def _policy(rule):
    return {"version": 1, "boot": {"fail_closed": True}, "agent_backend": rule}


def _registered(monkeypatch, *backends):
    """Pin what the BUILD serves, so a test states its own capability premise.

    Sets the baseline AND the effective set, mirroring what
    ``register_selectable_backend`` does — a test that set only the effective set
    would leave the recompute iterating an empty baseline and removing everything.

    The public baseline is ``{"", "kas"}``; a test about claude must widen it or it
    would be asserting the registry's absence rather than the policy's decision.
    """
    monkeypatch.setattr(acp_backends, "_baseline", set(backends))
    monkeypatch.setattr(acp_backends, "_selectable", set(backends))


class TestScopeRegistration:
    def test_scope_is_in_the_catalog(self):
        # The extension contract is "one row in SCOPE_CATALOG"; without the row every
        # permits() call below would answer from a default, not the rule under test.
        assert "agent_backend" in SCOPE_CATALOG

    def test_kiro_has_a_policy_spelling_that_is_not_the_empty_string(self):
        # An identifier matcher cannot carry "", and a blank allow/deny entry is
        # indistinguishable from a typo, so the wire name has to differ.
        assert FLOOR == ""
        assert acp_backends.POLICY_ID_BY_BACKEND[FLOOR] == "kiro"

    def test_every_known_backend_has_a_policy_id(self):
        # A backend with no wire spelling could never be named in a rule, so it would
        # be ungovernable while looking governed.
        assert set(acp_backends.POLICY_ID_BY_BACKEND) == set(acp_backends.ACP_BACKENDS_KNOWN)


class TestRegistryRecompute:
    """``baseline - denied``, assigned whole — the property the runtime path needs."""

    def test_denials_are_recomputed_not_subtracted(self, monkeypatch):
        _registered(monkeypatch, FLOOR, "kas", "claude")
        assert acp_backends.apply_selectable_denials({"kas", "claude"}) == frozenset(
            {"kas", "claude"}
        )
        assert acp_backends.selectable_backends() == frozenset({FLOOR})
        # The point of assigning rather than subtracting: a LATER, looser policy
        # restores what an earlier one removed. A destructive remove cannot.
        assert acp_backends.apply_selectable_denials({"claude"}) == frozenset({"claude"})
        assert acp_backends.selectable_backends() == frozenset({FLOOR, "kas"})

    def test_it_is_idempotent(self, monkeypatch):
        _registered(monkeypatch, FLOOR, "kas")
        first = acp_backends.apply_selectable_denials({"kas"})
        second = acp_backends.apply_selectable_denials({"kas"})
        assert first == second == frozenset({"kas"})
        assert acp_backends.selectable_backends() == frozenset({FLOOR})

    def test_the_floor_is_force_kept_even_when_named(self, monkeypatch):
        # Not defence against the governance caller (which never submits the floor)
        # but against any caller emptying the set — a state the dashboard cannot
        # repair, because the trust-root policy is the one file it may not write.
        _registered(monkeypatch, FLOOR, "kas")
        acp_backends.apply_selectable_denials({FLOOR, "kas"})
        assert FLOOR in acp_backends.selectable_backends()

    def test_registering_writes_the_baseline_too(self, monkeypatch):
        # An edition that registers AFTER a policy pass must still be visible to the
        # next recompute rather than being dropped by it.
        _registered(monkeypatch, FLOOR)
        acp_backends.register_selectable_backend("kas")
        assert "kas" in acp_backends.registered_backends()
        assert "kas" in acp_backends.selectable_backends()

    def test_registered_is_the_pre_narrowing_answer(self, monkeypatch):
        # The recompute must iterate this, not the narrowed set.
        _registered(monkeypatch, FLOOR, "kas")
        acp_backends.apply_selectable_denials({"kas"})
        assert acp_backends.registered_backends() == frozenset({FLOOR, "kas"})
        assert acp_backends.selectable_backends() == frozenset({FLOOR})


class TestUngoverned:
    def test_no_policy_removes_nothing(self, monkeypatch):
        _registered(monkeypatch, FLOOR, "kas")
        _install(None)
        assert abg.narrow_selectable_backends() == []
        assert acp_backends.selectable_backends() == frozenset({FLOOR, "kas"})

    def test_a_policy_without_the_scope_removes_nothing(self, monkeypatch):
        _registered(monkeypatch, FLOOR, "kas")
        _install({"version": 1, "boot": {"fail_closed": True}})
        assert abg.narrow_selectable_backends() == []
        assert acp_backends.selectable_backends() == frozenset({FLOOR, "kas"})

    def test_policy_cannot_add_a_backend_the_build_never_registered(self, monkeypatch):
        # Policy cannot conjure a harness the build has no code for.
        _registered(monkeypatch, FLOOR, "kas")
        _install(_policy({"mode": "allow", "allow": ["claude"]}))
        abg.narrow_selectable_backends()
        assert "claude" not in acp_backends.selectable_backends()


class TestAdditiveAllow:
    def test_allow_adds_to_the_floor_instead_of_replacing_the_set(self, monkeypatch):
        # THE semantics decision. Under the exclusive reading kiro would be denied by
        # omission and removed here.
        _registered(monkeypatch, FLOOR, "kas", "claude")
        _install(_policy({"mode": "allow", "allow": ["claude"]}))
        assert abg.narrow_selectable_backends() == ["kas"]
        assert acp_backends.selectable_backends() == frozenset({FLOOR, "claude"})

    def test_an_empty_allow_list_still_leaves_the_floor(self, monkeypatch):
        # mode=allow with nothing allowed is deny-all for an ordinary scope. Here it
        # must still not empty the set, or the install has nothing to start.
        _registered(monkeypatch, FLOOR, "kas", "claude")
        _install(_policy({"mode": "allow", "allow": []}))
        abg.narrow_selectable_backends()
        assert acp_backends.selectable_backends() == frozenset({FLOOR})


class TestDeny:
    def test_deny_removes_a_non_floor_backend(self, monkeypatch):
        _registered(monkeypatch, FLOOR, "kas", "claude")
        _install(_policy({"mode": "deny", "deny": ["claude"]}))
        assert abg.narrow_selectable_backends() == ["claude"]
        assert acp_backends.selectable_backends() == frozenset({FLOOR, "kas"})

    def test_denying_kiro_is_inert(self, monkeypatch):
        # The floor is never submitted to the scope, so a rule naming it has nothing
        # to act on — stated as a test because "the deny silently did nothing" is
        # exactly what an operator would otherwise report as a bug.
        _registered(monkeypatch, FLOOR, "kas")
        _install(_policy({"mode": "deny", "deny": ["kiro"]}))
        assert abg.narrow_selectable_backends() == []
        assert FLOOR in acp_backends.selectable_backends()


class TestHostIdentity:
    def test_the_scope_is_asked_with_the_host_sentinel_not_an_empty_key(self, monkeypatch):
        # An empty session key classifies to surface "unknown" and matches NO profile,
        # so a host-bound profile denying a harness would be silently ignored and the
        # denied harness would stay selectable. HOST_SESSION_KEY exists for this.
        _registered(monkeypatch, FLOOR, "kas")
        _install(None)
        seen: list[str] = []

        def _spy(scope, item, *, session_key="", **kw):
            seen.append(session_key)
            return type("D", (), {"permitted": True, "rule": "", "layer": "", "reason": ""})()

        monkeypatch.setattr(gp, "governance_permits", _spy)
        abg.narrow_selectable_backends()
        assert seen == [gp.HOST_SESSION_KEY]

    def test_a_host_bound_profile_deny_actually_removes_the_backend(self, tmp_path, monkeypatch):
        # The end-to-end consequence of the sentinel: bind a profile to the host
        # surface and it must bite. With an empty key this test fails.
        _registered(monkeypatch, FLOOR, "kas")
        (gp._PROFILES_DIR / "host.json").write_text(
            '{"version": 1, "bind": {"type": "surface", "id": "host"}, '
            '"scopes": {"agent_backend": {"mode": "deny", "deny": ["kas"]}}}',
            encoding="utf-8",
        )
        gp.reset_store()
        _install(None)
        assert abg.narrow_selectable_backends() == ["kas"]


class TestAudit:
    def test_every_decision_is_recorded_in_both_directions(self, monkeypatch):
        # Once per gateway start, so the full record is cheap — and "which harnesses
        # did this deployment admit at boot" is the question an operator actually
        # reconstructs afterwards, which a denials-only log cannot answer.
        _registered(monkeypatch, FLOOR, "kas", "claude")
        _install(_policy({"mode": "deny", "deny": ["claude"]}))
        records: list[dict] = []

        class _Sel:
            def log_governance_decision(self, **kw):
                records.append(kw)

        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
        abg.narrow_selectable_backends()
        outcomes = {r["item"]: r["outcome"] for r in records}
        # "allowed"/"denied" is the vocabulary log_governance_decision pins and the
        # canonical governance_profiles helper emits. A local "permitted" would make
        # this scope the only one a query for allowed decisions misses.
        assert outcomes == {"kas": "allowed", "claude": "denied"}
        assert {r["scope"] for r in records} == {"agent_backend"}
        assert {r["session_key"] for r in records} == {gp.HOST_SESSION_KEY}

    def test_an_audit_failure_never_breaks_the_narrowing(self, monkeypatch):
        _registered(monkeypatch, FLOOR, "kas")
        _install(_policy({"mode": "deny", "deny": ["kas"]}))

        def _boom():
            raise RuntimeError("SEL unavailable")

        monkeypatch.setattr("kiro_crew.sel.sel", _boom)
        assert abg.narrow_selectable_backends() == ["kas"]


class TestFailClosed:
    def test_an_evaluation_error_denies_and_leaves_the_floor(self, monkeypatch):
        _registered(monkeypatch, FLOOR, "kas", "claude")
        _install(None)

        def _boom(*_a, **_k):
            raise RuntimeError("policy store unreadable")

        monkeypatch.setattr(gp, "governance_permits", _boom)
        abg.narrow_selectable_backends()
        # Closed, but not bricked: exactly one harness survives, which is only true
        # because the floor is never submitted to the scope.
        assert acp_backends.selectable_backends() == frozenset({FLOOR})

    def test_narrowing_never_raises_into_boot(self, monkeypatch):
        # bootstrap treats this as best-effort; a boot that aborts because a policy
        # could not be read is worse than one that starts on the floor alone.
        def _boom():
            raise RuntimeError("registry gone")

        monkeypatch.setattr(abg, "selectable_backends", _boom)
        assert abg.narrow_selectable_backends() == []


class TestWhenAPolicyChangeBinds:
    """The contract this scope promises, and the one it deliberately does not.

    ``policy_distribution.apply_ceiling`` replaces ``current_context().governance``
    mid-process (the poll thread reaches it via ``refresh_now()``). Every OTHER
    chokepoint reads the ceiling per decision, so the swap is all they need. This scope
    cannot: its single gate runs inside ``KiroCrewConfig.load()`` and reading a ceiling
    there re-enters that load (H3), so its state lives in the registry.

    Re-deriving the registry on that path is deliberately NOT done. It would bind the
    new ceiling for backend SELECTION while leaving sessions and pooled providers
    already running a denied harness untouched — an enforcement that reads as complete
    and is not. So the promise is the narrow one that can be kept: decided at gateway
    start, binding on the next start, stated in the panel and the spec.
    """

    def test_apply_ceiling_does_not_renarrow_the_registry(self, monkeypatch):
        # Guards the contract in BOTH directions. Whoever wires the recompute in here
        # must also retire live work on the denied harness -- otherwise the panel stops
        # offering the backend while sessions keep running on it, and an operator reads
        # the disappearance as "it stopped being used". Deleting this test is part of
        # that change, not a way around it.
        from kiro_crew.platform import policy_distribution as pd

        _registered(monkeypatch, FLOOR, "kas")
        _install(None)
        assert "kas" in acp_backends.selectable_backends()

        pd.apply_ceiling(parse_policy(_policy({"mode": "deny", "deny": ["kas"]})))

        assert acp_backends.selectable_backends() == frozenset({FLOOR, "kas"})

    def test_the_ceiling_itself_is_still_installed(self, monkeypatch):
        # Not re-narrowing must not turn into not applying: every other scope reads the
        # new ceiling per decision and gets it immediately.
        from kiro_crew.platform import policy_distribution as pd

        _registered(monkeypatch, FLOOR, "kas")
        _install(None)
        ceiling = parse_policy(_policy({"mode": "deny", "deny": ["kas"]}))

        pd.apply_ceiling(ceiling)

        # ``apply_ceiling`` composes the whole ladder around the pushed document (the
        # local tiers beneath it), so the installed object is the COMPOSED ceiling
        # tagged ``tier="central"`` -- equal in every control to the argument, not the
        # same object. Asserting on the deny it carries, not on identity, is what this
        # test is about: the change bound.
        installed = ctx_mod.current_context().governance
        assert installed is not None
        assert installed.tier == "central"
        assert not installed.controls["agent_backend"].permits("kas").permitted

    def test_a_restart_applies_it(self, monkeypatch):
        # The promise itself: the same denied policy, taken at boot, does bind. This is
        # what makes the omission above a SCOPE boundary rather than the rule being
        # unenforced.
        _registered(monkeypatch, FLOOR, "kas")
        _install(_policy({"mode": "deny", "deny": ["kas"]}))

        abg.narrow_selectable_backends()

        assert acp_backends.selectable_backends() == frozenset({FLOOR})


class TestNoSecondGate:
    """H3 / H4 / H13: the enforcement position, not just the verdict."""

    def test_the_dashboard_allowlist_derives_only_from_the_registry(self, monkeypatch):
        # A second derivation here (an intersection with a policy read) is the drift
        # the registry replaced, and it would also put a ceiling read on a request
        # path. The narrowed registry is the whole answer.
        from kiro_crew.dashboard.handlers import core as core_mod

        _registered(monkeypatch, FLOOR, "kas", "claude")
        _install(_policy({"mode": "deny", "deny": ["claude"]}))
        abg.narrow_selectable_backends()
        assert core_mod._selectable_acp_backends() == [FLOOR, "kas"]

    def test_the_single_gate_degrades_a_policy_denied_persisted_value(self, monkeypatch):
        # No new gate is needed for the "config.json written before the policy
        # arrived" case: resolve_selected_backend reads the registry per call, so
        # narrowing it makes the existing gate do the degrade.
        _registered(monkeypatch, FLOOR, "kas", "claude")
        _install(_policy({"mode": "deny", "deny": ["claude"]}))
        abg.narrow_selectable_backends()
        assert acp_backends.resolve_selected_backend("claude") == FLOOR

    @pytest.mark.parametrize(
        "choice",
        [
            pytest.param(
                None, id="test_the_default_falls_back_to_the_kiro_floor_when_kas_is_denied"
            ),
            pytest.param("kas", id="test_an_explicit_denied_kas_choice_takes_the_floor"),
        ],
    )
    def test_a_denied_kas_takes_the_floor_whether_defaulted_or_chosen(self, monkeypatch, choice):
        _registered(monkeypatch, FLOOR, "kas")
        _install(_policy({"mode": "deny", "deny": ["kas"]}))
        abg.narrow_selectable_backends()
        assert acp_backends.resolve_selected_backend(choice) == FLOOR

    def test_only_an_absent_value_selects_kas(self, monkeypatch):
        # H1/H3: KAS is the default for "no choice made", never the landing spot
        # for a bad explicit value.
        _registered(monkeypatch, FLOOR, "kas")
        assert acp_backends.resolve_selected_backend(None) == "kas"
        assert acp_backends.resolve_selected_backend("not-a-backend") == FLOOR
        assert acp_backends.resolve_selected_backend(7) == FLOOR

    def test_a_host_unservable_explicit_choice_takes_the_floor(self, monkeypatch):
        _registered(monkeypatch, FLOOR, "kas", "claude")
        monkeypatch.setattr(acp_backends, "_host_unservable", {})
        acp_backends.mark_backend_unservable("claude", "adapter missing")
        assert acp_backends.resolve_selected_backend("claude") == FLOOR
        # KAS is still servable, so an absent choice keeps its default.
        assert acp_backends.resolve_selected_backend(None) == "kas"

    def test_masked_config_reports_the_floor_when_policy_removes_kas(self, monkeypatch, tmp_path):
        # Absent on disk + KAS denied: the dashboard must be told Kiro CLI, not KAS,
        # and save() must still keep the key absent rather than store the floor.
        import json
        from unittest.mock import patch

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.handlers.core import _masked_config_dict

        _registered(monkeypatch, FLOOR, "kas")
        _install(_policy({"mode": "deny", "deny": ["kas"]}))
        abg.narrow_selectable_backends()

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"agent": {}}), encoding="utf-8")
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=tmp_path / "config.local.json",
            ),
            patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
        ):
            cfg = KiroCrewConfig.load()

            assert cfg.agent.acp_backend == FLOOR
            # The response speaks the config-file vocabulary: Kiro CLI is "kiro".
            assert (
                _masked_config_dict(cfg)["agent"]["acp_backend"] == acp_backends.STORED_BACKEND_KIRO
            )
            cfg.save()

        assert "acp_backend" not in json.loads(cfg_file.read_text(encoding="utf-8"))["agent"]

    def test_save_keeps_a_stored_choice_the_policy_denied(self, monkeypatch, tmp_path):
        """A denied 'kas' resolves to the floor in memory and stays 'kas' on disk.

        Writing the resolved floor back would turn a policy narrowing into a
        permanent Kiro CLI choice the user never made, which survives the policy
        being lifted.
        """
        import json
        from unittest.mock import patch

        cfg_file = tmp_path / "config.json"
        stored = {"acp_backend": "kas", "member_acp_backend": "kas", "streaming": False}
        cfg = self._load_with_kas_denied(monkeypatch, tmp_path, stored)
        assert cfg.agent.acp_backend == FLOOR
        assert cfg.agent.member_acp_backend == FLOOR
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=tmp_path / "config.local.json",
            ),
            patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
        ):
            cfg.save()
        agent = json.loads(cfg_file.read_text(encoding="utf-8"))["agent"]
        assert agent["acp_backend"] == "kas"
        assert agent["member_acp_backend"] == "kas"
        assert agent["streaming"] is False

    @staticmethod
    def _load_with_kas_denied(monkeypatch, tmp_path, agent):
        """Load *agent* as config.json from disk after a policy removed KAS."""
        import json
        from unittest.mock import patch

        from kiro_crew.config.loader import KiroCrewConfig

        _registered(monkeypatch, FLOOR, "kas")
        _install(_policy({"mode": "deny", "deny": ["kas"]}))
        abg.narrow_selectable_backends()

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"agent": agent}), encoding="utf-8")
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=tmp_path / "config.local.json",
            ),
            patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
        ):
            return KiroCrewConfig.load()

    def test_a_kiro_member_backend_still_crosses_the_gate(self, monkeypatch, tmp_path, caplog):
        # The member "kiro" reads as KAS before the gate, not instead of it: the
        # gate judges 'kas' and takes the floor.
        caplog.set_level("WARNING")
        cfg = self._load_with_kas_denied(monkeypatch, tmp_path, {"member_acp_backend": "kiro"})
        assert cfg.agent.member_acp_backend == FLOOR
        assert "Ignoring agent.acp_backend 'kas'" in caplog.text

    def test_an_absent_key_loads_the_floor_without_a_warning(self, monkeypatch, tmp_path, caplog):
        # The load must hand the gate "no choice", not the field default: reading the
        # default as an explicit 'kas' warns on every load about a value nobody wrote.
        caplog.set_level("WARNING")
        cfg = self._load_with_kas_denied(monkeypatch, tmp_path, {})
        assert cfg.agent.acp_backend == FLOOR
        assert cfg.agent.member_acp_backend == FLOOR
        assert "Ignoring agent.acp_backend" not in caplog.text

    @pytest.mark.parametrize("deny_kas", [True, False], ids=["kas-denied", "no-policy"])
    @pytest.mark.parametrize("backend_key", ["acp_backend", "member_acp_backend"])
    def test_no_config_files_load_a_selectable_default(
        self, monkeypatch, tmp_path, caplog, deny_kas, backend_key
    ):
        from kiro_crew.config import loader as loader_mod

        _registered(monkeypatch, FLOOR, acp_backends.ACP_BACKEND_KAS)
        monkeypatch.setattr(loader_mod, "config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr(loader_mod, "config_local_path", lambda: tmp_path / "config.local.json")
        monkeypatch.setattr(loader_mod, "config_dir", lambda: tmp_path)
        _install(_policy({"mode": "deny", "deny": ["kas"]}) if deny_kas else None)
        abg.narrow_selectable_backends()
        expected = FLOOR if deny_kas else acp_backends.DEFAULT_ACP_BACKEND
        caplog.set_level("WARNING")

        for _ in range(2):
            cfg = loader_mod.KiroCrewConfig.load()
            assert getattr(cfg.agent, backend_key) == expected
        assert "Ignoring agent.acp_backend" not in caplog.text
        assert not loader_mod.config_path().exists()
        assert not loader_mod.config_local_path().exists()

    def test_an_explicit_denied_kas_loads_the_floor_with_a_warning(
        self, monkeypatch, tmp_path, caplog
    ):
        caplog.set_level("WARNING")
        cfg = self._load_with_kas_denied(monkeypatch, tmp_path, {"acp_backend": "kas"})
        assert cfg.agent.acp_backend == FLOOR
        assert "Ignoring agent.acp_backend 'kas'" in caplog.text

    @staticmethod
    def _boot_with_kas_denied(monkeypatch, *, declared):
        """Run ``bootstrap_context`` on a cfg already resolved to KAS, then deny KAS."""
        from kiro_crew.config import loader as loader_mod
        from kiro_crew.platform import bootstrap as bootstrap_mod

        _registered(monkeypatch, FLOOR, "kas")

        def _narrow():
            return sorted(acp_backends.apply_selectable_denials({"kas"}))

        monkeypatch.setattr(abg, "narrow_selectable_backends", _narrow)
        monkeypatch.setattr(loader_mod, "acp_backend_declared", lambda: declared)
        cfg = loader_mod.KiroCrewConfig.load()
        cfg.agent.acp_backend = "kas"
        bootstrap_mod.bootstrap_context(cfg)
        return cfg

    def test_boot_reresolve_of_an_undeclared_default_does_not_warn(self, monkeypatch, caplog):
        # Absent on disk, the load resolved the field to KAS. The boot re-resolve must
        # treat it as absent: the floor, with no warning about a value nobody wrote.
        caplog.set_level("WARNING")
        cfg = self._boot_with_kas_denied(monkeypatch, declared=False)
        assert cfg.agent.acp_backend == FLOOR
        assert "Ignoring agent.acp_backend" not in caplog.text

    def test_boot_reresolve_of_a_declared_denied_choice_warns(self, monkeypatch, caplog):
        # An explicit KAS choice the policy denied still degrades with the reason (H3).
        caplog.set_level("WARNING")
        cfg = self._boot_with_kas_denied(monkeypatch, declared=True)
        assert cfg.agent.acp_backend == FLOOR
        assert "Ignoring agent.acp_backend 'kas'" in caplog.text

    def test_the_provider_factory_path_gained_no_governance_call(self):
        # H13 names create_provider_factory as a constrained site: the Kiro
        # construction path must gain no conditional in service of an adapter.
        # Asserted on the source because the rule is about the code's shape.
        import inspect

        from kiro_crew.config.loader import KiroCrewConfig

        src = inspect.getsource(KiroCrewConfig.create_provider_factory)
        assert "effective_acp_backend" not in src
        assert "agent_backend_governance" not in src

    def test_the_config_load_path_never_reads_the_platform_context(self):
        # H3: the one gate runs inside KiroCrewConfig.load(); a ceiling read there
        # re-enters that load. acp_backends must stay a leaf.
        #
        # Parsed rather than grepped: the module's own docstring DISCUSSES
        # ``current_context()`` at length to explain why it must not reach it, so a
        # substring assertion would fail on the documentation while a real call added
        # inside a function would pass once the prose was reworded. The import graph
        # and the call nodes are what the rule is actually about.
        import ast
        import importlib
        import inspect

        facade = ast.parse(inspect.getsource(acp_backends))
        owners = sorted(
            node.module
            for node in facade.body
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith(acp_backends.__name__ + "_")
        )
        trees = [
            facade,
            *(ast.parse(inspect.getsource(importlib.import_module(m))) for m in owners),
        ]
        tree = ast.Module(body=[node for t in trees for node in t.body], type_ignores=[])
        imported = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not [m for m in imported if m.startswith("kiro_crew.platform")]
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        } | {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "current_context" not in called
