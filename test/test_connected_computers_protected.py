"""Connected computers: protected storage of the local grant secrets, and the local Off.

R2 -- the connector credential, the peer key and the install key are sealed under
a data key the OS keeps (macOS Keychain, the Linux Secret Service); with no OS
protection they are never written or used, and nothing falls back to plaintext.
Turning access off never needs that protection.

R8 -- a local Off is persisted with a generation any process sees, its remote
revokes are queued per pair before any remote call, connectors stop (bounded)
before a remote revoke is attempted, and an offline revoke stays pending without
the grant ever coming back.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Optional

import pytest

from kiro_crew.connected_computers import protected_store as ps
from kiro_crew.connected_computers.identity import install_fingerprint
from kiro_crew.connected_computers.local_store import (
    LOCAL_ENTRY,
    Grant,
    LocalGrantStore,
    SealBroken,
    TurnedOff,
    new_grant_id,
)
from kiro_crew.connected_computers.protected_store import ProtectionUnavailable
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until, finished

pytestmark = pytest.mark.asyncio

PEER_KEY = "cd" * 32


class MemoryProtector:
    """Stands in for the OS store: holds the data key in this process only."""

    backend = "test"

    def __init__(self) -> None:
        self.key: Optional[bytes] = None
        self.saves = 0
        self.down = False

    def load(self) -> Optional[bytes]:
        if self.down:
            raise ProtectionUnavailable("the test keyring is locked")
        return self.key

    def save(self, key: bytes) -> None:
        if self.down:
            raise ProtectionUnavailable("the test keyring is locked")
        self.saves += 1
        self.key = bytes(key)


@pytest.fixture
def vault(tmp_path):
    return SecretVault(tmp_path / "local-vault")


@pytest.fixture
def protector():
    return MemoryProtector()


@pytest.fixture
def store(vault, protector):
    return LocalGrantStore(vault, protector=protector)


def _grant(instance: str = "inst-1", device: str = "laptop") -> Grant:
    return Grant(
        grant_id=new_grant_id(),
        instance_id=instance,
        gateway_id=f"gw-{instance}",
        gateway_label="remote",
        device_id=device,
        root="/work/proj",
        credential="cred-secret-" + instance,
        credential_expires=4_000_000_000.0,
        peer_key=PEER_KEY,
    )


def _raw(vault) -> str:
    found = vault.get(LOCAL_ENTRY)
    return found.reveal() if found is not None else ""


# -- R2: sealed at rest ---------------------------------------------------------------


class TestSealedAtRest:
    def test_r2_the_credential_and_peer_key_are_never_stored_in_plaintext(self, store, vault):
        grant = store.add(_grant())
        raw = _raw(vault)
        assert "cred-secret-inst-1" not in raw
        assert PEER_KEY not in raw
        unsealed = store.unsealed(grant.grant_id)
        assert unsealed.credential == "cred-secret-inst-1"
        assert unsealed.peer_key == PEER_KEY

    def test_r2_metadata_reads_never_carry_a_secret(self, store):
        grant = store.add(_grant())
        assert store.get(grant.grant_id).credential == ""
        assert store.list()[0].peer_key == ""

    def test_r2_the_install_key_is_sealed_and_stable(self, store, vault):
        key = store.install_key()
        assert len(key) == 32 and store.install_key() == key
        assert key.hex() not in _raw(vault)
        assert store.fingerprint() == install_fingerprint(key)

    def test_r2_a_sealed_value_moved_to_another_grant_does_not_open(self, store, vault):
        a = store.add(_grant("inst-a"))
        b = store.add(_grant("inst-b"))
        doc = json.loads(_raw(vault))
        doc["grants"][b.grant_id]["credential_sealed"] = doc["grants"][a.grant_id][
            "credential_sealed"
        ]
        vault.set_sync(LOCAL_ENTRY, json.dumps(doc))
        with pytest.raises(SealBroken):
            store.unsealed(b.grant_id)


class TestProtectionUnavailable:
    def test_r2_no_grant_is_written_without_os_protection(self, store, protector):
        protector.down = True
        with pytest.raises(ProtectionUnavailable):
            store.add(_grant())
        protector.down = False
        assert store.list() == []

    def test_r2_no_install_key_is_made_or_read_without_os_protection(self, store, protector):
        protector.down = True
        with pytest.raises(ProtectionUnavailable):
            store.install_key()
        with pytest.raises(ProtectionUnavailable):
            store.protection_ready()

    def test_r2_a_renewal_is_not_stored_without_os_protection(self, store, protector):
        grant = store.add(_grant())
        protector.down = True
        with pytest.raises(ProtectionUnavailable):
            store.set_credential(grant.grant_id, "renewed", 4_000_000_000.0, PEER_KEY)
        protector.down = False
        assert store.unsealed(grant.grant_id).credential == "cred-secret-inst-1"

    def test_r2_r8_turning_off_works_with_the_keyring_locked(self, store, protector):
        a = store.add(_grant("inst-a"))
        b = store.add(_grant("inst-b"))
        protector.down = True
        assert store.revoke(a.grant_id) is not None
        assert [g.grant_id for g in store.revoke_all()] == [b.grant_id]
        assert store.active() == []

    def test_r2_a_lost_os_key_is_refused_never_replaced(self, store, protector):
        grant = store.add(_grant())
        saves = protector.saves
        protector.key = None  # the keyring item was deleted or reset
        with pytest.raises(SealBroken):
            store.unsealed(grant.grant_id)
        with pytest.raises(SealBroken):
            store.install_key()
        assert protector.saves == saves, "a new data key would orphan the sealed values"

    def test_r2_a_different_os_key_is_refused(self, store, protector):
        grant = store.add(_grant())
        protector.key = bytes(32)
        with pytest.raises(SealBroken):
            store.unsealed(grant.grant_id)


class TestResetProtection:
    def test_r2_r8_reset_after_a_lost_key_revokes_queues_and_lets_a_new_consent_seal(
        self, store, protector
    ):
        store.install_key()
        old_fingerprint = store.fingerprint()
        grant = store.add(_grant())
        protector.key = None
        generation = store.off_generation()
        assert [g.grant_id for g in store.reset_protection()] == [grant.grant_id]
        assert store.active() == []
        assert store.off_generation() == generation + 1
        [entry] = store.pending_forgets()
        assert entry.fingerprint == old_fingerprint, "the remote is told with the old identity"
        fresh = store.add(_grant("inst-2"))
        assert store.unsealed(fresh.grant_id).credential == "cred-secret-inst-2"
        assert store.fingerprint() != old_fingerprint

    def test_r8_an_enrollment_with_no_grant_can_be_queued(self, store):
        store.install_key()
        store.queue_forget("inst-9", "gw-9", "laptop")
        assert [(p.instance_id, p.device_id) for p in store.pending_forgets()] == [
            ("inst-9", "laptop")
        ]


class TestLegacyPlaintext:
    def _legacy(self, vault, grant_id: str, key: bytes) -> None:
        doc = {
            "version": 1,
            "install_key": key.hex(),
            "grants": {
                grant_id: {
                    "grant_id": grant_id,
                    "instance_id": "inst-1",
                    "gateway_id": "gw",
                    "gateway_label": "",
                    "device_id": "laptop",
                    "root": "/work/proj",
                    "mcp": [],
                    "status": "active",
                    "created": 1.0,
                    "revoked_at": 0.0,
                    "credential": "plain-cred",
                    "credential_expires": 4_000_000_000.0,
                    "peer_key": PEER_KEY,
                }
            },
        }
        vault.set_sync(LOCAL_ENTRY, json.dumps(doc))

    def test_r2_a_plaintext_install_key_is_sealed_in_place_keeping_the_device_identity(
        self, store, vault
    ):
        key = bytes(range(32))
        self._legacy(vault, "g1", key)
        assert store.install_key() == key, "the fingerprint, and so the device id, is kept"
        assert key.hex() not in _raw(vault)

    def test_r2_a_plaintext_install_key_is_not_used_without_os_protection(
        self, store, vault, protector
    ):
        self._legacy(vault, "g1", bytes(range(32)))
        protector.down = True
        with pytest.raises(ProtectionUnavailable):
            store.install_key()

    def test_r2_a_plaintext_credential_is_never_used(self, store, vault):
        self._legacy(vault, "g1", bytes(range(32)))
        assert store.unsealed("g1").credential == ""
        assert store.unsealed("g1").peer_key == ""


# -- R2: the OS adapters ----------------------------------------------------------------


class FakeRunner:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, argv, stdin: bytes = b""):
        self.calls.append((list(argv), stdin))
        return self.answers.pop(0)


KEY = bytes(range(32))


class TestLibsecretAdapter:
    def _store(self, runner):
        return ps.LibsecretStore("acct", runner=runner, tool="/usr/bin/secret-tool")

    def test_r2_the_key_goes_on_stdin_never_in_argv(self):
        runner = FakeRunner([(0, b"", b""), (0, KEY.hex().encode(), b"")])
        self._store(runner).save(KEY)
        for argv, _stdin in runner.calls:
            assert KEY.hex() not in " ".join(argv)
        assert runner.calls[0][1].strip() == KEY.hex().encode()

    def test_r2_not_found_is_none(self):
        assert self._store(FakeRunner([(1, b"", b"")])).load() is None

    def test_r2_a_locked_or_missing_service_is_unavailable(self):
        with pytest.raises(ProtectionUnavailable):
            self._store(
                FakeRunner(
                    [
                        (1, b"", b"Cannot autolaunch D-Bus"),
                    ]
                )
            ).load()

    def test_r2_a_save_that_does_not_read_back_is_unavailable(self):
        runner = FakeRunner([(0, b"", b""), (1, b"", b"")])
        with pytest.raises(ProtectionUnavailable):
            self._store(runner).save(KEY)

    def test_r2_a_malformed_stored_value_is_unavailable_not_used(self):
        with pytest.raises(ProtectionUnavailable):
            self._store(FakeRunner([(0, b"not-hex", b"")])).load()

    def test_r2_no_secret_service_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(ps, "trusted_system_bin_quiet", lambda _name: None)
        with pytest.raises(ProtectionUnavailable):
            ps.LibsecretStore.for_account("acct")


class TestKeychainAdapter:
    def _store(self, runner):
        return ps.MacKeychainStore("acct", runner=runner)

    def test_r2_not_found_is_none(self):
        assert self._store(FakeRunner([(44, b"", b"")])).load() is None

    def test_r2_the_key_goes_on_stdin_never_in_argv(self):
        runner = FakeRunner([(0, b"", b""), (0, KEY.hex().encode() + b"\n", b"")])
        self._store(runner).save(KEY)
        for argv, _stdin in runner.calls:
            assert KEY.hex() not in " ".join(argv)
        assert KEY.hex().encode() in runner.calls[0][1]

    def test_r2_a_save_that_does_not_read_back_is_unavailable(self):
        runner = FakeRunner([(0, b"", b""), (0, (b"00" * 32), b"")])
        with pytest.raises(ProtectionUnavailable):
            self._store(runner).save(KEY)

    def test_r2_any_other_failure_is_unavailable(self):
        with pytest.raises(ProtectionUnavailable):
            self._store(FakeRunner([(51, b"", b"user interaction is not allowed")])).load()


class TestPlatformChoice:
    def test_r2_b06_windows_has_no_protected_store_yet(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ps.sys, "platform", "win32")
        with pytest.raises(ProtectionUnavailable):
            ps.protected_store_for(tmp_path)

    def test_r2_each_data_home_has_its_own_item(self, tmp_path):
        assert ps.account_for(tmp_path / "a") != ps.account_for(tmp_path / "b")


# -- R8: the local Off's generation and pending remote revokes --------------------------


class TestOffGeneration:
    def test_r8_an_off_is_seen_by_every_process(self, store, vault, protector):
        before = store.off_generation()
        store.revoke_all()
        assert LocalGrantStore(vault, protector=protector).off_generation() == before + 1

    def test_r8_a_consent_redeemed_across_an_off_writes_nothing(self, store):
        generation = store.off_generation()
        LocalGrantStore(store._doc._vault, protector=store._protector).revoke_all()
        with pytest.raises(TurnedOff):
            store.add(_grant(), expect_off_generation=generation)
        assert store.list() == []

    def test_r8_a_consent_with_no_off_meanwhile_is_written(self, store):
        grant = store.add(_grant(), expect_off_generation=store.off_generation())
        assert store.get(grant.grant_id).active


class TestPendingRevokes:
    def test_r8_an_off_queues_one_remote_revoke_per_pair_and_it_survives_a_restart(
        self, store, vault, protector
    ):
        store.install_key()
        store.add(_grant("inst-a"))
        store.add(_grant("inst-b"))
        store.revoke_all()
        pending = LocalGrantStore(vault, protector=protector).pending_forgets()
        assert sorted((p.instance_id, p.device_id) for p in pending) == [
            ("inst-a", "laptop"),
            ("inst-b", "laptop"),
        ]
        assert all(p.fingerprint == store.fingerprint() for p in pending)

    def test_r8_queuing_needs_no_os_protection(self, store, protector):
        store.install_key()
        grant = store.add(_grant())
        protector.down = True
        store.revoke(grant.grant_id)
        assert [p.instance_id for p in store.pending_forgets()] == ["inst-1"]

    def test_r8_a_fresh_consent_for_the_pair_drops_only_its_stale_revoke(self, store):
        store.install_key()
        store.add(_grant("inst-a"))
        store.add(_grant("inst-b"))
        store.revoke_all()
        store.add(_grant("inst-a"))  # re-enrolment already refused the old credential
        assert [p.instance_id for p in store.pending_forgets()] == ["inst-b"]

    def test_r8_a_failed_remote_revoke_stays_pending_and_never_revives_the_grant(self, store):
        store.install_key()
        grant = store.add(_grant())
        store.revoke(grant.grant_id)
        [entry] = store.pending_forgets()
        store.forget_failed(entry.key, now=100.0)
        [again] = store.pending_forgets()
        assert again.attempts == 1 and again.next_try > 100.0
        assert store.active() == []
        assert store.set_credential(grant.grant_id, "x", 4_000_000_000.0, PEER_KEY) is None
        store.forget_done(entry.key)
        assert store.pending_forgets() == []

    def test_r8_a_revoke_that_failed_a_thousand_times_still_backs_off(self, store):
        from kiro_crew.connected_computers import local_store as local_store_mod

        store.install_key()
        store.revoke(store.add(_grant()).grant_id)
        [entry] = store.pending_forgets()

        def many(doc):
            local_store_mod._pending(doc)[entry.key]["attempts"] = 5000

        store._doc.update(many)
        store.forget_failed(entry.key, now=100.0)
        [again] = store.pending_forgets()
        assert again.attempts == 5001
        assert again.next_try == 100.0 + local_store_mod.FORGET_RETRY_MAX_SECS


class _HangingConnector:
    def __init__(self, events):
        self.events = events

    async def close(self) -> None:
        self.events.append("close-start")
        await asyncio.Event().wait()


async def test_r8_off_revokes_locally_then_stops_bounded_then_tells_the_remote(store, monkeypatch):
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import enroll as enroll_mod

    store.install_key()
    grant = store.add(_grant())
    events: list[str] = []
    instance = SimpleNamespace(ssh_host="remote")
    controller = controller_mod.LocalController(store, instance_lookup=lambda _id: instance)
    controller._connectors[grant.grant_id] = _HangingConnector(events)
    controller._tasks[grant.grant_id] = asyncio.ensure_future(asyncio.Event().wait())
    monkeypatch.setattr(controller_mod, "STOP_BOUND_SECS", 0.05)  # flake-ok: bound under test
    real_revoke_all = store.revoke_all

    def revoke_all():
        events.append("local-revoked")
        return real_revoke_all()

    monkeypatch.setattr(store, "revoke_all", revoke_all)

    async def fake_forget(_instance, *, device_id, fingerprint):
        events.append(f"forget:{device_id}")

    monkeypatch.setattr(enroll_mod, "forget", fake_forget)
    ended = await finished(controller.off(), "controller.off")
    await finished(controller.drain_pending(), "controller.drain_pending")
    assert [g.grant_id for g in ended] == [grant.grant_id]
    assert events[:2] == ["local-revoked", "close-start"]
    assert events[-1] == "forget:laptop"
    assert store.pending_forgets() == []


async def test_r8_an_offline_remote_revoke_stays_pending(store, monkeypatch):
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import enroll as enroll_mod

    store.install_key()
    store.add(_grant())
    controller = controller_mod.LocalController(
        store, instance_lookup=lambda _id: SimpleNamespace(ssh_host="remote")
    )

    async def offline(_instance, *, device_id, fingerprint):
        raise enroll_mod.EnrollmentFailed("unreachable")

    monkeypatch.setattr(enroll_mod, "forget", offline)
    await controller.off()
    await finished(controller.drain_pending(), "controller.drain_pending")
    [entry] = store.pending_forgets()
    assert entry.attempts == 1
    assert store.active() == []


def test_r8_cli_off_persists_then_waits_for_the_stop_then_tells_the_remote(
    store, monkeypatch, capsys
):
    from kiro_crew.connected_computers import cli
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import enroll as enroll_mod

    store.install_key()
    store.add(_grant())
    events: list[str] = []
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(
        cli, "_instance_lookup", lambda: (lambda _id: SimpleNamespace(ssh_host="remote"))
    )
    monkeypatch.setattr(cli, "_wait_for_local_stop", lambda: events.append("waited"))

    async def fake_forget(_instance, *, device_id, fingerprint):
        assert store.active() == [], "the local refusal is persisted first"
        events.append("forget")

    monkeypatch.setattr(enroll_mod, "forget", fake_forget)
    assert controller_mod.STOP_BOUND_SECS > 0
    generation = store.off_generation()
    assert cli._local_off() == 0
    assert store.off_generation() == generation + 1
    assert events == ["waited", "forget"]
    assert store.pending_forgets() == []


async def test_r2_redeem_refuses_before_enrolling_when_protection_is_unavailable(
    store, protector, monkeypatch
):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import enroll as enroll_mod
    from kiro_crew.connected_computers import local_routes

    controller = controller_mod.LocalController(store, instance_lookup=lambda _id: None)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: controller)
    monkeypatch.setattr(local_routes, "local_enabled", lambda cfg=None: True)
    monkeypatch.setattr(
        local_routes, "_instance", lambda _id: SimpleNamespace(ssh_host="h", name="remote")
    )
    called = []

    async def fake_enroll(*_a, **_k):
        called.append(True)
        raise AssertionError("must not enroll")

    monkeypatch.setattr(enroll_mod, "enroll", fake_enroll)
    consent = local_routes._mint_consent("inst-1", 0)
    protector.down = True

    @web.middleware
    async def internal(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[internal])
    local_routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            local_routes.REDEEM_PATH, json={"consent": consent, "root": "/tmp", "mcp": []}
        )
        assert resp.status == 503
        assert (await resp.json())["code"] == "protection_unavailable"
    assert called == [] and store.list() == []


def test_first_use_serializes_os_key_creation_across_store_instances(vault, monkeypatch):
    from kiro_crew import platform_compat

    first_load = threading.Event()
    contender_arrived = threading.Event()
    real_file_lock = platform_compat.file_lock
    key_locks = []

    @contextmanager
    def observed_lock(fd, *, exclusive):
        key_locks.append(fd)
        if first_load.is_set():
            contender_arrived.set()
        with real_file_lock(fd, exclusive=exclusive):
            yield

    class ContendedProtector(MemoryProtector):
        def load(self):
            snapshot = self.key
            if not first_load.is_set():
                first_load.set()
                assert contender_arrived.wait(FALLBACK_TIMEOUT_SECS), "no second key caller"
            else:
                contender_arrived.set()
            return snapshot

    protector = ContendedProtector()
    first = LocalGrantStore(vault, protector=protector)
    second = LocalGrantStore(vault, protector=protector)
    monkeypatch.setattr(platform_compat, "file_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(first._data_key)
        try:
            assert first_load.wait(FALLBACK_TIMEOUT_SECS), "first key read never started"
            follower = pool.submit(second._data_key)
            keys = [future.result(timeout=FALLBACK_TIMEOUT_SECS) for future in (leader, follower)]
        finally:
            contender_arrived.set()
    assert len(key_locks) == 2, "both callers must use the cross-process lock helper"
    assert keys[0] == keys[1] == protector.key
    assert protector.saves == 1
    grant = first.add(_grant())
    assert second.unsealed(grant.grant_id).credential == grant.credential
    assert first.install_key() == second.install_key()


# -- terminal outcomes are not retried ----------------------------------------------------


class _AuthRefusal(Exception):
    """The upgrade answered 401: unauthenticated, so only a failed attempt."""

    status = 401


class _Unadmitted(str):
    """A run that ended without the gateway's authenticated ``admitted``."""


class _ProtocolFailed(str):
    """A run that was admitted, then ended on a frame that failed its MAC."""


def _refused(code: str):
    from kiro_crew.connected_computers.connector import AuthenticatedRefusal

    return AuthenticatedRefusal(code)


def _scripted_connector(outcomes: list, runs: list):
    """A ``Connector`` stand-in whose runs return or raise ``outcomes`` in turn.

    A plain string return was admitted; :class:`_Unadmitted` and an exception
    were not; :class:`_ProtocolFailed` was admitted and then failed its MAC.
    """

    class _Scripted:
        def __init__(self, grant, *, url, plan, grant_still_active):
            self.grant = grant
            self.protocol_failed = False
            self.admitted = False

        async def run(self) -> str:
            runs.append(self.grant.grant_id)
            outcome = outcomes.pop(0)
            if callable(outcome):
                outcome = outcome()
            if isinstance(outcome, BaseException):
                raise outcome
            self.admitted = not isinstance(outcome, _Unadmitted)
            self.protocol_failed = isinstance(outcome, _ProtocolFailed)
            return str(outcome)

        async def close(self) -> None:
            return None

    return _Scripted


def _controller(store, monkeypatch, *, outcomes: list, runs: list, self_tests: list):
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import enroll as enroll_mod

    async def counted_self_test(plan, root):
        self_tests.append(root)

    async def no_remote(_instance, *, device_id, fingerprint):
        return None

    # A revoke here queues a remote revoke; it never leaves this process.
    monkeypatch.setattr(enroll_mod, "forget", no_remote)
    monkeypatch.setattr(controller_mod, "self_test", counted_self_test)
    monkeypatch.setattr(controller_mod, "Connector", _scripted_connector(outcomes, runs))
    # No wait between attempts: the attempts, not their spacing, are under test.
    monkeypatch.setattr(controller_mod, "BACKOFF_MIN_SECS", 0.0)  # flake-ok: back-off off
    return controller_mod.LocalController(
        store,
        instance_lookup=lambda _id: SimpleNamespace(local_port=1),
        plan_factory=lambda: SimpleNamespace(name="test-plan"),
        enabled=lambda: True,
    )


async def _run_once(controller, grant_id: str):
    await controller.reconcile()
    task = controller._tasks[grant_id]
    await await_until(task, what="the grant's connector task to end")
    return task


def _state(controller, grant_id: str) -> str:
    return next(r["state"] for r in controller.status() if r["grant_id"] == grant_id)


@pytest.mark.parametrize("outcome", ["remote_refusal", "confinement", "seal_broken"])
async def test_a_terminal_outcome_is_never_restarted_until_the_grant_is_replaced(
    store, monkeypatch, outcome
):
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers.confinement import ConfinementUnavailable

    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    self_tests: list[str] = []
    outcomes: list = [_refused("revoked")] if outcome == "remote_refusal" else []
    controller = _controller(
        store, monkeypatch, outcomes=outcomes, runs=runs, self_tests=self_tests
    )
    broken = {"sandbox": outcome == "confinement", "seal": outcome == "seal_broken"}

    async def self_test(plan, root):
        self_tests.append(root)
        if broken["sandbox"]:
            raise ConfinementUnavailable("install bubblewrap")

    real_unsealed = store.unsealed

    def unsealed(grant_id):
        if broken["seal"]:
            raise SealBroken("the OS key changed")
        return real_unsealed(grant_id)

    monkeypatch.setattr(controller_mod, "self_test", self_test)
    monkeypatch.setattr(store, "unsealed", unsealed)
    first = await _run_once(controller, grant.grant_id)
    shown = _state(controller, grant.grant_id)
    assert shown.startswith(("refused", "unavailable")), shown
    for _ in range(3):
        await controller.reconcile()
    assert controller._tasks[grant.grant_id] is first, "reconcile restarted an ended grant"
    assert len(self_tests) == 1, "the self-test ran again after a terminal outcome"
    assert len(runs) == (1 if outcome == "remote_refusal" else 0)
    assert _state(controller, grant.grant_id) == shown, "the reason stays visible"

    # A fresh consent is a new grant: it starts, and the ended entry is dropped.
    broken.update(sandbox=False, seal=False)
    replacement = store.add(_grant())

    def end_sharing() -> str:
        store.revoke(replacement.grant_id)
        return "the connection ended"

    outcomes.extend(["the connection ended", end_sharing])
    await _run_once(controller, replacement.grant_id)
    await controller.reconcile()
    assert grant.grant_id not in controller._ended
    assert replacement.grant_id not in controller._ended
    assert runs.count(replacement.grant_id) == 2
    assert len(self_tests) == 2
    await controller.shutdown()


async def test_an_expired_refusal_renews_every_time_and_never_ends_the_grant(store, monkeypatch):
    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []

    def end_sharing():
        store.revoke(grant.grant_id)
        return _refused("expired")

    outcomes: list = [_refused("expired"), "the connection ended", _refused("expired"), end_sharing]
    controller = _controller(store, monkeypatch, outcomes=outcomes, runs=runs, self_tests=[])
    renewals: list[str] = []

    async def renew(current):
        renewals.append(current.grant_id)
        return current

    controller._renew = renew
    await _run_once(controller, grant.grant_id)
    assert len(runs) == 4 and outcomes == []
    assert renewals == [grant.grant_id] * 2, "the last one met a grant turned off here"
    assert grant.grant_id not in controller._ended
    await controller.shutdown()


@pytest.mark.parametrize("code", ["revoked", "removed", "superseded", "wrong_device"])
async def test_an_explicitly_ended_credential_is_never_renewed(store, monkeypatch, code):
    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []
    controller = _controller(
        store,
        monkeypatch,
        outcomes=[_refused(code)],
        runs=runs,
        self_tests=[],
    )
    renewals: list[str] = []

    async def renew(current):
        renewals.append(current.grant_id)
        return current

    controller._renew = renew
    await _run_once(controller, grant.grant_id)
    await controller.reconcile()
    assert runs == [grant.grant_id] and renewals == []
    assert grant.grant_id in controller._ended
    assert code in _state(controller, grant.grant_id)
    await controller.shutdown()


async def test_an_unreadable_remote_state_is_retried_not_ended(store, monkeypatch):
    from kiro_crew.connected_computers.connector import ConnectorRefused

    store.install_key()
    grant = store.add(_grant())
    runs: list[str] = []

    def revoke_then_refuse():
        store.revoke(grant.grant_id)
        return ConnectorRefused("the remote gateway could not read its connector state")

    controller = _controller(
        store,
        monkeypatch,
        outcomes=[ConnectorRefused("not now"), revoke_then_refuse],
        runs=runs,
        self_tests=[],
    )
    await _run_once(controller, grant.grant_id)
    assert runs == [grant.grant_id, grant.grant_id], "the first refusal was retried"
    assert grant.grant_id not in controller._ended
    await controller.shutdown()


@pytest.mark.parametrize("switch", [False, None])
async def test_gateway_startup_off_persists_revocation_but_unknown_keeps_grants(
    store, protector, monkeypatch, switch
):
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import disable

    store.fingerprint()
    grant = store.add(_grant())
    generation = store.off_generation()
    protector.down = True
    controller = controller_mod.LocalController(store, instance_lookup=lambda _id: None)
    starts = []
    monkeypatch.setattr(controller, "start", lambda: starts.append(store.active()))
    monkeypatch.setattr(controller, "_kick_drain", lambda: None)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: controller)
    monkeypatch.setattr(disable, "_STARTUP_TASK", None)
    monkeypatch.setattr(disable, "_TASKS", set())
    monkeypatch.setattr(disable, "local_switch", lambda: switch)

    disable.start_if_enabled()
    await finished(asyncio.gather(*list(disable._TASKS)), "gateway startup settlement")
    assert starts == []
    saved = store.get(grant.grant_id)
    assert saved.active is (switch is None)
    assert store.off_generation() == generation + int(switch is False)
    pending = store.pending_forgets()
    if switch is False:
        assert [(p.instance_id, p.device_id) for p in pending] == [("inst-1", "laptop")]
    else:
        assert pending == []

    monkeypatch.setattr(disable, "local_switch", lambda: True)
    await finished(disable._start_local(), "re-enable after startup")
    assert [[g.grant_id for g in active] for active in starts] == (
        [[]] if switch is False else [[grant.grant_id]]
    )


async def test_failed_startup_revocation_blocks_direct_reconcile(store, monkeypatch):
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import disable

    store.add(_grant())
    controller = controller_mod.LocalController(
        store, instance_lookup=lambda _id: None, enabled=lambda: True
    )

    def failed_revoke():
        raise OSError("vault unavailable")

    def forbidden_active():
        pytest.fail("a failed startup Off must not reload reusable grants")

    monkeypatch.setattr(store, "revoke_all", failed_revoke)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: controller)
    monkeypatch.setattr(disable, "_STARTUP_TASK", None)
    monkeypatch.setattr(disable, "_TASKS", set())
    monkeypatch.setattr(disable, "local_switch", lambda: False)
    disable.start_if_enabled()
    await finished(asyncio.gather(*list(disable._TASKS)), "failed gateway startup settlement")
    monkeypatch.setattr(disable, "local_switch", lambda: True)
    monkeypatch.setattr(store, "active", forbidden_active)
    await finished(controller.reconcile(), "direct reconcile after failed startup")
    assert controller._tasks == {}
