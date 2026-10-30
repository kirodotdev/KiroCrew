"""Approval presses in Slack, driven through ``kiro_crew.slack.interactions``.

Covers the native tool-approval press (the DM-only rule for Trust, the outcome label
and a failing label edit) and the review-draft handlers: each refusal (no
orchestrator, no ``trigger_id``, a malformed draft key, a missing draft, an empty
modal field, a channels-governance deny), each best-effort cleanup that fails without
undoing the action, the auth-denied ephemeral posted through ``response_url``, and the
background turn a revise submission starts.

Every patch goes through the facade; it forwards the write to the owner module that
reads the name. ``aiohttp`` is replaced by a recorder, so no request leaves the
process.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.loader import ACTIVATION_REVIEW
from kiro_crew.slack import handler as sh
from kiro_crew.slack import interactions as ix
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import async_wait_until, await_until

LOGGER = "kiro_crew.slack.interactions"
OWNER = "UOWNER"
REQUESTER = "UREQ"
STRANGER = "USTRANGER"
DRAFT_KEY = "C1|ts1|abc"
RESPONSE_URL = "https://hooks.slack.com/actions/T1/fake"


# ---------------------------------------------------------------------------
# Doubles and fixtures
# ---------------------------------------------------------------------------


class _Http:
    """Stands in for ``aiohttp``: records each ``response_url`` post, never connects."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.sessions = 0
        self.fail = False

    def session(self) -> _Session:
        self.sessions += 1
        return _Session(self)


class _Session:
    def __init__(self, http: _Http) -> None:
        self._http = http

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, json: dict | None = None) -> SimpleNamespace:
        if self._http.fail:
            raise RuntimeError("response_url unreachable")
        self._http.posts.append((url, json or {}))
        return SimpleNamespace(status=200)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> _Http:
    recorder = _Http()
    monkeypatch.setattr(ix, "aiohttp", SimpleNamespace(ClientSession=recorder.session))
    return recorder


@pytest.fixture
def drafts() -> Any:
    """The handler's review-draft store, emptied for the test and restored after it."""
    saved = dict(sh._review_drafts)
    sh._review_drafts.clear()
    yield sh._review_drafts
    sh._review_drafts.clear()
    sh._review_drafts.update(saved)


def _make_orch() -> MagicMock:
    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock(return_value="posted-ts")
    orch.slack.update_message = AsyncMock()
    orch.slack.set_thread_status = AsyncMock()
    orch.slack.views_open = AsyncMock()
    orch.slack.is_dm = AsyncMock(return_value=True)
    orch.sessions = MagicMock()
    return orch


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The inbound channels-governance check, permitting Slack unless a test flips it."""
    permitted = AsyncMock(return_value=True)
    monkeypatch.setattr(ix, "channel_inbound_permitted", permitted)
    return permitted


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch, http: _Http, drafts: Any, gate: AsyncMock) -> MagicMock:
    """A bound orchestrator double; only ``OWNER`` is the bot owner."""
    o = _make_orch()
    monkeypatch.setattr(ix, "_orch", o)
    monkeypatch.setattr(ix, "is_owner", lambda uid: uid == OWNER)
    return o


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The SEL ``log_api_access`` recorder."""
    recorder = MagicMock()
    monkeypatch.setattr(ix, "sel", lambda: recorder)
    log: MagicMock = recorder.log_api_access
    return log


@pytest.fixture
def interaction(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    resolve = AsyncMock(return_value="approve_tool")
    monkeypatch.setattr(ix, "handle_interaction", resolve)
    return resolve


@pytest.fixture
def turns(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    run = AsyncMock()
    monkeypatch.setattr(ix, "handle_message", run)
    return run


def _outcomes(audit: MagicMock) -> list[tuple[str, str]]:
    return [(c.kwargs["operation"], c.kwargs["outcome"]) for c in audit.call_args_list]


def _payload(user: str = REQUESTER, **extra: Any) -> dict:
    p: dict[str, Any] = {"user": {"id": user}, "response_url": RESPONSE_URL}
    p.update(extra)
    return p


def _submission(user: str, block: str, field: str, text: str, meta: str = DRAFT_KEY) -> dict:
    return _payload(
        user,
        view={
            "private_metadata": meta,
            "state": {"values": {block: {field: {"value": text}}}},
        },
    )


def _edit_submission(user: str, text: str, meta: str = DRAFT_KEY) -> dict:
    return _submission(user, "mc_review_edit_block", "mc_review_edit_input", text, meta)


def _revise_submission(user: str, text: str, meta: str = DRAFT_KEY) -> dict:
    return _submission(user, "mc_review_revise_block", "mc_review_revise_input", text, meta)


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


# ---------------------------------------------------------------------------
# Native tool approval
# ---------------------------------------------------------------------------


class TestNativeToolApproval:
    @pytest.mark.asyncio
    async def test_trust_without_an_orchestrator_is_refused_before_resolving(
        self,
        monkeypatch: pytest.MonkeyPatch,
        gate: AsyncMock,
        interaction: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await ix._handle_tool_approval({"message": {}}, "trust_tool", "D1", "m1", "U1")
        interaction.assert_not_awaited()
        assert any("orchestrator not ready" in m for m in _messages(caplog))

    @pytest.mark.asyncio
    async def test_trust_outside_a_dm_is_refused_before_resolving(
        self,
        orch: MagicMock,
        interaction: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        orch.slack.is_dm = AsyncMock(return_value=False)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await ix._handle_tool_approval({"message": {}}, "trust_tool", "C1", "m1", "U1")
        orch.slack.is_dm.assert_awaited_once_with("C1")
        interaction.assert_not_awaited()
        orch.slack.update_message.assert_not_awaited()
        assert any(
            "non-DM channel C1" in m and "user=U1" in m for m in _messages(caplog)
        ), _messages(caplog)

    @pytest.mark.asyncio
    async def test_trust_in_a_dm_resolves_and_labels_the_prompt(
        self, orch: MagicMock, interaction: AsyncMock
    ) -> None:
        interaction.return_value = "trust_tool"
        payload = {"message": {"thread_ts": "th1"}}
        await ix._handle_tool_approval(payload, "trust_tool", "D1", "m1", "U1")
        interaction.assert_awaited_once_with(
            "D1",
            "m1",
            "trust_tool",
            user_id="U1",
            thread_ts="th1",
            slack=orch.slack,
            sessions=orch.sessions,
        )
        orch.slack.update_message.assert_awaited_once_with("D1", "m1", text="🤝 Trusted")

    @pytest.mark.asyncio
    async def test_approve_on_a_denied_channel_is_dropped(
        self,
        orch: MagicMock,
        gate: AsyncMock,
        interaction: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        gate.return_value = False
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await ix._handle_tool_approval({"message": {}}, "approve_tool", "C1", "m1", "U1")
        gate.assert_awaited_once_with("slack")
        interaction.assert_not_awaited()
        orch.slack.update_message.assert_not_awaited()
        assert any("denied by channels governance" in m for m in _messages(caplog))

    @pytest.mark.asyncio
    async def test_reject_on_a_denied_channel_still_resolves_and_is_labelled(
        self, orch: MagicMock, gate: AsyncMock, interaction: AsyncMock
    ) -> None:
        gate.return_value = False
        interaction.return_value = "reject_tool"
        await ix._handle_tool_approval({"message": {}}, "reject_tool", "C1", "m1", "U1")
        gate.assert_not_awaited()
        interaction.assert_awaited_once()
        orch.slack.update_message.assert_awaited_once_with("C1", "m1", text="🚫 Rejected")

    @pytest.mark.asyncio
    async def test_an_unprocessed_press_keeps_the_buttons(
        self, orch: MagicMock, interaction: AsyncMock
    ) -> None:
        interaction.return_value = None
        await ix._handle_tool_approval({"message": {}}, "approve_tool", "C1", "m1", "U9")
        interaction.assert_awaited_once()
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unlabelled_outcome_leaves_the_message_alone(
        self, orch: MagicMock, interaction: AsyncMock
    ) -> None:
        interaction.return_value = "some_other_outcome"
        await ix._handle_tool_approval({"message": {}}, "approve_tool", "C1", "m1", "U1")
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_label_edit_does_not_escape(
        self, orch: MagicMock, interaction: AsyncMock
    ) -> None:
        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("slack 500"))
        await ix._handle_tool_approval({"message": {}}, "approve_tool", "C1", "m1", "U1")
        interaction.assert_awaited_once()
        orch.slack.update_message.assert_awaited_once_with("C1", "m1", text="✅ Approved")


# ---------------------------------------------------------------------------
# Shared review helpers
# ---------------------------------------------------------------------------


class TestReviewHelpers:
    @pytest.mark.asyncio
    async def test_a_failing_status_clear_is_logged_not_raised(
        self, orch: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        orch.slack.set_thread_status = AsyncMock(side_effect=RuntimeError("slack 500"))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._delete_review_placeholder("C1", "ts1")
        orch.slack.set_thread_status.assert_awaited_once_with("C1", "ts1", "")
        assert "Failed to clear review thread status" in _messages(caplog)

    @pytest.mark.asyncio
    async def test_the_auth_error_without_a_response_url_posts_nothing(self, http: _Http) -> None:
        await ix._post_review_auth_error("")
        assert http.sessions == 0
        assert http.posts == []

    @pytest.mark.asyncio
    async def test_the_auth_error_replaces_the_draft_with_an_ephemeral(self, http: _Http) -> None:
        await ix._post_review_auth_error(RESPONSE_URL)
        assert http.posts == [
            (
                RESPONSE_URL,
                {
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "text": ix._REVIEW_AUTH_DENIED_MSG,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_a_failing_auth_error_post_is_logged_not_raised(
        self, http: _Http, caplog: pytest.LogCaptureFixture
    ) -> None:
        http.fail = True
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._post_review_auth_error(RESPONSE_URL)
        assert http.sessions == 1
        assert "Failed to post review auth-denied ephemeral" in _messages(caplog)


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


class TestReviewApprove:
    @pytest.mark.asyncio
    async def test_a_malformed_draft_key_is_ignored(
        self, orch: MagicMock, audit: MagicMock, http: _Http
    ) -> None:
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        await ix._handle_review_approve(_payload(OWNER), {"value": "no-separator"})
        orch.slack.post_message.assert_not_awaited()
        audit.assert_not_called()
        assert http.posts == []
        assert sh._review_drafts_get(DRAFT_KEY) == ("draft", REQUESTER)

    @pytest.mark.asyncio
    async def test_an_owner_approving_a_vanished_draft_posts_nothing(
        self,
        orch: MagicMock,
        audit: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await ix._handle_review_approve(_payload(OWNER), {"value": DRAFT_KEY})
        orch.slack.post_message.assert_not_awaited()
        orch.slack.set_thread_status.assert_not_awaited()
        assert ("slack.review_approve", "allowed") not in _outcomes(audit)
        assert f"Review approve: no draft found for {DRAFT_KEY}" in _messages(caplog)

    @pytest.mark.asyncio
    async def test_a_stranger_gets_the_denial_ephemeral_and_the_draft_survives(
        self, orch: MagicMock, audit: MagicMock, http: _Http
    ) -> None:
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        await ix._handle_review_approve(_payload(STRANGER), {"value": DRAFT_KEY})
        orch.slack.post_message.assert_not_awaited()
        assert _outcomes(audit) == [("slack.review_approve", "denied")]
        assert http.posts == [
            (
                RESPONSE_URL,
                {
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "text": ix._REVIEW_AUTH_DENIED_MSG,
                },
            )
        ]
        assert sh._review_drafts_get(DRAFT_KEY) == ("draft", REQUESTER)

    @pytest.mark.asyncio
    async def test_approval_deletes_the_ephemeral_draft(
        self, orch: MagicMock, audit: MagicMock, http: _Http
    ) -> None:
        sh._review_drafts_set(DRAFT_KEY, "ship it", REQUESTER)
        await ix._handle_review_approve(_payload(REQUESTER), {"value": DRAFT_KEY})
        orch.slack.post_message.assert_awaited_once_with("C1", "ship it", "ts1")
        orch.slack.set_thread_status.assert_awaited_once_with("C1", "ts1", "")
        assert http.posts == [(RESPONSE_URL, {"delete_original": True})]
        assert _outcomes(audit) == [("slack.review_approve", "allowed")]

    @pytest.mark.asyncio
    async def test_a_failing_ephemeral_delete_does_not_undo_the_approval(
        self,
        orch: MagicMock,
        audit: MagicMock,
        http: _Http,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        http.fail = True
        sh._review_drafts_set(DRAFT_KEY, "ship it", REQUESTER)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._handle_review_approve(_payload(REQUESTER), {"value": DRAFT_KEY})
        orch.slack.post_message.assert_awaited_once_with("C1", "ship it", "ts1")
        assert sh._review_drafts_get(DRAFT_KEY) == ("", "")
        assert _outcomes(audit) == [("slack.review_approve", "allowed")]
        assert "Failed to delete review ephemeral" in _messages(caplog)


# ---------------------------------------------------------------------------
# Edit button and edit submission
# ---------------------------------------------------------------------------


class TestReviewEdit:
    @pytest.mark.asyncio
    async def test_without_an_orchestrator_nothing_opens(
        self, monkeypatch: pytest.MonkeyPatch, http: _Http, audit: MagicMock
    ) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        await ix._handle_review_edit(_payload(trigger_id="T1"), {"value": DRAFT_KEY})
        audit.assert_not_called()
        assert http.posts == []

    @pytest.mark.asyncio
    async def test_a_press_without_a_trigger_id_opens_no_modal(
        self,
        orch: MagicMock,
        audit: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await ix._handle_review_edit(_payload(REQUESTER), {"value": DRAFT_KEY})
        orch.slack.views_open.assert_not_awaited()
        audit.assert_not_called()
        assert "Review edit: no trigger_id in payload" in _messages(caplog)

    @pytest.mark.asyncio
    async def test_a_malformed_draft_key_opens_no_modal(
        self, orch: MagicMock, audit: MagicMock
    ) -> None:
        await ix._handle_review_edit(_payload(OWNER, trigger_id="T1"), {"value": ""})
        orch.slack.views_open.assert_not_awaited()
        audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_owner_editing_a_vanished_draft_opens_no_modal(
        self,
        orch: MagicMock,
        audit: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await ix._handle_review_edit(_payload(OWNER, trigger_id="T1"), {"value": DRAFT_KEY})
        orch.slack.views_open.assert_not_awaited()
        audit.assert_not_called()
        assert f"Review edit: no draft found for {DRAFT_KEY}" in _messages(caplog)

    @pytest.mark.asyncio
    async def test_the_modal_opens_even_when_the_ephemeral_delete_fails(
        self,
        orch: MagicMock,
        audit: MagicMock,
        http: _Http,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        http.fail = True
        sh._review_drafts_set(DRAFT_KEY, "draft body", REQUESTER)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._handle_review_edit(_payload(REQUESTER, trigger_id="T1"), {"value": DRAFT_KEY})
        orch.slack.views_open.assert_awaited_once()
        trigger, modal = orch.slack.views_open.await_args.args
        assert trigger == "T1"
        assert modal["callback_id"] == "mc_review_edit_submit"
        assert modal["private_metadata"] == DRAFT_KEY
        assert modal["blocks"][0]["element"]["initial_value"] == "draft body"
        assert _outcomes(audit) == [("slack.review_edit", "allowed")]
        assert "Failed to delete review ephemeral" in _messages(caplog)
        # Opening the editor keeps the draft: the submission pops it.
        assert sh._review_drafts_get(DRAFT_KEY) == ("draft body", REQUESTER)


class TestReviewEditSubmit:
    @pytest.mark.asyncio
    async def test_without_an_orchestrator_the_gate_is_not_consulted(
        self, monkeypatch: pytest.MonkeyPatch, gate: AsyncMock, drafts: Any
    ) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        await ix._handle_review_edit_submit(_edit_submission(REQUESTER, "edited"))
        gate.assert_not_awaited()
        assert sh._review_drafts_get(DRAFT_KEY) == ("draft", REQUESTER)

    @pytest.mark.asyncio
    async def test_a_malformed_private_metadata_posts_nothing(
        self, orch: MagicMock, audit: MagicMock
    ) -> None:
        await ix._handle_review_edit_submit(_edit_submission(OWNER, "edited", meta="bare"))
        orch.slack.post_message.assert_not_awaited()
        audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_empty_edit_posts_nothing_and_keeps_the_draft(
        self, orch: MagicMock, audit: MagicMock
    ) -> None:
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        await ix._handle_review_edit_submit(_edit_submission(REQUESTER, ""))
        orch.slack.post_message.assert_not_awaited()
        orch.slack.set_thread_status.assert_not_awaited()
        audit.assert_not_called()
        assert sh._review_drafts_get(DRAFT_KEY) == ("draft", REQUESTER)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class TestReviewCancel:
    @pytest.mark.asyncio
    async def test_a_malformed_draft_key_keeps_every_draft(
        self, orch: MagicMock, audit: MagicMock, http: _Http
    ) -> None:
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        await ix._handle_review_cancel(_payload(OWNER), {"value": "C1"})
        assert sh._review_drafts_get(DRAFT_KEY) == ("draft", REQUESTER)
        orch.slack.set_thread_status.assert_not_awaited()
        audit.assert_not_called()
        assert http.posts == []

    @pytest.mark.asyncio
    async def test_a_failing_ephemeral_delete_still_discards_the_draft(
        self,
        orch: MagicMock,
        audit: MagicMock,
        http: _Http,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        http.fail = True
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._handle_review_cancel(_payload(REQUESTER), {"value": DRAFT_KEY})
        assert sh._review_drafts_get(DRAFT_KEY) == ("", "")
        orch.slack.set_thread_status.assert_awaited_once_with("C1", "ts1", "")
        orch.slack.post_message.assert_not_awaited()
        assert _outcomes(audit) == [("slack.review_cancel", "allowed")]
        assert "Failed to delete review ephemeral" in _messages(caplog)


# ---------------------------------------------------------------------------
# Revise button and revise submission
# ---------------------------------------------------------------------------


class TestReviewRevise:
    @pytest.mark.asyncio
    async def test_without_an_orchestrator_nothing_opens(
        self, monkeypatch: pytest.MonkeyPatch, http: _Http, audit: MagicMock
    ) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        await ix._handle_review_revise(_payload(trigger_id="T1"), {"value": DRAFT_KEY})
        audit.assert_not_called()
        assert http.posts == []

    @pytest.mark.asyncio
    async def test_a_press_without_a_trigger_id_opens_no_modal(
        self,
        orch: MagicMock,
        audit: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await ix._handle_review_revise(_payload(REQUESTER), {"value": DRAFT_KEY})
        orch.slack.views_open.assert_not_awaited()
        audit.assert_not_called()
        assert "Review revise: no trigger_id in payload" in _messages(caplog)

    @pytest.mark.asyncio
    async def test_a_malformed_draft_key_opens_no_modal(
        self, orch: MagicMock, audit: MagicMock
    ) -> None:
        await ix._handle_review_revise(_payload(OWNER, trigger_id="T1"), {"value": "x"})
        orch.slack.views_open.assert_not_awaited()
        audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_modal_opens_even_when_the_ephemeral_delete_fails(
        self,
        orch: MagicMock,
        audit: MagicMock,
        http: _Http,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        http.fail = True
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._handle_review_revise(
                _payload(REQUESTER, trigger_id="T1"), {"value": DRAFT_KEY}
            )
        orch.slack.views_open.assert_awaited_once()
        trigger, modal = orch.slack.views_open.await_args.args
        assert trigger == "T1"
        assert modal["callback_id"] == "mc_review_revise_submit"
        assert modal["private_metadata"] == DRAFT_KEY
        assert _outcomes(audit) == [("slack.review_revise", "allowed")]
        assert "Failed to delete review ephemeral" in _messages(caplog)


async def _submit_and_join(payload: dict) -> list[asyncio.Task]:
    """Run the revise submission, then wait out the background turn it starts."""
    before = asyncio.all_tasks()
    await ix._handle_review_revise_submit(payload)
    spawned = [
        t
        for t in asyncio.all_tasks() - before
        if getattr(t.get_coro(), "__qualname__", "").endswith("_do_revise")
    ]
    if spawned:
        await await_until(asyncio.gather(*spawned), what="the background revise turn")
    return spawned


class TestReviewReviseSubmit:
    @pytest.mark.asyncio
    async def test_without_an_orchestrator_the_gate_is_not_consulted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        gate: AsyncMock,
        drafts: Any,
        turns: AsyncMock,
    ) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        assert await _submit_and_join(_revise_submission(REQUESTER, "shorter")) == []
        gate.assert_not_awaited()
        turns.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_denied_channel_drops_the_submission(
        self,
        orch: MagicMock,
        gate: AsyncMock,
        audit: MagicMock,
        turns: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        gate.return_value = False
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert await _submit_and_join(_revise_submission(REQUESTER, "shorter")) == []
        gate.assert_awaited_once_with("slack")
        turns.assert_not_awaited()
        audit.assert_not_called()
        assert sh._review_drafts_get(DRAFT_KEY) == ("draft", REQUESTER)
        assert any("review-revise submit dropped" in m for m in _messages(caplog))

    @pytest.mark.asyncio
    async def test_a_malformed_private_metadata_starts_no_turn(
        self, orch: MagicMock, audit: MagicMock, turns: AsyncMock
    ) -> None:
        assert await _submit_and_join(_revise_submission(OWNER, "shorter", meta="")) == []
        turns.assert_not_awaited()
        audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_feedback_starts_no_turn_and_keeps_the_draft(
        self, orch: MagicMock, audit: MagicMock, turns: AsyncMock
    ) -> None:
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        assert await _submit_and_join(_revise_submission(REQUESTER, "")) == []
        turns.assert_not_awaited()
        audit.assert_not_called()
        assert sh._review_drafts_get(DRAFT_KEY) == ("draft", REQUESTER)

    @pytest.mark.asyncio
    async def test_an_owner_revising_a_vanished_draft_starts_no_turn(
        self,
        orch: MagicMock,
        audit: MagicMock,
        turns: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert await _submit_and_join(_revise_submission(OWNER, "shorter")) == []
        turns.assert_not_awaited()
        audit.assert_not_called()
        assert f"Review revise: no draft found for {DRAFT_KEY}" in _messages(caplog)

    @pytest.mark.asyncio
    async def test_the_revision_turn_carries_the_draft_and_the_feedback(
        self,
        orch: MagicMock,
        audit: MagicMock,
        turns: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sh._review_drafts_set(DRAFT_KEY, "the first draft", REQUESTER)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            spawned = await _submit_and_join(_revise_submission(REQUESTER, "make it shorter"))
        assert len(spawned) == 1
        turns.assert_awaited_once()
        args, kwargs = turns.await_args.args, turns.await_args.kwargs
        assert args[0] is orch.slack
        assert args[1] is orch.sessions
        channel, prompt, thread_ts, msg_ts, caller = args[2:7]
        assert (channel, thread_ts, msg_ts, caller) == ("C1", "ts1", "ts1", REQUESTER)
        assert "---\nthe first draft\n---" in prompt
        assert "based on this feedback: make it shorter" in prompt
        assert kwargs["approval_mode"] == sh.APPROVAL_INTERACTIVE
        assert kwargs["channel_activation"] == ACTIVATION_REVIEW
        assert kwargs["start_priority"] == StartPriority.FOREGROUND
        assert kwargs["context_builder"] is orch.ctx_builder
        assert kwargs["task_runner"] is orch.task_runner
        # The draft is spent and the permission decision is on record.
        assert sh._review_drafts_get(DRAFT_KEY) == ("", "")
        assert _outcomes(audit) == [("slack.review_revise_submit", "allowed")]
        assert f"Review revision requested by {REQUESTER} in C1" in _messages(caplog)

    @pytest.mark.asyncio
    async def test_a_failing_revision_turn_is_audited_as_an_error(
        self,
        orch: MagicMock,
        audit: MagicMock,
        turns: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        turns.side_effect = RuntimeError("turn exploded")
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            spawned = await _submit_and_join(_revise_submission(REQUESTER, "again"))
        assert len(spawned) == 1
        assert spawned[0].exception() is None
        assert _outcomes(audit) == [
            ("slack.review_revise_submit", "allowed"),
            ("slack.review_revise_submit", "error"),
        ]
        assert audit.call_args_list[-1].kwargs["error"] == "handle_message failed"
        assert audit.call_args_list[-1].kwargs["resources"] == "C1"
        assert f"Review revision failed for {REQUESTER} in C1" in _messages(caplog)

    @pytest.mark.asyncio
    async def test_the_orchestrator_holds_the_revision_turn_until_it_ends(
        self, orch: MagicMock, audit: MagicMock, turns: AsyncMock
    ) -> None:
        # The event loop keeps only a weak reference to a task, so a revise turn
        # nobody holds can be collected mid-turn after the modal has closed.
        orch._handler_tasks = set()
        release = asyncio.Event()

        async def _turn(*_a: Any, **_k: Any) -> None:
            await release.wait()

        turns.side_effect = _turn
        sh._review_drafts_set(DRAFT_KEY, "draft", REQUESTER)
        before = asyncio.all_tasks()
        await ix._handle_review_revise_submit(_revise_submission(REQUESTER, "shorter"))
        (task,) = [
            t
            for t in asyncio.all_tasks() - before
            if getattr(t.get_coro(), "__qualname__", "").endswith("_do_revise")
        ]
        assert orch._handler_tasks == {task}
        release.set()
        await await_until(task, what="the background revise turn")
        await async_wait_until(lambda: not orch._handler_tasks)
        turns.assert_awaited_once()
