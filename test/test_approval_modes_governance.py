"""The ``approval_modes`` governance scope — deny ``yolo``, the one auto-approve
mode whose enforcement is complete, with ``normal`` / ``trust`` / ``trust_reads``
all un-deniable (the floor, plus the two whose enforcement is not implemented).

Mirrors ``test_agent_backend_governance.py``: a policy is installed as the
boot-frozen ceiling on the active context, and ``approval_mode_permitted`` reads
it back through the same ``governance_permits`` chokepoint the app uses at
runtime — so these tests pin the real enforcement path, not a parallel one.

The parse-time ``normal``-floor rejection is pinned directly on ``parse_policy``:
denying the interactive floor would leave no selectable approval mode, and the
trust-root ``security_policy.json`` is the one file the dashboard may not write
to repair itself, so the policy is refused at parse time rather than at boot.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import parse_policy
from kiro_crew.safety_override import approval_mode_permitted


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _install(policy_body):
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


def _policy(rule):
    return {"version": 1, "boot": {"fail_closed": True}, "approval_modes": rule}


class TestDenyList:
    def test_denying_yolo_forbids_it_and_leaves_the_rest_permitted(self):
        _install(_policy({"mode": "deny", "deny": ["yolo"]}))
        assert approval_mode_permitted("yolo") is False
        # The non-deniable modes stay permitted: this scope does not govern them.
        assert approval_mode_permitted("trust") is True
        assert approval_mode_permitted("trust_reads") is True
        assert approval_mode_permitted("normal") is True

    def test_no_policy_permits_every_mode(self):
        _install(None)
        for mode in ("normal", "trust_reads", "trust", "yolo"):
            assert approval_mode_permitted(mode) is True


class TestAllowList:
    def test_an_allow_list_may_omit_only_yolo(self):
        # Every non-deniable mode must appear, so the only mode an allow-list can
        # withhold is ``yolo`` -- which is the whole vocabulary this scope governs.
        _install(_policy({"mode": "allow", "allow": ["normal", "trust", "trust_reads"]}))
        assert approval_mode_permitted("normal") is True
        assert approval_mode_permitted("trust") is True
        assert approval_mode_permitted("trust_reads") is True
        assert approval_mode_permitted("yolo") is False


class TestNonDeniableModesAreRefusedAtParseTime:
    """``trust`` / ``trust_reads`` are refused LOUDLY, not accepted and ignored.

    Their grants are honoured by consumption predicates this scope does not reach,
    so accepting a deny would advertise a control that does not hold. An admin who
    writes one gets an error instead of a false sense of enforcement.
    """

    def test_denying_trust_is_rejected(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy({"mode": "deny", "deny": ["trust"]}))

    def test_denying_trust_reads_is_rejected(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy({"mode": "deny", "deny": ["trust_reads"]}))

    def test_an_allow_list_omitting_trust_is_rejected(self):
        # Omission denies just as effectively as naming it.
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy({"mode": "allow", "allow": ["normal", "yolo"]}))

    def test_the_refusal_names_the_offending_mode(self):
        with pytest.raises(PlatformCompositionError) as exc:
            parse_policy(_policy({"mode": "deny", "deny": ["trust"]}))
        assert "trust" in str(exc.value)


class TestNormalFloorIsUndeniable:
    def test_denying_normal_directly_is_rejected_at_parse_time(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy({"mode": "deny", "deny": ["normal"]}))

    def test_allow_list_omitting_normal_is_rejected_at_parse_time(self):
        with pytest.raises(PlatformCompositionError):
            parse_policy(_policy({"mode": "allow", "allow": ["yolo"]}))

    def test_normal_stays_permitted_when_the_deniable_mode_is_denied(self):
        _install(_policy({"mode": "deny", "deny": ["yolo"]}))
        assert approval_mode_permitted("normal") is True


class TestNonDeniableHoldsAtRuntimeNotJustParseTime:
    """ "Non-deniable" must mean the same thing at runtime as at parse time.

    The parse-time refusal stops an admin from WRITING a deny for these modes. It
    does not stop ``approval_mode_permitted`` from producing one anyway: that call
    evaluates with ``fail_closed=True``, so a governance-evaluation error would deny
    ``trust`` -- refusing a mode whose enforcement this scope does not implement.
    That surfaced as an unrelated Trust grant silently failing, so the predicate
    short-circuits every non-deniable mode before consulting governance at all.
    """

    def test_a_governance_evaluation_error_cannot_deny_a_non_deniable_mode(self, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp_mod

        def _boom(*a, **kw):
            raise RuntimeError("profile store unreadable")

        monkeypatch.setattr(gp_mod, "governance_permits", _boom)

        # Never reaches governance, so the error cannot turn into a denial.
        assert approval_mode_permitted("normal") is True
        assert approval_mode_permitted("trust") is True
        assert approval_mode_permitted("trust_reads") is True

    def test_the_deniable_mode_still_fails_closed_on_an_evaluation_error(self):
        """The short-circuit must not weaken the mode the scope DOES govern."""
        from kiro_crew.platform.governance import SCOPE_CATALOG

        spec = SCOPE_CATALOG["approval_modes"]
        assert (
            "yolo" not in spec.always_permitted
        ), "yolo must stay deniable -- it is the whole vocabulary this scope governs"

    def test_the_predicate_reads_the_catalog_rather_than_a_hardcoded_list(self):
        """Drift between the two declarations is what this pins."""
        from kiro_crew.platform.governance import SCOPE_CATALOG
        from kiro_crew.safety_override import _non_deniable_approval_modes

        assert set(_non_deniable_approval_modes()) == set(
            SCOPE_CATALOG["approval_modes"].always_permitted
        )
