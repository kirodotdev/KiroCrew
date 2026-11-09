"""The grant state machine: what may end, renew or reset a grant, deterministically.

Sharing renews automatically for as long as it is on, before or after expiry.
Only an authenticated ``refusal`` naming an explicit end (revoked, removed,
replaced, another computer) ends a grant; a forged close, an upgrade status, a
MAC failure, any other refusal or a run that never got the authenticated
``admitted`` is a failed attempt with growing back-off. A renewal presents the
old credential and is accepted iff the device record is still active at its
generation, so it can never restore a Disconnect, a Remove or a local Off.
"""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import test_connected_computers_confinement as handshake
import test_connected_computers_protected as helpers
from test_connected_computers_protected import (
    _AuthRefusal,
    _controller,
    _grant,
    _ProtocolFailed,
    _refused,
    _run_once,
    _state,
    _Unadmitted,
)

from kiro_crew.connected_computers import credentials, protocol
from kiro_crew.connected_computers.devices import DeviceRegistry
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.asyncio

vault = helpers.vault
protector = helpers.protector
store = helpers.store
GATEWAY = handshake.GATEWAY
FP = handshake.FP_1
UNADMITTED = "the connection ended before the gateway admitted this computer"


def _renewals(controller) -> list[str]:
    calls: list[str] = []

    async def renew(current):
        calls.append(current.grant_id)
        return current

    controller._renew = renew
    return calls


def _sleeps(controller) -> list[float]:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)  # flake-ok: only yields; the recorded wait is the subject

    controller._sleep = sleep
    return waits


def _then_revoke(store, grant_id: str, outcome):
    def run():
        store.revoke(grant_id)
        return outcome

    return run


# -- A: transport evidence never decides -------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [_Unadmitted(UNADMITTED), _AuthRefusal("401"), _ProtocolFailed("protocol error: invalid MAC")],
    ids=["forged-close", "upgrade-401", "mac-failure"],
)
async def test_a_forged_close_or_status_neither_renews_nor_ends(store, monkeypatch, failure):
    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    outcomes: list = [failure, failure, _then_revoke(store, grant.grant_id, failure)]
    controller = _controller(store, monkeypatch, outcomes=outcomes, runs=runs, self_tests=[])
    renewals = _renewals(controller)
    await _run_once(controller, grant.grant_id)
    assert len(runs) == 3, "every failed attempt was retried"
    assert renewals == [], "a credential far from expiry is not renewed"
    assert grant.grant_id not in controller._ended
    assert store.get(grant.grant_id).ended == ""
    await controller.shutdown()


async def test_back_off_grows_across_forged_closes_and_resets_only_on_admission(store, monkeypatch):
    from kiro_crew.connected_computers import controller as controller_mod

    store.install_key()
    grant = store.add(_grant())
    forged = _Unadmitted(UNADMITTED)
    mac = _ProtocolFailed("protocol error: invalid MAC")
    outcomes: list = [forged] * 8 + [mac, "the connection ended", forged]
    outcomes.append(_then_revoke(store, grant.grant_id, forged))
    controller = _controller(store, monkeypatch, outcomes=outcomes, runs=[], self_tests=[])
    monkeypatch.setattr(controller_mod, "BACKOFF_MIN_SECS", 5.0)  # flake-ok: recorded, not slept
    waits = _sleeps(controller)
    await _run_once(controller, grant.grant_id)
    # Capped at BACKOFF_MAX_SECS; a MAC failure after admission does not reset it.
    assert waits[:9] == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]
    assert controller_mod.BACKOFF_MAX_SECS == 300.0
    # The authenticated admission resets it; the next failures grow it again.
    assert waits[9:] == [5.0, 10.0, 20.0]
    await controller.shutdown()


@pytest.mark.parametrize("code", sorted(protocol.TERMINAL_REFUSALS))
async def test_an_authenticated_explicit_end_ends_the_grant(store, monkeypatch, code):
    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    controller = _controller(
        store, monkeypatch, outcomes=[_refused(code)], runs=runs, self_tests=[]
    )
    renewals = _renewals(controller)
    await _run_once(controller, grant.grant_id)
    await controller.reconcile()
    assert runs == [grant.grant_id] and renewals == []
    assert code in store.get(grant.grant_id).ended
    assert controller._tasks[grant.grant_id].done(), "never restarted"
    await controller.shutdown()


@pytest.mark.parametrize("code", ["invalid", "state_unreadable", "capacity", "wrong_gateway"])
async def test_any_other_authenticated_refusal_is_retried(store, monkeypatch, code):
    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    outcomes: list = [_refused(code), _then_revoke(store, grant.grant_id, _refused(code))]
    controller = _controller(store, monkeypatch, outcomes=outcomes, runs=runs, self_tests=[])
    await _run_once(controller, grant.grant_id)
    assert len(runs) == 2
    assert grant.grant_id not in controller._ended
    assert store.get(grant.grant_id).ended == ""
    await controller.shutdown()


@pytest.mark.parametrize(
    "between",
    [_Unadmitted(UNADMITTED), _ProtocolFailed("protocol error: invalid MAC"), _AuthRefusal("401")],
    ids=["no-admission", "mac-failure", "upgrade-401"],
)
async def test_expired_renews_every_time_and_never_ends_the_grant(store, monkeypatch, between):
    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    outcomes: list = [
        _refused("expired"),
        between,
        _refused("expired"),
        _then_revoke(store, grant.grant_id, "the connection ended"),
    ]
    controller = _controller(store, monkeypatch, outcomes=outcomes, runs=runs, self_tests=[])
    renewals = _renewals(controller)
    await _run_once(controller, grant.grant_id)
    assert renewals == [grant.grant_id] * 2, "one renewal after each authenticated expired"
    assert len(runs) == 4 and outcomes == []
    assert grant.grant_id not in controller._ended
    await controller.shutdown()


async def test_an_unreachable_renewal_after_expiry_retries_the_renewal_not_the_dial(
    store, monkeypatch
):
    from kiro_crew.connected_computers.enroll import EnrollmentUnavailable

    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    outcomes: list = [_refused("expired"), _then_revoke(store, grant.grant_id, "ended")]
    controller = _controller(store, monkeypatch, outcomes=outcomes, runs=runs, self_tests=[])
    answers: list = [EnrollmentUnavailable("could not reach the remote gateway over SSH"), None]
    renewals: list[int] = []

    async def renew(current):
        renewals.append(len(runs))
        answer = answers.pop(0)
        if answer is not None:
            raise answer
        return current

    controller._renew = renew
    waits = _sleeps(controller)
    await _run_once(controller, grant.grant_id)
    # Both renewal attempts ran before the second dial, each after a wait.
    assert renewals == [1, 1] and len(runs) == 2
    assert len(waits) == 3
    assert grant.grant_id not in controller._ended
    await controller.shutdown()


# -- renewal before and after expiry --------------------------------------------------------

#: The clock when the grant's credential was issued (realistic, not far-future).
ISSUED = 1_900_000_000.0


def _dated_renewals(controller, now, runs: list) -> list[int]:
    """Renewals that answer a fresh credential; records the dials made before each."""
    calls: list[int] = []

    async def renew(current):
        calls.append(len(runs))
        return replace(current, credential_expires=now + credentials.CREDENTIAL_TTL_SECS)

    controller._renew = renew
    return calls


@pytest.mark.parametrize("left", [-10 * 3600.0, -60.0, 600.0], ids=["long-gone", "just", "near"])
async def test_a_credential_expired_or_near_expiry_is_renewed_before_the_dial(
    store, monkeypatch, left
):
    """No expired refusal is needed first: the computer renews on its own."""
    store.install_key()
    expires = ISSUED + credentials.CREDENTIAL_TTL_SECS
    grant = store.add(replace(_grant(), credential_expires=expires))
    now = expires - left
    runs: list[str] = []
    outcomes: list = [_then_revoke(store, grant.grant_id, "the connection ended")]
    controller = _controller(store, monkeypatch, outcomes=outcomes, runs=runs, self_tests=[])
    controller._clock = lambda: now
    renewals = _dated_renewals(controller, now, runs)
    await _run_once(controller, grant.grant_id)
    assert renewals == [0], "renewed once, before the first dial"
    assert len(runs) == 1 and outcomes == []
    await controller.shutdown()


# -- C: terminal states persist; a failed write is retried; the self-test runs once ------


@pytest.mark.parametrize("outcome", ["refusal", "confinement", "oserror", "seal"])
async def test_a_terminal_state_survives_a_controller_restart(store, monkeypatch, outcome):
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers.confinement import ConfinementUnavailable
    from kiro_crew.connected_computers.local_store import SealBroken

    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    self_tests: list[str] = []
    outcomes: list = [_refused("revoked")] if outcome == "refusal" else []
    first = _controller(store, monkeypatch, outcomes=outcomes, runs=runs, self_tests=self_tests)

    async def self_test(plan, root):
        self_tests.append(root)
        if outcome == "confinement":
            raise ConfinementUnavailable("install bubblewrap")
        if outcome == "oserror":
            raise PermissionError(13, "denied", "/secret/witness")

    monkeypatch.setattr(controller_mod, "self_test", self_test)
    if outcome == "seal":
        monkeypatch.setattr(store, "unsealed", lambda _id: (_ for _ in ()).throw(SealBroken("x")))
    await _run_once(first, grant.grant_id)
    reason = store.get(grant.grant_id).ended
    assert reason, "the terminal state is on the grant"
    if outcome == "oserror":
        assert reason.startswith("unavailable:") and "PermissionError" in reason
        assert "/secret" not in reason, "only the error's type is shown"
    await first.shutdown()

    second = _restarted(store)
    for _ in range(3):
        await second.reconcile()
    assert grant.grant_id not in second._tasks, "an ended grant never starts again"
    assert len(self_tests) == 1, "the self-test did not run again"
    assert len(runs) == (1 if outcome == "refusal" else 0)
    assert _state(second, grant.grant_id) == reason
    # Only a replacement (a new grant id) or a revoke moves past it.
    replacement = store.add(_grant())
    assert store.get(replacement.grant_id).ended == ""
    assert not store.get(grant.grant_id).active
    await second.shutdown()


def _restarted(store):
    """A new controller on the same store: a gateway or desktop-host restart."""
    from kiro_crew.connected_computers import controller as controller_mod

    return controller_mod.LocalController(
        store,
        instance_lookup=lambda _id: SimpleNamespace(local_port=1),
        plan_factory=lambda: SimpleNamespace(name="test-plan"),
        enabled=lambda: True,
    )


async def test_a_failed_terminal_write_is_retried_while_the_grant_stays_stopped(store, monkeypatch):
    from kiro_crew.connected_computers.vault_doc import VaultDocError

    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    controller = _controller(
        store, monkeypatch, outcomes=[_refused("revoked")], runs=runs, self_tests=[]
    )
    real_set_ended = store.set_ended
    storage = {"broken": True}

    def set_ended(grant_id, reason):
        if storage["broken"]:
            raise VaultDocError("the vault is not writable")
        return real_set_ended(grant_id, reason)

    monkeypatch.setattr(store, "set_ended", set_ended)
    first = await _run_once(controller, grant.grant_id)
    assert store.get(grant.grant_id).ended == "", "the write failed"
    assert _state(controller, grant.grant_id).endswith("(not yet saved)")
    for _ in range(2):
        await controller.reconcile()
    assert controller._tasks[grant.grant_id] is first and runs == [grant.grant_id]
    assert store.get(grant.grant_id).ended == ""

    storage["broken"] = False
    await controller.reconcile()
    saved = store.get(grant.grant_id).ended
    assert "revoked" in saved, "the next reconcile wrote it"
    assert _state(controller, grant.grant_id) == saved
    await controller.shutdown()

    second = _restarted(store)
    await second.reconcile()
    assert grant.grant_id not in second._tasks, "the restarted host does not redial it"
    await second.shutdown()


async def test_the_self_test_is_cached_per_grant_for_the_controller_life(store, monkeypatch):
    store.install_key()
    grant = store.add(_grant())
    self_tests: list[str] = []
    controller = _controller(store, monkeypatch, outcomes=[], runs=[], self_tests=self_tests)
    first = await controller._checked_plan(grant)
    again = await controller._checked_plan(grant)
    assert first is again and self_tests == [grant.root]
    await controller.shutdown()


# -- D: the remote's SSH answer to a renewal -------------------------------------------------


@pytest.mark.parametrize("field", ["gateway_id", "device_id"])
async def test_a_renewal_with_another_identity_ends_the_grant(store, monkeypatch, field):
    from kiro_crew.connected_computers import enroll as enroll_mod

    store.install_key()
    grant = store.add(replace(_grant(), credential_expires=0.0))
    calls: list[str] = []

    async def renew(instance, *, base, fingerprint, label, credential):
        calls.append(credential)
        answer = {"gateway_id": grant.gateway_id, "device_id": grant.device_id}
        answer[field] = "someone-else"
        return enroll_mod.Enrolled(
            gateway_label="other",
            credential="new-cred",
            expires=4_000_000_000.0,
            peer_key=handshake.PEER_KEY,
            **answer,
        )

    monkeypatch.setattr(enroll_mod, "renew", renew)
    runs: list[str] = []
    controller = _controller(store, monkeypatch, outcomes=[], runs=runs, self_tests=[])
    await _run_once(controller, grant.grant_id)
    assert calls == [grant.credential], "the renewal presented the old credential"
    assert runs == [], "never dialled with the mismatched answer"
    assert "no longer matches" in store.get(grant.grant_id).ended
    await controller.shutdown()


async def test_an_ssh_renewal_is_retried_unless_the_remote_names_an_explicit_end(
    store, monkeypatch
):
    from kiro_crew.connected_computers import enroll as enroll_mod

    store.install_key()
    grant = store.add(replace(_grant(), credential_expires=0.0))
    answers: list = [
        enroll_mod.EnrollmentUnavailable("could not reach the remote gateway over SSH"),
        enroll_mod.EnrollmentFailed("connected computers is turned off on this gateway"),
        enroll_mod.EnrollmentFailed("access was revoked on this gateway", "revoked"),
    ]

    async def renew(instance, **_kw):
        raise answers.pop(0)

    monkeypatch.setattr(enroll_mod, "renew", renew)
    runs: list[str] = []
    controller = _controller(store, monkeypatch, outcomes=[], runs=runs, self_tests=[])
    await _run_once(controller, grant.grant_id)
    assert answers == [] and runs == []
    assert "revoked" in store.get(grant.grant_id).ended
    await controller.shutdown()


# -- B: the remote side of a renewal ------------------------------------------------------


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A remote gateway's registry and the ``enroll --stdin`` CLI wired to it."""
    from kiro_crew import gateway_identity
    from kiro_crew.connected_computers import flag, vault_doc

    remote_vault = SecretVault(tmp_path / "remote-vault")
    monkeypatch.setattr(flag, "preview_on", lambda *a, **k: True)
    monkeypatch.setattr(vault_doc, "open_vault", lambda *a, **k: remote_vault)
    monkeypatch.setattr(gateway_identity, "gateway_id", lambda create=False: GATEWAY)
    monkeypatch.setattr("getpass.getuser", lambda: "owner")
    return DeviceRegistry(remote_vault)


def _remote(monkeypatch, capsys, command: str, body: dict) -> dict:
    from kiro_crew.connected_computers import cli

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(body)))
    code = cli._remote_enroll() if command == "enroll" else cli._remote_forget()
    answer = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (code == 0) is (answer["ok"] is True)
    return answer


def test_the_enrollment_credential_goes_only_to_the_ssh_reply(remote, monkeypatch, capsys):
    """The credential and peer key reach the computer on stdout, the SSH channel,
    and never pass through ``_print``, the module's general answer writer."""
    from kiro_crew.connected_computers import cli

    printed: list[dict] = []
    monkeypatch.setattr(cli, "_print", printed.append)
    answer = _remote(
        monkeypatch, capsys, "enroll", {"base": "laptop", "fingerprint": FP, "label": "Laptop"}
    )
    assert answer["ok"] is True and answer["credential"] and answer["peer_key"]
    assert printed == []


def _remote_renew(monkeypatch, capsys, credential) -> dict:
    body = {"base": "laptop", "fingerprint": FP, "label": "Laptop", "renew": True}
    if credential is not None:
        body["credential"] = credential
    return _remote(monkeypatch, capsys, "enroll", body)


def _clocked(monkeypatch, manual_clock) -> None:
    """One manual clock for minting (the token primitive) and checking (credentials)."""
    from kiro_crew.dashboard import token_auth_tokens

    manual_clock.install(monkeypatch, token_auth_tokens)
    manual_clock.install(monkeypatch, credentials)


def test_a_renewal_presents_the_old_credential_and_keeps_the_generation(
    remote, monkeypatch, capsys
):
    record = handshake._enroll(remote)
    token, _ = credentials.mint(record, GATEWAY)
    answer = _remote_renew(monkeypatch, capsys, token)
    assert answer["ok"] is True and answer["device_id"] == record.device_id
    assert remote.get(record.device_id).cgen == record.cgen, "a renewal moves nothing"
    assert answer["peer_key"] == credentials.peer_key_from_secret(record, GATEWAY)
    assert credentials.verify(answer["credential"], gateway_id=GATEWAY, registry=remote)
    # An answer lost on the way back is repeated with the same credential.
    assert _remote_renew(monkeypatch, capsys, token)["ok"] is True


@pytest.mark.parametrize("credential", [None, "", "not-a-token"])
def test_a_renewal_without_a_valid_old_credential_is_refused(
    remote, monkeypatch, capsys, credential
):
    record = handshake._enroll(remote)
    answer = _remote_renew(monkeypatch, capsys, credential)
    assert answer["ok"] is False
    assert remote.get(record.device_id).cgen == record.cgen, "nothing was re-enrolled"


def test_an_expired_credential_renews_while_the_record_is_active(
    remote, monkeypatch, capsys, manual_clock
):
    _clocked(monkeypatch, manual_clock)
    record = handshake._enroll(remote)
    token, _ = credentials.mint(record, GATEWAY)
    manual_clock.advance(10 * credentials.CREDENTIAL_TTL_SECS)
    with pytest.raises(credentials.CredentialRejected) as rejected:
        credentials.verify(token, gateway_id=GATEWAY, registry=remote)
    assert rejected.value.code == "expired"
    answer = _remote_renew(monkeypatch, capsys, token)
    assert answer["ok"] is True
    assert credentials.verify(answer["credential"], gateway_id=GATEWAY, registry=remote)


@pytest.mark.parametrize("offline", [False, True], ids=["connected", "offline-past-expiry"])
@pytest.mark.parametrize("action", ["disconnect", "remove", "local-off-forget"])
def test_no_renewal_after_disconnect_remove_or_off(
    remote, monkeypatch, capsys, manual_clock, action, offline
):
    """Recorded on the device at revoke time: no verify has to observe it first."""
    _clocked(monkeypatch, manual_clock)
    record = handshake._enroll(remote)
    token, _ = credentials.mint(record, GATEWAY)
    if action == "disconnect":
        remote.revoke(record.device_id)
    elif action == "remove":
        remote.remove(record.device_id)
    else:
        forgot = _remote(
            monkeypatch, capsys, "forget", {"device_id": record.device_id, "fingerprint": FP}
        )
        assert forgot["ok"] is True
    if offline:
        manual_clock.advance(credentials.CREDENTIAL_TTL_SECS + 3600)
    expected = "removed" if action == "remove" else "revoked"
    answer = _remote_renew(monkeypatch, capsys, token)
    assert answer["ok"] is False and answer["code"] == expected
    assert expected in protocol.TERMINAL_REFUSALS
    with pytest.raises(credentials.CredentialRejected) as rejected:
        credentials.verify(token, gateway_id=GATEWAY, registry=remote)
    assert rejected.value.code == expected, "an explicit end, never the renewable expired"
    assert remote.get(record.device_id).status == expected


def test_a_newer_consent_supersedes_the_old_credential_for_renewal(remote, monkeypatch, capsys):
    record = handshake._enroll(remote)
    old, _ = credentials.mint(record, GATEWAY)
    handshake._enroll(remote)  # a fresh native consent re-enrolls the computer
    answer = _remote_renew(monkeypatch, capsys, old)
    assert answer["ok"] is False and answer["code"] == "superseded"


async def test_a_dashboard_logout_neither_accepts_revokes_nor_ends_sharing(
    remote, monkeypatch, capsys, tmp_path
):
    """The connector credential in the access cookie: logout refuses it, sharing goes on."""
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard import token_auth, token_auth_tokens
    from kiro_crew.dashboard.handlers import auth_refresh

    nonces = token_auth_tokens.RevokedNonceStore(tmp_path / "revoked-nonces.json")
    monkeypatch.setattr(token_auth, "_get_revoked_store", lambda: nonces)
    audits: list[tuple] = []
    monkeypatch.setattr(auth_refresh, "_audit", lambda *args: audits.append(args))
    record = handshake._enroll(remote)
    token, _ = credentials.mint(record, GATEWAY)
    nonce = json.loads(token_auth_tokens._b64url_decode(token.split(".")[0]))["nonce"]

    app = web.Application()
    app["port"] = 7777
    app["allowed_origins"] = {"http://localhost:7777"}
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        ("127.0.0.1", 44444) if key == "peername" else default
    )
    request = make_mocked_request(
        "POST",
        "/api/auth/logout",
        headers={"Host": "localhost:7777", "Cookie": f"mc_token_7777={token}"},
        app=app,
        transport=transport,
    )
    response = await auth_refresh.api_auth_logout(request)
    assert json.loads(response.body) == {"logged_out": True}
    assert ("", "access_cookie_revoked", "noop") in audits, "the logout did not revoke it"
    assert not nonces.is_revoked(nonce)
    assert token_auth_tokens.revoke_access_cookie(token) is False

    # A global `kirocrew logout` (the revocation generation) does not reach it either.
    minted = token_auth_tokens.current_revocation_gen()
    monkeypatch.setattr(token_auth, "current_revocation_gen_or_none", lambda: minted + 1)
    assert credentials.verify(token, gateway_id=GATEWAY, registry=remote) == record
    assert _remote_renew(monkeypatch, capsys, token)["ok"] is True


# -- local read failures are failed attempts, and never block the loop -------------------


@pytest.mark.parametrize("where", ["get", "unsealed"])
async def test_an_unreadable_grant_waits_with_growing_back_off_in_one_task(
    store, monkeypatch, where
):
    """A VaultDocError reading the grant is retried in the same task, back-off growing."""
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers.vault_doc import VaultDocError

    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    controller = _controller(store, monkeypatch, outcomes=[], runs=runs, self_tests=[])
    monkeypatch.setattr(controller_mod, "BACKOFF_MIN_SECS", 5.0)  # flake-ok: recorded, not slept
    switch = {"on": True}
    controller._enabled = lambda: switch["on"]
    waits: list[float] = []
    states: list[str] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        states.append(_state(controller, grant.grant_id))
        await asyncio.sleep(0)  # flake-ok: only yields; the recorded wait is the subject

    controller._sleep = sleep
    failures = {"left": 4}

    def unreadable(_grant_id):
        failures["left"] -= 1
        if failures["left"] == 0:
            # The last failure; the next check finds sharing off and the task ends.
            switch["on"] = False
        raise VaultDocError("the vault is busy")

    monkeypatch.setattr(store, where, unreadable)
    await controller.reconcile()
    task = controller._tasks[grant.grant_id]
    from kiro_crew.testing.wait import await_until

    await await_until(task, what="the grant's connector task to end")
    assert task.exception() is None, "a read failure escaped the per-grant retry loop"
    assert waits == [5.0, 10.0, 20.0, 40.0], "the back-off restarted instead of growing"
    assert all("could not be read" in s and "VaultDocError" in s for s in states), states
    assert runs == [], "nothing was dialled without a readable grant"
    assert grant.grant_id not in controller._ended
    await controller.shutdown()


async def _loop_stays_free(entered, outcome) -> None:
    """While a switch read is stalled on its thread, a watchdog still fires.

    The watchdog is the authenticated-receive one (:class:`.authenticated.Liveness`)
    on an injected clock past its deadline. A read made ON the loop would hold it
    until the read timed out, which ``outcome`` then records.
    """
    from kiro_crew.connected_computers.authenticated import Liveness
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    await async_wait_until(entered.is_set, describe=lambda: "no read started")
    now = [0.0]

    async def no_ping() -> None:
        return None

    liveness = Liveness(clock=lambda: now[0], deadline=1.0)  # flake-ok: fake clock, never slept
    now[0] = 2.0
    await asyncio.wait_for(liveness.run(no_ping), FALLBACK_TIMEOUT_SECS)
    assert liveness.lost.is_set()
    assert "released" not in outcome, "the switch read ran on the event loop"


def _stalled_switch():
    import threading

    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    entered = threading.Event()
    release = threading.Event()
    outcome: dict = {}

    def read() -> bool:
        entered.set()
        outcome["released"] = release.wait(FALLBACK_TIMEOUT_SECS)
        return False

    return read, entered, release, outcome


async def test_a_stalled_switch_read_holds_neither_the_watchdog_nor_a_stop(store):
    """reconcile reads the switch off the loop: a watchdog and a channel stop still run."""
    from kiro_crew.connected_computers.controller import LocalController
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    read, entered, release, outcome = _stalled_switch()
    controller = LocalController(store, instance_lookup=lambda _id: None, enabled=read)
    stopped = asyncio.Event()

    class _Running:
        async def close(self) -> None:
            stopped.set()

    blocker = asyncio.Event()
    controller._connectors["grant-1"] = _Running()
    controller._tasks["grant-1"] = asyncio.ensure_future(blocker.wait())
    reconciling = asyncio.ensure_future(controller.reconcile())
    try:
        await _loop_stays_free(entered, outcome)
        # A connector's stop (its channels and process trees) still runs meanwhile.
        await asyncio.wait_for(controller.stop("grant-1"), FALLBACK_TIMEOUT_SECS)
        assert stopped.is_set()
        assert "released" not in outcome
    finally:
        release.set()
    await asyncio.wait_for(reconciling, FALLBACK_TIMEOUT_SECS)
    assert outcome["released"] is True
    await controller.shutdown()
