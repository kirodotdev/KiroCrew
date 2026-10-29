"""Owner-granted ``SSH_AUTH_SOCK`` forwarding: the grant surface.

The READ side (``is_granted`` and the spawn-time scrub) is pinned by
``test_sandbox_ssh_auth_sock_forward.py``; this module pins the WRITE side the
Settings panel drives: the owner-gated grant / revoke round trip, its owner
gate, its audit trail, and the exact JSON the SPA consumes.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock

import pytest

from kiro_crew import security, ssh_auth_sock_consent
from kiro_crew.config.loader import ssh_auth_sock_consent_path

# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, _floor_monkeypatch):
    """Point the store at tmp so no test touches the real one."""
    store = tmp_path / "ssh_auth_sock_consent.json"
    _floor_monkeypatch.setattr(
        ssh_auth_sock_consent, "ssh_auth_sock_consent_path", lambda: store, raising=True
    )
    # ``socket_present`` resolves a live agent socket the way the spawn path does,
    # which on a host with a launchd/keyring/systemd socket reads True. Pin the
    # resolver to "nothing found" so every assertion here is host-independent;
    # the socket tests override it per test.
    from kiro_crew.agent_sdk.drivers import acp as sdk_acp

    _floor_monkeypatch.setattr(sdk_acp, "resolve_ssh_auth_sock", lambda env: None, raising=True)
    return store


def _owner_request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1"):
    """A request shaped like a real DASHBOARD OWNER call (see ``test_aws_consent``)."""
    req = MagicMock()
    req.path = "/api/ssh-agent/consent"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.query = {}
    req.rel_url.query = {}
    return req


def _handler():
    from kiro_crew.dashboard.handlers import ssh_auth_sock_consent as handler

    return handler


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the store


class TestGrantStore:
    def test_absent_store_grants_nothing(self):
        assert ssh_auth_sock_consent.is_granted() is False
        assert ssh_auth_sock_consent.read_grant() is None

    def test_record_then_read(self, _isolated_store):
        grant = ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")
        assert grant.granted_at == "2026-10-09T00:00:00+00:00"
        assert ssh_auth_sock_consent.is_granted() is True
        got = ssh_auth_sock_consent.read_grant()
        assert got is not None and got.granted_at == "2026-10-09T00:00:00+00:00"
        # The on-disk shape is exactly the documented one.
        assert json.loads(_isolated_store.read_text(encoding="utf-8")) == {
            "enabled": True,
            "granted_at": "2026-10-09T00:00:00+00:00",
        }

    def test_a_hand_written_enable_is_the_same_grant(self, _isolated_store):
        # The hand-edit grant path. ``is_granted`` reads ``enabled`` alone, so the
        # dashboard grant and the hand edit are indistinguishable at spawn, and
        # ``read_grant`` reports it with an empty timestamp rather than refusing.
        _isolated_store.write_text('{"enabled": true}', encoding="utf-8")
        assert ssh_auth_sock_consent.is_granted() is True
        got = ssh_auth_sock_consent.read_grant()
        assert got is not None and got.granted_at == ""

    @pytest.mark.parametrize("body", ['{"enabled": "true"}', '{"enabled": 1}', "{ not json", "[]"])
    def test_only_a_boolean_true_counts(self, _isolated_store, body):
        _isolated_store.write_text(body, encoding="utf-8")
        assert ssh_auth_sock_consent.is_granted() is False
        assert ssh_auth_sock_consent.read_grant() is None

    def test_revoke_writes_an_explicit_false(self, _isolated_store):
        ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")
        assert ssh_auth_sock_consent.revoke() is True
        assert ssh_auth_sock_consent.is_granted() is False
        assert json.loads(_isolated_store.read_text(encoding="utf-8")) == {"enabled": False}
        # Revoking twice is idempotent and reports nothing removed.
        assert ssh_auth_sock_consent.revoke() is False

    def test_revoke_writes_false_even_when_the_store_cannot_be_read(self, _isolated_store):
        # persist-before-you-publish: an unreadable store reads as "no consent",
        # but a withdrawal must still land on disk, or the enabled record is
        # back the moment the fault clears while the owner was told "not granted".
        ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")
        with pytest.MonkeyPatch.context() as mp:
            # What _read_all returns on an unreadable store.
            mp.setattr(ssh_auth_sock_consent, "_read_all", lambda: {}, raising=True)
            assert ssh_auth_sock_consent.revoke() is False  # nothing READABLE was held
        assert json.loads(_isolated_store.read_text(encoding="utf-8")) == {"enabled": False}
        assert ssh_auth_sock_consent.is_granted() is False

    def test_a_failed_revoke_write_propagates(self, _isolated_store, monkeypatch):
        ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")

        def _boom(data):
            raise OSError(30, "read-only file system")

        monkeypatch.setattr(ssh_auth_sock_consent, "_write_all", _boom, raising=True)
        with pytest.raises(OSError):
            ssh_auth_sock_consent.revoke()

    def test_the_store_is_written_owner_only(self, _isolated_store):
        ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")
        if os.name == "posix":
            assert (_isolated_store.stat().st_mode & 0o077) == 0

    def test_no_lock_artifact_is_created_beside_the_grant(self, _isolated_store):
        ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")
        ssh_auth_sock_consent.revoke()
        assert sorted(p.name for p in _isolated_store.parent.iterdir()) == [
            "ssh_auth_sock_consent.json"
        ]

    def test_socket_present_answers_as_the_spawn_path_would(self, monkeypatch):
        # The panel holds Allow on False, so False must mean "a spawn would
        # forward nothing": the same resolver the spawn prelude runs decides,
        # and it runs on a COPY of the environment.
        from kiro_crew.agent_sdk.drivers import acp as sdk_acp

        seen: list[dict[str, str]] = []

        def _resolve(env: dict[str, str]) -> None:
            seen.append(env)
            if env.get("SSH_AUTH_SOCK") == "":
                env["SSH_AUTH_SOCK"] = "/run/user/1000/ssh-agent.socket"

        monkeypatch.setattr(sdk_acp, "resolve_ssh_auth_sock", _resolve, raising=True)
        # Only a path that EXISTS counts: the resolver leaves a value it cannot
        # replace in place, so a dead socket from an ended login must read False.
        monkeypatch.setattr(
            ssh_auth_sock_consent.os.path, "exists", lambda p: p.endswith(".socket")
        )
        monkeypatch.setenv("SSH_AUTH_SOCK", "")
        # Unset in the gateway, but the resolver finds the systemd user socket.
        assert ssh_auth_sock_consent.socket_present() is True
        assert os.environ["SSH_AUTH_SOCK"] == ""  # the gateway's own env is untouched
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dead-login/agent.sock")
        assert ssh_auth_sock_consent.socket_present() is False
        assert seen and seen[-1] is not os.environ

    @pytest.mark.parametrize(
        ("configured", "effective", "expected"),
        [
            ("auto", "auto", True),  # the shipped default: ~/.ssh readable
            ("standard", "standard", True),
            ("off", "off", True),
            ("cc", "cc", True),
            ("strict", "strict", False),  # keys hidden: only the grant forwards
            ("auto", "strict", False),  # a governance floor raised it to strict
        ],
    )
    def test_forwarded_without_consent_follows_the_spawn_tier_verdict(
        self, monkeypatch, configured, effective, expected
    ):
        # The panel shows "the sandbox already forwards" from this, so it must
        # agree with the spawn path's own tier rule (sandbox._forward_ssh_auth_sock)
        # for an ordinary session, including the governed min_level clamp.
        from kiro_crew import platform_compat, sandbox

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False, raising=True)
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: configured, raising=True)
        monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda m: effective, raising=True)
        # Every harness unmasked: the tier alone decides.
        monkeypatch.setattr(
            ssh_auth_sock_consent, "_harness_masks", lambda: {"a": (), "b": ()}, raising=True
        )
        assert ssh_auth_sock_consent.forwarded_without_consent() is expected
        assert ssh_auth_sock_consent.forwarding_harnesses() == (["a", "b"] if expected else [])
        # And it is the same answer the spawn gives with no grant and no mask.
        monkeypatch.setattr(ssh_auth_sock_consent, "is_granted", lambda: False, raising=True)
        assert sandbox._forward_for_effective_tier(effective, ()) is expected

    def test_one_harness_masking_ssh_makes_the_grant_matter(self, monkeypatch):
        # A harness whose credential mask hides ~/.ssh (an enforced routing such
        # as codex) keeps the socket only with the grant, even on a tier that
        # leaves ~/.ssh readable; the panel must not say the grant is moot.
        from kiro_crew import platform_compat, sandbox

        ssh_dir = os.path.join(os.path.expanduser("~"), ".ssh")
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False, raising=True)
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "auto", raising=True)
        monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda m: m, raising=True)
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KIRO

        # Keyed by the RUNTIME ids, as ACP_BACKEND_ROUTING is: Kiro CLI's is "".
        monkeypatch.setattr(
            ssh_auth_sock_consent,
            "_harness_masks",
            lambda: {ACP_BACKEND_KIRO: (), ACP_BACKEND_CODEX: (ssh_dir,)},
            raising=True,
        )
        assert sandbox._forwards_without_consent("auto", ()) is True
        assert sandbox._forwards_without_consent("auto", (ssh_dir,)) is False
        assert ssh_auth_sock_consent.forwarded_without_consent() is False
        # The mixed state is named in the config spelling, so the panel can name
        # Kiro CLI rather than an empty string.
        assert ssh_auth_sock_consent.forwarding_harnesses() == ["kiro"]

    def test_harness_labels_cover_every_harness_in_the_forwarding_spelling(self):
        # Same keys as forwarding_harnesses() can return, so the panel's
        # "decided here" set is labels minus that list.
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_ROUTING, backend_to_stored

        labels = ssh_auth_sock_consent.harness_labels()
        assert set(labels) == {backend_to_stored(b) for b in ACP_BACKEND_ROUTING}
        assert labels["kiro"] == "Kiro CLI"
        assert all(labels.values())

    def test_harness_labels_fail_to_empty(self, monkeypatch):
        def boom():
            raise RuntimeError("masks unavailable")

        monkeypatch.setattr(ssh_auth_sock_consent, "_harness_masks", boom, raising=True)
        assert ssh_auth_sock_consent.harness_labels() == {}

    def test_forwarded_without_consent_reads_every_declared_harness_mask(self):
        # The real mask source: the same set kirocrew doctor walks for the notice.
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_ROUTING

        assert len(ssh_auth_sock_consent._harness_masks()) == len(ACP_BACKEND_ROUTING)

    def test_forwarded_without_consent_fails_closed(self, monkeypatch):
        # The spawn forwards nothing when the tier cannot be resolved, so the
        # panel must not claim a forward it would not get; same on Windows.
        from kiro_crew import platform_compat, sandbox

        def _boom() -> str:
            raise RuntimeError("no platform context")

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False, raising=True)
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", _boom, raising=True)
        assert ssh_auth_sock_consent.forwarded_without_consent() is False
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "auto", raising=True)
        monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda m: m, raising=True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True, raising=True)
        assert ssh_auth_sock_consent.forwarded_without_consent() is False

    def test_socket_present_is_false_when_nothing_resolves(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers import acp as sdk_acp

        monkeypatch.setattr(sdk_acp, "resolve_ssh_auth_sock", lambda env: None, raising=True)
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        assert ssh_auth_sock_consent.socket_present() is False


# ---------------------------------------------------------------- the fences


class TestKeystoneFencing:
    def test_the_grant_file_is_fenced_from_agent_file_tools(self):
        assert security.is_sensitive_path(str(ssh_auth_sock_consent_path())) is True

    def test_no_cli_verb_or_host_step_up_records_a_grant(self):
        # The grant is one owner-gated dashboard act, like the paid-AWS consent.
        # A terminal command that records it on request is a grant an automated
        # caller can take, so none exists.
        from kiro_crew.dashboard.handlers import ssh_auth_sock_consent as handler

        for name in ("arm_grant", "claim_grant", "claim_and_record", "pending_grant_path"):
            assert not hasattr(ssh_auth_sock_consent, name), name
        assert not hasattr(handler, "api_ssh_agent_consent_approve")


# ---------------------------------------------------------------- the owner gate


class TestConsentEndpointRequiresTheOwner:
    _VERBS = (
        "api_ssh_agent_consent_get",
        "api_ssh_agent_consent_post",
        "api_ssh_agent_consent_delete",
    )

    @pytest.mark.parametrize("verb", _VERBS)
    def test_an_app_token_is_refused(self, verb):
        resp = _run(getattr(_handler(), verb)(_owner_request(app="notes")))
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "dashboard_owner_required"

    @pytest.mark.parametrize("verb", _VERBS)
    def test_an_allow_listed_non_owner_is_refused(self, verb):
        resp = _run(
            getattr(_handler(), verb)(_owner_request(app="", user="slack-guest", owner="owner-1"))
        )
        assert resp.status == 403

    def test_a_non_owner_refusal_is_audited_and_grants_nothing(self, monkeypatch):
        recorded: list[tuple[str, str]] = []
        monkeypatch.setattr(
            ssh_auth_sock_consent,
            "audit_decision",
            lambda *, outcome, detail="": recorded.append((outcome, detail)),
            raising=True,
        )
        resp = _run(_handler().api_ssh_agent_consent_post(_owner_request(app="notes")))
        assert resp.status == 403
        assert recorded == [
            ("refused", "ssh_auth_sock_forward_consent.grant: non-owner caller refused")
        ]
        assert ssh_auth_sock_consent.is_granted() is False


# ---------------------------------------------------------------- the round trip


class TestConsentEndpointAsOwner:
    def test_get_reports_the_documented_shape(self, monkeypatch, tmp_path):
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KIRO

        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        monkeypatch.setattr(
            ssh_auth_sock_consent,
            "_harness_masks",
            lambda: {ACP_BACKEND_KIRO: (), ACP_BACKEND_CODEX: ()},
            raising=True,
        )
        monkeypatch.setattr(ssh_auth_sock_consent, "forwarding_harnesses", lambda: [], raising=True)
        resp = _run(_handler().api_ssh_agent_consent_get(_owner_request()))
        assert resp.status == 200
        assert json.loads(resp.text) == {
            "granted": False,
            "granted_at": None,
            "socket_present": False,
            "forwarded_without_consent": False,
            "forwarded_harnesses": [],
            # Keyed in the config spelling, like forwarded_harnesses, so the panel
            # can name the harnesses the grant decides.
            "harness_labels": {"kiro": "Kiro CLI", "codex": "OpenAI Codex"},
        }
        monkeypatch.setattr(
            ssh_auth_sock_consent, "forwarding_harnesses", lambda: ["kiro"], raising=True
        )
        body = json.loads(_run(_handler().api_ssh_agent_consent_get(_owner_request())).text)
        # Mixed: kiro forwards without the grant, codex needs it.
        assert body["forwarded_harnesses"] == ["kiro"]
        assert body["forwarded_without_consent"] is False
        monkeypatch.setattr(
            ssh_auth_sock_consent, "forwarding_harnesses", lambda: ["codex", "kiro"], raising=True
        )
        body = json.loads(_run(_handler().api_ssh_agent_consent_get(_owner_request())).text)
        # The tier verdict is reported beside the grant, never folded into it:
        # "granted" stays the RECORDED consent.
        assert body["forwarded_without_consent"] is True
        assert body["granted"] is False
        # A path that EXISTS under tmp (socket_present checks existence, and the
        # autouse fixture pins the resolver to a no-op), so the answer does not
        # depend on what the host happens to have under /tmp.
        sock = tmp_path / "agent.sock"
        sock.write_bytes(b"")
        monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
        assert (
            json.loads(_run(_handler().api_ssh_agent_consent_get(_owner_request())).text)[
                "socket_present"
            ]
            is True
        )

    def test_a_hand_written_grant_reads_as_granted_with_no_timestamp(self, _isolated_store):
        _isolated_store.write_text('{"enabled": true}', encoding="utf-8")
        body = json.loads(_run(_handler().api_ssh_agent_consent_get(_owner_request())).text)
        assert body["granted"] is True
        assert body["granted_at"] is None

    def test_delete_of_an_absent_grant_still_answers_not_granted(self):
        resp = _run(_handler().api_ssh_agent_consent_delete(_owner_request()))
        assert resp.status == 200
        assert json.loads(resp.text) == {"granted": False}

    def test_post_then_get_then_delete_round_trip(self, _isolated_store):
        handler = _handler()
        granted = _run(handler.api_ssh_agent_consent_post(_owner_request()))
        assert granted.status == 200
        body = json.loads(granted.text)
        assert body["granted"] is True and body["granted_at"]
        assert ssh_auth_sock_consent.is_granted() is True
        assert json.loads(_isolated_store.read_text(encoding="utf-8"))["enabled"] is True

        got = json.loads(_run(handler.api_ssh_agent_consent_get(_owner_request())).text)
        assert got["granted"] is True
        assert got["granted_at"] == body["granted_at"]

        deleted = _run(handler.api_ssh_agent_consent_delete(_owner_request()))
        assert deleted.status == 200
        assert json.loads(deleted.text) == {"granted": False}
        assert ssh_auth_sock_consent.is_granted() is False

    def test_post_reports_a_failed_write_instead_of_a_grant(self, monkeypatch):
        def _boom(data):
            raise OSError(30, "read-only file system")

        monkeypatch.setattr(ssh_auth_sock_consent, "_write_all", _boom, raising=True)
        resp = _run(_handler().api_ssh_agent_consent_post(_owner_request()))
        assert resp.status == 500
        body = json.loads(resp.text)
        assert body["code"] == "ssh_agent_grant_write_failed"
        assert "granted" not in body
        assert ssh_auth_sock_consent.is_granted() is False

    def test_delete_reports_a_failed_withdraw_instead_of_success(self, monkeypatch):
        ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")

        def _boom():
            raise OSError(30, "read-only file system")

        monkeypatch.setattr(ssh_auth_sock_consent, "revoke", _boom, raising=True)
        resp = _run(_handler().api_ssh_agent_consent_delete(_owner_request()))
        assert resp.status == 500
        body = json.loads(resp.text)
        assert body["code"] == "ssh_agent_withdraw_failed"
        assert "granted" not in body
        assert ssh_auth_sock_consent.is_granted() is True

    def test_grant_and_revoke_are_audited_under_the_named_event(self, monkeypatch):
        calls: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                calls.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel(), raising=True)
        ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")
        ssh_auth_sock_consent.revoke()
        ops = [(c["operation"], c["outcome"], c["caller"]) for c in calls]
        assert ops == [
            ("ssh_auth_sock_forward_consent.granted", "granted", "owner"),
            ("ssh_auth_sock_forward_consent.revoked", "revoked", "owner"),
        ]
        assert all(c["source"] == "ssh-auth-sock-consent" for c in calls)
