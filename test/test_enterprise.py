"""Tests for kiro_crew.slack.enterprise — workspace validation.

Focus: the default-open behaviour AND the fail-closed security property
when auth.test cannot verify the workspace identity but an allowlist is
configured.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.slack import enterprise


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset cached module state and silence SEL between tests."""
    enterprise._validated_team_id = ""
    enterprise._validated_enterprise_id = ""
    enterprise._allowed_team_ids = set()
    enterprise._allowlist_configured = False
    with patch.object(enterprise, "sel", return_value=MagicMock()):
        yield
    enterprise._validated_team_id = ""
    enterprise._validated_enterprise_id = ""
    enterprise._allowed_team_ids = set()
    enterprise._allowlist_configured = False


def _install_fake_slack_sdk(resp: dict | None = None, *, raise_exc: bool = False):
    """Install a fake ``slack_sdk.web`` module exposing WebClient.

    Returns a context-managing patch on sys.modules. ``auth_test`` returns
    ``resp`` (or raises if ``raise_exc``).
    """
    mod = types.ModuleType("slack_sdk")
    web_mod = types.ModuleType("slack_sdk.web")

    class _FakeWebClient:
        def __init__(self, *_, **__):
            pass

        def auth_test(self):
            if raise_exc:
                raise RuntimeError("auth.test boom")
            return resp or {}

    web_mod.WebClient = _FakeWebClient
    mod.web = web_mod
    return patch.dict(sys.modules, {"slack_sdk": mod, "slack_sdk.web": web_mod})


def _fake_config(allowed_ids: list[str]):
    cfg = MagicMock()
    cfg.slack.allowed_enterprise_ids = list(allowed_ids)
    return cfg


# --------------------------------------------------------------------------
# Default-open behaviour (no allowlist)
# --------------------------------------------------------------------------


def test_no_allowlist_accepts_any_workspace():
    resp = {"team_id": "T_RANDOM", "team": "Random Co", "url": "https://x"}
    with _install_fake_slack_sdk(resp), patch.object(
        enterprise.KiroCrewConfig, "load", return_value=_fake_config([])
    ):
        assert enterprise.validate_enterprise("xoxb-token") is True
    assert enterprise._allowlist_configured is False
    # check_message_origin accepts anything when no allowlist configured.
    assert enterprise.check_message_origin("T_ANYTHING") is True
    assert enterprise.check_message_origin("") is True


def test_auth_test_failure_no_allowlist_defaults_open():
    """auth.test fails, no allowlist -> default-open (return True)."""
    with _install_fake_slack_sdk(raise_exc=True), patch.object(
        enterprise.KiroCrewConfig, "load", return_value=_fake_config([])
    ):
        assert enterprise.validate_enterprise("xoxb-token") is True
    assert enterprise._allowlist_configured is False
    assert enterprise.check_message_origin("T_WHATEVER") is True


# --------------------------------------------------------------------------
# Allowlist configured + auth.test succeeds
# --------------------------------------------------------------------------


def test_allowlist_accepts_listed_workspace():
    resp = {"team_id": "T_GOOD", "team": "Good Co", "url": "https://x"}
    with _install_fake_slack_sdk(resp), patch.object(
        enterprise.KiroCrewConfig, "load", return_value=_fake_config(["T_GOOD"])
    ):
        assert enterprise.validate_enterprise("xoxb-token") is True
    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_GOOD") is True
    assert enterprise.check_message_origin("T_OTHER") is False


def test_allowlist_rejects_unlisted_enterprise():
    # On Enterprise Grid auth.test returns an org-level enterprise_id; when
    # an allowlist is configured and that enterprise_id is not listed,
    # validation must fail (the token's own team_id does not bypass it).
    resp = {
        "team_id": "T_BAD",
        "enterprise_id": "E_NOT_LISTED",
        "team": "Bad Co",
        "url": "https://x",
    }
    with _install_fake_slack_sdk(resp), patch.object(
        enterprise.KiroCrewConfig, "load", return_value=_fake_config(["E_GOOD"])
    ):
        assert enterprise.validate_enterprise("xoxb-token") is False


def test_extra_ids_form_allowlist_and_enforce():
    resp = {
        "team_id": "T_GOOD",
        "enterprise_id": "E_NOT_LISTED",
        "team": "Good Co",
        "url": "https://x",
    }
    with _install_fake_slack_sdk(resp), patch.object(
        enterprise.KiroCrewConfig, "load", return_value=_fake_config([])
    ):
        # extra_ids does not contain the enterprise_id -> validation fails.
        assert (
            enterprise.validate_enterprise("xoxb-token", extra_ids={"T_ONLY"})
            is False
        )


# --------------------------------------------------------------------------
# Fail-closed: allowlist configured + auth.test FAILS (the security hole)
# --------------------------------------------------------------------------


def test_auth_test_failure_with_config_allowlist_fails_closed():
    """auth.test fails but slack.allowed_enterprise_ids is set -> deny."""
    sel_mock = MagicMock()
    with _install_fake_slack_sdk(raise_exc=True), patch.object(
        enterprise.KiroCrewConfig, "load", return_value=_fake_config(["T_ALLOWED"])
    ), patch.object(enterprise, "sel", return_value=sel_mock):
        assert enterprise.validate_enterprise("xoxb-token") is False
    # A denial must be SEL-audited.
    audited = [
        c.kwargs
        for c in sel_mock.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "denied"
    ]
    assert audited, "expected a SEL denial audit entry"
    assert audited[-1]["error"] == "auth_test_unavailable_with_allowlist"
    # check_message_origin must also deny: no validated team_id was cached.
    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_REAL_WORKSPACE") is False
    # Only the explicitly allowlisted id (which we could not verify against
    # the live workspace) is in the set; the real workspace id is denied.
    assert enterprise.check_message_origin("T_ALLOWED") is True


def test_auth_test_failure_with_extra_ids_fails_closed():
    """auth.test fails but extra_ids passed -> deny (no fail-open)."""
    sel_mock = MagicMock()
    with _install_fake_slack_sdk(raise_exc=True), patch.object(
        enterprise.KiroCrewConfig, "load", return_value=_fake_config([])
    ), patch.object(enterprise, "sel", return_value=sel_mock):
        assert (
            enterprise.validate_enterprise("xoxb-token", extra_ids={"T_EXTRA"})
            is False
        )
    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_REAL_WORKSPACE") is False


def test_auth_test_failure_with_allowlist_and_bad_config_load_fails_closed():
    """auth.test fails, config load also fails, but extra_ids set -> deny.

    Even if config cannot be read, an explicit extra_ids allowlist must
    still force fail-closed.
    """
    with _install_fake_slack_sdk(raise_exc=True), patch.object(
        enterprise.KiroCrewConfig, "load", side_effect=RuntimeError("no config")
    ):
        assert (
            enterprise.validate_enterprise("xoxb-token", extra_ids={"T_EXTRA"})
            is False
        )
    assert enterprise._allowlist_configured is True


def test_auth_test_failure_bad_config_no_allowlist_defaults_open():
    """auth.test fails, config load fails, no extra_ids -> default-open."""
    with _install_fake_slack_sdk(raise_exc=True), patch.object(
        enterprise.KiroCrewConfig, "load", side_effect=RuntimeError("no config")
    ):
        assert enterprise.validate_enterprise("xoxb-token") is True
    assert enterprise._allowlist_configured is False


# --------------------------------------------------------------------------
# check_message_origin direct coverage
# --------------------------------------------------------------------------


def test_check_message_origin_denies_empty_team_id_when_allowlist():
    enterprise._allowlist_configured = True
    enterprise._allowed_team_ids = {"T_GOOD"}
    assert enterprise.check_message_origin("") is False


# --------------------------------------------------------------------------
# Governance channels.posture (un-weakenable, agent cannot edit) — the
# enterprise security policy pins allowed_enterprise_ids ABOVE the operator's
# config.json allowlist. A workspace must satisfy BOTH.
# --------------------------------------------------------------------------


def _install_governance_posture(allowed_enterprise_ids: list[str]):
    """Install a PlatformContext carrying a channels.posture slack allowlist."""
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    policy = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "channels": {
                "members": {"mode": "allow", "allow": ["slack"]},
                "posture": {
                    "slack": {
                        "allowed_enterprise_ids": {
                            "mode": "allow",
                            "allow": list(allowed_enterprise_ids),
                        }
                    }
                },
            },
        }
    )
    base = build_default_context(KiroCrewConfig.load())
    ctx_mod.set_context(dataclasses.replace(base, governance=policy))


def test_governance_posture_blocks_workspace_outside_policy():
    # config.json has NO allowlist (default-open), but the governance posture
    # pins enterprise E_GOOD. A workspace E_EVIL must be REJECTED by the policy
    # ceiling even though the operator config would have accepted it.
    from kiro_crew.platform import context as ctx_mod

    resp = {"enterprise_id": "E_EVIL", "team_id": "T1", "team": "Evil", "url": "https://x"}
    try:
        _install_governance_posture(["E_GOOD"])
        with _install_fake_slack_sdk(resp), patch.object(
            enterprise.KiroCrewConfig, "load", return_value=_fake_config([])
        ):
            assert enterprise.validate_enterprise("xoxb-token") is False
    finally:
        ctx_mod.reset_context()


def test_governance_posture_allows_pinned_workspace():
    from kiro_crew.platform import context as ctx_mod

    resp = {"enterprise_id": "E_GOOD", "team_id": "T1", "team": "Good", "url": "https://x"}
    try:
        _install_governance_posture(["E_GOOD"])
        with _install_fake_slack_sdk(resp), patch.object(
            enterprise.KiroCrewConfig, "load", return_value=_fake_config([])
        ):
            assert enterprise.validate_enterprise("xoxb-token") is True
    finally:
        ctx_mod.reset_context()


def test_no_governance_posture_is_default_open():
    # No policy installed → the governance posture check is a no-op (default-open).
    resp = {"enterprise_id": "E_ANY", "team_id": "T1", "team": "Any", "url": "https://x"}
    with _install_fake_slack_sdk(resp), patch.object(
        enterprise.KiroCrewConfig, "load", return_value=_fake_config([])
    ):
        assert enterprise.validate_enterprise("xoxb-token") is True


def test_governance_posture_blocks_empty_enterprise_id_when_pinned():
    # Slack returns enterprise_id="" for EVERY non-Enterprise-Grid workspace (the
    # common case). An empty id cannot satisfy an explicitly-pinned
    # allowed_enterprise_ids ceiling, so it must FAIL CLOSED — not silently pass
    # via the old `if not value: continue`. (security-review blocking.)
    from kiro_crew.platform import context as ctx_mod

    resp = {"enterprise_id": "", "team_id": "T1", "team": "NonGrid", "url": "https://x"}
    try:
        _install_governance_posture(["E_GOOD"])
        with _install_fake_slack_sdk(resp), patch.object(
            enterprise.KiroCrewConfig, "load", return_value=_fake_config([])
        ):
            assert enterprise.validate_enterprise("xoxb-token") is False
    finally:
        ctx_mod.reset_context()


def test_governance_posture_empty_enterprise_id_ok_when_not_pinned():
    # Symmetry: with NO enterprise_ids leaf pinned (only allowed_team_ids is), an
    # empty enterprise_id must NOT be over-rejected — the sentinel probe sees the
    # enterprise leaf is unpinned and skips it, while the pinned team leaf still
    # gates. The common non-Enterprise-Grid workspace (enterprise_id="") on a
    # team-pinned policy is accepted iff its team matches.
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    policy = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "channels": {
                "members": {"mode": "allow", "allow": ["slack"]},
                "posture": {"slack": {"allowed_team_ids": {"mode": "allow", "allow": ["T_OK"]}}},
            },
        }
    )
    resp = {"enterprise_id": "", "team_id": "T_OK", "team": "NonGrid", "url": "https://x"}
    try:
        base = build_default_context(KiroCrewConfig.load())
        ctx_mod.set_context(dataclasses.replace(base, governance=policy))
        with _install_fake_slack_sdk(resp), patch.object(
            enterprise.KiroCrewConfig, "load", return_value=_fake_config([])
        ):
            # enterprise leaf unpinned → empty id skipped; team T_OK matches → True.
            assert enterprise.validate_enterprise("xoxb-token") is True
    finally:
        ctx_mod.reset_context()


# --------------------------------------------------------------------------
# A config that cannot be READ is not a config that says "no allowlist" (#3945)
# --------------------------------------------------------------------------
#
# KiroCrewConfig.load() catches JSONDecodeError/OSError, warns, and returns
# DEFAULTS -- it never raises. So the `except Exception` in
# _load_allowed_team_ids, written for exactly this case, never fired: a corrupt
# or torn config.json handed back an empty allowed_enterprise_ids,
# _allowlist_configured flipped to False, and check_message_origin took its
# default-open path, accepting every origin. These tests drive the REAL loader
# against a real corrupt file on disk rather than mocking load(), because the
# whole defect is that the mock-visible failure path is not the one taken.


@pytest.fixture
def _config_on_disk(tmp_path, monkeypatch):
    """Point the loader at a tmp config pair and return (base, overlay)."""
    import kiro_crew.config.loader as loader

    base = tmp_path / "config.json"
    overlay = tmp_path / "config.local.json"
    monkeypatch.setattr(loader, "config_path", lambda: base)
    monkeypatch.setattr(loader, "config_local_path", lambda: overlay)
    return base, overlay


def test_corrupt_config_refuses_a_foreign_workspace_instead_of_admitting_it(
    _config_on_disk,
):
    """The regression the issue asks for: a malformed config.json must not
    reopen the allowlist for an origin the operator never listed."""
    base, _ = _config_on_disk
    base.write_text('{"slack": {"allowed_enterprise_ids": ["T_ALLOWED"]')  # truncated

    resp = {"team_id": "T_HOME", "team": "Home Co", "url": "https://x"}
    with _install_fake_slack_sdk(resp):
        enterprise.validate_enterprise("xoxb-token")

    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_FOREIGN") is False
    # The validated workspace itself still works -- this narrows, not bricks.
    assert enterprise.check_message_origin("T_HOME") is True


def test_a_corrupt_overlay_fails_closed_too(_config_on_disk):
    """config.local.json is deep-merged and swallows its own parse errors, so
    a corrupt overlay hides the allowlist just as effectively as a corrupt base."""
    base, overlay = _config_on_disk
    base.write_text('{"slack": {"allowed_enterprise_ids": ["T_ALLOWED"]}}')
    overlay.write_text("}not json{")

    resp = {"team_id": "T_HOME", "team": "Home Co", "url": "https://x"}
    with _install_fake_slack_sdk(resp):
        enterprise.validate_enterprise("xoxb-token")

    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_FOREIGN") is False


def test_a_readable_config_with_no_allowlist_still_defaults_open(_config_on_disk):
    """Control: the fail-closed path must key off UNREADABLE, not off empty.
    An operator who configured nothing is still unrestricted."""
    base, _ = _config_on_disk
    base.write_text('{"slack": {}}')

    resp = {"team_id": "T_HOME", "team": "Home Co", "url": "https://x"}
    with _install_fake_slack_sdk(resp):
        assert enterprise.validate_enterprise("xoxb-token") is True

    assert enterprise._allowlist_configured is False
    assert enterprise.check_message_origin("T_ANYTHING") is True


def test_an_absent_config_still_defaults_open(_config_on_disk):
    """Control: absent is the ordinary unconfigured state, not an error."""
    resp = {"team_id": "T_HOME", "team": "Home Co", "url": "https://x"}
    with _install_fake_slack_sdk(resp):
        assert enterprise.validate_enterprise("xoxb-token") is True

    assert enterprise._allowlist_configured is False
    assert enterprise.check_message_origin("T_ANYTHING") is True


def test_corrupt_config_is_sel_audited(_config_on_disk):
    """The degradation must be visible to an operator, not silent."""
    base, _ = _config_on_disk
    base.write_text("}not json{")

    fake_sel = MagicMock()
    resp = {"team_id": "T_HOME", "team": "Home Co", "url": "https://x"}
    with _install_fake_slack_sdk(resp), patch.object(
        enterprise, "sel", return_value=fake_sel
    ):
        enterprise.validate_enterprise("xoxb-token")

    errors = [
        c.kwargs.get("error") for c in fake_sel.log_api_access.call_args_list
    ]
    assert "config_unreadable_failing_closed" in errors


def test_auth_test_failure_with_corrupt_config_fails_closed(_config_on_disk):
    """The other read of the allowlist in this module has the same shape: an
    unverifiable workspace plus an unreadable config must not continue
    default-open on the strength of an allowlist we could not read."""
    base, _ = _config_on_disk
    base.write_text('{"slack": {"allowed_enterprise_ids": ["T_ALLOWED"]')  # truncated

    with _install_fake_slack_sdk(raise_exc=True):
        assert enterprise.validate_enterprise("xoxb-token") is False
    assert enterprise._allowlist_configured is True


def test_auth_test_failure_with_readable_empty_config_still_defaults_open(
    _config_on_disk,
):
    """Control for the above: a readable config with no allowlist keeps the
    documented default-open behaviour when auth.test is unavailable."""
    base, _ = _config_on_disk
    base.write_text('{"slack": {}}')

    with _install_fake_slack_sdk(raise_exc=True):
        assert enterprise.validate_enterprise("xoxb-token") is True
    assert enterprise._allowlist_configured is False
