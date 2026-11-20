"""Connected computers: the approval snapshot and the early refusals of a share.

Every refusal here happens before the SSH enrollment, so no remote is called:
the store and the controller are stand-ins that record what was asked of them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.connected_computers import enroll, share
from kiro_crew.connected_computers.protected_store import ProtectionUnavailable
from kiro_crew.connected_computers.share import COORDINATE_FIELDS, ShareRefused
from kiro_crew.connected_computers.vault_doc import VaultDocError
from kiro_crew.testing.wait import finished

pytestmark = pytest.mark.asyncio

INSTANCE = SimpleNamespace(ssh_host="remote", local_port=1)


class TestConnectionCoordinates:
    def test_a_complete_snapshot_lists_every_field_in_order(self):
        got = share.connection_coordinates(INSTANCE)
        assert tuple(name for name, _ in got) == COORDINATE_FIELDS
        assert dict(got)["ssh_host"] == "remote"
        assert dict(got)["remote_port"] == ""

    def test_an_overlong_value_is_refused(self):
        with pytest.raises(ShareRefused, match="too long") as info:
            share.connection_coordinates(SimpleNamespace(ssh_host="h" * 1025))
        assert (info.value.status, info.value.code) == (400, "invalid_request")

    @pytest.mark.parametrize("port", [-1, 65536])
    def test_a_port_out_of_range_is_refused(self, port):
        with pytest.raises(ShareRefused, match="port is invalid"):
            share.connection_coordinates(SimpleNamespace(ssh_host="h", remote_port=port))

    @pytest.mark.parametrize("value", [None, True, 1.5, ["h"]])
    def test_a_value_of_another_type_is_refused(self, value):
        with pytest.raises(ShareRefused, match="details are invalid") as info:
            share.connection_coordinates(SimpleNamespace(ssh_host=value))
        assert info.value.message == "the connection details are invalid"


class TestStoredCoordinates:
    def test_a_round_trip_of_a_stored_approval_is_kept(self):
        stored = [list(pair) for pair in share.connection_coordinates(INSTANCE)]
        assert share.stored_coordinates(stored) == share.connection_coordinates(INSTANCE)

    @pytest.mark.parametrize("raw", [None, "text", {"ssh_host": "h"}, [], [("id", "")]])
    def test_a_missing_or_short_approval_grants_nothing(self, raw):
        assert share.stored_coordinates(raw) == ()

    def test_a_malformed_pair_grants_nothing(self):
        stored = [list(pair) for pair in share.connection_coordinates(INSTANCE)]
        stored[3] = ["ssh_host"]
        assert share.stored_coordinates(stored) == ()
        stored[3] = "ssh_host=remote"
        assert share.stored_coordinates(stored) == ()

    def test_an_invalid_stored_value_grants_nothing(self):
        stored = [list(pair) for pair in share.connection_coordinates(INSTANCE)]
        index = COORDINATE_FIELDS.index("remote_port")
        stored[index] = ["remote_port", 70000]
        assert share.stored_coordinates(stored) == ()
        stored[index] = ["remote_port", None]
        assert share.stored_coordinates(stored) == ()


class TestApprovedConnection:
    @pytest.mark.parametrize("port", ["1234", 70000, -5, None, True])
    def test_an_unusable_live_port_dials_nothing(self, port):
        instance = SimpleNamespace(ssh_host="remote", local_port=port)
        approved = share.approved_connection(instance, share.connection_coordinates(instance))
        assert approved.local_port == 0
        assert approved.ssh_host == "remote"

    def test_a_live_port_is_carried_but_never_compared(self):
        approved = share.approved_connection(
            SimpleNamespace(ssh_host="remote", local_port=4321),
            share.connection_coordinates(INSTANCE),
        )
        assert approved.local_port == 4321

    def test_a_replaced_connection_is_refused(self):
        with pytest.raises(ShareRefused) as info:
            share.approved_connection(
                SimpleNamespace(ssh_host="other"), share.connection_coordinates(INSTANCE)
            )
        assert (info.value.status, info.value.code) == (409, "connection_changed")


class _Store:
    def __init__(self, *, ready=None, generation=4) -> None:
        self.ready = ready
        self.generation = generation
        self.asked: list[str] = []

    def protection_ready(self):
        self.asked.append("protection_ready")
        if self.ready is not None:
            raise self.ready
        return "test"

    def off_generation(self):
        self.asked.append("off_generation")
        return self.generation

    def install_key(self):
        raise AssertionError("no install key is read before the early refusals")


class TestReadOffGeneration:
    async def test_the_generation_is_read_after_the_protection_check(self):
        store = _Store(generation="7")
        assert await finished(share.read_off_generation(store), "read generation") == 7
        assert store.asked == ["protection_ready", "off_generation"]

    async def test_no_protection_is_503(self):
        store = _Store(ready=ProtectionUnavailable("keyring locked"))
        with pytest.raises(ShareRefused) as info:
            await finished(share.read_off_generation(store), "read generation")
        assert (info.value.status, info.value.code) == (503, "protection_unavailable")
        assert info.value.message == "keyring locked"
        assert store.asked == ["protection_ready"]

    async def test_a_broken_document_is_409(self):
        store = _Store(ready=VaultDocError("connected computers state is unreadable"))
        with pytest.raises(ShareRefused) as info:
            await finished(share.read_off_generation(store), "read generation")
        assert (info.value.status, info.value.code) == (409, "protection_broken")
        assert "unreadable" in info.value.message


class TestForgetEnrolled:
    async def test_an_unconfirmed_undo_is_false(self, monkeypatch):
        monkeypatch.setattr(
            enroll, "forget", AsyncMock(side_effect=enroll.EnrollmentUnavailable("offline"))
        )
        assert await finished(share._forget_enrolled(INSTANCE, "laptop", "fp", 3), "undo") is False

    async def test_any_failure_of_the_undo_is_false(self, monkeypatch):
        monkeypatch.setattr(enroll, "forget", AsyncMock(side_effect=RuntimeError("boom")))
        assert await finished(share._forget_enrolled(INSTANCE, "laptop", "fp", 3), "undo") is False


def _controller(store):
    return SimpleNamespace(
        store=store, start=AsyncMock(), reconcile=AsyncMock(), pair_lock=AsyncMock()
    )


async def _share(controller, *, lookup, mcp=None, coordinates=None):
    return await finished(
        share.share_computer(
            controller,
            instance_id="inst-1",
            instance_lookup=lookup,
            root="/work/proj",
            mcp=[] if mcp is None else mcp,
            enabled=lambda: True,
            off_generation=0,
            coordinates=(
                share.connection_coordinates(INSTANCE) if coordinates is None else coordinates
            ),
        ),
        "share_computer",
    )


class TestShareEarlyRefusals:
    @pytest.mark.parametrize("mcp", ["fs", None, [1], ["ok", None]])
    async def test_mcp_must_be_a_list_of_names(self, mcp):
        looked = []
        controller = _controller(_Store())
        with pytest.raises(ShareRefused) as info:
            await finished(
                share.share_computer(
                    controller,
                    instance_id="inst-1",
                    instance_lookup=looked.append,
                    root="/work/proj",
                    mcp=mcp,
                    enabled=lambda: True,
                    off_generation=0,
                    coordinates=share.connection_coordinates(INSTANCE),
                ),
                "share_computer",
            )
        assert (info.value.status, info.value.code) == (400, "invalid_request")
        assert looked == [], "the connection is not even looked up"

    async def test_a_gone_connection_is_404(self):
        store = _Store()
        with pytest.raises(ShareRefused) as info:
            await _share(_controller(store), lookup=lambda _id: None)
        assert (info.value.status, info.value.code) == (404, "unknown_instance")
        assert store.asked == []

    async def test_a_changed_connection_is_409_before_the_store_is_asked(self):
        store = _Store()
        with pytest.raises(ShareRefused) as info:
            await _share(_controller(store), lookup=lambda _id: SimpleNamespace(ssh_host="new"))
        assert info.value.code == "connection_changed"
        assert store.asked == []

    async def test_no_protection_refuses_before_anything_reaches_the_remote(self, monkeypatch):
        remote = AsyncMock()
        monkeypatch.setattr(enroll, "enroll", remote)
        store = _Store(ready=ProtectionUnavailable("keyring locked"))
        with pytest.raises(ShareRefused) as info:
            await _share(_controller(store), lookup=lambda _id: INSTANCE)
        assert (info.value.status, info.value.code) == (503, "protection_unavailable")
        remote.assert_not_awaited()

    async def test_a_broken_document_refuses_before_anything_reaches_the_remote(self, monkeypatch):
        remote = AsyncMock()
        monkeypatch.setattr(enroll, "enroll", remote)
        store = _Store(ready=VaultDocError("connected computers state is invalid"))
        controller = _controller(store)
        with pytest.raises(ShareRefused) as info:
            await _share(controller, lookup=lambda _id: INSTANCE)
        assert (info.value.status, info.value.code) == (409, "protection_broken")
        assert info.value.message == "connected computers state is invalid"
        remote.assert_not_awaited()
        controller.start.assert_not_called()
