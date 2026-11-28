"""A delayed cleanup can retire only the enrollment that requested it."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_connected_computers_protected as local_helpers

from kiro_crew.connected_computers import cli, controller, enroll, share, vault_doc
from kiro_crew.connected_computers.devices import DEVICES_ENTRY, DeviceRegistry
from kiro_crew.connected_computers.enrollment_generation import MAX_GENERATION
from kiro_crew.connected_computers.local_store import LocalGrantStore
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.testing.wait import finished

vault = local_helpers.vault
protector = local_helpers.protector
store = local_helpers.store
FINGERPRINT = "ab" * 32


@pytest.fixture
def remote_vault(tmp_path):
    return SecretVault(tmp_path / "remote")


def enrollment(registry, fingerprint=FINGERPRINT):
    return registry.enroll(base="laptop", fingerprint=fingerprint, label="Laptop", owner="owner")


def forget_cli(monkeypatch, remote_vault, record, cgen):
    replies = []
    body = {"device_id": record.device_id, "fingerprint": record.fingerprint}
    if cgen is not None:
        body["cgen"] = cgen
    monkeypatch.setattr(vault_doc, "open_vault", lambda: remote_vault)
    monkeypatch.setattr(cli, "_read_stdin", lambda: body)
    monkeypatch.setattr(cli, "_print", replies.append)
    assert cli._remote_forget() == 0
    return replies[0]


def test_delayed_forget_after_reenrollment_preserves_new_record(remote_vault, monkeypatch):
    registry = DeviceRegistry(remote_vault)
    retired = enrollment(registry)
    replacement = enrollment(registry)
    assert replacement.cgen > retired.cgen
    answer = forget_cli(monkeypatch, remote_vault, retired, retired.cgen)
    assert answer == {"ok": True, "status": "already replaced"}
    assert registry.get(replacement.device_id) == replacement
    assert registry.get(replacement.device_id).active


def test_matching_forget_revokes_and_duplicate_is_already_replaced(remote_vault, monkeypatch):
    registry = DeviceRegistry(remote_vault)
    record = enrollment(registry)
    assert forget_cli(monkeypatch, remote_vault, record, record.cgen)["status"] == "revoked"
    revoked = registry.get(record.device_id)
    assert not revoked.active
    assert revoked.cgen == record.cgen + 1
    assert (
        forget_cli(monkeypatch, remote_vault, record, record.cgen)["status"] == "already replaced"
    )
    assert registry.get(record.device_id) == revoked


@pytest.mark.parametrize("cgen", [None, 0, -1, True, "1", 1.0, MAX_GENERATION + 1])
def test_legacy_or_invalid_forget_never_revokes(remote_vault, monkeypatch, cgen):
    registry = DeviceRegistry(remote_vault)
    enrollment(registry)
    replacement = enrollment(registry)
    assert forget_cli(monkeypatch, remote_vault, replacement, cgen)["status"] == "already replaced"
    assert registry.get(replacement.device_id) == replacement


def test_generation_comparison_repeats_after_competing_registry_write(remote_vault, monkeypatch):
    registry = DeviceRegistry(remote_vault)
    retired = enrollment(registry)
    real_set = remote_vault.set_if_revision_sync
    replacement = []

    def competing_write(name, text, revision):
        if name == DEVICES_ENTRY and not replacement:
            replacement.append(None)
            replacement[0] = enrollment(DeviceRegistry(remote_vault))
        return real_set(name, text, revision)

    monkeypatch.setattr(remote_vault, "set_if_revision_sync", competing_write)
    assert registry.forget_enrollment(retired.device_id, retired.fingerprint, retired.cgen) is False
    assert registry.get(retired.device_id) == replacement[0]
    assert replacement[0].active


def test_matching_generation_with_wrong_fingerprint_does_not_revoke(remote_vault):
    registry = DeviceRegistry(remote_vault)
    record = enrollment(registry)
    assert registry.forget_enrollment(record.device_id, "cd" * 32, record.cgen) is None
    assert registry.get(record.device_id) == record


def test_grant_and_queued_forget_preserve_generation_across_reload(store, vault, protector):
    store.install_key()
    grant = store.add(replace(local_helpers._grant(), cgen=7))
    reopened = LocalGrantStore(vault, protector=protector)
    assert reopened.get(grant.grant_id).cgen == 7
    assert reopened.unsealed(grant.grant_id).cgen == 7
    reopened.set_credential(grant.grant_id, "renewed", 4000000000.0, "cd" * 32, cgen=7)
    assert reopened.get(grant.grant_id).cgen == 7
    reopened.revoke(grant.grant_id)
    pending = LocalGrantStore(vault, protector=protector).pending_forgets()
    assert len(pending) == 1 and pending[0].cgen == 7
    assert "cgen" not in grant.public()


@pytest.mark.asyncio
async def test_delayed_queue_drain_clears_stale_entry_without_revoking_replacement(
    store,
    remote_vault,
    monkeypatch,
):
    registry = DeviceRegistry(remote_vault)
    store.install_key()
    retired = enrollment(registry, store.fingerprint())
    instance = SimpleNamespace(ssh_host="remote", local_port=1)
    grant = store.add(
        replace(
            local_helpers._grant(),
            cgen=retired.cgen,
            coordinates=share.connection_coordinates(instance),
        )
    )
    store.revoke(grant.grant_id)
    sent = []
    replacement = []

    async def remote_call(_instance, subcommand, payload, *, timeout):
        sent.append((subcommand, payload))
        replacement.append(enrollment(registry, retired.fingerprint))
        assert (
            registry.forget_enrollment(
                payload["device_id"],
                payload["fingerprint"],
                payload["cgen"],
            )
            is False
        )
        return {"ok": True, "status": "already replaced"}

    monkeypatch.setattr(enroll, "run_remote", remote_call)
    count = await finished(
        controller.drain_pending_forgets(store, lambda _id: instance),
        "finish stale cleanup",
    )
    assert count == 1 and store.pending_forgets() == []
    assert sent[0][0] == enroll.FORGET_SUBCOMMAND
    assert sent[0][1]["cgen"] == retired.cgen
    assert registry.get(retired.device_id) == replacement[0]


@pytest.mark.asyncio
async def test_rollback_and_queued_rollback_carry_issued_generation(store, monkeypatch):
    run = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(enroll, "run_remote", run)
    instance = SimpleNamespace(ssh_host="remote")
    assert await finished(
        share._forget_enrolled(instance, "laptop", FINGERPRINT, 9), "undo enrollment"
    )
    assert run.call_args.args[2] == {"device_id": "laptop", "fingerprint": FINGERPRINT, "cgen": 9}
    store.install_key()
    store.queue_forget("inst-1", "gw-1", "laptop", cgen=9)
    assert store.pending_forgets()[0].cgen == 9


@pytest.mark.asyncio
async def test_enrollment_response_retains_remote_generation(monkeypatch):
    answer = {
        "device_id": "laptop",
        "gateway_id": "gateway",
        "credential": "test",
        "expires": 4000000000.0,
        "peer_key": "ab" * 32,
        "cgen": 12,
    }
    monkeypatch.setattr(enroll, "run_remote", AsyncMock(return_value=answer))
    result = await finished(enroll._enroll(SimpleNamespace(), {}), "read enrollment identity")
    assert result.cgen == 12


def test_legacy_persisted_grant_and_queue_decode_as_unscoped(store):
    store.install_key()
    grant = store.add(replace(local_helpers._grant(), cgen=7))

    def old_grant(doc):
        doc["grants"][grant.grant_id].pop("cgen")

    store._doc.update(old_grant)
    assert store.get(grant.grant_id).cgen == 0
    store.revoke(grant.grant_id)
    [pending] = store.pending_forgets()

    def old_queue(doc):
        doc["pending_forget"][pending.key].pop("cgen")

    store._doc.update(old_queue)
    assert store.pending_forgets()[0].cgen == 0


@pytest.mark.asyncio
async def test_a_remote_that_never_answers_is_unavailable_not_a_crash(monkeypatch):
    # A stalled ssh session must surface as EnrollmentUnavailable, never a bare timeout.
    import asyncio

    class _Stalled:
        returncode = None

        async def communicate(self, _data):
            await asyncio.Event().wait()

    async def spawn(*_argv, **_kw):
        return _Stalled()

    reaped = []

    async def reap(proc):
        reaped.append(proc)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("kiro_crew.platform_compat.kill_and_reap", reap)
    monkeypatch.setattr(
        "kiro_crew.instances.token_mint.ssh_spawn_argv_env", lambda argv: (argv, {})
    )
    instance = SimpleNamespace(ssh_host="remote", remote_bin="", remote_port=0)
    with pytest.raises(enroll.EnrollmentUnavailable, match="did not answer in time"):
        await finished(
            enroll.run_remote(instance, enroll.ENROLL_SUBCOMMAND, {}, timeout=0),
            "turn a stalled remote into EnrollmentUnavailable",
        )
    assert len(reaped) == 1


def _queued(store, cgen=7):
    store.install_key()
    store.queue_forget(
        "inst-1",
        "gw-inst-1",
        "laptop",
        cgen=cgen,
        coordinates=share.connection_coordinates(SimpleNamespace(ssh_host="remote", local_port=1)),
    )
    return store.pending_forgets()[0]


@pytest.mark.parametrize("operation", ["forget_done", "forget_failed"])
@pytest.mark.parametrize(
    "field,value", [("cgen", 8), ("device_id", "desktop"), ("fingerprint", "cd" * 32)]
)
def test_queue_completion_compares_identity_inside_cas(
    store, vault, monkeypatch, operation, field, value
):
    from kiro_crew.connected_computers.local_store import LOCAL_ENTRY

    retired = _queued(store)
    real_set = vault.set_if_revision_sync
    replacement = []

    def competing_write(name, text, revision):
        if name == LOCAL_ENTRY and not replacement:
            replacement.append(None)

            def replace_identity(doc):
                doc["pending_forget"][retired.key][field] = value

            store._doc.update(replace_identity)
            replacement[0] = store.pending_forgets()[0]
        return real_set(name, text, revision)

    monkeypatch.setattr(vault, "set_if_revision_sync", competing_write)
    getattr(store, operation)(retired)
    assert store.pending_forgets() == replacement


def test_queue_ack_matches_enrollment_despite_retry_metadata_change(store):
    retired = _queued(store)
    store.forget_failed(retired, now=100.0)
    assert store.pending_forgets()[0].attempts == 1
    assert store.forget_done(retired) is True
    assert store.pending_forgets() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_inflight_forget_never_drops_or_delays_new_queue_entry(store, monkeypatch, failure):
    import asyncio

    retired = await asyncio.to_thread(_queued, store)
    replacements = []

    async def remote_call(_instance, subcommand, payload, *, timeout):
        assert payload["cgen"] == retired.cgen
        replacements.append(await asyncio.to_thread(_queued, store, retired.cgen + 1))
        if failure:
            raise enroll.EnrollmentUnavailable("remote unavailable")
        return {"ok": True}

    monkeypatch.setattr(enroll, "run_remote", remote_call)
    count = await finished(
        controller.drain_pending_forgets(
            store, lambda _: SimpleNamespace(ssh_host="remote", local_port=1)
        ),
        "finish retired enrollment without touching its replacement",
    )
    assert count == 0
    assert await asyncio.to_thread(store.pending_forgets) == replacements


@pytest.mark.asyncio
async def test_queue_identity_is_rechecked_after_instance_lookup_before_send(store, monkeypatch):
    import asyncio

    retired = await asyncio.to_thread(_queued, store)
    replacements = []

    def lookup(_instance_id):
        replacements.append(_queued(store, retired.cgen + 1))
        return SimpleNamespace(ssh_host="remote", local_port=1)

    remote = AsyncMock()
    monkeypatch.setattr(enroll, "run_remote", remote)
    count = await finished(
        controller.drain_pending_forgets(store, lookup), "recheck queued identity"
    )
    assert count == 0
    remote.assert_not_awaited()
    assert await asyncio.to_thread(store.pending_forgets) == replacements


def test_a_late_older_enrollment_does_not_replace_a_newer_grant(store):
    from kiro_crew.connected_computers.local_store import EnrollmentSuperseded

    store.install_key()
    newer = store.add(replace(local_helpers._grant(), cgen=6))
    with pytest.raises(EnrollmentSuperseded):
        store.add(replace(local_helpers._grant(), cgen=5))
    assert store.get(newer.grant_id).active
    assert [g.grant_id for g in store.active()] == [newer.grant_id]
    assert store.pending_forgets() == []
    newest = store.add(replace(local_helpers._grant(), cgen=7))
    assert [g.grant_id for g in store.active()] == [newest.grant_id]


def test_a_local_off_still_saves_when_the_document_is_nearly_full(store, monkeypatch):
    # The queued remote revoke must never be what stops the Off from saving.
    store.install_key()
    # Long approved coordinates: the queued revoke grows the document by far
    # more than the credential the revocation drops.
    coordinates = share.connection_coordinates(SimpleNamespace(ssh_host="h" * 1000, local_port=1))
    old = store.add(replace(local_helpers._grant(instance="inst-0"), root="/work/" + "r" * 2000))
    store.revoke(old.grant_id)
    live = store.add(replace(local_helpers._grant(instance="inst-1"), coordinates=coordinates))
    generation = store.off_generation()
    size = len(vault_doc.encode(store._doc.read()))
    monkeypatch.setattr(vault_doc, "MAX_DOC_BYTES", size + 8)

    ended = store.revoke_all()

    assert [g.grant_id for g in ended] == [live.grant_id]
    assert store.active() == []
    assert store.off_generation() == generation + 1
    assert store.get(old.grant_id) is None, "the oldest revoked record made room"
    assert len(vault_doc.encode(store._doc.read())) <= size + 8


def test_an_older_cleanup_does_not_replace_a_newer_queued_revoke(store):
    store.install_key()
    store.queue_forget("inst-1", "gw-1", "laptop", cgen=9)
    store.queue_forget("inst-1", "gw-1", "laptop", cgen=8)
    [pending] = store.pending_forgets()
    assert pending.cgen == 9
    store.queue_forget("inst-1", "gw-1", "laptop", cgen=10)
    [pending] = store.pending_forgets()
    assert pending.cgen == 10


def test_a_rollback_after_reset_keeps_the_fingerprint_it_enrolled_with(store):
    # reset-protection drops the install key while an enrollment is in flight;
    # its rollback must still be queued under the key it enrolled with.
    store.install_key()
    enrolled_with = store.fingerprint()
    store.reset_protection()
    store.queue_forget("inst-1", "gw-1", "laptop", cgen=3, fingerprint=enrolled_with)
    [pending] = store.pending_forgets()
    assert pending.fingerprint == enrolled_with
    assert pending.cgen == 3
