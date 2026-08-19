"""Tests for kiro_crew.slack.enterprise — workspace validation.

Focus: the default-open behaviour AND the fail-closed security property
when auth.test cannot verify the workspace identity but an allowlist is
configured.
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.slack import enterprise


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path, monkeypatch):
    """Reset cached module state and silence SEL between tests.

    ``_load_allowed_team_ids`` now inspects ``config.json`` on disk to tell a
    corrupt config apart from a genuinely unconfigured allowlist, so every test
    runs against an isolated, initially-empty ``KIROCREW_HOME`` to avoid coupling
    to the developer's / CI runner's ambient config file.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
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


def _write_allowlist(home, allowed_ids: list[str]) -> None:
    """Write a real config.json carrying *allowed_ids*.

    The allowlist is read from the FILE by one validated reader, so these
    tests drive real config bytes instead of mocking the loader -- mocking it
    is what let three widening bugs through unnoticed.
    """
    (home / "config.json").write_text(
        json.dumps({"slack": {"allowed_enterprise_ids": list(allowed_ids)}}),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Default-open behaviour (no allowlist)
# --------------------------------------------------------------------------


def test_no_allowlist_accepts_any_workspace(tmp_path):
    resp = {"team_id": "T_RANDOM", "team": "Random Co", "url": "https://x"}
    _write_allowlist(tmp_path, [])
    with _install_fake_slack_sdk(resp):
        assert enterprise.validate_enterprise("xoxb-token") is True
    assert enterprise._allowlist_configured is False
    # check_message_origin accepts anything when no allowlist configured.
    assert enterprise.check_message_origin("T_ANYTHING") is True
    assert enterprise.check_message_origin("") is True


def test_auth_test_failure_no_allowlist_defaults_open(tmp_path):
    """auth.test fails, no allowlist -> default-open (return True)."""
    _write_allowlist(tmp_path, [])
    with _install_fake_slack_sdk(raise_exc=True):
        assert enterprise.validate_enterprise("xoxb-token") is True
    assert enterprise._allowlist_configured is False
    assert enterprise.check_message_origin("T_WHATEVER") is True


# --------------------------------------------------------------------------
# Allowlist configured + auth.test succeeds
# --------------------------------------------------------------------------


def test_allowlist_accepts_listed_workspace(tmp_path):
    resp = {"team_id": "T_GOOD", "team": "Good Co", "url": "https://x"}
    _write_allowlist(tmp_path, ["T_GOOD"])
    with _install_fake_slack_sdk(resp):
        assert enterprise.validate_enterprise("xoxb-token") is True
    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_GOOD") is True
    assert enterprise.check_message_origin("T_OTHER") is False


def test_allowlist_rejects_unlisted_enterprise(tmp_path):
    # On Enterprise Grid auth.test returns an org-level enterprise_id; when
    # an allowlist is configured and that enterprise_id is not listed,
    # validation must fail (the token's own team_id does not bypass it).
    resp = {
        "team_id": "T_BAD",
        "enterprise_id": "E_NOT_LISTED",
        "team": "Bad Co",
        "url": "https://x",
    }
    _write_allowlist(tmp_path, ["E_GOOD"])
    with _install_fake_slack_sdk(resp):
        assert enterprise.validate_enterprise("xoxb-token") is False


def test_auth_test_failure_reader_exception_fails_closed_not_crash(tmp_path):
    """An unexpected reader exception must fail closed, never escape.

    This branch runs inside the `except` that handles an auth.test failure, so
    an exception raised here propagates out of `validate_enterprise` and up
    through `init_socket_mode()` -- taking the gateway down. The pre-fix code
    wrapped its own config read in `except Exception`; the sibling call in
    `_load_allowed_team_ids` still does, so leaving this one bare was an
    asymmetry. `ConfigReadError` is already handled by the reader; this covers
    the classes it does not model (e.g. a RecursionError from pathologically
    nested JSON).
    """
    boom = RecursionError("maximum recursion depth exceeded")
    with _install_fake_slack_sdk(raise_exc=True):
        with patch.object(enterprise, "_read_allowlist", side_effect=boom):
            # Must return a verdict, not raise.
            assert enterprise.validate_enterprise("xoxb-token") is False

    # And the verdict must be the fail-closed one.
    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_ANY") is False


def test_degraded_read_refuses_swapped_non_grid_workspace(tmp_path):
    """A degraded read must not admit whichever workspace authenticated.

    The reachable shape GPT named. `candidate = enterprise_id or team_id`, so on
    a NON-Grid workspace the candidate IS the bare team_id -- which used to be
    the one id a degraded read admitted. A bot token pointing at a foreign
    workspace therefore validated against itself and the operator's restriction
    silently stopped applying, while startup still reported success.

    The two inputs live in separate places -- the token in `.env` / the
    environment, the allowlist in `config.json` -- so an env-only token swap
    needs no file edit and the unreadable config can be an independent
    accident. They do not imply one another.
    """
    (tmp_path / "config.json").write_text("}{ broken", encoding="utf-8")

    resp = {
        "team_id": "T_EVIL",
        "enterprise_id": "",  # non-Grid: candidate falls back to team_id
        "team": "Foreign Co",
        "url": "https://evil",
    }
    with _install_fake_slack_sdk(resp):
        assert enterprise.validate_enterprise("xoxb-token") is False

    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_EVIL") is False


def test_clean_config_still_admits_its_own_workspace(tmp_path):
    """Boundary in the other direction: a READABLE allowlist must still work.

    Guards against "simplifying" the degraded refusal into refusing whenever an
    allowlist is configured at all, which would lock out every legitimate
    operator. Without this the tightening above has no lower bound.
    """
    _write_allowlist(tmp_path, ["T_CORP"])
    resp = {
        "team_id": "T_CORP",
        "enterprise_id": "",
        "team": "Corp",
        "url": "https://corp",
    }
    with _install_fake_slack_sdk(resp):
        assert enterprise.validate_enterprise("xoxb-token") is True
    assert enterprise.check_message_origin("T_CORP") is True
    assert enterprise.check_message_origin("T_OTHER") is False


def test_dangling_symlink_config_fails_closed(tmp_path):
    """A symlinked config whose target is gone must NOT read as unconfigured.

    `read_config_for_update` returns `{}` for a dangling symlink exactly as it
    does for a missing file, so the allowlist would have silently reopened. The
    link is a configuration artifact: it says the operator meant config to live
    here, so this is "config unavailable", not "never configured".
    """
    target = tmp_path / "real-config.json"
    target.write_text(
        json.dumps({"slack": {"allowed_enterprise_ids": ["T_REAL"]}}),
        encoding="utf-8",
    )
    link = tmp_path / "config.json"
    link.symlink_to(target)

    # Intact symlink: read normally, allowlist enforced.
    ids, refusal = enterprise._read_allowlist()
    assert refusal == ""
    assert ids == {"T_REAL"}

    target.unlink()  # now dangling

    ids, refusal = enterprise._read_allowlist()
    assert ids is None, "a dangling symlinked config was treated as absent"
    assert "symlink target is missing" in refusal

    enterprise._load_allowed_team_ids()
    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_ANY") is False


def test_symlink_to_empty_config_stays_default_open(tmp_path):
    """An INTACT symlink to a file holding `{}` is genuinely unconfigured.

    Guards the boundary of the test above: the refusal must key on the target
    being missing, not on the path being a symlink. Failing closed here would
    lock out anyone whose config is symlink-managed but carries no allowlist.
    """
    target = tmp_path / "real-config.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "config.json"
    link.symlink_to(target)

    ids, refusal = enterprise._read_allowlist()
    assert refusal == ""
    assert ids == set()

    enterprise._load_allowed_team_ids()
    assert enterprise._allowlist_configured is False
    assert enterprise.check_message_origin("T_ANY") is True


def test_degraded_read_does_not_admit_caller_supplied_ids(tmp_path):
    """A degraded read must not be widened by caller-supplied ``extra_ids``.

    The caller passes ``extra_ids`` from its OWN ``KiroCrewConfig.load()``-derived
    config object, and `load()` degrades a torn ``config.local.json`` overlay by
    DROPPING it -- so the caller's value is the pre-overlay BASE list. If the
    union happened anyway, an origin the operator removed in the overlay would
    stay admitted, defeating the refusal that `_read_allowlist` just made. That
    is the two-reader problem one level up: the caller is the second reader.

    Here the overlay is torn, so the intended list is unknowable and nothing is
    admitted -- ``T_REMOVED`` (present in the base list the caller still holds)
    least of all.
    """
    (tmp_path / "config.json").write_text(
        json.dumps({"slack": {"allowed_enterprise_ids": ["T_KEPT", "T_REMOVED"]}}),
        encoding="utf-8",
    )
    # Torn overlay: load() drops it and returns the base list; _read_allowlist refuses.
    (tmp_path / "config.local.json").write_text('{"slack": {', encoding="utf-8")

    resp = {
        "team_id": "T_KEPT",
        "enterprise_id": "",
        "team": "Kept Co",
        "url": "https://x",
    }
    with _install_fake_slack_sdk(resp):
        # The caller hands back the base list its own degraded load() produced.
        enterprise.validate_enterprise(
            "xoxb-token", extra_ids={"T_KEPT", "T_REMOVED"}
        )

    assert enterprise._allowlist_configured is True
    assert "T_REMOVED" not in enterprise._allowed_team_ids, (
        "a degraded read was re-widened by caller-supplied extra_ids"
    )
    assert enterprise.check_message_origin("T_REMOVED") is False


def test_clean_read_does_not_admit_caller_supplied_ids(tmp_path):
    """A SUCCESSFUL read is authoritative; a stale snapshot must not widen it.

    ``extra_ids`` is the caller's earlier ``KiroCrewConfig.load()`` snapshot of
    the same ``slack.allowed_enterprise_ids`` key this module reads, so the read
    here is never older. An id the caller still holds but the read did not
    return is therefore an id REMOVED from the file since the snapshot -- and
    unioning it would undo that removal.

    Without this, an operator who removes a workspace mid-startup keeps
    admitting it until the process restarts.
    """
    _write_allowlist(tmp_path, ["T_KEPT"])

    resp = {
        "team_id": "T_KEPT",
        "enterprise_id": "",
        "team": "Kept Co",
        "url": "https://x",
    }
    with _install_fake_slack_sdk(resp):
        # The caller's snapshot predates the removal of T_REMOVED.
        assert (
            enterprise.validate_enterprise(
                "xoxb-token", extra_ids={"T_KEPT", "T_REMOVED"}
            )
            is True
        )

    assert enterprise._allowlist_configured is True
    assert "T_REMOVED" not in enterprise._allowed_team_ids, (
        "a stale caller snapshot re-admitted a removed workspace"
    )
    assert enterprise.check_message_origin("T_REMOVED") is False
    # The boundary: the id the file still lists stays admitted.
    assert enterprise.check_message_origin("T_KEPT") is True


def test_emptied_allowlist_goes_default_open_despite_caller_snapshot(tmp_path):
    """An allowlist emptied in the file means "no allowlist", snapshot or not.

    This is the direction where honouring the file WIDENS: the caller's
    snapshot still lists an id, but the file now lists none, and a clean file
    listing none is default-open by contract. Enforcing the snapshot instead
    would keep applying a restriction the operator just deleted, and would
    disagree with what the very next restart does.
    """
    _write_allowlist(tmp_path, [])

    resp = {
        "team_id": "T_GOOD",
        "enterprise_id": "E_NOT_LISTED",
        "team": "Good Co",
        "url": "https://x",
    }
    with _install_fake_slack_sdk(resp):
        assert (
            enterprise.validate_enterprise("xoxb-token", extra_ids={"T_ONLY"})
            is True
        )

    assert enterprise._allowlist_configured is False
    assert enterprise.check_message_origin("T_ANY") is True


# --------------------------------------------------------------------------
# Fail-closed: allowlist configured + auth.test FAILS (the security hole)
# --------------------------------------------------------------------------


def test_auth_test_failure_with_config_allowlist_fails_closed(tmp_path):
    """auth.test fails but slack.allowed_enterprise_ids is set -> deny."""
    sel_mock = MagicMock()
    _write_allowlist(tmp_path, ["T_ALLOWED"])
    with _install_fake_slack_sdk(raise_exc=True), patch.object(
        enterprise, "sel", return_value=sel_mock
    ):
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


def test_auth_test_failure_with_only_a_stale_snapshot_stays_default_open():
    """A stale snapshot must not manufacture a restriction on the failure path.

    ``auth.test`` failing means the workspace identity is unverifiable. Whether
    that is fatal depends on one thing: is a restriction actually configured?
    Here the config honestly lists none, and ``extra_ids`` is the caller's older
    snapshot of that same key -- so treating it as a restriction would refuse
    startup for a workspace nobody restricted, and would contradict the rule
    that the file decides even when it lists nothing.

    The unreadable-config case is deliberately NOT this case and still refuses;
    ``test_auth_test_failure_with_allowlist_and_bad_config_load_fails_closed``
    pins that.
    """
    with _install_fake_slack_sdk(raise_exc=True):
        assert (
            enterprise.validate_enterprise("xoxb-token", extra_ids={"T_EXTRA"})
            is True
        )
    assert enterprise._allowlist_configured is False


def test_auth_test_failure_with_configured_allowlist_still_fails_closed(tmp_path):
    """The boundary: a restriction the FILE lists still forces a deny.

    This is the half that must not move -- an unverifiable workspace against a
    real configured allowlist is the original hole.
    """
    _write_allowlist(tmp_path, ["T_ALLOWED"])
    with _install_fake_slack_sdk(raise_exc=True):
        assert (
            enterprise.validate_enterprise("xoxb-token", extra_ids={"T_EXTRA"})
            is False
        )
    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_EXTRA") is False
    assert enterprise.check_message_origin("T_REAL_WORKSPACE") is False


def test_auth_test_failure_with_allowlist_and_bad_config_load_fails_closed(tmp_path):
    """auth.test fails, config unreadable, but extra_ids set -> deny.

    Even if config cannot be read, an explicit extra_ids allowlist must
    still force fail-closed.
    """
    (tmp_path / "config.json").write_text("{ torn", encoding="utf-8")
    with _install_fake_slack_sdk(raise_exc=True):
        assert (
            enterprise.validate_enterprise("xoxb-token", extra_ids={"T_EXTRA"})
            is False
        )
    assert enterprise._allowlist_configured is True


def test_auth_test_failure_unreadable_config_no_extra_ids_fails_closed(tmp_path):
    """auth.test fails AND config is unreadable, no extra_ids -> deny.

    BEHAVIOUR CHANGE. This branch used to swallow the config-read error, leave
    the allowlist empty, and read that as "no restriction configured" -- which
    ACCEPTS an unverifiable workspace. An unreadable config cannot be told apart
    from a configured restriction, so it must not be read as permission: this
    path now fails closed like the startup path. A genuinely ABSENT config still
    defaults open (next test).
    """
    (tmp_path / "config.json").write_text("}{ broken", encoding="utf-8")
    with _install_fake_slack_sdk(raise_exc=True):
        assert enterprise.validate_enterprise("xoxb-token") is False
    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_ANY") is False


def test_auth_test_failure_no_config_file_defaults_open(tmp_path):
    """auth.test fails with NO config file and no extra_ids -> default-open.

    Guards against over-fail-closing: an absent config is genuinely
    unconfigured, so this branch stays permissive exactly as before.
    """
    with _install_fake_slack_sdk(raise_exc=True):
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


def test_governance_posture_blocks_workspace_outside_policy(tmp_path):
    # config.json has NO allowlist (default-open), but the governance posture
    # pins enterprise E_GOOD. A workspace E_EVIL must be REJECTED by the policy
    # ceiling even though the operator config would have accepted it.
    from kiro_crew.platform import context as ctx_mod

    resp = {"enterprise_id": "E_EVIL", "team_id": "T1", "team": "Evil", "url": "https://x"}
    try:
        _install_governance_posture(["E_GOOD"])
        _write_allowlist(tmp_path, [])
        with _install_fake_slack_sdk(resp):
            assert enterprise.validate_enterprise("xoxb-token") is False
    finally:
        ctx_mod.reset_context()


def test_governance_posture_allows_pinned_workspace(tmp_path):
    from kiro_crew.platform import context as ctx_mod

    resp = {"enterprise_id": "E_GOOD", "team_id": "T1", "team": "Good", "url": "https://x"}
    try:
        _install_governance_posture(["E_GOOD"])
        _write_allowlist(tmp_path, [])
        with _install_fake_slack_sdk(resp):
            assert enterprise.validate_enterprise("xoxb-token") is True
    finally:
        ctx_mod.reset_context()


def test_no_governance_posture_is_default_open(tmp_path):
    # No policy installed → the governance posture check is a no-op (default-open).
    resp = {"enterprise_id": "E_ANY", "team_id": "T1", "team": "Any", "url": "https://x"}
    _write_allowlist(tmp_path, [])
    with _install_fake_slack_sdk(resp):
        assert enterprise.validate_enterprise("xoxb-token") is True


def test_governance_posture_blocks_empty_enterprise_id_when_pinned(tmp_path):
    # Slack returns enterprise_id="" for EVERY non-Enterprise-Grid workspace (the
    # common case). An empty id cannot satisfy an explicitly-pinned
    # allowed_enterprise_ids ceiling, so it must FAIL CLOSED — not silently pass
    # via the old `if not value: continue`. (security-review blocking.)
    from kiro_crew.platform import context as ctx_mod

    resp = {"enterprise_id": "", "team_id": "T1", "team": "NonGrid", "url": "https://x"}
    try:
        _install_governance_posture(["E_GOOD"])
        _write_allowlist(tmp_path, [])
        with _install_fake_slack_sdk(resp):
            assert enterprise.validate_enterprise("xoxb-token") is False
    finally:
        ctx_mod.reset_context()


def test_governance_posture_empty_enterprise_id_ok_when_not_pinned(tmp_path):
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
        _write_allowlist(tmp_path, [])
        with _install_fake_slack_sdk(resp):
            # enterprise leaf unpinned → empty id skipped; team T_OK matches → True.
            assert enterprise.validate_enterprise("xoxb-token") is True
    finally:
        ctx_mod.reset_context()


# --------------------------------------------------------------------------
# Fail-closed: a corrupt config.json silently reopens the allowlist (#3945).
#
# KiroCrewConfig.load() degrades a torn/corrupt config to a *defaults* object
# instead of raising, so allowed_enterprise_ids comes back empty -- which the
# old code could not tell apart from "operator configured no allowlist" and so
# fell back to default-open. These tests exercise the REAL loader against a
# genuinely malformed file on disk.
# --------------------------------------------------------------------------


def test_corrupt_config_json_fails_closed(tmp_path):
    """A malformed config.json must fail CLOSED, not reopen the allowlist.

    Regression for #3945: writes a malformed config.json, then asserts
    check_message_origin() REFUSES a foreign team_id (and still admits the
    validated one). Without the fix _allowlist_configured flips False and the
    foreign origin is accepted default-open.
    """
    (tmp_path / "config.json").write_text("{ not valid json ", encoding="utf-8")
    # State reached inside validate_enterprise() after auth.test caches the
    # workspace team_id; then the allowlist is (re)loaded from the corrupt file.
    enterprise._validated_team_id = "T_VALIDATED"
    enterprise._load_allowed_team_ids()

    # The degraded read is treated as "could not read config", not "unconfigured".
    assert enterprise._allowlist_configured is True
    # Degraded: NOTHING is admitted, not even the validated workspace --
    # admitting it would answer "who may authenticate" with "whoever just
    # did", which is the restriction's own question.
    assert enterprise.check_message_origin("T_FOREIGN") is False
    assert enterprise.check_message_origin("T_VALIDATED") is False


def test_non_object_config_json_fails_closed(tmp_path):
    """Valid JSON that is not an OBJECT must also fail CLOSED.

    ``[]`` parses cleanly, so a bare ``json.loads`` probe calls the file healthy
    -- but ``load()`` still discards a non-dict for defaults, so the allowlist
    would reopen through this sibling branch. Delegating to
    ``read_config_for_update`` closes it, since that raises ``ConfigReadError``
    for a non-object too.
    """
    (tmp_path / "config.json").write_text("[]", encoding="utf-8")
    enterprise._validated_team_id = "T_VALIDATED"
    enterprise._load_allowed_team_ids()

    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_FOREIGN") is False
    assert enterprise.check_message_origin("T_VALIDATED") is False


def test_corrupt_config_json_degradation_is_sel_audited(tmp_path):
    """The fail-closed degradation must be SEL-audited."""
    (tmp_path / "config.json").write_text("}{ broken", encoding="utf-8")
    sel_mock = MagicMock()
    enterprise._validated_team_id = "T_VALIDATED"
    with patch.object(enterprise, "sel", return_value=sel_mock):
        enterprise._load_allowed_team_ids()
    audited = [
        c.kwargs
        for c in sel_mock.log_api_access.call_args_list
        if c.kwargs.get("error") == "config_load_degraded_fail_closed"
    ]
    assert audited, "expected a SEL audit entry for the degraded config load"
    assert audited[-1]["outcome"] == "denied"


def test_corrupt_config_local_overlay_fails_closed(tmp_path):
    """A malformed config.local.json overlay must also fail CLOSED.

    The user-owned overlay carries operator config that survives upgrades; a
    torn overlay silently drops the allowlist the same way a torn base does.
    """
    # Valid base, corrupt overlay -> load() degrades the overlay to a warning
    # and drops it; the on-disk overlay still parses-fails, so we fail closed.
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config.local.json").write_text("{ broken overlay ", encoding="utf-8")
    enterprise._validated_team_id = "T_VALIDATED"
    enterprise._load_allowed_team_ids()

    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_FOREIGN") is False


def test_corrupt_overlay_does_not_retain_base_allowlist(tmp_path):
    """A corrupt overlay must not leave the BASE file's allowlist in force.

    ``load()`` merges ``config.local.json`` over ``config.json`` and swallows a
    torn overlay, so the base file's entries survive and ``configured`` comes
    back NON-EMPTY. Testing "did the operator configure entries?" before testing
    for degradation therefore applied a list the operator did not ask for -- the
    operator's overlay (which may narrow the base) was silently dropped. The
    degradation gate runs first, so this fails closed to the validated team_id.
    """
    (tmp_path / "config.json").write_text(
        '{"slack": {"allowed_enterprise_ids": ["T_BASE_ONLY"]}}', encoding="utf-8"
    )
    (tmp_path / "config.local.json").write_text("{ torn overlay", encoding="utf-8")
    enterprise._validated_team_id = "T_VALIDATED"
    enterprise._load_allowed_team_ids()

    assert enterprise._allowlist_configured is True
    # The base entry is NOT admitted: we cannot know the overlay's intent.
    assert enterprise.check_message_origin("T_BASE_ONLY") is False
    assert enterprise.check_message_origin("T_FOREIGN") is False
    # Nothing is admitted on a degraded read, not even the authenticated
    # workspace: which workspace may authenticate is the allowlist's own
    # question, so it cannot answer itself.
    assert enterprise.check_message_origin("T_VALIDATED") is False


def test_non_object_slack_section_fails_closed(tmp_path):
    """A non-object `slack` section must fail CLOSED.

    `{"slack": []}` is a valid top-level object, so a whole-file health probe
    accepts it -- but the loader coerces a non-dict section to `{}`
    (`config/loader.py` slack_data guard), dropping the allowlist and going
    default-open. Reading the value and judging usability in ONE pass closes it.
    """
    (tmp_path / "config.json").write_text('{"slack": []}', encoding="utf-8")
    enterprise._validated_team_id = "T_VALIDATED"
    enterprise._load_allowed_team_ids()

    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_FOREIGN") is False
    assert enterprise.check_message_origin("T_VALIDATED") is False


def test_allowlist_of_only_unusable_ids_fails_closed(tmp_path):
    """An allowlist whose entries are ALL unusable must fail CLOSED.

    The loader keeps only ids starting with E or T, so `["bogus"]` collapses to
    an empty list -- indistinguishable from "configured nothing", which means
    default-open. The operator plainly asked for a restriction, so an allowlist
    we cannot honour must not be read as permission.
    """
    (tmp_path / "config.json").write_text(
        '{"slack": {"allowed_enterprise_ids": ["bogus", "also-bad"]}}',
        encoding="utf-8",
    )
    enterprise._validated_team_id = "T_VALIDATED"
    enterprise._load_allowed_team_ids()

    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_FOREIGN") is False
    assert enterprise.check_message_origin("T_VALIDATED") is False


def test_allowlist_mixed_validity_keeps_the_usable_ids(tmp_path):
    """Mixed valid/invalid entries keep the valid ones -- no regression.

    Dropping a non-conforming entry NARROWS the allowlist, so it is not a
    widening door; failing closed here would break every operator who has a
    stray typo alongside working ids.
    """
    (tmp_path / "config.json").write_text(
        '{"slack": {"allowed_enterprise_ids": ["T_REAL", "bogus"]}}',
        encoding="utf-8",
    )
    enterprise._validated_team_id = "T_VALIDATED"
    enterprise._load_allowed_team_ids()

    assert enterprise._allowlist_configured is True
    assert enterprise.check_message_origin("T_REAL") is True
    assert enterprise.check_message_origin("T_VALIDATED") is True
    assert enterprise.check_message_origin("T_FOREIGN") is False


def test_reader_entry_filter_does_not_drift_from_the_loader(tmp_path):
    """Pin `_read_allowlist`'s entry filter to `KiroCrewConfig.load()`'s.

    The reader necessarily re-states which entries are usable (the loader keeps
    only ids starting with E or T). That is a standing sync obligation, and
    drift here fails toward LOCKOUT rather than widening -- an availability
    trap, not a security hole -- but nothing pinned the two together. This
    compares them BEHAVIOURALLY over a spread of shapes instead of copying the
    predicate, so a change to either side fails here rather than surfacing as
    an operator whose allowlist quietly stops matching.
    """
    candidates = [
        "T_TEAM",        # usable
        "E_ENTERPRISE",  # usable
        "t_lowercase",   # dropped: case-sensitive prefix
        "X_OTHER",       # dropped: wrong prefix
        "",              # dropped: empty
    ]
    (tmp_path / "config.json").write_text(
        json.dumps({"slack": {"allowed_enterprise_ids": candidates}}),
        encoding="utf-8",
    )

    from kiro_crew.config.loader import KiroCrewConfig

    via_loader = set(KiroCrewConfig.load().slack.allowed_enterprise_ids)
    via_reader, refusal = enterprise._read_allowlist()

    assert refusal == "", f"reader unexpectedly refused a usable config: {refusal}"
    assert via_reader == via_loader, (
        "entry-filter drift between slack/enterprise.py and config/loader.py: "
        f"reader={sorted(via_reader)} loader={sorted(via_loader)}"
    )
    # Guard the pin itself: a spread that collapsed to all-or-nothing would make
    # the equality assertion above pass without discriminating anything.
    assert via_loader, "fixture must yield at least one usable id"
    assert len(via_loader) < len(candidates), "fixture must drop at least one id"


def test_clean_config_no_allowlist_stays_default_open(tmp_path):
    """Healthy path preserved: a clean config with no allowlist is default-open.

    Guards against over-fail-closing -- a genuinely unconfigured allowlist (a
    valid config file that simply lists none) must stay default-open exactly as
    before the fix.
    """
    (tmp_path / "config.json").write_text('{"slack": {}}', encoding="utf-8")
    enterprise._validated_team_id = "T_VALIDATED"
    enterprise._load_allowed_team_ids()

    assert enterprise._allowlist_configured is False
    assert enterprise.check_message_origin("T_FOREIGN") is True


def test_no_config_file_stays_default_open(tmp_path):
    """A never-set-up home (no config file at all) stays default-open."""
    # tmp_path has no config.json / config.local.json.
    enterprise._validated_team_id = "T_VALIDATED"
    enterprise._load_allowed_team_ids()

    assert enterprise._allowlist_configured is False
    assert enterprise.check_message_origin("T_FOREIGN") is True
