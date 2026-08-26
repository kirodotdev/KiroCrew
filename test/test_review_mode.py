"""Tests for review-mode interactions, blocks, client, and handler draft storage."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack.blocks import (
    review_draft_blocks,
    review_edit_modal,
    review_revise_modal,
)
from kiro_crew.slack.handler import (
    _REVIEW_DRAFT_MAX,
    _REVIEW_DRAFT_TTL,
    _review_drafts,
    _review_drafts_get,
    _review_drafts_pop,
    _review_drafts_set,
)
from kiro_crew.slack.interactions import (
    _can_act_on_review_draft,
    _delete_review_placeholder,
    _handle_review_approve,
    _handle_review_cancel,
    _handle_review_edit,
    _handle_review_edit_submit,
    _handle_review_revise,
    _handle_review_revise_submit,
    _parse_draft_key,
)


class TestReviewDraftBlocks:
    def test_basic_structure(self) -> None:
        blocks = review_draft_blocks("hello", "C1|ts1|abc")
        assert len(blocks) == 5
        assert blocks[0]["type"] == "section"
        assert blocks[4]["type"] == "actions"
        elements = blocks[4]["elements"]
        assert len(elements) == 4
        assert elements[0]["action_id"] == "mc_review_approve"
        assert elements[0]["value"] == "C1|ts1|abc"

    def test_truncates_long_text(self) -> None:
        long_text = "x" * 4000
        blocks = review_draft_blocks(long_text, "C1|ts1|abc")
        display = blocks[2]["text"]["text"]
        assert len(display) <= 3000
        assert display.endswith("…")

    def test_short_text_not_truncated(self) -> None:
        blocks = review_draft_blocks("short", "C1|ts1|abc")
        assert blocks[2]["text"]["text"] == "short"


class TestReviewEditModal:
    def test_structure(self) -> None:
        modal = review_edit_modal("draft text", "C1|ts1|abc")
        assert modal["callback_id"] == "mc_review_edit_submit"
        assert modal["private_metadata"] == "C1|ts1|abc"
        inp = modal["blocks"][0]["element"]
        assert inp["initial_value"] == "draft text"

    def test_truncates_initial_value(self) -> None:
        modal = review_edit_modal("x" * 4000, "key")
        assert len(modal["blocks"][0]["element"]["initial_value"]) <= 3000


class TestReviewReviseModal:
    def test_structure(self) -> None:
        modal = review_revise_modal("C1|ts1|abc")
        assert modal["callback_id"] == "mc_review_revise_submit"
        assert modal["private_metadata"] == "C1|ts1|abc"


# ---------------------------------------------------------------------------
# Draft storage helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_drafts():
    """Clear draft storage between tests."""
    _review_drafts.clear()
    yield
    _review_drafts.clear()


@pytest.fixture(autouse=True)
def _mock_aiohttp():
    """Mock aiohttp.ClientSession to prevent real HTTP calls to Slack response_url.

    Handlers post to response_url (e.g. delete_original) via aiohttp; without
    this mock the test would attempt a real outbound TCP connection that hangs
    in CI/test environments with no network access.
    """
    mock_session = AsyncMock()
    # Support: async with aiohttp.ClientSession() as sess: await sess.post(...)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        yield mock_session


REQUESTER = "UREQ"


class TestReviewDraftStorage:
    def test_set_and_get_returns_draft_and_requester(self) -> None:
        _review_drafts_set("k1", "hello", REQUESTER)
        assert _review_drafts_get("k1") == ("hello", REQUESTER)

    def test_pop_returns_draft_and_requester_then_removes(self) -> None:
        _review_drafts_set("k1", "hello", REQUESTER)
        assert _review_drafts_pop("k1") == ("hello", REQUESTER)
        assert _review_drafts_get("k1") == ("", "")

    def test_get_missing_returns_empty_tuple(self) -> None:
        assert _review_drafts_get("missing") == ("", "")

    def test_pop_missing_returns_empty_tuple(self) -> None:
        assert _review_drafts_pop("missing") == ("", "")

    def test_expired_entry_returns_empty(self) -> None:
        _review_drafts["k1"] = ("old", REQUESTER, time.monotonic() - _REVIEW_DRAFT_TTL - 1)
        assert _review_drafts_get("k1") == ("", "")

    def test_expired_entry_popped_returns_empty(self) -> None:
        _review_drafts["k1"] = ("old", REQUESTER, time.monotonic() - _REVIEW_DRAFT_TTL - 1)
        assert _review_drafts_pop("k1") == ("", "")

    def test_evicts_oldest_at_capacity(self) -> None:
        for i in range(_REVIEW_DRAFT_MAX):
            _review_drafts_set(f"k{i}", f"v{i}", REQUESTER)
        assert len(_review_drafts) == _REVIEW_DRAFT_MAX
        _review_drafts_set("overflow", "new", REQUESTER)
        assert len(_review_drafts) == _REVIEW_DRAFT_MAX
        assert _review_drafts_get("overflow") == ("new", REQUESTER)


# ---------------------------------------------------------------------------
# Client post_ephemeral
# ---------------------------------------------------------------------------
class TestPostEphemeral:
    @pytest.mark.asyncio
    async def test_post_ephemeral_with_blocks_and_thread(self) -> None:
        from kiro_crew.slack.client import RealSlackClient

        mock_web = MagicMock()
        mock_web.chat_postEphemeral = AsyncMock()
        client = RealSlackClient.__new__(RealSlackClient)
        client._web = mock_web
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        await client.post_ephemeral("C1", "U1", "fallback", blocks=blocks, thread_ts="ts1")
        mock_web.chat_postEphemeral.assert_awaited_once_with(
            channel="C1",
            user="U1",
            text="fallback",
            blocks=blocks,
            thread_ts="ts1",
        )

    @pytest.mark.asyncio
    async def test_post_ephemeral_without_optional_params(self) -> None:
        from kiro_crew.slack.client import RealSlackClient

        mock_web = MagicMock()
        mock_web.chat_postEphemeral = AsyncMock()
        client = RealSlackClient.__new__(RealSlackClient)
        client._web = mock_web
        await client.post_ephemeral("C1", "U1", "text")
        mock_web.chat_postEphemeral.assert_awaited_once_with(
            channel="C1",
            user="U1",
            text="text",
        )


# ---------------------------------------------------------------------------
# Org-wide install team_id injection
# ---------------------------------------------------------------------------
class TestTeamIdInjection:
    """Cover RealSlackClient channel→team_id cache + outbound auto-inject."""

    def _client(self):
        """Build a real SlackClient instance with __init__ run so the
        cache exists; the AsyncWebClient is replaced by a MagicMock."""
        from kiro_crew.slack.client import RealSlackClient

        client = RealSlackClient.__new__(RealSlackClient)
        client._web = MagicMock()
        client._channel_team = {}
        return client

    # ── record_channel_team ───────────────────────────────────────────

    def test_record_caches_channel_team_pair(self) -> None:
        c = self._client()
        c.record_channel_team("C1", "TWORK")
        assert c._channel_team == {"C1": "TWORK"}

    def test_record_overwrites_existing_mapping(self) -> None:
        c = self._client()
        c.record_channel_team("C1", "TOLD")
        c.record_channel_team("C1", "TNEW")
        assert c._channel_team["C1"] == "TNEW"

    def test_record_skips_empty_channel_or_team(self) -> None:
        c = self._client()
        c.record_channel_team("", "TWORK")
        c.record_channel_team("C1", "")
        assert c._channel_team == {}

    def test_record_works_when_init_bypassed(self) -> None:
        """Tests using RealSlackClient.__new__() must not crash."""
        from kiro_crew.slack.client import RealSlackClient

        c = RealSlackClient.__new__(RealSlackClient)
        c._web = MagicMock()
        c.record_channel_team("C1", "TWORK")
        assert c._channel_team == {"C1": "TWORK"}

    # ── _inject_team ──────────────────────────────────────────────────

    def test_inject_adds_team_id_when_cached(self) -> None:
        c = self._client()
        c.record_channel_team("C1", "TWORK")
        kw: dict = {"channel": "C1", "text": "hi"}
        c._inject_team("C1", kw)
        assert kw["team_id"] == "TWORK"

    def test_inject_noop_when_channel_unknown(self) -> None:
        c = self._client()
        kw: dict = {"channel": "C_unseen", "text": "hi"}
        c._inject_team("C_unseen", kw)
        assert "team_id" not in kw

    def test_inject_respects_explicit_team_id(self) -> None:
        """Explicit team_id arg wins over the cache."""
        c = self._client()
        c.record_channel_team("C1", "TCACHED")
        kw: dict = {"channel": "C1", "team_id": "TOVERRIDE"}
        c._inject_team("C1", kw)
        assert kw["team_id"] == "TOVERRIDE"

    def test_inject_safe_when_init_bypassed(self) -> None:
        """No AttributeError when __init__ skipped (legacy test pattern)."""
        from kiro_crew.slack.client import RealSlackClient

        c = RealSlackClient.__new__(RealSlackClient)
        c._web = MagicMock()
        kw: dict = {"channel": "C1", "text": "hi"}
        c._inject_team("C1", kw)  # must not raise
        assert "team_id" not in kw

    # ── End-to-end injection on each outbound method ─────────────────

    @pytest.mark.asyncio
    async def test_post_message_injects_cached_team(self) -> None:
        c = self._client()
        c._web.chat_postMessage = AsyncMock(return_value={"ts": "1"})
        c.record_channel_team("C1", "TWORK")
        await c.post_message("C1", "hi")
        kwargs = c._web.chat_postMessage.await_args.kwargs
        assert kwargs["team_id"] == "TWORK"

    @pytest.mark.asyncio
    async def test_post_blocks_injects_cached_team(self) -> None:
        c = self._client()
        c._web.chat_postMessage = AsyncMock(return_value={"ts": "1"})
        c.record_channel_team("C1", "TWORK")
        await c.post_blocks("C1", [{"type": "section"}], "fallback")
        kwargs = c._web.chat_postMessage.await_args.kwargs
        assert kwargs["team_id"] == "TWORK"

    @pytest.mark.asyncio
    async def test_update_delete_inject_cached_team(self) -> None:
        c = self._client()
        c._web.chat_update = AsyncMock()
        c._web.chat_delete = AsyncMock()
        c.record_channel_team("C1", "TWORK")
        await c.update_message("C1", "ts1", text="x")
        await c.delete_message("C1", "ts1")
        assert c._web.chat_update.await_args.kwargs["team_id"] == "TWORK"
        assert c._web.chat_delete.await_args.kwargs["team_id"] == "TWORK"

    @pytest.mark.asyncio
    async def test_reactions_inject_cached_team(self) -> None:
        c = self._client()
        c._web.reactions_add = AsyncMock()
        c._web.reactions_remove = AsyncMock()
        c.record_channel_team("C1", "TWORK")
        await c.add_reaction("C1", "ts1", "thumbsup")
        await c.remove_reaction("C1", "ts1", "thumbsup")
        assert c._web.reactions_add.await_args.kwargs["team_id"] == "TWORK"
        assert c._web.reactions_remove.await_args.kwargs["team_id"] == "TWORK"

    @pytest.mark.asyncio
    async def test_post_ephemeral_injects_cached_team(self) -> None:
        c = self._client()
        c._web.chat_postEphemeral = AsyncMock()
        c.record_channel_team("C1", "TWORK")
        await c.post_ephemeral("C1", "U1", "hi")
        assert c._web.chat_postEphemeral.await_args.kwargs["team_id"] == "TWORK"

    @pytest.mark.asyncio
    async def test_is_dm_injects_cached_team_on_conversations_info(self) -> None:
        c = self._client()
        resp = MagicMock()
        resp.data = {"channel": {"is_im": True}}
        c._web.conversations_info = AsyncMock(return_value=resp)
        c.record_channel_team("D1", "TWORK")
        result = await c.is_dm("D1")
        assert result is True
        assert c._web.conversations_info.await_args.kwargs["team_id"] == "TWORK"

    # ── ensure_channel_team: the cache's only production feeder ──────

    @pytest.mark.asyncio
    async def test_ensure_resolves_the_home_workspace_not_the_author(self) -> None:
        """The value that enters the routing cache is the channel's HOME
        workspace (conversations_info's ``context_team_id``). An inbound
        event's ``team`` is the author's workspace — a participant's, on a
        shared channel — and must never reach this cache; the inbound
        handler therefore never passes it here."""
        c = self._client()
        resp = MagicMock()
        resp.data = {"channel": {"id": "C1", "context_team_id": "THOME"}}
        c._web.conversations_info = AsyncMock(return_value=resp)
        await c.ensure_channel_team("C1")
        assert c._channel_team == {"C1": "THOME"}

    @pytest.mark.asyncio
    async def test_ensure_skips_a_channel_already_resolved(self) -> None:
        c = self._client()
        c.record_channel_team("C1", "TKNOWN")
        c._web.conversations_info = AsyncMock()
        await c.ensure_channel_team("C1")
        c._web.conversations_info.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_lookup_is_retried_once_per_process_not_per_message(self) -> None:
        """A channel whose home team cannot be resolved must not cost one
        conversations_info call per inbound message for the rest of the run."""
        c = self._client()
        c._web.conversations_info = AsyncMock(side_effect=RuntimeError("missing scope"))
        await c.ensure_channel_team("C1")
        await c.ensure_channel_team("C1")
        await c.ensure_channel_team("C1")
        assert c._web.conversations_info.await_count == 1
        assert c._channel_team == {}
        # And the outbound routing answer for it is a no-op, exactly as for
        # a channel the cache has never seen.
        kw: dict = {"channel": "C1"}
        c._inject_team("C1", kw)
        assert "team_id" not in kw

    @pytest.mark.asyncio
    async def test_an_answer_without_context_team_id_counts_as_unresolved(self) -> None:
        c = self._client()
        resp = MagicMock()
        resp.data = {"channel": {"id": "C1"}}
        c._web.conversations_info = AsyncMock(return_value=resp)
        await c.ensure_channel_team("C1")
        assert c._channel_team == {}
        # Same per-process bound as an erroring lookup.
        await c.ensure_channel_team("C1")
        assert c._web.conversations_info.await_count == 1

    # ── start_stream workspace_team priority ─────────────────────────

    @pytest.mark.asyncio
    async def test_start_stream_uses_cached_team_for_workspace_routing(self) -> None:
        """Regression: ``body['team_id']`` is the channel's workspace
        (cached), NOT the recipient's. ``recipient_team_id`` is separate
        and used for cross-workspace recipient routing."""
        c = self._client()
        c._web.api_call = AsyncMock(return_value={"ts": "1"})
        # channel C1 lives in workspace A; recipient lives in workspace B
        c.record_channel_team("C1", "TCHANNEL_A")
        await c.start_stream("C1", "thread1", team_id="TRECIPIENT_B")
        body = c._web.api_call.await_args.kwargs["json"]
        # Workspace routing: channel's home (cached) takes priority.
        assert body["team_id"] == "TCHANNEL_A"
        # Recipient routing: explicit arg used as-is.
        assert body["recipient_team_id"] == "TRECIPIENT_B"

    @pytest.mark.asyncio
    async def test_start_stream_falls_back_to_recipient_when_no_cache(self) -> None:
        """When the channel is unknown to the cache, recipient team_id is
        the only signal we have for workspace routing."""
        c = self._client()
        c._web.api_call = AsyncMock(return_value={"ts": "1"})
        await c.start_stream("C_unseen", "thread1", team_id="TRECIPIENT")
        body = c._web.api_call.await_args.kwargs["json"]
        assert body["team_id"] == "TRECIPIENT"

    @pytest.mark.asyncio
    async def test_start_stream_omits_team_id_when_neither_known(self) -> None:
        """Single-workspace install: no cache, no recipient — no team_id
        in the body."""
        c = self._client()
        c._web.api_call = AsyncMock(return_value={"ts": "1"})
        await c.start_stream("C1", "thread1")
        body = c._web.api_call.await_args.kwargs["json"]
        assert "team_id" not in body
        assert "recipient_team_id" not in body

    @pytest.mark.asyncio
    async def test_start_stream_derives_recipient_team_from_cache(self) -> None:
        """Regression: an org-wide install answers startStream with
        ``missing_recipient_team_id`` when the recipient's workspace is absent,
        and the renderer transport path never threads an explicit ``team_id`` —
        so the cached channel team must also satisfy ``recipient_team_id``. For a
        channel message the recipient's workspace IS the channel's workspace."""
        c = self._client()
        c._web.api_call = AsyncMock(return_value={"ts": "1"})
        c.record_channel_team("C1", "TCHANNEL_A")
        await c.start_stream("C1", "thread1")
        body = c._web.api_call.await_args.kwargs["json"]
        assert body["team_id"] == "TCHANNEL_A"
        assert body["recipient_team_id"] == "TCHANNEL_A"

    # ── Streaming append/stop ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_append_stream_injects_cached_team(self) -> None:
        c = self._client()
        c._web.api_call = AsyncMock()
        c.record_channel_team("C1", "TWORK")
        await c.append_stream("C1", "ts1", "hello")
        body = c._web.api_call.await_args.kwargs["json"]
        assert body["team_id"] == "TWORK"

    @pytest.mark.asyncio
    async def test_stop_stream_injects_cached_team(self) -> None:
        c = self._client()
        c._web.api_call = AsyncMock()
        c.record_channel_team("C1", "TWORK")
        await c.stop_stream("C1", "ts1")
        body = c._web.api_call.await_args.kwargs["json"]
        assert body["team_id"] == "TWORK"

    @pytest.mark.asyncio
    async def test_append_task_injects_cached_team(self) -> None:
        c = self._client()
        c._web.api_call = AsyncMock()
        c.record_channel_team("C1", "TWORK")
        await c.append_task("C1", "ts1", "task-1", "Run", "running")
        body = c._web.api_call.await_args.kwargs["json"]
        assert body["team_id"] == "TWORK"


# ---------------------------------------------------------------------------
# Interaction handlers
# ---------------------------------------------------------------------------

OWNER_ID = "UOWNER"
REQUESTER_ID = "UREQ"  # the user who @mentioned the bot to produce the draft
STRANGER = "USTRANGER"  # neither owner nor requester


def _make_payload(user_id: str = OWNER_ID, **extra) -> dict:
    p = {"user": {"id": user_id}, "response_url": "https://hooks.slack.com/x"}
    p.update(extra)
    return p


def _make_action(draft_key: str = "C1|ts1|abc") -> dict:
    return {"value": draft_key}


@pytest.fixture
def mock_orch():
    """Patch _orch with a mock orchestrator."""
    orch = MagicMock()
    orch.slack = AsyncMock()
    orch.slack.post_message = AsyncMock()
    orch.slack.set_thread_status = AsyncMock()
    orch.slack.views_open = AsyncMock()
    orch.sessions = MagicMock()
    orch.ctx_builder = MagicMock()
    orch.cron_svc = MagicMock()
    orch.conv_log = MagicMock()
    orch.consolidator = MagicMock()
    orch.subagent_mgr = MagicMock()
    orch.task_runner = MagicMock()
    with patch("kiro_crew.slack.interactions._orch", orch):
        yield orch


@pytest.fixture
def owner_patch():
    """Patch is_owner to return True for OWNER_ID only."""

    def _is_owner(uid: str) -> bool:
        return uid == OWNER_ID

    with patch("kiro_crew.slack.interactions.is_owner", side_effect=_is_owner):
        yield


@pytest.fixture
def sel_mock():
    """Patch sel() to capture audit calls."""
    mock_sel = MagicMock()
    mock_log = mock_sel.log_api_access
    with patch("kiro_crew.slack.interactions.sel", return_value=mock_sel):
        yield mock_log


@pytest.fixture
def auth_err_mock():
    """Patch _post_review_auth_error so we can assert it was invoked on denials."""
    mock = AsyncMock()
    with patch("kiro_crew.slack.interactions._post_review_auth_error", mock):
        yield mock


class TestParseKey:
    def test_valid_three_part(self) -> None:
        assert _parse_draft_key("C1|ts1|abc") == ("C1", "ts1", "C1|ts1|abc")

    def test_two_part(self) -> None:
        assert _parse_draft_key("C1|ts1") == ("C1", "ts1", "C1|ts1")

    def test_single_part_returns_none(self) -> None:
        assert _parse_draft_key("C1") is None

    def test_empty_returns_none(self) -> None:
        assert _parse_draft_key("") is None


class TestCanActOnReviewDraft:
    def test_requester_allowed(self, owner_patch) -> None:
        assert _can_act_on_review_draft(REQUESTER_ID, REQUESTER_ID) is True

    def test_owner_allowed(self, owner_patch) -> None:
        assert _can_act_on_review_draft(OWNER_ID, REQUESTER_ID) is True

    def test_stranger_denied(self, owner_patch) -> None:
        assert _can_act_on_review_draft(STRANGER, REQUESTER_ID) is False

    def test_empty_caller_denied(self, owner_patch) -> None:
        assert _can_act_on_review_draft("", REQUESTER_ID) is False


def _has_sel_call(sel_mock, outcome: str) -> bool:
    """Check if sel_mock (log_api_access) was called with given outcome."""
    return any(c.kwargs.get("outcome") == outcome for c in sel_mock.call_args_list)


class TestHandleReviewApprove:
    @pytest.mark.asyncio
    async def test_owner_can_approve(self, mock_orch, owner_patch, sel_mock) -> None:
        _review_drafts_set("C1|ts1|abc", "safe text", REQUESTER_ID)
        await _handle_review_approve(_make_payload(user_id=OWNER_ID), _make_action())
        mock_orch.slack.post_message.assert_awaited_once()
        assert _has_sel_call(sel_mock, "allowed")

    @pytest.mark.asyncio
    async def test_requester_can_approve_own_draft(self, mock_orch, owner_patch, sel_mock) -> None:
        _review_drafts_set("C1|ts1|abc", "safe text", REQUESTER_ID)
        await _handle_review_approve(_make_payload(user_id=REQUESTER_ID), _make_action())
        mock_orch.slack.post_message.assert_awaited_once()
        assert _has_sel_call(sel_mock, "allowed")

    @pytest.mark.asyncio
    async def test_stranger_denied_and_notified(
        self,
        mock_orch,
        owner_patch,
        sel_mock,
        auth_err_mock,
    ) -> None:
        _review_drafts_set("C1|ts1|abc", "text", REQUESTER_ID)
        await _handle_review_approve(_make_payload(user_id=STRANGER), _make_action())
        mock_orch.slack.post_message.assert_not_awaited()
        assert _has_sel_call(sel_mock, "denied")
        auth_err_mock.assert_awaited_once()  # feedback given, not silent

    @pytest.mark.asyncio
    async def test_no_orch_returns(self) -> None:
        with patch("kiro_crew.slack.interactions._orch", None):
            await _handle_review_approve(_make_payload(), _make_action())


class TestHandleReviewCancel:
    @pytest.mark.asyncio
    async def test_requester_can_cancel_own_draft(self, mock_orch, owner_patch, sel_mock) -> None:
        _review_drafts_set("C1|ts1|abc", "text", REQUESTER_ID)
        await _handle_review_cancel(_make_payload(user_id=REQUESTER_ID), _make_action())
        assert _review_drafts_get("C1|ts1|abc") == ("", "")
        assert _has_sel_call(sel_mock, "allowed")

    @pytest.mark.asyncio
    async def test_stranger_denied_and_draft_preserved(
        self,
        mock_orch,
        owner_patch,
        sel_mock,
        auth_err_mock,
    ) -> None:
        _review_drafts_set("C1|ts1|abc", "text", REQUESTER_ID)
        await _handle_review_cancel(_make_payload(user_id=STRANGER), _make_action())
        assert _review_drafts_get("C1|ts1|abc") == ("text", REQUESTER_ID)
        assert _has_sel_call(sel_mock, "denied")
        auth_err_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_orch_returns(self) -> None:
        with patch("kiro_crew.slack.interactions._orch", None):
            await _handle_review_cancel(_make_payload(), _make_action())


class TestHandleReviewEdit:
    @pytest.mark.asyncio
    async def test_requester_opens_modal(self, mock_orch, owner_patch, sel_mock) -> None:
        _review_drafts_set("C1|ts1|abc", "draft", REQUESTER_ID)
        payload = _make_payload(user_id=REQUESTER_ID, trigger_id="T123")
        await _handle_review_edit(payload, _make_action())
        mock_orch.slack.views_open.assert_awaited_once()
        modal = mock_orch.slack.views_open.call_args[0][1]
        assert modal["callback_id"] == "mc_review_edit_submit"

    @pytest.mark.asyncio
    async def test_stranger_denied_and_notified(
        self,
        mock_orch,
        owner_patch,
        sel_mock,
        auth_err_mock,
    ) -> None:
        _review_drafts_set("C1|ts1|abc", "draft", REQUESTER_ID)
        payload = _make_payload(user_id=STRANGER, trigger_id="T123")
        await _handle_review_edit(payload, _make_action())
        mock_orch.slack.views_open.assert_not_awaited()
        assert _has_sel_call(sel_mock, "denied")
        auth_err_mock.assert_awaited_once()


class TestHandleReviewEditSubmit:
    @pytest.mark.asyncio
    async def test_requester_submit_posts_redacted(
        self,
        mock_orch,
        owner_patch,
        sel_mock,
    ) -> None:
        _review_drafts_set("C1|ts1|abc", "original", REQUESTER_ID)
        payload = _make_payload(
            user_id=REQUESTER_ID,
            view={
                "private_metadata": "C1|ts1|abc",
                "state": {
                    "values": {
                        "mc_review_edit_block": {"mc_review_edit_input": {"value": "edited text"}}
                    }
                },
            },
        )
        await _handle_review_edit_submit(payload)
        mock_orch.slack.post_message.assert_awaited_once()
        assert _review_drafts_get("C1|ts1|abc") == ("", "")

    @pytest.mark.asyncio
    async def test_stranger_submit_denied(self, mock_orch, owner_patch, sel_mock) -> None:
        _review_drafts_set("C1|ts1|abc", "original", REQUESTER_ID)
        payload = _make_payload(
            user_id=STRANGER,
            view={
                "private_metadata": "C1|ts1|abc",
                "state": {
                    "values": {
                        "mc_review_edit_block": {"mc_review_edit_input": {"value": "hacked text"}}
                    }
                },
            },
        )
        await _handle_review_edit_submit(payload)
        mock_orch.slack.post_message.assert_not_awaited()
        assert _has_sel_call(sel_mock, "denied")
        # Draft preserved since denial happens before pop
        assert _review_drafts_get("C1|ts1|abc") == ("original", REQUESTER_ID)

    @pytest.mark.asyncio
    async def test_channels_deny_after_modal_open_blocks_edit_submit(
        self,
        mock_orch,
        owner_patch,
        sel_mock,
        tmp_path,
        monkeypatch,
    ) -> None:
        # HIGH (GPT round-10): the edit MODAL may have opened while slack was
        # permitted, then a profile hot-reload denied it before submit. The submit
        # handler must re-check the channels gate — a denied channel must NOT
        # receive the edited agent content, even for the legitimate requester.
        import json

        from kiro_crew.platform import governance_profiles as gp

        pdir = tmp_path / "profiles"
        pdir.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
        gp.reset_store()
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
                }
            )
        )
        _review_drafts_set("C1|ts1|abc", "original", REQUESTER_ID)
        payload = _make_payload(
            user_id=REQUESTER_ID,
            view={
                "private_metadata": "C1|ts1|abc",
                "state": {
                    "values": {
                        "mc_review_edit_block": {"mc_review_edit_input": {"value": "edited text"}}
                    }
                },
            },
        )
        try:
            await _handle_review_edit_submit(payload)
            # Gate dropped it before posting — nothing posted to the denied channel.
            mock_orch.slack.post_message.assert_not_awaited()
        finally:
            gp.reset_store()


class TestHandleReviewRevise:
    @pytest.mark.asyncio
    async def test_requester_opens_modal(self, mock_orch, owner_patch, sel_mock) -> None:
        _review_drafts_set("C1|ts1|abc", "draft", REQUESTER_ID)
        payload = _make_payload(user_id=REQUESTER_ID, trigger_id="T123")
        await _handle_review_revise(payload, _make_action())
        mock_orch.slack.views_open.assert_awaited_once()
        modal = mock_orch.slack.views_open.call_args[0][1]
        assert modal["callback_id"] == "mc_review_revise_submit"

    @pytest.mark.asyncio
    async def test_stranger_denied_and_notified(
        self,
        mock_orch,
        owner_patch,
        sel_mock,
        auth_err_mock,
    ) -> None:
        _review_drafts_set("C1|ts1|abc", "draft", REQUESTER_ID)
        payload = _make_payload(user_id=STRANGER, trigger_id="T123")
        await _handle_review_revise(payload, _make_action())
        mock_orch.slack.views_open.assert_not_awaited()
        assert _has_sel_call(sel_mock, "denied")
        auth_err_mock.assert_awaited_once()


class TestHandleReviewReviseSubmit:
    @pytest.mark.asyncio
    async def test_requester_submit_spawns_handle_message(
        self,
        mock_orch,
        owner_patch,
        sel_mock,
    ) -> None:
        _review_drafts_set("C1|ts1|abc", "original draft", REQUESTER_ID)
        payload = _make_payload(
            user_id=REQUESTER_ID,
            view={
                "private_metadata": "C1|ts1|abc",
                "state": {
                    "values": {
                        "mc_review_revise_block": {
                            "mc_review_revise_input": {"value": "make it shorter"}
                        }
                    }
                },
            },
        )

        # Deterministic: capture fire-and-forget tasks and await them, avoiding
        # the time-based sleep heuristic flagged by review-bot.
        background_tasks: list[asyncio.Task] = []
        orig_create_task = asyncio.create_task

        def _track_task(coro, **kwargs):
            task = orig_create_task(coro, **kwargs)
            background_tasks.append(task)
            return task

        with (
            patch(
                "kiro_crew.slack.interactions.handle_message",
                new_callable=AsyncMock,
            ) as mock_hm,
            patch(
                "kiro_crew.slack.interactions.asyncio.create_task",
                side_effect=_track_task,
            ),
        ):
            await _handle_review_revise_submit(payload)
            await asyncio.gather(*background_tasks)
            mock_hm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stranger_submit_denied_draft_preserved(
        self,
        mock_orch,
        owner_patch,
        sel_mock,
    ) -> None:
        _review_drafts_set("C1|ts1|abc", "draft", REQUESTER_ID)
        payload = _make_payload(
            user_id=STRANGER,
            view={
                "private_metadata": "C1|ts1|abc",
                "state": {
                    "values": {
                        "mc_review_revise_block": {
                            "mc_review_revise_input": {"value": "exfiltrate"}
                        }
                    }
                },
            },
        )
        await _handle_review_revise_submit(payload)
        # Draft should NOT be popped since handler returns early
        assert _review_drafts_get("C1|ts1|abc") == ("draft", REQUESTER_ID)
        assert _has_sel_call(sel_mock, "denied")


class TestDeleteReviewPlaceholder:
    @pytest.mark.asyncio
    async def test_clears_thread_status(self, mock_orch) -> None:
        await _delete_review_placeholder("C1", "ts1")
        mock_orch.slack.set_thread_status.assert_awaited_once_with("C1", "ts1", "")

    @pytest.mark.asyncio
    async def test_no_orch_no_crash(self) -> None:
        with patch("kiro_crew.slack.interactions._orch", None):
            await _delete_review_placeholder("C1", "ts1")  # no crash
