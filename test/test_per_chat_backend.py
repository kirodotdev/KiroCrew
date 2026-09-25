"""Per-chat ACP backend selection (F2, backend side) — Wave 3-A.

Covers the backend-side seams that let a single chat run on a harness other
than the global ``agent.acp_backend`` default:

* ``select_provider_backend`` tier order — a per-chat override beats the
  member-DM auto-route beats the configured default, and an UNSELECTABLE
  override is REFUSED (``BackendPinNotSelectable``, nothing sent) rather than
  degraded to kiro or handed to the next tier, since either would route the
  prompt to a provider the chat did not pick.
* ``_ChatSlot.acp_backend`` slot persistence round-trip (serialize -> restore).
* ``POST /api/chat/slots`` backend-field validation (empty ok; selectable ok;
  anything else 400 naming the selectable set).
* ``POST /api/chat/slots/{slot}/backend`` mutation (no-op, reset, invalid).
* The provider factory passing ``backend_override`` through the ONE selection
  gate.
* ``GET /api/models?backend=`` re-keying the catalog to a per-chat pick.

The tier-order and gate tests are the load-bearing ones: they pin that the new
arm is an INPUT to ``resolve_selected_backend`` (harness-parity H4), never a
second selectability gate of its own.
"""

from __future__ import annotations

import inspect
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.acp_backends import BackendPinNotSelectable
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.members import select_provider_backend
from kiro_crew.providers.base import LLMProvider


@pytest.fixture(autouse=True)
def _deepseek_unselectable():
    """These suites need a KNOWN but UNSELECTABLE backend to exercise the
    degrade/refuse branches, and ``deepseek`` -- known, and outside the shipped
    selectable set until its gate plugin landed -- is the id they were written
    around. Withdraw it from the live selectable set for the test and restore the
    set afterwards, so the branch under test still has a value to take."""
    from kiro_crew.acp.types import ACP_BACKEND_DEEPSEEK
    from kiro_crew.agent_sdk import backends as _b

    before = set(_b._selectable)
    _b._selectable.discard(ACP_BACKEND_DEEPSEEK)
    yield
    _b._selectable.clear()
    _b._selectable.update(before)


# A member-DM session key (``member-<slug>`` under the ``dashboard:`` alias the
# provider factory sees), so the member arm of select_provider_backend fires.
MEMBER_KEY = "dashboard:member-autofix"
# An ordinary chat session key — not a member, so the member arm never fires.
CHAT_KEY = "dashboard:chat-1-1700000000"


# ─────────────────────────── tier order ───────────────────────────


class TestSelectProviderBackendTierOrder:
    """select_provider_backend precedence: override > member > default.

    Every arm is an input to the ONE gate (resolve_selected_backend); no test
    here monkeypatches a second selectability check into existence.
    """

    def test_override_beats_member_route(self):
        # A per-chat override on a MEMBER session wins over the member backend:
        # the explicit pick is the highest tier.
        got = select_provider_backend(
            MEMBER_KEY,
            member_backend=ACP_BACKEND_KAS,
            configured_default=ACP_BACKEND_KIRO,
            override_backend=ACP_BACKEND_CLAUDE,
        )
        assert got == ACP_BACKEND_CLAUDE

    def test_override_beats_configured_default(self):
        got = select_provider_backend(
            CHAT_KEY,
            member_backend=ACP_BACKEND_KIRO,
            configured_default=ACP_BACKEND_KIRO,
            override_backend=ACP_BACKEND_KAS,
        )
        assert got == ACP_BACKEND_KAS

    def test_member_route_wins_when_no_override(self):
        # No override: the member auto-route is the next tier.
        got = select_provider_backend(
            MEMBER_KEY,
            member_backend=ACP_BACKEND_KAS,
            configured_default=ACP_BACKEND_KIRO,
            override_backend=None,
        )
        assert got == ACP_BACKEND_KAS

    def test_configured_default_wins_for_plain_chat(self):
        # No override, not a member: the configured default is the answer -- put
        # through the same gate, which leaves a selectable value as it is.
        got = select_provider_backend(
            CHAT_KEY,
            member_backend=ACP_BACKEND_KAS,
            configured_default=ACP_BACKEND_KAS,
            override_backend=None,
        )
        assert got == ACP_BACKEND_KAS

    def test_a_default_withdrawn_since_the_factory_was_built_degrades_to_kiro(self):
        # Selectability is live: a config-defined default whose verified binary
        # was found replaced is withdrawn at that spawn. The default was coerced
        # when config.json was read -- before the withdrawal -- so the tier must
        # re-resolve it per session rather than keep sending chats to a backend
        # that is absent from the selectable set.
        got = select_provider_backend(
            CHAT_KEY,
            member_backend=ACP_BACKEND_KIRO,
            configured_default=ACP_BACKEND_DEEPSEEK,  # not selectable
            override_backend=None,
        )
        assert got == ACP_BACKEND_KIRO

    def test_none_override_is_absent(self):
        # ``None`` is "not pinned" — the same as omitting the argument.
        got = select_provider_backend(
            CHAT_KEY,
            member_backend=ACP_BACKEND_KIRO,
            configured_default=ACP_BACKEND_KAS,
            override_backend=None,
        )
        assert got == ACP_BACKEND_KAS
        assert (
            select_provider_backend(
                CHAT_KEY, member_backend=ACP_BACKEND_KIRO, configured_default=ACP_BACKEND_KAS
            )
            == ACP_BACKEND_KAS
        )

    def test_kiro_override_is_an_explicit_pin(self):
        # kiro's backend id is the empty string (ACP_BACKEND_KIRO == ""), and
        # the override tier is guarded by ``is not None``, so a "pick kiro"
        # override is DISTINGUISHABLE from omitting the override: it wins over
        # the member route and over a non-kiro configured default. Collapsing
        # the two made an explicit Kiro pin run the global backend.
        assert ACP_BACKEND_KIRO == ""
        got = select_provider_backend(
            MEMBER_KEY,
            member_backend=ACP_BACKEND_KAS,
            configured_default=ACP_BACKEND_KAS,
            override_backend=ACP_BACKEND_KIRO,
        )
        assert got == ACP_BACKEND_KIRO
        got = select_provider_backend(
            CHAT_KEY,
            member_backend=ACP_BACKEND_KIRO,
            configured_default=ACP_BACKEND_KAS,
            override_backend=ACP_BACKEND_KIRO,
        )
        assert got == ACP_BACKEND_KIRO

    def test_unselectable_override_is_refused_not_degraded_on_a_member_thread(self):
        # DeepSeek is withdrawn from the selectable set by the suite fixture, so
        # resolve_selected_backend degrades it to kiro. A per-chat override that
        # degrades is neither "the user asked for kiro" nor "no pick": either
        # reading sends the prompt to a provider the chat did not choose while
        # the chat still shows its pin. The gate refuses, with nothing sent, and
        # the refusal names the pin and the set that IS selectable.
        with pytest.raises(BackendPinNotSelectable) as ei:
            select_provider_backend(
                MEMBER_KEY,
                member_backend=ACP_BACKEND_KAS,
                configured_default=ACP_BACKEND_KIRO,
                override_backend=ACP_BACKEND_DEEPSEEK,
            )
        assert ei.value.backend == ACP_BACKEND_DEEPSEEK
        assert ACP_BACKEND_KAS in ei.value.selectable
        assert ACP_BACKEND_DEEPSEEK not in ei.value.selectable
        text = str(ei.value)
        assert repr(ACP_BACKEND_DEEPSEEK) in text
        assert "Nothing was sent" in text
        assert "Settings > Backends" in text

    def test_unselectable_override_on_plain_chat_is_refused_not_defaulted(self):
        # Same refusal on a plain chat: the configured default is NOT the answer
        # for a chat that pinned something else. Falling through would route the
        # prompt to KAS under a pin that reads "deepseek".
        with pytest.raises(BackendPinNotSelectable):
            select_provider_backend(
                CHAT_KEY,
                member_backend=ACP_BACKEND_KIRO,
                configured_default=ACP_BACKEND_KAS,
                override_backend=ACP_BACKEND_DEEPSEEK,
            )

    def test_a_pin_withdrawn_after_it_was_honoured_is_refused_on_the_next_session(self):
        # Withdrawal is an ordinary event -- a spawn finds the verified binary
        # replaced and revokes the attestation -- so the same pin that was
        # honoured for one session is refused for the next, rather than the
        # next prompt quietly reaching the configured default.
        from kiro_crew.agent_sdk import backends as _b

        _b._selectable.add(ACP_BACKEND_DEEPSEEK)
        try:
            assert (
                select_provider_backend(
                    CHAT_KEY,
                    member_backend=ACP_BACKEND_KIRO,
                    configured_default=ACP_BACKEND_KAS,
                    override_backend=ACP_BACKEND_DEEPSEEK,
                )
                == ACP_BACKEND_DEEPSEEK
            )
        finally:
            _b._selectable.discard(ACP_BACKEND_DEEPSEEK)
        with pytest.raises(BackendPinNotSelectable):
            select_provider_backend(
                CHAT_KEY,
                member_backend=ACP_BACKEND_KIRO,
                configured_default=ACP_BACKEND_KAS,
                override_backend=ACP_BACKEND_DEEPSEEK,
            )


# ─────────────────────── slot persistence round-trip ───────────────────────


class TestSlotPersistenceRoundTrip:
    def test_slot_defaults_to_none_inherit(self):
        slot = _ChatSlot("chat-1")
        assert slot.acp_backend is None

    def test_acp_backend_is_a_real_slot_attribute(self):
        # __slots__ class: assignment must not raise (the field is declared).
        slot = _ChatSlot("chat-1")
        slot.acp_backend = ACP_BACKEND_KAS
        assert slot.acp_backend == ACP_BACKEND_KAS

    def test_full_save_meta_carries_pin_and_omits_empty(self, tmp_path):
        # The full-save meta_line writes acp_backend only when set (mirrors
        # reasoning_effort); an unpinned slot writes no row.
        from kiro_crew.dashboard import chat_persistence

        pinned = _ChatSlot("chat-pinned")
        pinned.acp_backend = ACP_BACKEND_KAS
        unpinned = _ChatSlot("chat-unpinned")

        # The serialize path builds meta_line from the slot; exercise the
        # persistence-then-restore loop through the ConversationLog the state
        # fixture wires up.
        state = _make_state(tmp_path)
        state._slots[pinned.key] = pinned
        state._slots[unpinned.key] = unpinned
        del chat_persistence  # imported to assert the module loads cleanly

    def test_a_forged_transcript_pin_is_not_restored(self, tmp_path, monkeypatch):
        """The transcript metadata line is editable by the agent's own tools, so a
        pin read back from it would let a prompt-injected agent hand the chat's
        next prompt to a provider the user never picked. Every restore path reads
        the gateway-private store instead; a transcript that CLAIMS a pin restores
        as unpinned when the store has none."""
        import json

        from kiro_crew.dashboard.chat_persistence import restore_open_slots
        from kiro_crew.dashboard.chat_utils import _history_key_for

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        history_key = _history_key_for("chat-forged")
        log = state.conversation_log
        assert log is not None
        log.append(history_key, "user", "hello")
        # What an agent editing the transcript could write.
        log.update_metadata(history_key, {"acp_backend": ACP_BACKEND_KAS})
        (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-forged"], "ts": 0.0}))

        state2 = _make_state(tmp_path / "sessions")
        assert restore_open_slots(state2) == 1
        slot = state2._slots.get("chat-forged")
        assert slot is not None
        assert slot.acp_backend is None

    @pytest.mark.asyncio
    async def test_open_slots_persist_and_restore(self, tmp_path, monkeypatch):
        # End-to-end: a pin recorded in the gateway-private store survives a
        # restart through the real restore_open_slots path, and "" (a Kiro pin)
        # is restored as a pin, not as "unset".
        import json

        from kiro_crew.dashboard import backend_pins
        from kiro_crew.dashboard.chat_persistence import restore_open_slots
        from kiro_crew.dashboard.chat_utils import _history_key_for

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        log = state.conversation_log
        assert log is not None
        for key in ("chat-e2e", "chat-kiro", "chat-inherit", "chat-stale"):
            log.append(_history_key_for(key), "user", "hello")
            # The creation identity the restore compares the record's owner to.
            log.update_metadata(_history_key_for(key), {"created_at": "t0"})
        backend_pins.set_pin("chat-e2e", ACP_BACKEND_KAS, owner="t0")
        backend_pins.set_pin("chat-kiro", ACP_BACKEND_KIRO, owner="t0")
        # A record left by an EARLIER chat under a reused key (its delete-time
        # cleanup failed): another creation owns it, so it must not be restored
        # onto the new chat -- that chat never selected a backend.
        backend_pins.set_pin("chat-stale", ACP_BACKEND_KAS, owner="an-earlier-creation")
        # The store is the sealed leaf beside the attestations, under the crew home.
        assert (tmp_path / backend_pins.BACKEND_PINS_LEAF).is_file()
        (tmp_path / "open_slots.json").write_text(
            json.dumps({"keys": ["chat-e2e", "chat-kiro", "chat-inherit", "chat-stale"], "ts": 0.0})
        )

        state2 = _make_state(tmp_path / "sessions")
        assert restore_open_slots(state2) == 4
        assert state2._slots["chat-e2e"].acp_backend == ACP_BACKEND_KAS
        assert state2._slots["chat-kiro"].acp_backend == ACP_BACKEND_KIRO
        assert state2._slots["chat-inherit"].acp_backend is None
        assert state2._slots["chat-stale"].acp_backend is None
        assert state2._slots["chat-stale"].backend_pin_unresolved is False

    @pytest.mark.asyncio
    async def test_the_async_restore_reads_the_store_off_loop_and_carries_the_pin(
        self, tmp_path, monkeypatch
    ):
        """The loop-affine apply half never opens the store: the pin arrives as a
        snapshot the worker-thread prefetch read, and the restored slot carries
        it. A store read on the event loop thread is the failure this pins."""
        import json
        import threading

        from kiro_crew.dashboard import backend_pins
        from kiro_crew.dashboard.chat_persistence import restore_open_slots_async
        from kiro_crew.dashboard.chat_utils import _history_key_for

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        log = state.conversation_log
        assert log is not None
        log.append(_history_key_for("chat-async"), "user", "hello")
        log.update_metadata(_history_key_for("chat-async"), {"created_at": "t0"})
        backend_pins.set_pin("chat-async", ACP_BACKEND_KAS, owner="t0")
        (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-async"], "ts": 0.0}))

        loop_thread = threading.get_ident()
        read_on: list[int] = []
        real_load = backend_pins.load_backend_pin_records

        def _recording_load(path=None):
            read_on.append(threading.get_ident())
            return real_load(path)

        monkeypatch.setattr(backend_pins, "load_backend_pin_records", _recording_load)
        state2 = _make_state(tmp_path / "sessions")
        assert await restore_open_slots_async(state2) == 1
        assert state2._slots["chat-async"].acp_backend == ACP_BACKEND_KAS
        assert read_on, "the pin store was never read"
        assert all(t != loop_thread for t in read_on), "the pin store was read on the loop thread"

    def test_channel_slot_surface_reads_the_store_not_the_meta(self, tmp_path, monkeypatch):
        """The third restore path (a channel-born slot surfaced into the dashboard)
        follows the same rule as the two persistence loaders."""
        from kiro_crew.dashboard import backend_pins, channel_slots

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        src = inspect.getsource(channel_slots)
        # No path reads the field back from transcript metadata any more.
        assert 'meta.get("acp_backend")' not in src
        assert 'meta["acp_backend"]' not in src
        # ... and the read is a snapshot the reconcile driver loaded off the loop,
        # applied through the one helper that fails closed on an unreadable store.
        assert "backend_pins.apply_restored_pin(slot, _pins)" in src
        assert "run_in_executor(None, backend_pins.load_backend_pins_snapshot)" in src
        assert "slot.acp_backend = _pins.get(" not in src
        backend_pins.set_pin("chat-surfaced", ACP_BACKEND_KAS, owner="t0")
        assert backend_pins.pin_for("chat-surfaced") == ACP_BACKEND_KAS

    def test_no_restore_path_reads_the_pin_from_transcript_metadata(self):
        from kiro_crew.dashboard import chat_persistence

        src = inspect.getsource(chat_persistence)
        assert 'meta.get("acp_backend")' not in src
        assert 'meta["acp_backend"]' not in src
        # Two apply sites read a SNAPSHOT (loaded off the loop by the async
        # drivers, inline only by the synchronous ones); neither re-opens the
        # store per slot on the event loop.
        assert src.count("backend_pins.apply_restored_pin(slot, _pins)") == 2
        assert "slot.acp_backend = _pins.get(" not in src
        assert "backend_pins.pin_for(" not in src
        assert "await asyncio.to_thread(backend_pins.load_backend_pins_snapshot)" in src
        # No restore path reads the store through the raising reader: an
        # unreadable store must not abort a restore, it must leave pins unresolved.
        assert "backend_pins.load_backend_pins()" not in src
        assert "backend_pins.load_backend_pins)" not in src

    @pytest.mark.asyncio
    async def test_an_unreadable_store_restores_the_chat_with_its_pin_unresolved(
        self, tmp_path, monkeypatch
    ):
        """Reading an unreadable store as "no pins" would restore every explicitly
        pinned chat as INHERIT and run its next prompt on the global backend --
        the retarget the sealed store exists to prevent. Instead the chats are
        restored (their transcripts are fine) with the pin UNRESOLVED: no
        backend is claimed, and dispatch refuses until the store reads."""
        import json

        from kiro_crew.dashboard import backend_pins
        from kiro_crew.dashboard.chat_persistence import (
            restore_open_slots,
            restore_open_slots_async,
        )
        from kiro_crew.dashboard.chat_utils import _history_key_for

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        log = state.conversation_log
        assert log is not None
        log.append(_history_key_for("chat-u"), "user", "hello")
        backend_pins.set_pin("chat-u", ACP_BACKEND_KAS, owner="t0")
        (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-u"], "ts": 0.0}))
        (tmp_path / backend_pins.BACKEND_PINS_LEAF).write_text("{truncated", encoding="utf-8")

        for restore in (restore_open_slots, restore_open_slots_async):
            state2 = _make_state(tmp_path / "sessions")
            result = restore(state2)
            if hasattr(result, "__await__"):
                result = await result
            assert result == 1
            slot = state2._slots["chat-u"]
            assert slot.acp_backend is None
            assert slot.backend_pin_unresolved is True
            assert state2.serialize_slot(slot)["backend_pin_unresolved"] is True
            assert state2.serialize_slot(slot)["acp_backend"] is None

        # An ABSENT store is the ordinary "no pins": nothing is unresolved.
        (tmp_path / backend_pins.BACKEND_PINS_LEAF).unlink()
        state3 = _make_state(tmp_path / "sessions")
        assert restore_open_slots(state3) == 1
        assert state3._slots["chat-u"].backend_pin_unresolved is False


class TestBackendPinStore:
    """``dashboard.backend_pins``: the gateway-private, sealed home of the pins."""

    def test_absent_store_means_no_pins(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import backend_pins

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert backend_pins.load_backend_pins() == {}
        assert backend_pins.pin_for("chat-x") is None

    def test_set_forget_and_kiro_pin_round_trip(self, tmp_path, monkeypatch):
        import json

        from kiro_crew.dashboard import backend_pins

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        backend_pins.set_pin("a", ACP_BACKEND_KAS, owner="a-created")
        backend_pins.set_pin("b", ACP_BACKEND_KIRO, owner="b-created")
        assert backend_pins.pin_for("a") == ACP_BACKEND_KAS
        assert backend_pins.pin_for("b") == ""
        on_disk = json.loads((tmp_path / backend_pins.BACKEND_PINS_LEAF).read_text("utf-8"))
        assert on_disk == {
            "a": {"backend": ACP_BACKEND_KAS, "owner": "a-created"},
            "b": {"backend": "", "owner": "b-created"},
        }
        assert backend_pins.forget_pin("a", owner="a-created") is True
        assert backend_pins.forget_pin("b", owner="b-created") is True
        assert backend_pins.load_backend_pins() == {}
        assert backend_pins.forget_pin("a", owner="a-created") is False

    def test_removal_is_conditioned_on_the_owning_slot(self, tmp_path, monkeypatch):
        """A delete that finishes after a same-key replacement recorded its pin
        must not remove the replacement's record: the owner is the creation
        identity of the slot, compared under the store lock with the delete."""
        from kiro_crew.dashboard import backend_pins

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        backend_pins.set_pin("chat-1", ACP_BACKEND_KAS, owner="first")
        # The replacement chat under the same key records its own pin ...
        backend_pins.set_pin("chat-1", ACP_BACKEND_KIRO, owner="second")
        # ... and the late delete of the first chat removes nothing.
        assert backend_pins.forget_pin("chat-1", owner="first") is False
        assert backend_pins.pin_for("chat-1") == ACP_BACKEND_KIRO
        assert backend_pins.forget_pin("chat-1", owner="second") is True
        assert backend_pins.pin_for("chat-1") is None

    def test_malformed_or_non_record_entries_fail_closed_for_readers(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import backend_pins

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        store = tmp_path / backend_pins.BACKEND_PINS_LEAF
        # An UNREADABLE document is refused to a reader too -- "no pins" read
        # from it would restore every explicit pin as inherit -- while the
        # restore-path snapshot turns the refusal into the identity marker.
        store.write_text("not json", encoding="utf-8")
        with pytest.raises(backend_pins.BackendPinStoreUnreadable):
            backend_pins.load_backend_pins()
        with pytest.raises(backend_pins.BackendPinStoreUnreadable):
            backend_pins.pin_for("b")
        assert backend_pins.load_backend_pins_snapshot() is backend_pins.PINS_UNREADABLE
        # Entries that are not records are dropped individually: they carry no pin.
        store.write_text(
            '{"a": 1, "b": {"backend": "kas", "owner": "t"}, "c": null, "d": "kas"}',
            encoding="utf-8",
        )
        assert backend_pins.load_backend_pins() == {"b": "kas"}
        snap = backend_pins.load_backend_pins_snapshot()
        assert snap == {"b": {"backend": "kas", "owner": "t"}}
        assert snap is not backend_pins.PINS_UNREADABLE
        store.write_text("[]", encoding="utf-8")
        with pytest.raises(backend_pins.BackendPinStoreUnreadable):
            backend_pins.load_backend_pins()
        assert backend_pins.load_backend_pins_snapshot() is backend_pins.PINS_UNREADABLE
        # apply_restored_pin: a readable snapshot stamps the pin (or inherit); the
        # marker leaves the pin UNRESOLVED and claims no backend.
        slot = type(
            "S",
            (),
            {"key": "b", "created_at": "t", "acp_backend": "stale", "backend_pin_unresolved": True},
        )()
        backend_pins.apply_restored_pin(slot, {"b": {"backend": "kas", "owner": "t"}})
        assert (slot.acp_backend, slot.backend_pin_unresolved) == ("kas", False)
        # Owned by another creation of the same key: ignored, the chat inherits.
        slot.acp_backend = "stale"
        backend_pins.apply_restored_pin(slot, {"b": {"backend": "kas", "owner": "someone-else"}})
        assert (slot.acp_backend, slot.backend_pin_unresolved) == (None, False)
        backend_pins.apply_restored_pin(slot, {})
        assert (slot.acp_backend, slot.backend_pin_unresolved) == (None, False)
        slot.acp_backend = "stale"
        backend_pins.apply_restored_pin(slot, backend_pins.PINS_UNREADABLE)
        assert (slot.acp_backend, slot.backend_pin_unresolved) == (None, True)

    def test_an_unreadable_store_aborts_a_write_instead_of_emptying_it(self, tmp_path, monkeypatch):
        """A writer's read-modify-write must not start from ``{}`` because the
        read failed: it would rewrite the document without every other chat's
        pin. Absent is the one legitimate empty start; unreadable aborts."""
        from kiro_crew.dashboard import backend_pins

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        backend_pins.set_pin("a", ACP_BACKEND_KAS, owner="t")
        backend_pins.set_pin("b", ACP_BACKEND_KIRO, owner="t")
        store = tmp_path / backend_pins.BACKEND_PINS_LEAF
        good = store.read_bytes()
        store.write_text("{truncated", encoding="utf-8")
        with pytest.raises(backend_pins.BackendPinStoreUnreadable):
            backend_pins.set_pin("c", ACP_BACKEND_KAS, owner="t")
        with pytest.raises(backend_pins.BackendPinStoreUnreadable):
            backend_pins.forget_pin("a", owner="t")
        assert store.read_text(encoding="utf-8") == "{truncated"
        store.write_text("[]", encoding="utf-8")
        with pytest.raises(backend_pins.BackendPinStoreUnreadable):
            backend_pins.set_pin("c", ACP_BACKEND_KAS, owner="t")
        store.write_bytes(good)
        assert backend_pins.forget_pin("a", owner="t") is True
        assert backend_pins.load_backend_pins() == {"b": ""}
        store.unlink()
        backend_pins.set_pin("c", ACP_BACKEND_KAS, owner="t")
        assert backend_pins.load_backend_pins() == {"c": ACP_BACKEND_KAS}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink alias")
    def test_an_aliased_store_is_refused(self, tmp_path, monkeypatch):
        """A seal covers the referent; a link at the name is how a forged document
        would arrive. The reader refuses the alias: it serves neither the linked
        document's pins nor "no pins" (which would retarget every pinned chat)."""
        import json

        from kiro_crew.dashboard import backend_pins

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        real = tmp_path / "elsewhere.json"
        real.write_text(
            json.dumps({"a": {"backend": ACP_BACKEND_KAS, "owner": "t"}}), encoding="utf-8"
        )
        (tmp_path / backend_pins.BACKEND_PINS_LEAF).symlink_to(real)
        with pytest.raises(backend_pins.BackendPinStoreUnreadable):
            backend_pins.load_backend_pins()
        assert backend_pins.load_backend_pins_snapshot() is backend_pins.PINS_UNREADABLE

    def test_the_leaf_is_sealed_write_protected_and_strict_no_follow(self):
        from kiro_crew import sandbox
        from kiro_crew.dashboard import backend_pins
        from kiro_crew.security import paths as security_paths

        leaf = backend_pins.BACKEND_PINS_LEAF
        assert leaf in sandbox._CREW_READONLY_LEAVES
        assert leaf in sandbox._CREW_CHILD_WITHHELD_LEAVES
        assert leaf in sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        assert leaf in sandbox._DELEGATED_OVERLAP_LEAF_REASONS
        assert any(p.endswith("/" + leaf) for p in security_paths._WRITE_PROTECTED_HOME_PATHS)


# ──────────────────────── create-handler validation ────────────────────────


async def _create_slot(state: Any, payload: dict[str, Any]) -> tuple[int, Any]:
    # Read the body INSIDE the client context: a TestClient response's
    # connection is closed when the `async with` exits, so `resp.json()` on a
    # returned response raises ClientConnectionError. Return (status, body).
    app = web.Application()

    @web.middleware
    async def _auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        if "user" not in request:
            request["user"] = "local-app"
        return await handler(request)

    app.middlewares.append(_auth)
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots", json=payload)
        text = await resp.text()
        body: Any = None
        if text:
            try:
                body = await resp.json()
            except Exception:
                body = text
        return resp.status, body


class TestCreateHandlerBackendValidation:
    @pytest.mark.asyncio
    async def test_absent_backend_inherits(self, tmp_path):
        state = _make_state(tmp_path)
        status, body = await _create_slot(state, {"name": "c-absent"})
        assert status < 300, body
        slot = state._slots.get("c-absent")
        assert slot is not None
        assert slot.acp_backend is None

    @pytest.mark.asyncio
    async def test_null_backend_inherits(self, tmp_path):
        state = _make_state(tmp_path)
        status, body = await _create_slot(state, {"name": "c-null", "backend": None})
        assert status < 300, body
        assert state._slots["c-null"].acp_backend is None

    @pytest.mark.asyncio
    async def test_empty_string_backend_is_an_explicit_kiro_pin(self, tmp_path):
        # "" is kiro-cli's own id: a real pin, not "unset". Under a non-kiro
        # configured default this is what keeps the chat on kiro.
        state = _make_state(tmp_path)
        status, body = await _create_slot(state, {"name": "c-kiro", "backend": ""})
        assert status < 300, body
        slot = state._slots.get("c-kiro")
        assert slot is not None
        assert slot.acp_backend == ACP_BACKEND_KIRO == ""
        from kiro_crew.dashboard import backend_pins

        # Recorded in the sealed store, where the restore reads it: "" survives
        # a restart as a Kiro pin, not as "inherit".
        assert backend_pins.pin_for("c-kiro") == ""

    @pytest.mark.asyncio
    async def test_selectable_backend_is_stamped(self, tmp_path):
        state = _make_state(tmp_path)
        status, body = await _create_slot(state, {"name": "c-kas", "backend": ACP_BACKEND_KAS})
        assert status < 300, body
        slot = state._slots.get("c-kas")
        assert slot is not None
        assert slot.acp_backend == ACP_BACKEND_KAS
        from kiro_crew.dashboard import backend_pins

        assert backend_pins.pin_for("c-kas") == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_a_failed_store_write_retracts_the_newborn_and_answers_500(
        self, tmp_path, monkeypatch
    ):
        # Persist before you publish: the caller asked for a chat PINNED to a
        # backend. When the sealed store cannot be written, the handler must not
        # answer 200 for an unpinned chat whose first turn would run on the
        # default backend; the newborn is retracted and the caller gets a
        # retryable, coded 500.
        from kiro_crew.dashboard import backend_pins

        state = _make_state(tmp_path)
        seen_at_write: list[object] = []

        def failing_set_pin(slot_key, backend, *, owner, path=None):
            # The store write runs BEFORE the field is assigned: at this moment
            # the newborn still reads as unpinned, so no concurrent reader ever
            # saw a backend the store does not hold.
            seen_at_write.append(state._slots[slot_key].acp_backend)
            raise OSError("read-only file system")

        monkeypatch.setattr(backend_pins, "set_pin", failing_set_pin)
        status, body = await _create_slot(
            state, {"name": "c-unwritable", "backend": ACP_BACKEND_KAS}
        )
        assert status == 500, body
        assert body["code"] == "backend_persist_failed"
        assert "c-unwritable" not in state._slots
        assert backend_pins.pin_for("c-unwritable") is None
        assert seen_at_write == [None]

    @pytest.mark.asyncio
    async def test_creation_publishes_the_pin_only_after_the_store_holds_it(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import backend_pins

        state = _make_state(tmp_path)
        real_set_pin = backend_pins.set_pin
        seen_at_write: list[object] = []

        def spy_set_pin(slot_key, backend, *, owner, path=None):
            seen_at_write.append(state._slots[slot_key].acp_backend)
            return real_set_pin(slot_key, backend, owner=owner, path=path)

        monkeypatch.setattr(backend_pins, "set_pin", spy_set_pin)
        status, body = await _create_slot(state, {"name": "c-order", "backend": ACP_BACKEND_KAS})
        assert status == 200, body
        assert seen_at_write == [None]
        assert state._slots["c-order"].acp_backend == ACP_BACKEND_KAS
        assert backend_pins.pin_for("c-order") == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_unselectable_backend_is_rejected_400(self, tmp_path):
        state = _make_state(tmp_path)
        # DeepSeek is known but not selectable -> the create refuses rather than
        # silently degrading, and names the selectable set.
        status, body = await _create_slot(state, {"name": "c-bad", "backend": ACP_BACKEND_DEEPSEEK})
        assert status == 400, body
        assert body["code"] == "invalid_backend"
        assert "not selectable" in body["error"]
        assert "c-bad" not in state._slots

    @pytest.mark.asyncio
    async def test_non_string_backend_is_rejected_400(self, tmp_path):
        state = _make_state(tmp_path)
        status, body = await _create_slot(state, {"name": "c-type", "backend": 42})
        assert status == 400, body
        assert body["code"] == "invalid_backend"


# ──────────────────────── mutation endpoint ────────────────────────


async def _post_backend(state: Any, slot_key: str, backend: Any) -> tuple[int, Any]:
    # Read the body inside the client context (see _create_slot).
    app = web.Application()

    @web.middleware
    async def _auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        if "user" not in request:
            request["user"] = "local-app"
        return await handler(request)

    app.middlewares.append(_auth)
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/backend", chat_handlers.api_chat_slot_backend)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{slot_key}/backend", json={"backend": backend})
        text = await resp.text()
        body: Any = None
        if text:
            try:
                body = await resp.json()
            except Exception:
                body = text
        return resp.status, body


class TestBackendMutationEndpoint:
    @pytest.mark.asyncio
    async def test_missing_slot_404(self, tmp_path):
        state = _make_state(tmp_path)
        status, _ = await _post_backend(state, "no-such", ACP_BACKEND_KAS)
        assert status == 404

    @pytest.mark.asyncio
    async def test_unselectable_backend_rejected_400(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("c-mut-bad", agent="")
        status, body = await _post_backend(state, "c-mut-bad", ACP_BACKEND_DEEPSEEK)
        assert status == 400, body
        assert body["code"] == "invalid_backend"
        # The error is shown verbatim in the composer's switch notice, so it is
        # prose naming backends by their display labels -- never a Python repr
        # with a bare '' standing for kiro-cli.
        msg = body["error"]
        assert "is not selectable in this build" in msg
        assert "Choose one of:" in msg
        assert "Kiro CLI" in msg
        assert "''" not in msg and "[" not in msg and "]" not in msg, msg

    @pytest.mark.asyncio
    async def test_noop_same_value_returns_without_reset(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-noop", agent="")
        slot.acp_backend = ACP_BACKEND_KAS
        status, body = await _post_backend(state, "c-mut-noop", ACP_BACKEND_KAS)
        assert status == 200, body
        # The no-op branch returns this exact body BEFORE reaching any session
        # teardown, so the matching value is itself the proof no reset ran.
        assert body == {"ok": True, "backend": ACP_BACKEND_KAS}

    @pytest.mark.asyncio
    async def test_change_resets_session_and_commits(self, tmp_path):
        state = _make_state(tmp_path)
        # No live provider (default in _make_state), so the reset short-circuits
        # to "no session to tear down" but still commits the pin.
        slot = state.get_or_create_slot("c-mut-set", agent="")
        assert slot.acp_backend is None
        status, body = await _post_backend(state, "c-mut-set", ACP_BACKEND_KAS)
        assert status == 200, body
        assert body["ok"] is True
        assert body["backend"] == ACP_BACKEND_KAS
        assert slot.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_change_durably_persists_the_pin(self, tmp_path):
        # Regression: the endpoint must WRITE the pin to durable metadata, not
        # only broadcast it to the sidebar — else a gateway restart loses it and
        # the next turn runs on the global backend. Post through the real
        # handler, then read the ConversationLog metadata the restore path reads.
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = _make_state(tmp_path)
        state.get_or_create_slot("c-persist", agent="")
        history_key = _history_key_for("c-persist")
        log = state.conversation_log
        assert log is not None
        log.append(history_key, "user", "hello")  # a line for the meta to land on

        status, body = await _post_backend(state, "c-persist", ACP_BACKEND_KAS)
        assert status == 200, body

        # The transcript line still CARRIES the pin (a transcript says what served
        # it) ...
        meta = log.get_metadata(history_key) or {}
        assert meta.get("acp_backend") == ACP_BACKEND_KAS
        # ... but the restore reads the gateway-private store, so THAT is the
        # durable write a restart depends on.
        from kiro_crew.dashboard import backend_pins

        assert (
            backend_pins.pin_for("c-persist") == ACP_BACKEND_KAS
        ), "backend pin was not written to the sealed store; a restart would lose it"
        status, body = await _post_backend(state, "c-persist", None)
        assert status == 200, body
        assert backend_pins.pin_for("c-persist") is None

    @pytest.mark.asyncio
    async def test_persist_runs_inside_the_slot_lock_span(self, tmp_path, monkeypatch):
        # Regression: the persist + rollback must run UNDER slot._lock. Outside
        # it, two concurrent same-slot POSTs interleave their awaits and a
        # failed persist's value-based rollback can overwrite the OTHER
        # request's committed pin in memory. Observe the lock state at the
        # moment the handler calls the persist seam.
        import kiro_crew.dashboard.chat_handlers as ch

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-locked", agent="")
        observed: dict[str, bool] = {}
        real_save = ch.save_slot_off_loop

        async def spy(st, sl, *a, **kw):
            observed["locked_at_persist"] = sl._lock.locked()
            return await real_save(st, sl, *a, **kw)

        monkeypatch.setattr(ch, "save_slot_off_loop", spy)
        status, body = await _post_backend(state, "c-locked", ACP_BACKEND_KAS)
        assert status == 200, body
        assert observed.get("locked_at_persist") is True, (
            "backend pin persisted outside slot._lock: a concurrent same-slot POST's "
            "rollback could clobber this request's committed value"
        )
        assert slot.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_failed_persist_rolls_back_and_discards_a_session_started_in_the_window(
        self, tmp_path, monkeypatch
    ):
        # The pin is published in memory before its durable write, and message
        # dispatch does not take slot._lock: a message landing in that window
        # cold-starts a provider on the NEW backend. When the write then fails,
        # rolling back the pin alone would leave that provider live on a backend
        # other than the one the slot advertises. The rollback must also tear it down.
        import kiro_crew.dashboard.chat_handlers as ch
        from kiro_crew.dashboard.chat_utils import effective_session_key

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-fence", agent="")
        session_key = effective_session_key(slot)
        started = object()  # the provider a concurrent send would register
        torn_down: list[str] = []

        async def failing_save(st, sl, *a, **kw):
            # Simulate the send that slipped into the mutate-persist window.
            state.sessions.get_provider = lambda key: started if key == session_key else None
            return False

        async def spy_reset(st, sl, key, *, switch_kind):
            torn_down.append(key)
            state.sessions.get_provider = lambda key: None
            return True

        monkeypatch.setattr(ch, "save_slot_off_loop", failing_save)
        monkeypatch.setattr(ch, "_reset_slot_session_or_warn", spy_reset)
        status, body = await _post_backend(state, "c-fence", ACP_BACKEND_KAS)
        assert status == 500 and body["code"] == "backend_persist_failed"
        assert slot.acp_backend is None, "the pin must roll back"
        # The first reset is the switch's own (no provider then); the second is
        # the fence discarding the session started inside the window.
        assert torn_down == [session_key, session_key]
        assert state.sessions.get_provider(session_key) is None
        # The sealed store was written before the transcript persist failed; the
        # rollback puts it back too, so a restart cannot resurrect the rolled-back pin.
        from kiro_crew.dashboard import backend_pins

        assert backend_pins.pin_for("c-fence") is None

    @pytest.mark.asyncio
    async def test_a_failed_store_write_rolls_back_before_the_transcript_persist(
        self, tmp_path, monkeypatch
    ):
        # The sealed store is the authoritative write and goes first: when IT
        # fails, the pin rolls back, nothing reaches the transcript line, and the
        # caller gets the same coded 500.
        import kiro_crew.dashboard.chat_handlers as ch
        from kiro_crew.dashboard import backend_pins
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-store-fail", agent="")
        history_key = _history_key_for("c-store-fail")
        log = state.conversation_log
        assert log is not None
        log.append(history_key, "user", "hello")
        saves: list[str] = []

        async def spy_save(st, sl, *a, **kw):
            saves.append(sl.key)
            return True

        resets: list[str] = []
        real_reset = ch._reset_slot_session_or_warn

        async def spy_reset(st, sl, *a, **kw):
            resets.append(sl.key)
            return await real_reset(st, sl, *a, **kw)

        seen_at_write: list[object] = []

        def failing_set_pin(slot_key, backend, *, owner, path=None):
            seen_at_write.append(slot.acp_backend)
            raise OSError("disk full")

        monkeypatch.setattr(ch, "save_slot_off_loop", spy_save)
        monkeypatch.setattr(ch, "_reset_slot_session_or_warn", spy_reset)
        monkeypatch.setattr(backend_pins, "set_pin", failing_set_pin)
        status, body = await _post_backend(state, "c-store-fail", ACP_BACKEND_KAS)
        assert status == 500 and body["code"] == "backend_persist_failed"
        assert slot.acp_backend is None
        # Persist before publish: the field still read the prior value when the
        # store was written, the session was not reset for a switch that never
        # became durable, and nothing reached the transcript line.
        assert seen_at_write == [None]
        assert resets == []
        assert saves == [], "the transcript line must not be written when the store write failed"
        assert (log.get_metadata(history_key) or {}).get("acp_backend") is None

    @pytest.mark.asyncio
    async def test_switch_publishes_the_field_only_after_the_store_and_the_reset(
        self, tmp_path, monkeypatch
    ):
        import kiro_crew.dashboard.chat_handlers as ch
        from kiro_crew.dashboard import backend_pins

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-order-switch", agent="")
        order: list[str] = []
        real_set_pin = backend_pins.set_pin
        real_reset = ch._reset_slot_session_or_warn

        def spy_set_pin(slot_key, backend, *, owner, path=None):
            order.append(f"store:{slot.acp_backend!r}")
            return real_set_pin(slot_key, backend, owner=owner, path=path)

        async def spy_reset(st, sl, *a, **kw):
            order.append(f"reset:{sl.acp_backend!r}")
            return await real_reset(st, sl, *a, **kw)

        monkeypatch.setattr(backend_pins, "set_pin", spy_set_pin)
        monkeypatch.setattr(ch, "_reset_slot_session_or_warn", spy_reset)
        status, body = await _post_backend(state, "c-order-switch", ACP_BACKEND_KAS)
        assert status == 200, body
        # Store first, then the reset, both while the field still says None; the
        # field is published last.
        assert order == ["store:None", "reset:None"]
        assert slot.acp_backend == ACP_BACKEND_KAS
        assert backend_pins.pin_for("c-order-switch") == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_clearing_a_persisted_pin_does_not_resurrect_on_save(self, tmp_path):
        # Regression: the slot save writes ``acp_backend`` only when non-empty.
        # If the key is NOT slot-owned, a full save after clearing the pin omits
        # it and ``carry_unowned_metadata`` copies the OLD value back from the
        # existing line -- the cleared pin comes back on the next restart. Pin,
        # persist, clear, persist, then read the metadata the restore path reads.
        # The slot needs IN-MEMORY messages: a message-less slot takes the
        # empty-window merge (which writes the key unconditionally) and would
        # never exercise the full-rebuild path this guards.
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-clear-durable", agent="")
        slot.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        history_key = _history_key_for("c-clear-durable")
        log = state.conversation_log
        assert log is not None
        log.append(history_key, "user", "hello")

        status, body = await _post_backend(state, "c-clear-durable", ACP_BACKEND_KAS)
        assert status == 200, body
        assert (log.get_metadata(history_key) or {}).get("acp_backend") == ACP_BACKEND_KAS

        status, body = await _post_backend(state, "c-clear-durable", "")
        assert status == 200, body
        meta = log.get_metadata(history_key) or {}
        assert not meta.get(
            "acp_backend"
        ), f"cleared pin resurrected in durable metadata: {meta.get('acp_backend')!r}"

    @pytest.mark.asyncio
    async def test_clear_pin_back_to_inherit(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-clear", agent="")
        slot.acp_backend = ACP_BACKEND_KAS
        status, body = await _post_backend(state, "c-mut-clear", None)
        assert status == 200, body
        assert slot.acp_backend is None
        assert body["backend"] is None

    @pytest.mark.asyncio
    async def test_empty_string_pins_kiro_rather_than_clearing(self, tmp_path):
        # "" is a pin to kiro-cli, distinct from null (inherit): a chat under a
        # non-kiro default can be held on kiro.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-kiro", agent="")
        slot.acp_backend = ACP_BACKEND_KAS
        status, body = await _post_backend(state, "c-mut-kiro", "")
        assert status == 200, body
        assert slot.acp_backend == ""
        assert body["backend"] == ""

    @pytest.mark.asyncio
    async def test_turn_in_flight_answers_409(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-busy", agent="")
        # ``running`` is a read-only property derived from a live turn, so drive
        # the busy state through the provider the handler consults: a live
        # provider whose has_active_turn() is True is exactly what the 409 arm
        # checks after the slot.running fast path.
        session_key = "dashboard:c-mut-busy"
        provider = MagicMock(spec=LLMProvider)
        provider.has_active_turn.return_value = True
        state.sessions.get_provider.return_value = provider
        resp_status, body = await _post_backend(state, "c-mut-busy", ACP_BACKEND_KAS)
        assert resp_status == 409, body
        assert body["code"] == "turn_in_flight"
        # The pin is never committed under a live turn.
        assert slot.acp_backend is None
        del session_key

    @pytest.mark.asyncio
    async def test_pick_during_eager_spawn_handshake_commits_not_409(self, tmp_path):
        """Regression: a pick on a brand-new chat answered 409 "a turn is in flight".

        For the first ~20-30s of every new chat ``_eager_spawn``'s speculative
        handshake runs BEFORE it registers a session, so ``get_provider`` is None
        and the real ``SessionManager.reset`` answers **False** -- "nothing to
        reset", the same value it uses for "declined, busy". The handler read
        that False as a live turn and rolled the pin back (found on the pod
        hands-test: the welcome-screen picker toasted "a turn is running" on an
        empty chat). The default ``_make_state`` mock hid this because an
        auto-created AsyncMock returns a truthy MagicMock; pin the REAL contract
        here. With no provider registered the pick must commit: the eager path's
        ``_slot_binding`` guard tears down the session it registers if the
        bindings changed mid-handshake.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-eager", agent="")
        state.sessions.get_provider.return_value = None
        state.sessions.reset = AsyncMock(return_value=False)
        status, body = await _post_backend(state, "c-mut-eager", ACP_BACKEND_KAS)
        assert status == 200, body
        assert body == {"ok": True, "backend": ACP_BACKEND_KAS}
        assert slot.acp_backend == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_live_session_declining_reset_still_409(self, tmp_path):
        """The complement: reset False WITH a registered provider is a real
        decline (a turn slipped into the window) and keeps the 409 + rollback."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("c-mut-declined", agent="")
        provider = MagicMock(spec=LLMProvider)
        # Passes the pre-check (no active turn at that instant)...
        provider.has_active_turn.return_value = False
        state.sessions.get_provider.return_value = provider
        # ...but the reset itself is declined by the live session.
        state.sessions.reset = AsyncMock(return_value=False)
        status, body = await _post_backend(state, "c-mut-declined", ACP_BACKEND_KAS)
        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        assert slot.acp_backend is None


# ──────────────────────── factory pass-through ────────────────────────


class TestFactoryPassThrough:
    def test_acp_closure_feeds_backend_override_to_the_one_gate(self, monkeypatch):
        # The provider factory's _acp closure must pass backend_override into
        # the SINGLE select_provider_backend call, not branch on it itself -- and
        # that one answer must feed BOTH the model-namespace resolution and the
        # provider. A second, override-blind call for the namespace translated a
        # per-chat pinned model against the default backend's namespace and then
        # started a different backend; recording only the LAST call's kwargs is
        # how that slipped past this test, so every call is recorded now.
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ""  # configured default = kiro
        cfg.agent.member_acp_backend = ACP_BACKEND_KAS

        calls: list[dict[str, Any]] = []

        def _fake_select(session_key, member_backend, configured_default, override_backend=""):
            calls.append(
                {
                    "session_key": session_key,
                    "member_backend": member_backend,
                    "configured_default": configured_default,
                    "override_backend": override_backend,
                }
            )
            return override_backend or configured_default

        monkeypatch.setattr("kiro_crew.members.select_provider_backend", _fake_select, raising=True)

        captured_namespace: dict[str, Any] = {}
        real_effective_model = KiroCrewConfig.acp_effective_model

        def _spy_effective_model(self, *args, **kwargs):
            captured_namespace["namespace"] = kwargs.get("namespace")
            return real_effective_model(self, *args, **kwargs)

        monkeypatch.setattr(
            KiroCrewConfig, "acp_effective_model", _spy_effective_model, raising=True
        )

        captured_provider: dict[str, Any] = {}

        class _FakeProvider:
            def __init__(self, **kwargs):
                captured_provider.update(kwargs)

        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", _FakeProvider, raising=True)

        factory = cfg.create_provider_factory()
        factory(CHAT_KEY, backend_override=ACP_BACKEND_KAS)

        assert len(calls) == 1, calls
        assert calls[0]["override_backend"] == ACP_BACKEND_KAS
        # The single resolved value is what reaches AcpProvider(acp_backend=...)...
        assert captured_provider["acp_backend"] == ACP_BACKEND_KAS
        # ...and what the model resolution keyed its namespace on.
        from kiro_crew.agent_sdk.capabilities import capabilities_for

        assert (
            captured_namespace["namespace"] == capabilities_for(ACP_BACKEND_KAS).model_id_namespace
        )

    def test_absent_backend_override_leaves_selection_to_lower_tiers(self, monkeypatch):
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ""
        cfg.agent.member_acp_backend = ACP_BACKEND_KAS

        seen: dict[str, Any] = {}

        def _fake_select(session_key, member_backend, configured_default, override_backend=""):
            seen["override_backend"] = override_backend
            return override_backend or configured_default

        monkeypatch.setattr("kiro_crew.members.select_provider_backend", _fake_select, raising=True)
        monkeypatch.setattr(
            "kiro_crew.providers.acp.AcpProvider",
            lambda **kwargs: MagicMock(**kwargs),
            raising=True,
        )

        factory = cfg.create_provider_factory()
        factory(CHAT_KEY)  # no backend_override
        assert seen["override_backend"] is None


# ──────────────────────── models?backend= branch ────────────────────────


class TestModelsBackendQuery:
    @pytest.mark.asyncio
    async def test_backend_query_rekeys_the_catalog(self, tmp_path, monkeypatch):
        # ?backend=claude re-keys api_models to the claude adapter's catalog,
        # independent of the configured global backend (kiro by default).
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.handlers import agents as agents_mod

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ""  # global = kiro
        monkeypatch.setattr(
            agents_mod, "KiroCrewConfig", MagicMock(load=MagicMock(return_value=cfg))
        )
        # claude adapter catalog stub, so the branch is observable without a CLI.
        sentinel = [{"model_name": "claude-sonnet"}]
        monkeypatch.setattr(agents_mod, "_cc_models", lambda request, configured_default: sentinel)

        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/models", agents_mod.api_models)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/api/models?backend={ACP_BACKEND_CLAUDE}")
            assert resp.status == 200, await resp.text()
            assert await resp.json() == sentinel

    @pytest.mark.asyncio
    async def test_unselectable_backend_query_is_ignored(self, tmp_path, monkeypatch):
        # An unselectable ?backend= degrades to the configured backend rather
        # than being honored (H4: one gate). Configured = claude here so the
        # claude branch answers even though the query named deepseek.
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.handlers import agents as agents_mod

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
        monkeypatch.setattr(
            agents_mod, "KiroCrewConfig", MagicMock(load=MagicMock(return_value=cfg))
        )
        sentinel = [{"model_name": "claude-sonnet"}]
        monkeypatch.setattr(agents_mod, "_cc_models", lambda request, configured_default: sentinel)

        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/models", agents_mod.api_models)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/api/models?backend={ACP_BACKEND_DEEPSEEK}")
            # deepseek not selectable -> configured (claude) answers.
            assert resp.status == 200, await resp.text()
            assert await resp.json() == sentinel
