"""A value a remote-bound slot owns ON THE PEER must not be interpreted here.

Five review rounds of this family were one rule found a site at a time: a
peer-owned value met by a LOCAL vocabulary, which either refused it (membership,
display-key rejection) or rewrote it (provider-keyed canonicalization, a
deprecation rename) — and either way handed the picker a value that disagrees
with the machine actually running the turns, whose next pick forwards and
overwrites the peer's live setting.

These tests pin the chokepoint that ends the enumeration rather than the four
sites: one admission table, one decision function, and a guard that a control
made forwardable without an entry cannot silently fall through to the local
treatment.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_model
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

#: A canonical registry KEY. Display-only for every provider except claude_code,
#: which is why ``_model_rejected_reason`` 400s it under the default ``acp``
#: provider — and why a peer whose own roster advertises it could never re-select
#: its own model while that gate ran first.
CANONICAL_KEY = "opus-4.8-1m"

#: kiro-cli's own id for that model. A claude_code-backed hub folds it ONTO
#: ``opus-4.8-1m``, so it is the value that exposes the rewrite.
PEER_ACP_ID = "claude-opus-4.8"

#: In ``_DEPRECATED_MODEL_MAP``, so ``_normalize_model`` renames it regardless of
#: provider — the rewrite that needs no config to demonstrate.
DEPRECATED_ID = "claude-opus-4.6-1m"


def _remote(slot: _ChatSlot) -> _ChatSlot:
    slot.executor = "remote"
    slot.instance_id = "nobita"
    slot.remote_slot = "peer-chat-9"
    return slot


def _mock_state(*slots: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {s.key: s for s in slots}
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    return state


def _app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    return app


class TestTheChokepointOwnsEveryPeerOwnedControl:
    """The table is the mechanism: a control added to ``_PEER_CONTROL_SEGMENTS``
    with no admission must not inherit the LOCAL treatment by default."""

    def test_an_unregistered_control_fails_closed(self, caplog):
        from kiro_crew.dashboard.chat_persistence import restore_peer_owned

        with caplog.at_level("ERROR"):
            assert restore_peer_owned("workspace", "/peer/ws") == ""
        assert "_PEER_OWNED_RESTORE" in caplog.text

    def test_the_table_dispatches_to_the_shared_admissions(self):
        """Routing through the table must be the same answer as calling the
        admission directly — otherwise the chokepoint is a third spelling."""
        from kiro_crew.dashboard.chat_persistence import (
            _PEER_OWNED_RESTORE,
            admit_peer_effort,
            admit_peer_value,
            restore_peer_owned,
        )

        assert _PEER_OWNED_RESTORE["agent"] is admit_peer_value
        assert _PEER_OWNED_RESTORE["model"] is admit_peer_value
        assert _PEER_OWNED_RESTORE["reasoning_effort"] is admit_peer_effort
        assert restore_peer_owned("model", PEER_ACP_ID) == admit_peer_value(PEER_ACP_ID)
        assert restore_peer_owned("reasoning_effort", "turbo") == admit_peer_effort("turbo")


class TestRestoreModelHonoursTheExecutor:
    """A local slot's model is a value this machine chose, so it keeps the
    deprecation rename and the provider-keyed canonicalization. A peer's is
    admitted, not interpreted."""

    def test_a_local_slot_still_canonicalizes_for_claude_code(self):
        from kiro_crew.dashboard.chat_persistence import _restore_model

        assert _restore_model(PEER_ACP_ID, remote=False, provider="claude_code") == CANONICAL_KEY

    def test_a_local_slot_still_takes_the_deprecation_rename(self):
        from kiro_crew.dashboard.chat_persistence import _restore_model

        assert _restore_model(DEPRECATED_ID, remote=False, provider="acp") == "claude-opus-4.6"

    def test_a_remote_slot_keeps_the_peers_own_id_unrewritten(self):
        """The blocking defect: three lines below the effort line this file's
        predecessor made remote-aware, the model was still canonicalized against
        the LOCAL provider. A claude_code-backed hub rewrote a kiro peer's pin and
        the next flush persisted the rewrite."""
        from kiro_crew.dashboard.chat_persistence import _restore_model

        assert _restore_model(PEER_ACP_ID, remote=True, provider="claude_code") == PEER_ACP_ID

    def test_a_remote_slot_does_not_take_this_builds_deprecation_rename(self):
        from kiro_crew.dashboard.chat_persistence import _restore_model

        assert _restore_model(DEPRECATED_ID, remote=True, provider="acp") == DEPRECATED_ID

    def test_a_remote_slot_keeps_an_id_no_local_registry_knows(self):
        from kiro_crew.dashboard.chat_persistence import _restore_model

        assert (
            _restore_model("peer-only-model-9", remote=True, provider="claude_code")
            == "peer-only-model-9"
        )

    @pytest.mark.parametrize(
        "malformed",
        ["id\nwith-newline", "id\twith-tab", "id\rwith-cr", "", "   "],
    )
    def test_a_remote_slot_drops_a_value_that_cannot_be_an_id(self, malformed: str):
        """``\\n``, ``\\r`` and ``\\t`` are the only control characters the
        sanitizer preserves, so they are the ones the admission has to refuse."""
        from kiro_crew.dashboard.chat_persistence import _restore_model

        assert _restore_model(malformed, remote=True, provider="acp") == ""

    def test_a_stripped_control_character_leaves_the_id_rather_than_dropping_it(self):
        """The sanitizer removes a NUL and a zero-width space outright, so what
        reaches the slot is the clean id — dropping the whole value instead would
        blank the picker over a character the peer never meant to send."""
        from kiro_crew.dashboard.chat_persistence import _restore_model

        assert (
            _restore_model("\x00claude\u200b-opus-4.8", remote=True, provider="acp")
            == "claude-opus-4.8"
        )

    def test_a_space_bearing_value_is_kept_because_redaction_produces_one(self):
        """A model id that matched a credential pattern arrives here as
        ``[REDACTED: credential]``, which contains a space. Refusing whitespace
        would drop it and blank the picker — a visible bounded pin is the better
        failure, and the peer refuses the value if it is ever re-selected."""
        from kiro_crew.dashboard.chat_persistence import _restore_model

        assert (
            _restore_model("model-[REDACTED: credential]", remote=True, provider="acp")
            == "model-[REDACTED: credential]"
        )

    def test_a_remote_slot_clamps_an_overlong_id(self):
        from kiro_crew.dashboard.chat_persistence import _PEER_VALUE_MAX, _restore_model

        restored = _restore_model("m" * 400, remote=True, provider="acp")
        assert restored == "m" * _PEER_VALUE_MAX

    @pytest.mark.parametrize("bad", [5, None, ["opus"], {"model": "opus"}])
    def test_a_non_string_is_dropped_on_both_branches(self, bad: object):
        from kiro_crew.dashboard.chat_persistence import _restore_model

        assert _restore_model(bad, remote=True, provider="acp") == ""
        assert _restore_model(bad, remote=False, provider="acp") == ""


class TestRestoreAgentHonoursTheExecutor:
    """The fourth forwardable control, and the one whose restore path applied no
    local vocabulary at all — but it was still assigned raw, so the adopt path's
    bound-and-scrubbed value and the value read back off disk were admitted by
    different rules. The guard test is what surfaced it."""

    def test_a_local_slot_is_assigned_the_recorded_agent(self):
        from kiro_crew.dashboard.chat_persistence import _restore_agent

        assert _restore_agent("writer", remote=False) == "writer"

    def test_a_remote_slot_keeps_the_agent_the_peer_resolved(self):
        """The adopt path deliberately skips LOCAL agent resolution ("the peer
        resolves its own"), so this value is the peer's and is admitted as-is."""
        from kiro_crew.dashboard.chat_persistence import _restore_agent

        assert _restore_agent("peer-only-agent", remote=True) == "peer-only-agent"

    def test_a_remote_slot_drops_an_agent_that_cannot_be_a_name(self):
        from kiro_crew.dashboard.chat_persistence import _restore_agent

        assert _restore_agent("writer\nrm -rf /", remote=True) == ""

    @pytest.mark.parametrize("bad", [5, None, ["writer"]])
    def test_a_non_string_agent_reads_as_absent_on_both_branches(self, bad: object):
        """Deliberate change: the old local path assigned the non-string through,
        which every ``str`` consumer of ``slot.agent`` would then trip on."""
        from kiro_crew.dashboard.chat_persistence import _restore_agent

        assert _restore_agent(bad, remote=True) == ""
        assert _restore_agent(bad, remote=False) == ""


class TestAdoptAndRestoreAdmitTheSameValues:
    """The round-4 defect class, made unreachable rather than fixed once more: a
    value the adopt path accepts and the restore path then destroys blanks the
    picker one restart later, which is the corruption the inherit exists to
    prevent. Both sides call ONE function, so the property is structural — this
    pins it against drift and against a future second spelling."""

    #: Adversarial inputs, including the shapes that make the two passes differ:
    #: a hidden character (redaction-defeating), an overlong id, a credential-
    #: shaped value (redacted to a tag containing a space, so dropped).
    CANDIDATES = [
        PEER_ACP_ID,
        CANONICAL_KEY,
        DEPRECATED_ID,
        "peer-only-model-9",
        "global.anthropic.claude-opus-4-8[1m]",
        "  claude-opus-4.8  ",
        "claude\u200b-opus-4.8",
        "m" * 400,
        "sk-ant-api03-" + "A" * 80,
        "id\nwith-newline",
    ]

    @pytest.mark.parametrize("raw", CANDIDATES)
    def test_the_restore_path_admits_whatever_adopt_stored(self, raw: str):
        from kiro_crew.dashboard.chat_persistence import restore_peer_owned
        from kiro_crew.dashboard.remote_adopt import peer_row_metadata

        stored = peer_row_metadata({"model": raw}).get("model", "")
        # An admitted value must survive the read back unchanged; a dropped one
        # must stay dropped (nothing is invented on the way in).
        assert restore_peer_owned("model", stored) == stored

    @pytest.mark.parametrize("raw", CANDIDATES)
    def test_the_same_holds_for_the_agent(self, raw: str):
        from kiro_crew.dashboard.chat_persistence import restore_peer_owned
        from kiro_crew.dashboard.remote_adopt import peer_row_metadata

        stored = peer_row_metadata({"agent": raw}).get("agent", "")
        assert restore_peer_owned("agent", stored) == stored

    @pytest.mark.parametrize("level", ["low", "turbo", "ultra-high", "level_2", "high\n", "LOW"])
    def test_the_same_holds_for_the_effort_level(self, level: str):
        from kiro_crew.dashboard.chat_persistence import restore_peer_owned
        from kiro_crew.dashboard.remote_adopt import peer_row_metadata

        stored = peer_row_metadata({"reasoning_effort": level}).get("reasoning_effort", "")
        assert restore_peer_owned("reasoning_effort", stored) == stored


@pytest.mark.asyncio
class TestTheModelEndpointLetsARemoteSlotReselectAPeerModel:
    """A model inherited from the peer is rendered in the header, so refusing it
    on re-selection makes the header lie: after any other pick, the model the
    session actually runs could never be chosen again."""

    async def test_a_peer_model_the_local_provider_rejects_is_forwarded(self):
        """``_model_rejected_reason`` 400s a canonical key under the default
        ``acp`` provider. The peer's roster is the only authority over the peer's
        ids, so the remote branch has to come first."""
        slot = _remote(_ChatSlot("test"))
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_handlers._apply_remote_pick",
            new=AsyncMock(return_value=web.json_response({"ok": True})),
        ) as pick:
            async with TestClient(TestServer(_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/model", json={"model": CANONICAL_KEY}
                )
                assert resp.status == 200
        assert pick.await_count == 1
        assert pick.await_args.args[4] == {"model": CANONICAL_KEY}

    async def test_the_peers_spelling_is_forwarded_without_the_local_rename(self):
        """``_normalize_model`` is this build's registry generation talking. The
        peer named the model it runs; forwarding a renamed spelling asks it for
        something it may not have."""
        slot = _remote(_ChatSlot("test"))
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_handlers._apply_remote_pick",
            new=AsyncMock(return_value=web.json_response({"ok": True})),
        ) as pick:
            async with TestClient(TestServer(_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/model", json={"model": DEPRECATED_ID}
                )
                assert resp.status == 200
        assert pick.await_args.args[4] == {"model": DEPRECATED_ID}

    async def test_clearing_to_the_provider_default_still_works(self):
        slot = _remote(_ChatSlot("test"))
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_handlers._apply_remote_pick",
            new=AsyncMock(return_value=web.json_response({"ok": True})),
        ) as pick:
            async with TestClient(TestServer(_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/model", json={"model": ""})
                assert resp.status == 200
        assert pick.await_args.args[4] == {"model": ""}

    @pytest.mark.parametrize(
        "bad", ["id\nwith-newline", "id\twith-tab", " claude-opus-4.8", "m" * 400, 5, None]
    )
    async def test_a_malformed_pick_is_refused_and_never_forwarded(self, bad: object):
        slot = _remote(_ChatSlot("test"))
        state = _mock_state(slot)
        with patch(
            "kiro_crew.dashboard.chat_handlers._apply_remote_pick",
            new=AsyncMock(return_value=web.json_response({"ok": True})),
        ) as pick:
            async with TestClient(TestServer(_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/model", json={"model": bad})
                assert resp.status == 400
                assert (await resp.json())["code"] == "invalid_model_shape"
        assert pick.await_count == 0

    async def test_a_local_slot_still_refuses_a_display_only_key(self):
        """The relaxation is scoped to the remote branch: a local pick is applied
        by this process, whose provider does have standing over it."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": CANONICAL_KEY})
            assert resp.status == 400
        assert slot.model == ""

    async def test_peer_model_response_controls_the_committed_value_when_admitted(self):
        """Canonical peer ids win; absent or refused ids keep the sent value."""
        slot = _remote(_ChatSlot("test"))
        state = _mock_state(slot)
        state.conversation_log = MagicMock()
        forward = AsyncMock(
            side_effect=[
                {"ok": True, "model": "claude-opus-4.6"},
                {"ok": True},
                {"ok": True, "model": "bad\nmodel"},
            ]
        )
        with (
            patch(
                "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
                new=forward,
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.deny_non_owner_remote_operation",
                return_value=None,
            ),
        ):
            async with TestClient(TestServer(_app(state))) as client:
                canonical = await client.post(
                    "/api/chat/slots/test/model", json={"model": DEPRECATED_ID}
                )
                assert canonical.status == 200
                assert await canonical.json() == {
                    "ok": True,
                    "model": "claude-opus-4.6",
                    "remote": True,
                }
                assert slot.model == "claude-opus-4.6"
                assert state.conversation_log.update_metadata.call_args.args[1] == {
                    "model": "claude-opus-4.6"
                }

                state.conversation_log.update_metadata.reset_mock()
                omitted = await client.post(
                    "/api/chat/slots/test/model", json={"model": DEPRECATED_ID}
                )
                assert omitted.status == 200
                assert (await omitted.json())["model"] == DEPRECATED_ID
                assert slot.model == DEPRECATED_ID
                assert state.conversation_log.update_metadata.call_args.args[1] == {
                    "model": DEPRECATED_ID
                }

                state.conversation_log.update_metadata.reset_mock()
                refused = await client.post(
                    "/api/chat/slots/test/model", json={"model": DEPRECATED_ID}
                )
                assert refused.status == 200
                assert (await refused.json())["model"] == DEPRECATED_ID
                assert slot.model == DEPRECATED_ID
                assert state.conversation_log.update_metadata.call_args.args[1] == {
                    "model": DEPRECATED_ID
                }

        assert [call.args[3] for call in forward.await_args_list] == [
            {"model": DEPRECATED_ID},
            {"model": DEPRECATED_ID},
            {"model": DEPRECATED_ID},
        ]

    async def test_a_credential_shaped_peer_model_is_redacted_before_it_is_mirrored(self):
        """The mirrored value is rendered in the header AND persisted, so it meets
        the same redaction sink adopt applies to the very same field — an
        unscrubbed credential here would outlive the session. The secret is
        split with a zero-width space so the test also pins that sanitizing runs
        BEFORE the redactor, exactly as adopt orders the two."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        split_secret = f"{secret[:4]}\u200b{secret[4:]}"
        slot = _remote(_ChatSlot("test"))
        state = _mock_state(slot)
        state.conversation_log = MagicMock()
        forward = AsyncMock(return_value={"ok": True, "model": f"model-{split_secret}"})
        with (
            patch(
                "kiro_crew.dashboard.chat_handlers.forward_peer_selection",
                new=forward,
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.deny_non_owner_remote_operation",
                return_value=None,
            ),
        ):
            async with TestClient(TestServer(_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/model", json={"model": DEPRECATED_ID}
                )
                assert resp.status == 200
                mirrored = (await resp.json())["model"]

        persisted = state.conversation_log.update_metadata.call_args.args[1]["model"]
        for surface in (slot.model, mirrored, persisted):
            assert surface.startswith("model-"), "the peer's value must have been mirrored at all"
            assert secret not in surface
            assert split_secret not in surface
            assert "\u200b" not in surface
        assert slot.model == mirrored == persisted
