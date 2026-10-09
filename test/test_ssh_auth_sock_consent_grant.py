"""Owner-granted ``SSH_AUTH_SOCK`` forwarding: the grant surface.

The READ side (``is_granted`` and the spawn-time scrub) is pinned by
``test_sandbox_ssh_auth_sock_forward.py``; this module pins the WRITE side the
Settings panel drives: the owner-gated arm / host-only approve / owner revoke
round trip, its fences, its audit trail, the keystone and sandbox registration
of the new nonce leaf, and the exact JSON the SPA consumes.

Mirrors ``test_file_delivery_consent.py`` deliberately -- the two consents are
one design applied twice, and a divergence between the two test files would be
the first sign the implementations had drifted apart.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import security, ssh_auth_sock_consent
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import ssh_auth_sock_consent_path

# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, _floor_monkeypatch):
    """Point the store AND the armed-nonce file at tmp so no test touches the real ones.

    Through ``_floor_monkeypatch`` (D11): an autouse patch made through the shared
    ``monkeypatch`` is lifted by any test that calls ``monkeypatch.undo()``, and a
    lifted store path would point the next assertion at the real data home.
    """
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
    _floor_monkeypatch.setattr(
        ssh_auth_sock_consent,
        "pending_grant_path",
        lambda: tmp_path / ssh_auth_sock_consent._PENDING_GRANT_DIRNAME / "nonce.json",
        raising=True,
    )
    return store


@pytest.fixture
def _permissive_host(monkeypatch):
    """Every approve-time fence in its PERMITTING state.

    Each one resolves to a REFUSING value on some supported CI host (a backend-less
    runner makes ``credential_mask_applies`` False; native Windows delegates masking
    and reads every pid as unconfined), so a test about the round trip rather than
    about one fence has to stub them all -- see the matching fixture in
    ``test_file_delivery_consent.py`` for the full argument.
    """
    from kiro_crew import sandbox
    from kiro_crew.computer_use import enable_state

    monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=True)
    monkeypatch.setattr(sandbox, "spawn_delegates_masking", lambda **k: False, raising=True)
    monkeypatch.setattr(sandbox, "unconfined_live_agent_pid", lambda pids: None, raising=True)


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


def _local_approve_request(*, nonce, local: bool = True):
    """A request shaped like the host CLI's approve POST."""
    req = MagicMock()
    store = {"internal_auth": True} if local else {}
    if not local:
        req.remote = "203.0.113.7"
    req.get = lambda key, default=None: store.get(key, default)

    async def _json():
        return {"nonce": nonce}

    req.json = _json
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
        with pytest.raises(OSError):
            ssh_auth_sock_consent.withdraw()

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

    def test_socket_present_is_false_when_nothing_resolves(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers import acp as sdk_acp

        monkeypatch.setattr(sdk_acp, "resolve_ssh_auth_sock", lambda env: None, raising=True)
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        assert ssh_auth_sock_consent.socket_present() is False


# ---------------------------------------------------------------- the fences


class TestKeystoneFencing:
    def test_the_grant_file_is_fenced_from_agent_file_tools(self):
        assert security.is_sensitive_path(str(ssh_auth_sock_consent_path())) is True

    def test_the_nonce_leaf_is_masked_from_the_agent_sandbox(self):
        # Same registration as the file-delivery leaf, for the same forge path:
        # keystone-fenced (file gate) AND bind-masked (no runtime-shell forge) AND
        # precreated (the mask loop is isdir-guarded and the dir is created lazily
        # at arm time). Not under trust/.
        from kiro_crew import sandbox

        leaf = ssh_auth_sock_consent._PENDING_GRANT_DIRNAME
        assert leaf == "ssh-auth-sock-consent-pending"
        assert leaf in security._CREW_SECRET_LEAVES
        assert leaf in sandbox._CREW_HIDDEN_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert leaf not in sandbox._CREW_READONLY_LEAVES
        real_parent = (
            ssh_auth_sock_consent.data_home() / leaf / ssh_auth_sock_consent._PENDING_GRANT_FILENAME
        ).parent.name
        assert real_parent == leaf
        assert real_parent != "trust"

    def test_precreate_materialises_the_nonce_dir_before_spawn(self, tmp_path, monkeypatch):
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
        target = tmp_path / ssh_auth_sock_consent._PENDING_GRANT_DIRNAME
        assert not target.exists()
        created = sandbox._materialize_maskable_dirs()
        assert target.is_dir()
        assert str(target) in created
        if os.name == "posix":
            assert (target.stat().st_mode & 0o077) == 0

    def test_the_approve_path_is_strict_internal_not_mixed(self):
        from kiro_crew.dashboard import server

        path = "/api/ssh-agent/consent/approve"
        assert path in server._STRICT_INTERNAL_API_PATHS
        assert path not in server._MIXED_INTERNAL_API_PATHS
        assert "/api/file-delivery/consent/approve" in server._STRICT_INTERNAL_API_PATHS

    def test_the_cli_verb_is_an_approve_step_up_not_a_self_grant(self):
        """The verb finishes an owner-armed grant by presenting the host nonce.

        It must NOT record a grant on request: it reads the armed nonce and POSTs
        it to the approve endpoint, so it authorizes nothing on its own.
        """
        from kiro_crew import cli, cli_server

        cli_src = inspect.getsource(cli)
        assert 'add_command(sub, "ssh-agent")' in cli_src
        assert "_ssh_agent_approve" in cli_src
        block = cli_src[cli_src.index('add_command(sub, "ssh-agent")') :]
        block = block[: block.index('add_argument(\n        "action"') + 400]
        assert 'nargs="?"' not in block, "ssh-agent action must be required"

        approve_src = inspect.getsource(cli_server._ssh_agent_approve)
        assert "read_pending_grant" in approve_src
        assert "/api/ssh-agent/consent/approve" in approve_src
        assert "record_grant" not in approve_src

    def test_the_agent_is_refused_the_verb_by_the_denied_command_floor(self):
        effective = list(
            security.compute_effective_denied(security.BUILTIN_DENIED_RULES, (), False, (), ())
        )
        assert security.is_denied("kirocrew ssh-agent approve", denied_regexes=effective)
        assert security.is_denied("kirocrew -v ssh-agent approve", denied_regexes=effective)
        assert not security.is_denied("kirocrew ssh-agent --help", denied_regexes=effective)
        assert "self-protection-ssh-agent" in security._SELF_PROTECTION_UNGATED_FLOOR_IDS

    def test_no_stale_no_cli_verb_sentence_survives(self):
        # The old module said "deliberately no CLI verb"; every copy of that claim
        # has to go, in code and in the owning spec, or a reader is told the only
        # grant is a hand edit.
        import pathlib

        from kiro_crew import sandbox
        from kiro_crew.config import loader
        from kiro_crew.security import paths as _sp

        for mod in (ssh_auth_sock_consent, loader, sandbox, _sp):
            assert "no CLI verb" not in inspect.getsource(mod), mod.__name__
        spec = pathlib.Path(__file__).resolve().parents[1] / "docs/system-specs/modules/security.md"
        if spec.exists():
            text = spec.read_text(encoding="utf-8")
            assert "and no CLI verb" not in text
            assert "kirocrew ssh-agent approve" in text


# ---------------------------------------------------------------- the step-up


@pytest.mark.usefixtures("_permissive_host")
class TestGrantRequiresAHostStepUp:
    def test_arming_records_no_grant_and_leaks_no_nonce(self):
        pending = ssh_auth_sock_consent.arm_grant()
        assert ssh_auth_sock_consent.is_granted() is False
        view = ssh_auth_sock_consent.public_pending_view(pending)
        assert view["armed"] is True
        assert "nonce" not in view
        assert view["approve_command"] == "kirocrew ssh-agent approve"
        assert view["request_id"] == pending.request_id
        assert 0 < view["expires_in"] <= ssh_auth_sock_consent.GRANT_PENDING_TTL_SECS
        # The nonce file itself is owner-only from birth.
        if os.name == "posix":
            assert (ssh_auth_sock_consent.pending_grant_path().stat().st_mode & 0o077) == 0

    def test_the_unarmed_view_is_the_documented_shape(self):
        assert ssh_auth_sock_consent.public_pending_view(None) == {
            "armed": False,
            "request_id": None,
            "expires_in": None,
            "approve_command": "kirocrew ssh-agent approve",
        }

    def test_approve_from_a_remote_caller_is_refused(self):
        pending = ssh_auth_sock_consent.arm_grant()
        resp = _run(
            _handler().api_ssh_agent_consent_approve(
                _local_approve_request(nonce=pending.nonce, local=False)
            )
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "ssh_agent_approve_not_local"
        assert ssh_auth_sock_consent.is_granted() is False

    def test_approve_with_a_wrong_nonce_is_a_409(self):
        # A stale/wrong nonce is "arm again", not "you may not": 409, distinct
        # from the 403 authorization refusals, and nothing is consumed.
        ssh_auth_sock_consent.arm_grant()
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce="0" * 64))
        )
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "ssh_agent_nonce_stale"
        assert ssh_auth_sock_consent.is_granted() is False
        assert ssh_auth_sock_consent.read_pending_grant() is not None

    def test_approve_with_nothing_armed_is_a_409(self):
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce="0" * 64))
        )
        assert resp.status == 409
        assert "Security panel" in json.loads(resp.text)["error"]

    def test_approve_is_refused_while_computer_use_is_enabled(self, monkeypatch):
        from kiro_crew.computer_use import enable_state

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: True, raising=True)
        pending = ssh_auth_sock_consent.arm_grant()
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce=pending.nonce))
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "ssh_agent_approve_computer_use_active"
        assert ssh_auth_sock_consent.is_granted() is False
        # Fail fast BEFORE consume: the owner can retry after disabling.
        assert ssh_auth_sock_consent.read_pending_grant() is not None

    def test_approve_is_allowed_when_computer_use_is_disabled(self):
        pending = ssh_auth_sock_consent.arm_grant()
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce=pending.nonce))
        )
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["granted"] is True
        assert isinstance(body["granted_at"], str) and body["granted_at"]
        assert set(body) == {"granted", "granted_at"}
        assert ssh_auth_sock_consent.is_granted() is True

    def test_approve_is_refused_when_the_sandbox_mask_does_not_apply(self, monkeypatch):
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off", raising=True)
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False, raising=True)
        pending = ssh_auth_sock_consent.arm_grant()
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce=pending.nonce))
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "ssh_agent_approve_unsandboxed"
        assert ssh_auth_sock_consent.is_granted() is False

    def test_approve_is_refused_when_the_spawn_delegates_masking(self, monkeypatch):
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "spawn_delegates_masking", lambda **k: True, raising=True)
        pending = ssh_auth_sock_consent.arm_grant()
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce=pending.nonce))
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "ssh_agent_approve_unsandboxed"

    def test_approve_is_refused_while_a_live_agent_session_is_unconfined(self, monkeypatch):
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "unconfined_live_agent_pid", lambda pids: 4242, raising=True)
        pending = ssh_auth_sock_consent.arm_grant()
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce=pending.nonce))
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "ssh_agent_approve_unsandboxed"

    def test_every_approve_denial_is_audited(self, monkeypatch):
        # A denial that leaves no SEL entry is a denial an incident review cannot
        # see. One test over the SET of refusal legs, so a leg added later
        # without an audit has to redden something.
        from kiro_crew import sandbox
        from kiro_crew.computer_use import enable_state

        recorded: list[tuple[str, str]] = []

        def _capture(*, outcome, detail=""):
            recorded.append((outcome, detail))

        monkeypatch.setattr(ssh_auth_sock_consent, "audit_decision", _capture, raising=True)

        legs: dict[str, tuple[int, dict]] = {
            "ssh_agent_approve_not_local": (403, {"local": False}),
            "ssh_agent_approve_computer_use_active": (403, {"computer_use": True}),
            "ssh_agent_approve_unsandboxed": (403, {"mask": False}),
            "ssh_agent_nonce_stale": (409, {"nonce": "0" * 64}),
        }
        for code, (status, setup) in legs.items():
            recorded.clear()
            if "computer_use" in setup:
                monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: True, raising=True)
            if "mask" in setup:
                monkeypatch.setattr(
                    sandbox, "credential_mask_applies", lambda mode: False, raising=True
                )
            pending = ssh_auth_sock_consent.arm_grant()
            req = _local_approve_request(
                nonce=setup.get("nonce", pending.nonce), local=setup.get("local", True)
            )
            resp = _run(_handler().api_ssh_agent_consent_approve(req))
            assert resp.status == status, code
            assert json.loads(resp.text)["code"] == code
            assert recorded, f"{code} returned {status} with no SEL entry"
            assert [row[0] for row in recorded] == ["refused"], code
            assert recorded[0][1].startswith("approve: "), code
            assert ssh_auth_sock_consent.is_granted() is False, code
            monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
            monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True, raising=True)

    def test_a_refusal_never_puts_the_nonce_in_the_audit(self, monkeypatch):
        recorded: list[str] = []
        monkeypatch.setattr(
            ssh_auth_sock_consent,
            "audit_decision",
            lambda *, outcome, detail="": recorded.append(detail),
            raising=True,
        )
        pending = ssh_auth_sock_consent.arm_grant()
        _run(_handler().api_ssh_agent_consent_approve(_local_approve_request(nonce="f" * 64)))
        assert recorded and all(pending.nonce not in d and "f" * 64 not in d for d in recorded)

    def test_only_one_of_two_concurrent_approvals_can_claim_one_nonce(self):
        from concurrent.futures import ThreadPoolExecutor

        pending = ssh_auth_sock_consent.arm_grant()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(ssh_auth_sock_consent.claim_grant, pending.nonce) for _ in range(2)
            ]
            outcomes = []
            for fut in futures:
                try:
                    outcomes.append(fut.result().request_id)
                except ssh_auth_sock_consent.StepUpError:
                    outcomes.append(None)
        assert sorted(o is None for o in outcomes) == [False, True], outcomes
        assert ssh_auth_sock_consent.read_pending_grant() is None

    def test_the_nonce_is_single_use(self):
        pending = ssh_auth_sock_consent.arm_grant()
        ssh_auth_sock_consent.claim_grant(pending.nonce)
        with pytest.raises(ssh_auth_sock_consent.StaleNonceError):
            ssh_auth_sock_consent.claim_grant(pending.nonce)

    def test_a_claimed_request_is_restored_only_when_nothing_newer_is_armed(self):
        first = ssh_auth_sock_consent.arm_grant()
        claimed = ssh_auth_sock_consent.claim_grant(first.nonce)
        assert ssh_auth_sock_consent.read_pending_grant() is None
        assert ssh_auth_sock_consent.restore_pending_grant(claimed) is True
        back = ssh_auth_sock_consent.read_pending_grant()
        assert back is not None and back.request_id == first.request_id

        again = ssh_auth_sock_consent.claim_grant(first.nonce)
        newer = ssh_auth_sock_consent.arm_grant()
        assert ssh_auth_sock_consent.restore_pending_grant(again) is False
        survivor = ssh_auth_sock_consent.read_pending_grant()
        assert survivor is not None and survivor.request_id == newer.request_id

    def test_a_failed_grant_write_leaves_the_nonce_retryable(self, monkeypatch):
        pending = ssh_auth_sock_consent.arm_grant()

        def _boom(**kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(ssh_auth_sock_consent, "record_grant", _boom, raising=True)
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce=pending.nonce))
        )
        assert resp.status == 500
        assert json.loads(resp.text)["code"] == "ssh_agent_approve_write_failed"
        assert ssh_auth_sock_consent.is_granted() is False
        back = ssh_auth_sock_consent.read_pending_grant()
        assert back is not None and back.request_id == pending.request_id

    def test_a_withdraw_during_an_approve_waits_and_then_wins(self, monkeypatch, _isolated_store):
        # The interleaving the transaction lock closes: the owner's DELETE lands
        # after the nonce is consumed but before the grant is written. Without the
        # lock, withdraw finds nothing and answers "not granted", then the approve
        # persists consent anyway. With it, withdraw blocks until the approve
        # transaction ends, then revokes the grant it just wrote -- so the owner's
        # "no" is the final state.
        pending = ssh_auth_sock_consent.arm_grant()
        outcome: dict[str, object] = {}
        real_record = ssh_auth_sock_consent.record_grant

        def _withdraw_from_another_thread() -> None:
            outcome["withdraw"] = ssh_auth_sock_consent.withdraw()

        def _record_with_a_concurrent_withdraw(*, granted_at: str):
            # Inside the approve transaction: the lock is held, so a withdraw
            # started now cannot run until claim_and_record returns.
            assert ssh_auth_sock_consent._TXN_LOCK.locked()
            t = threading.Thread(target=_withdraw_from_another_thread)
            t.start()
            outcome["thread"] = t
            return real_record(granted_at=granted_at)

        monkeypatch.setattr(
            ssh_auth_sock_consent, "record_grant", _record_with_a_concurrent_withdraw, raising=True
        )
        grant = ssh_auth_sock_consent.claim_and_record(
            pending.nonce, granted_at="2026-10-09T00:00:00+00:00"
        )
        assert grant.granted_at == "2026-10-09T00:00:00+00:00"
        thread = outcome["thread"]
        assert isinstance(thread, threading.Thread)
        thread.join(timeout=10)
        assert not thread.is_alive()
        # The withdraw saw the COMPLETED grant (revoked=True) and found no
        # pending request (the approve consumed it), and the store reads false.
        assert outcome["withdraw"] == (True, False)
        assert ssh_auth_sock_consent.is_granted() is False
        assert json.loads(_isolated_store.read_text(encoding="utf-8")) == {"enabled": False}

    def test_a_failed_nonce_unlink_is_not_reported_as_a_cancel(self, monkeypatch):
        # persist-before-you-publish: False from discard means "nothing was
        # armed". A nonce that could not be removed is still approvable, so the
        # failure must surface instead of reading as a successful cancel.
        ssh_auth_sock_consent.arm_grant()
        real_unlink = os.unlink

        def _refuse(path, *a, **k):
            if str(path).endswith("nonce.json"):
                raise PermissionError(13, "read-only store", str(path))
            return real_unlink(path, *a, **k)

        monkeypatch.setattr(ssh_auth_sock_consent.os, "unlink", _refuse, raising=True)
        with pytest.raises(PermissionError):
            ssh_auth_sock_consent.discard_pending_grant()
        with pytest.raises(PermissionError):
            ssh_auth_sock_consent.withdraw()
        # Still armed, honestly: the owner can cancel again once storage recovers.
        assert ssh_auth_sock_consent.read_pending_grant() is not None

    def test_a_withdraw_before_the_claim_leaves_the_approve_a_stale_nonce(self):
        pending = ssh_auth_sock_consent.arm_grant()
        assert ssh_auth_sock_consent.withdraw() == (False, True)
        with pytest.raises(ssh_auth_sock_consent.StaleNonceError):
            ssh_auth_sock_consent.claim_and_record(
                pending.nonce, granted_at="2026-10-09T00:00:00+00:00"
            )
        assert ssh_auth_sock_consent.is_granted() is False

    def test_a_request_armed_under_computer_use_cannot_be_claimed_after_it_is_disabled(
        self, monkeypatch
    ):
        # The epoch is the IMPORTED file-delivery one: arm with computer use on,
        # turn it off, and the claim must refuse with a 403-class StepUpError
        # (not the 409-class StaleNonceError).
        from kiro_crew.computer_use import enable_state

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: True, raising=True)
        pending = ssh_auth_sock_consent.arm_grant()
        assert pending.safety_epoch
        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        with pytest.raises(ssh_auth_sock_consent.StepUpError, match="configuration changed") as e:
            ssh_auth_sock_consent.claim_grant(pending.nonce)
        assert not isinstance(e.value, ssh_auth_sock_consent.StaleNonceError)
        assert ssh_auth_sock_consent.is_granted() is False

    def test_an_epoch_mismatch_is_a_403_at_the_route(self, monkeypatch):
        from kiro_crew.computer_use import enable_state

        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: True, raising=True)
        pending = ssh_auth_sock_consent.arm_grant()
        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce=pending.nonce))
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "ssh_agent_approve_refused"

    def test_a_setting_toggled_away_and_back_does_not_restore_claimability(
        self, monkeypatch, tmp_path
    ):
        # The epoch is a VALUE digest, so computer use off -> on -> off inside the
        # TTL reproduces the digest the owner armed under. The revision binds the
        # request to the IDENTITY of the governing files instead: each toggle is an
        # atomic_write (new inode, new mtime), so the request is refused even
        # though every value reads exactly as it did at arm.
        from kiro_crew.computer_use import enable_state

        state = tmp_path / "computer_use.json"
        monkeypatch.setattr(enable_state, "computer_use_state_path", lambda: state, raising=True)
        monkeypatch.setattr(enable_state, "is_enabled", lambda *a, **k: False, raising=True)
        state.write_text('{"enabled": false}', encoding="utf-8")
        pending = ssh_auth_sock_consent.arm_grant()
        assert pending.safety_revision
        # off -> on -> off: two writes that end on the same bytes and the same epoch.
        atomic_write(state, '{"enabled": true}')
        atomic_write(state, '{"enabled": false}')
        assert ssh_auth_sock_consent.safety_epoch() == pending.safety_epoch
        assert ssh_auth_sock_consent.safety_revision() != pending.safety_revision
        with pytest.raises(ssh_auth_sock_consent.StepUpError, match="configuration changed") as e:
            ssh_auth_sock_consent.claim_grant(pending.nonce)
        assert not isinstance(e.value, ssh_auth_sock_consent.StaleNonceError)
        # Not consumed: the owner re-arms rather than losing the request silently.
        assert ssh_auth_sock_consent.read_pending_grant() is not None

    def test_a_request_without_a_revision_cannot_be_claimed(self):
        pending = ssh_auth_sock_consent.arm_grant()
        path = ssh_auth_sock_consent.pending_grant_path()
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("safety_revision")
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ssh_auth_sock_consent.StepUpError, match="configuration changed"):
            ssh_auth_sock_consent.claim_grant(pending.nonce)

    def test_a_request_armed_before_the_epoch_existed_cannot_be_claimed(self):
        pending = ssh_auth_sock_consent.arm_grant()
        path = ssh_auth_sock_consent.pending_grant_path()
        row = json.loads(path.read_text(encoding="utf-8"))
        del row["safety_epoch"]
        path.write_text(json.dumps(row), encoding="utf-8")
        assert ssh_auth_sock_consent.read_pending_grant().safety_epoch == ""
        with pytest.raises(ssh_auth_sock_consent.StepUpError, match="configuration changed"):
            ssh_auth_sock_consent.claim_grant(pending.nonce)

    def test_the_epoch_is_the_shared_one_not_a_copy(self):
        from kiro_crew import file_delivery_consent

        assert ssh_auth_sock_consent.safety_epoch is file_delivery_consent.safety_epoch
        assert ssh_auth_sock_consent.StepUpError is file_delivery_consent.StepUpError
        assert issubclass(ssh_auth_sock_consent.StaleNonceError, file_delivery_consent.StepUpError)

    def test_expired_request_reads_as_none_without_unlinking(self):
        # Not unlinked on READ: the next arm's os.replace overwrites the single
        # file, so an unlink here would race a concurrent arm.
        a = ssh_auth_sock_consent.arm_grant()
        real_time = time.time
        with pytest.MonkeyPatch.context() as clock:
            # The MODULE's own ``time`` binding is replaced (D2/D11), never the
            # stdlib module's attribute: every thread and event loop in the worker
            # reads ``time.time``, and only this module should see the jump.
            clock.setattr(
                ssh_auth_sock_consent,
                "time",
                SimpleNamespace(
                    time=lambda: real_time() + ssh_auth_sock_consent.GRANT_PENDING_TTL_SECS + 1
                ),
                raising=True,
            )
            assert ssh_auth_sock_consent.read_pending_grant() is None
            assert ssh_auth_sock_consent.pending_grant_path().exists() is True
        assert a.request_id
        b = ssh_auth_sock_consent.arm_grant()
        live = ssh_auth_sock_consent.read_pending_grant()
        assert live is not None and live.request_id == b.request_id

    def test_discard_removes_the_armed_request_under_the_lock(self):
        ssh_auth_sock_consent.arm_grant()
        assert ssh_auth_sock_consent.discard_pending_grant() is True
        assert ssh_auth_sock_consent.read_pending_grant() is None
        assert not ssh_auth_sock_consent.pending_grant_path().exists()
        assert ssh_auth_sock_consent.discard_pending_grant() is False
        # Discard and claim share _PENDING_LOCK, so a claim after a discard finds
        # nothing rather than a half-removed file.
        pending = ssh_auth_sock_consent.arm_grant()
        ssh_auth_sock_consent.discard_pending_grant()
        with pytest.raises(ssh_auth_sock_consent.StaleNonceError):
            ssh_auth_sock_consent.claim_grant(pending.nonce)

    def test_a_non_ascii_nonce_is_refused_not_raised(self):
        ssh_auth_sock_consent.arm_grant()
        with pytest.raises(ssh_auth_sock_consent.StaleNonceError):
            ssh_auth_sock_consent.claim_grant("é" * 32)
        resp = _run(
            _handler().api_ssh_agent_consent_approve(_local_approve_request(nonce="\udcff" * 8))
        )
        assert resp.status == 409

    def test_a_non_string_nonce_is_a_400(self):
        req = _local_approve_request(nonce=None)

        async def _json():
            return {"nonce": 12345}

        req.json = _json
        resp = _run(_handler().api_ssh_agent_consent_approve(req))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_nonce"


# ---------------------------------------------------------------- the owner gate


class TestConsentEndpointRequiresTheOwner:
    _VERBS = (
        "api_ssh_agent_consent_get",
        "api_ssh_agent_consent_arm",
        "api_ssh_agent_consent_arm_status",
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

    def test_a_non_owner_refusal_is_audited_and_arms_nothing(self, monkeypatch):
        recorded: list[tuple[str, str]] = []
        monkeypatch.setattr(
            ssh_auth_sock_consent,
            "audit_decision",
            lambda *, outcome, detail="": recorded.append((outcome, detail)),
            raising=True,
        )
        resp = _run(_handler().api_ssh_agent_consent_arm(_owner_request(app="notes")))
        assert resp.status == 403
        assert recorded == [
            ("refused", "ssh_auth_sock_forward_consent.arm: non-owner caller refused")
        ]
        assert ssh_auth_sock_consent.read_pending_grant() is None


# ---------------------------------------------------------------- the round trip


@pytest.mark.usefixtures("_permissive_host")
class TestConsentEndpointAsOwner:
    def test_get_reports_the_documented_shape(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        resp = _run(_handler().api_ssh_agent_consent_get(_owner_request()))
        assert resp.status == 200
        assert json.loads(resp.text) == {
            "granted": False,
            "granted_at": None,
            "socket_present": False,
        }
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

    def test_arm_then_approve_then_get_then_delete_round_trip(self):
        handler = _handler()

        # ARM: no grant yet, no nonce in the response, documented keys only.
        armed_resp = _run(handler.api_ssh_agent_consent_arm(_owner_request()))
        assert armed_resp.status == 200
        armed = json.loads(armed_resp.text)
        assert set(armed) == {"request_id", "expires_in", "approve_command"}
        assert armed["approve_command"] == "kirocrew ssh-agent approve"
        assert isinstance(armed["expires_in"], int)
        assert ssh_auth_sock_consent.is_granted() is False

        # ARM STATUS: the SPA-safe view, still no nonce.
        status = json.loads(_run(handler.api_ssh_agent_consent_arm_status(_owner_request())).text)
        assert status["armed"] is True
        assert status["request_id"] == armed["request_id"]
        assert "nonce" not in status

        # APPROVE with the nonce read from the host file RECORDS the grant.
        pending = ssh_auth_sock_consent.read_pending_grant()
        approved = _run(
            handler.api_ssh_agent_consent_approve(_local_approve_request(nonce=pending.nonce))
        )
        assert approved.status == 200
        approved_body = json.loads(approved.text)
        assert approved_body["granted"] is True

        got = json.loads(_run(handler.api_ssh_agent_consent_get(_owner_request())).text)
        assert got["granted"] is True
        assert got["granted_at"] == approved_body["granted_at"]

        # The armed request is consumed.
        status = json.loads(_run(handler.api_ssh_agent_consent_arm_status(_owner_request())).text)
        assert status == {
            "armed": False,
            "request_id": None,
            "expires_in": None,
            "approve_command": "kirocrew ssh-agent approve",
        }

        # DELETE revokes without a step-up.
        deleted = _run(handler.api_ssh_agent_consent_delete(_owner_request()))
        assert deleted.status == 200
        assert json.loads(deleted.text) == {"granted": False}
        assert ssh_auth_sock_consent.is_granted() is False

    def test_delete_of_an_absent_grant_still_answers_not_granted(self):
        resp = _run(_handler().api_ssh_agent_consent_delete(_owner_request()))
        assert resp.status == 200
        assert json.loads(resp.text) == {"granted": False}

    def test_delete_reports_a_failed_withdraw_instead_of_a_cancel(self, monkeypatch):
        ssh_auth_sock_consent.arm_grant()

        def _boom():
            raise OSError(30, "read-only file system")

        monkeypatch.setattr(ssh_auth_sock_consent, "withdraw", _boom, raising=True)
        resp = _run(_handler().api_ssh_agent_consent_delete(_owner_request()))
        assert resp.status == 500
        body = json.loads(resp.text)
        assert body["code"] == "ssh_agent_withdraw_failed"
        assert "granted" not in body
        assert ssh_auth_sock_consent.read_pending_grant() is not None

    def test_delete_cancels_an_armed_request(self, monkeypatch):
        # The panel's Cancel button is this DELETE: if the nonce survived, the
        # next 3s poll would re-show the armed block AND the request would stay
        # approvable for the rest of its TTL after the owner said no.
        handler = _handler()
        recorded: list[str] = []
        monkeypatch.setattr(
            ssh_auth_sock_consent,
            "audit_decision",
            lambda *, outcome, detail="": recorded.append(outcome),
            raising=True,
        )
        _run(handler.api_ssh_agent_consent_arm(_owner_request()))
        assert (
            json.loads(_run(handler.api_ssh_agent_consent_arm_status(_owner_request())).text)[
                "armed"
            ]
            is True
        )

        deleted = _run(handler.api_ssh_agent_consent_delete(_owner_request()))
        assert deleted.status == 200
        assert json.loads(deleted.text) == {"granted": False}

        status = json.loads(_run(handler.api_ssh_agent_consent_arm_status(_owner_request())).text)
        assert status["armed"] is False
        assert ssh_auth_sock_consent.read_pending_grant() is None
        # Arm-only cancel audits ``cancelled``, never ``revoked`` (no grant existed).
        assert recorded == ["cancelled"]

    def test_delete_after_a_grant_and_a_fresh_arm_clears_both(self, monkeypatch):
        handler = _handler()
        recorded: list[str] = []
        monkeypatch.setattr(
            ssh_auth_sock_consent,
            "audit_decision",
            lambda *, outcome, detail="": recorded.append(outcome),
            raising=True,
        )
        ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")
        ssh_auth_sock_consent.arm_grant()
        recorded.clear()
        _run(handler.api_ssh_agent_consent_delete(_owner_request()))
        assert ssh_auth_sock_consent.is_granted() is False
        assert ssh_auth_sock_consent.read_pending_grant() is None
        assert recorded == ["revoked", "cancelled"]

    def test_grant_and_revoke_are_audited_under_the_named_event(self, monkeypatch):
        calls: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                calls.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel(), raising=True)
        ssh_auth_sock_consent.record_grant(granted_at="2026-10-09T00:00:00+00:00")
        ssh_auth_sock_consent.revoke()
        ssh_auth_sock_consent.arm_grant()
        ssh_auth_sock_consent.discard_pending_grant()
        ops = [(c["operation"], c["outcome"], c["caller"]) for c in calls]
        assert ops == [
            ("ssh_auth_sock_forward_consent.granted", "granted", "owner"),
            ("ssh_auth_sock_forward_consent.revoked", "revoked", "owner"),
            ("ssh_auth_sock_forward_consent.cancelled", "cancelled", "owner"),
        ]
        assert all(c["source"] == "ssh-auth-sock-consent" for c in calls)
