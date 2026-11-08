"""The two legacy identity consumers read the signed per-session token.

Covers ``mcp_caller.CallerContext.from_env`` (the key gatewayd stamps on forwarded calls)
and ``mcp_shared._resolve_tool_policy`` (the ``X-Session-Key`` and policy cache key). The
env and pid sources name the wrong session after a warm-pool rekey or under
``spawn_run`` sharing.

Two traps are caches: ``from_env`` memoises a legacy identity, so the token must be read
above that cache, and the policy cache is keyed on the resolved session, not the
gateway-supplied one, or sessions on one process would share a policy.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from test_mcp_shared import _LoopHarness, _tools_call

from kiro_crew import mcp_caller, mcp_shared, session_pid_sig, session_token_sig
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import stub as mcp_stub
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

LIVE_KEY = "dashboard:chat-9-current"
STALE_KEY = "dashboard:chat-7-previous"
PARENT_KEY = "dashboard:chat-1-parent"
SUBAGENT_KEY = "dashboard:chat-2-subagent"
TOKEN = "e" * 64
OTHER_TOKEN = "f" * 64


def _path_for(cfg, token: str):
    return cfg / f"session_token_{hashlib.sha256(token.encode()).hexdigest()}.sig"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Isolated mapping dir + synthetic trust root, with every other source off.

    ``config_dir`` is patched on three modules that resolve it independently; any left
    on the real home could let a host ``session_pid`` file for an ancestor answer.
    """
    key_path = tmp_path / "sel_hmac.key"
    key_path.write_bytes(b"\x03" * 32)
    (tmp_path / ".local_secret").write_text("test-secret", encoding="utf-8")
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV, raising=False)
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.setattr(mcp_caller, "_FROM_ENV_CACHE", None)
    mcp_shared._excluded_tools_by_session.clear()
    monkeypatch.setattr(mcp_shared, "_last_failure_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_key", "")
    monkeypatch.setattr(mcp_shared, "_failure_count", 0)
    with (
        patch.object(session_token_sig, "config_dir", return_value=tmp_path),
        patch.object(session_pid_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_token_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None),
        patch.object(mcp_shared, "config_dir", return_value=tmp_path),
        patch("kiro_crew.config.loader.config_dir", return_value=tmp_path),
        patch("kiro_crew.member_memory_auth.protected_member_session_for_pid", return_value=None),
    ):
        session_pid_sig._reported.clear()
        yield tmp_path
        session_pid_sig._reported.clear()
    mcp_shared._excluded_tools_by_session.clear()


@pytest.fixture
def policy(monkeypatch, cfg):
    """Drive ``_resolve_tool_policy`` with the gateway round-trip captured.

    Returns a callable recording every request the resolver made, so a test can read
    the ``X-Session-Key`` the policy was ASKED under rather than only the set it got
    back — the header and the cache key are the two things attribution consists of.
    """
    conf = MagicMock()
    conf.dashboard.url = "http://localhost:5476/"
    monkeypatch.setattr(mcp_shared.KiroCrewConfig, "load", classmethod(lambda cls: conf))
    monkeypatch.setattr(mcp_shared, "parse_dashboard_url", lambda url: ("localhost", 5476))
    audit = MagicMock()
    monkeypatch.setattr(mcp_shared, "sel", lambda: audit)
    requests: list[dict[str, str]] = []
    policies: dict[str, list[str]] = {}

    def _urlopen(req, timeout=None):
        requests.append(dict(req.headers))
        body = MagicMock()
        body.read.return_value = json.dumps(
            {"exclude": policies.get(req.headers.get("X-session-key", ""), [])}
        ).encode("utf-8")
        body.__enter__ = MagicMock(return_value=body)
        body.__exit__ = MagicMock(return_value=False)
        return body

    monkeypatch.setattr(mcp_shared, "loopback_urlopen", _urlopen)

    return SimpleNamespace(
        audit=audit,
        requests=requests,
        policies=policies,
        resolve=lambda caller_session="": mcp_shared._resolve_tool_policy(caller_session).excluded,
        policy_for=mcp_shared._resolve_tool_policy,
        asked_keys=lambda: [r.get("X-session-key", "") for r in requests],
    )


# CallerContext.from_env — the client-side resolver behind the stub's caller block


class TestFromEnvReadsTheToken:
    def test_a_valid_token_beats_a_stale_env_key(self, cfg, monkeypatch):
        """THE warm-pool case, at the resolver the stub's register block uses."""
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        ctx = CallerContext.from_env()
        assert ctx.session_key == LIVE_KEY
        assert ctx.session_type == "token"
        assert ctx.from_gateway is False

    def test_the_stub_caller_block_carries_the_token_resolved_key(self, cfg, monkeypatch):
        """Through the real inbound caller: what gatewayd is told this stub is.

        ``_build_caller_block`` is what the register and recaller frames carry, so a
        token read that never reached it would leave the wire naming the stale
        session while the backend-side resolver named the live one.
        """
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        assert mcp_stub._build_caller_block(None)["session_key"] == LIVE_KEY

    def test_a_primed_legacy_cache_does_not_shadow_the_token(self, cfg, monkeypatch):
        """The cache is the reason a token read BELOW it would be inert.

        ``from_env`` memoises the first resolved legacy identity for the life of the
        process, and a warm-pool child resolves at least once before it is rekeyed —
        so the ordering that matters is token-above-CACHE, not only token-above-env.
        """
        monkeypatch.setattr(mcp_caller, "_FROM_ENV_CACHE", CallerContext(session_key=STALE_KEY))
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        assert CallerContext.from_env().session_key == LIVE_KEY

    def test_a_rekey_is_observable_in_the_same_process(self, cfg, monkeypatch):
        """One process, one token, two successive owners.

        The token deliberately survives a rekey, so the mapping is what changes.
        Both calls happen in one interpreter with the cache primed by the first, which
        is the state a live MCP child is in when its runtime is re-claimed.
        """
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        session_token_sig.publish_session_token(TOKEN, STALE_KEY)
        assert CallerContext.from_env().session_key == STALE_KEY
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        assert CallerContext.from_env().session_key == LIVE_KEY

    def test_a_resolved_token_is_never_memoised(self, cfg, monkeypatch):
        """The cache holds legacy identities only — the pin behind the rekey one."""
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        assert CallerContext.from_env().session_key == LIVE_KEY
        assert mcp_caller._FROM_ENV_CACHE is None

    def test_two_sessions_on_one_runtime_resolve_their_own_key(self, cfg, monkeypatch):
        """``spawn_run`` session sharing: one process, one pid, two sessions."""
        session_token_sig.publish_session_token(TOKEN, PARENT_KEY)
        session_token_sig.publish_session_token(OTHER_TOKEN, SUBAGENT_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        assert CallerContext.from_env().session_key == PARENT_KEY
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, OTHER_TOKEN)
        assert CallerContext.from_env().session_key == SUBAGENT_KEY

    def test_an_unverifiable_token_falls_through_to_the_env_key(self, cfg, monkeypatch):
        """Canonical semantics: an unresolvable token costs nothing.

        A token that SHADOWED the env var would trade a stale-identity bug for a
        no-identity one, so a missing mapping and a forged one both fall through.
        """
        _path_for(cfg, OTHER_TOKEN).write_text(f"{'0' * 64}\n{LIVE_KEY}", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        for token in (TOKEN, OTHER_TOKEN):
            monkeypatch.setattr(mcp_caller, "_FROM_ENV_CACHE", None)
            monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, token)
            ctx = CallerContext.from_env()
            assert ctx.session_key == STALE_KEY
            assert ctx.session_type == "env"

    def test_no_token_leaves_behaviour_unchanged(self, cfg, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        ctx = CallerContext.from_env()
        assert ctx.session_key == STALE_KEY
        assert ctx.session_type == "env"

    @pytest.mark.parametrize("protected", ["dashboard:member-review", ""])
    def test_a_protected_record_outranks_the_token(self, cfg, monkeypatch, protected):
        """Protected member ancestry stays above the token, valid or invalid.

        The empty case is the load-bearing half: an invalid or revoked protected
        record is an EXPLICIT refusal, so it must not be converted into a
        token/env/pid answer. A token naming another session is present to make a
        fall-through visible if one were introduced.
        """
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        with patch(
            "kiro_crew.member_memory_auth.protected_member_session_for_pid",
            return_value=protected,
        ):
            ctx = CallerContext.from_env()
        assert ctx.session_key == protected
        assert ctx.session_type == "protected-pid"

    def test_a_raising_protected_probe_refuses_and_never_reaches_the_env_key(
        self, cfg, monkeypatch
    ):
        """A raising binding probe is a REFUSAL, not absence — the fence holds.

        The fenced process can ``chmod`` its own binding record unreadable and could then
        fall through to the ambient session, so every exception maps to ``""`` (hard
        deny), ``PermissionError`` included. Only a host override that can tell a
        sandbox deny apart returns ``None``.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        monkeypatch.setattr(mcp_caller, "_FROM_ENV_CACHE", None)

        for exc in (PermissionError("seatbelt deny"), OSError(5, "EIO"), RuntimeError("bug")):

            def _boom(_pid, _exc=exc, **_kw):
                raise _exc

            monkeypatch.setattr(mcp_caller, "_FROM_ENV_CACHE", None)
            with patch(
                "kiro_crew.member_memory_auth.protected_member_session_for_pid",
                _boom,
            ):
                ctx = CallerContext.from_env()
            assert ctx.session_key == "", f"{type(exc).__name__} fell through to env"
            assert ctx.session_type == "protected-pid"


# _resolve_excluded_tools — the managed-tool-policy lookup


class TestToolPolicyReadsTheToken:
    def test_the_policy_is_asked_under_the_token_resolved_key(self, cfg, monkeypatch, policy):
        """Attribution: the resolved session is what the request names.

        A policy fetched under a stale key is the WRONG policy, not merely a
        misattributed one — the endpoint answers per session.
        """
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        policy.policies[LIVE_KEY] = ["live_only_tool"]
        policy.policies[STALE_KEY] = ["stale_tool"]
        assert policy.resolve() == {"live_only_tool"}
        assert policy.asked_keys() == [LIVE_KEY]

    def test_two_sessions_on_one_process_do_not_share_a_policy(self, cfg, monkeypatch, policy):
        """The cache was keyed on the GATEWAY identity, so every unnamed caller
        shared one entry. Each session's own token is what separates them."""
        session_token_sig.publish_session_token(TOKEN, PARENT_KEY)
        session_token_sig.publish_session_token(OTHER_TOKEN, SUBAGENT_KEY)
        policy.policies[PARENT_KEY] = ["parent_tool"]
        policy.policies[SUBAGENT_KEY] = ["subagent_tool"]
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        assert policy.resolve() == {"parent_tool"}
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, OTHER_TOKEN)
        assert policy.resolve() == {"subagent_tool"}
        assert policy.asked_keys() == [PARENT_KEY, SUBAGENT_KEY]

    def test_the_cache_is_keyed_on_the_resolved_session(self, cfg, monkeypatch, policy):
        """A repeat call for the SAME resolved session still costs no round-trip."""
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        policy.policies[LIVE_KEY] = ["live_only_tool"]
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        assert policy.resolve() == {"live_only_tool"}
        assert policy.resolve() == {"live_only_tool"}
        assert policy.asked_keys() == [LIVE_KEY]
        assert set(mcp_shared._excluded_tools_by_session) == {LIVE_KEY}

    def test_a_rekey_refetches_instead_of_serving_the_primed_policy(self, cfg, monkeypatch, policy):
        """Warm rekey with the cache already primed, in one process.

        Keyed on the gateway identity, the first fetch's entry answered for every
        later call whatever the mapping now said, so a rekeyed session enforced the
        previous session's policy for the life of the process.
        """
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        policy.policies[STALE_KEY] = ["stale_tool"]
        policy.policies[LIVE_KEY] = ["live_only_tool"]
        session_token_sig.publish_session_token(TOKEN, STALE_KEY)
        assert policy.resolve() == {"stale_tool"}
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        assert policy.resolve() == {"live_only_tool"}
        assert policy.asked_keys() == [STALE_KEY, LIVE_KEY]

    def test_the_gateway_caller_outranks_the_token(self, cfg, monkeypatch, policy):
        """Per-call gateway identity is stamped per CALL, so it cannot go stale."""
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        policy.policies["dashboard:from-gateway"] = ["gateway_tool"]
        policy.policies[LIVE_KEY] = ["live_only_tool"]
        assert policy.resolve("dashboard:from-gateway") == {"gateway_tool"}
        assert policy.asked_keys() == ["dashboard:from-gateway"]

    def test_an_unverifiable_token_falls_through_to_the_env_key(self, cfg, monkeypatch, policy):
        _path_for(cfg, TOKEN).write_text(f"{'0' * 64}\n{LIVE_KEY}", encoding="utf-8")
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        policy.policies[STALE_KEY] = ["stale_tool"]
        assert policy.resolve() == {"stale_tool"}
        assert policy.asked_keys() == [STALE_KEY]

    def test_no_token_and_no_key_still_fails_open_without_caching(self, cfg, monkeypatch, policy):
        """Unchanged: kiro-cli caches one ``tools/list``, so an unidentified caller
        must not be told it has no tools."""
        assert policy.resolve() == set()
        assert policy.asked_keys() == []
        assert mcp_shared._excluded_tools_by_session == {}
        ops = [c.kwargs.get("operation") for c in policy.audit.log_api_access.call_args_list]
        assert "tool_policy.no_session_key" in ops

    @pytest.mark.parametrize("protected", ["dashboard:member-review", ""])
    def test_a_protected_record_outranks_the_token(self, cfg, monkeypatch, policy, protected):
        """An invalid protected record refuses rather than falling through."""
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        policy.policies[protected or "unused"] = ["protected_tool"]
        policy.policies[LIVE_KEY] = ["live_only_tool"]
        with patch(
            "kiro_crew.member_memory_auth.protected_member_session_for_pid",
            return_value=protected,
        ):
            resolved = policy.resolve()
        if protected:
            assert resolved == {"protected_tool"}
            assert policy.asked_keys() == [protected]
        else:
            assert resolved == set()
            assert policy.asked_keys() == []
            # A refused identity is "no session key", not a broken resolution: the
            # record answered, and what it said was no.
            with patch(
                "kiro_crew.member_memory_auth.protected_member_session_for_pid",
                return_value=protected,
            ):
                assert (
                    mcp_shared._resolve_tool_policy(ignore_negative_cache=True).unresolved
                    == "no_session_key"
                )

    def test_an_identity_resolved_mid_race_window_is_not_masked_by_it(
        self, cfg, monkeypatch, policy
    ):
        """The startup-race window is for a call that found NO identity.

        The window is process-global, and ``tools/call`` does not refuse on
        ``no_session_key``, so masking a resolved key would leave an operator exclusion
        unenforced until the window closed.
        """
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        policy.policies[LIVE_KEY] = ["live_only_tool"]
        # No mapping yet: this call opens the race window.
        first = mcp_shared._resolve_tool_policy()
        assert first.unresolved == "no_session_key"
        assert mcp_shared._last_startup_race_time > 0.0
        # The claim lands inside the window.
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        second = mcp_shared._resolve_tool_policy()
        assert second.unresolved == ""
        assert second.excluded == {"live_only_tool"}
        assert policy.asked_keys() == [LIVE_KEY]

    def test_a_404_window_debounces_the_same_key_and_no_other(self, cfg, monkeypatch, policy):
        """The window keeps its 404 debounce -- for the identity it was opened for.

        A gateway that has not registered THIS session yet is a race of this
        session's; a sibling on the same process that resolves a different key is
        not in it, and a rekey mid-window is the sibling case in one process.
        """
        session_token_sig.publish_session_token(TOKEN, STALE_KEY)
        session_token_sig.publish_session_token(OTHER_TOKEN, LIVE_KEY)
        policy.policies[LIVE_KEY] = ["live_only_tool"]
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)

        def _not_registered(req, timeout=None):
            raise urllib.error.HTTPError(
                url="", code=404, msg="agent not resolved", hdrs=None, fp=None
            )

        with patch.object(mcp_shared, "loopback_urlopen", _not_registered):
            assert mcp_shared._resolve_tool_policy().unresolved == "agent_not_resolved"
            # Same key, inside the window: debounced, no second round-trip.
            assert mcp_shared._resolve_tool_policy().unresolved == "no_session_key"
        assert mcp_shared._last_startup_race_key == STALE_KEY
        # A different key on the same process is not in that race.
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, OTHER_TOKEN)
        third = mcp_shared._resolve_tool_policy()
        assert third.unresolved == ""
        assert third.excluded == {"live_only_tool"}
        assert policy.asked_keys() == [LIVE_KEY]

    def test_broken_resolution_keeps_the_long_window_not_the_race_one(
        self, cfg, monkeypatch, policy
    ):
        """A broken host is not a startup race, and the two have different windows.

        Resolution answers a key, ``""`` (none yet or refused) or ``None`` (broken), so a
        raising probe gets the 60s window and ``resolution_failed``, not the 5s race.
        """

        def _boom(_pid, **_kw):
            raise RuntimeError("unreadable home")

        with patch("kiro_crew.member_memory_auth.protected_member_session_for_pid", _boom):
            assert policy.resolve() == set()
        ops = [c.kwargs.get("operation") for c in policy.audit.log_api_access.call_args_list]
        assert ops == ["tool_policy.resolution_failed"]
        assert mcp_shared._last_failure_time > 0.0
        assert mcp_shared._last_startup_race_time == 0.0
        with patch("kiro_crew.member_memory_auth.protected_member_session_for_pid", _boom):
            assert (
                mcp_shared._resolve_tool_policy(ignore_negative_cache=True).unresolved
                == "resolution_failed"
            )

    def test_an_eperm_probe_is_resolution_failed_not_env_fallthrough(
        self, cfg, monkeypatch, policy
    ):
        """``PermissionError`` from the probe is no different from any other raise.

        A sandbox deny and one the fenced process induced look the same here, so EPERM
        as absence would fall through to ``KIROCREW_SESSION_KEY``.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        policy.policies[STALE_KEY] = ["stale_tool"]

        def _deny(_pid, **_kw):
            raise PermissionError(1, "Operation not permitted")

        with patch("kiro_crew.member_memory_auth.protected_member_session_for_pid", _deny):
            assert policy.resolve() == set()
            assert (
                mcp_shared._resolve_tool_policy(ignore_negative_cache=True).unresolved
                == "resolution_failed"
            )
        assert policy.asked_keys() == []

    def test_a_failed_fetch_is_attributed_to_the_resolved_session(self, cfg, monkeypatch, policy):
        """The audit trail names the session the request was made for.

        Every failure path read ``KIROCREW_SESSION_KEY`` directly, so a rekeyed or
        subagent session's fail-open was recorded against the wrong session — the
        one case where the trail is all that is left.
        """
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)

        def _boom(req, timeout=None):
            raise urllib.error.URLError("gateway down")

        monkeypatch.setattr(mcp_shared, "loopback_urlopen", _boom)
        assert policy.resolve() == set()
        callers = {c.kwargs.get("caller") for c in policy.audit.log_api_access.call_args_list}
        assert callers == {LIVE_KEY}


class TestDispatchLoopAuditAttribution:
    """The stdio loop's audit records for an unstamped call name the token's session.

    Three SEL records share ``_ambient_audit_session``, which reads the token before the
    env var; after a warm-pool rekey the env var names the previous session.
    """

    def test_an_unstamped_call_is_audited_under_the_token_session(self, cfg, monkeypatch):
        session_token_sig.publish_session_token(TOKEN, LIVE_KEY)
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, TOKEN)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)

        def _tool(name, args):
            # A tool that escapes with an exception is audited as ``failed`` --
            # the second ambient-attribution site, behind the rejection one.
            raise RuntimeError("boom")

        harness = _LoopHarness(monkeypatch, _tool)
        # The harness installs its own permissive policy stub; this one excludes
        # ``blocked`` so the rejection audit fires.
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset({"blocked"}), ""),
        )
        try:
            harness.send(_tools_call(31, "blocked"))
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_excluded"
            assert kw["session_key"] == LIVE_KEY
            harness.send(_tools_call(32, "echo"))
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 2)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "failed"
            assert kw["session_key"] == LIVE_KEY
        finally:
            harness.close()

    def test_no_token_keeps_the_env_fallback(self, cfg, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", STALE_KEY)
        assert mcp_shared._ambient_audit_session() == STALE_KEY
        monkeypatch.delenv("KIROCREW_SESSION_KEY")
        assert mcp_shared._ambient_audit_session() == "mcp"


# A locally resolved key with no attestation fails closed without a futile dial
#
# With no gateway caller block and no signed token, the only key left is the
# unsigned ``session_pid_<pid>.txt`` read. The gateway answers a request under it
# with ``409 member_identity_unavailable``, so the resolver must not dial; it
# returns ``identity_unattestable``, a reason in ``_UNRESOLVED_REFUSES_CALL``, so
# ``tools/call`` refuses (an unread operator exclusion is an unknown deny).

LENIENT_KEY = "dashboard:chat-9-current"


class _Body:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def close(self) -> None:
        pass


def _refuse_unattested(req, timeout=None):
    raise urllib.error.HTTPError(
        url="",
        code=409,
        msg="unattested",
        hdrs=None,
        fp=_Body(b'{"code": "member_identity_unavailable"}'),
    )


@pytest.fixture
def dialled(monkeypatch, tmp_path):
    """A resolver primed to dial, recording every request it puts on the wire."""
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV, raising=False)
    monkeypatch.setattr(mcp_shared, "_last_failure_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_key", "")
    monkeypatch.setattr(mcp_shared, "_failure_count", 0)
    mcp_shared._excluded_tools_by_session.clear()
    monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda _x: (5476, "test"))
    monkeypatch.setattr(mcp_shared, "read_local_secret", lambda _port: "test-secret")
    monkeypatch.setattr(mcp_shared, "current_caller", lambda: None)
    audit = MagicMock()
    monkeypatch.setattr(mcp_shared, "sel", lambda: audit)
    sent: list[dict] = []

    def record(req, timeout=None):
        sent.append(dict(req.headers))
        return _refuse_unattested(req, timeout)

    monkeypatch.setattr(mcp_shared, "loopback_urlopen", record)
    yield audit, sent
    mcp_shared._excluded_tools_by_session.clear()


def test_a_locally_resolved_key_with_no_token_is_not_sent(monkeypatch, dialled):
    """A lenient key, no token, no gateway caller: the key never reaches the wire.

    That request can only ever be refused, so the resolver returns the fail-closed
    ``identity_unattestable`` reason instead of dialling.
    """
    audit, sent = dialled
    monkeypatch.setattr(mcp_shared, "_policy_session_key", lambda: LENIENT_KEY)
    policy = mcp_shared._resolve_tool_policy()
    assert policy.unresolved == "identity_unattestable"
    # Fail CLOSED: a key resolved, so an operator exclusion may exist, and the
    # reason must make ``tools/call`` refuse rather than silently widen it.
    assert policy.unresolved in mcp_shared._UNRESOLVED_REFUSES_CALL
    assert sent == [], "an unattestable key was put on the wire"
    ops = [c.kwargs.get("operation") for c in audit.log_api_access.call_args_list]
    assert "tool_policy.unattestable_key" in ops


def test_a_locally_resolved_key_with_a_token_is_still_sent(monkeypatch, dialled):
    """A signed token on the element makes the key attestable, so it is sent."""
    audit, sent = dialled
    monkeypatch.setattr(mcp_shared, "_policy_session_key", lambda: LENIENT_KEY)
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, "f" * 64)
    monkeypatch.setattr(
        "kiro_crew.session_token_sig.session_token_header",
        lambda token="": {"X-Session-Token": "f" * 64},
    )

    def ok(req, timeout=None):
        sent.append(dict(req.headers))
        body = MagicMock()
        body.read.return_value = b'{"exclude": []}'
        body.__enter__ = MagicMock(return_value=body)
        body.__exit__ = MagicMock(return_value=False)
        return body

    monkeypatch.setattr(mcp_shared, "loopback_urlopen", ok)
    policy = mcp_shared._resolve_tool_policy()
    assert policy.unresolved == ""
    assert len(sent) == 1
    assert sent[0].get("X-session-key") == LENIENT_KEY
    assert sent[0].get("X-session-token") == "f" * 64


def test_a_gateway_stamped_caller_without_a_token_is_still_sent(monkeypatch, dialled):
    """A gateway-named caller is vouched for even with no per-call token, so it dials.

    ``caller_session`` is the gateway's own per-call identity; the gateway already
    named this caller, so the key is legitimate and the resolver must still ask under
    it. Only a LOCALLY resolved key with no token is withheld.
    """
    audit, sent = dialled
    policy = mcp_shared._resolve_tool_policy("dashboard:from-gateway")
    # It dialled (and got the 409 refusal), so the key reached the wire.
    assert len(sent) == 1
    assert sent[0].get("X-session-key") == "dashboard:from-gateway"
    assert policy.unresolved == "identity_unattested"
