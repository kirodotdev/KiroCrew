"""Coverage for the untested helpers and small routes in ``chat_handlers``.

Scope is deliberately the parts of the module no existing test file reaches:
the context-meter number gate, the live model-switch path (``_wire_model_id`` /
``_try_live_model_switch`` / ``_reapply_effort_after_live_switch``), the
follow-up redaction + owner gate, the recent-projects store, and the small
slot routes (color / context-inject / queue edit-cancel-reorder / workspace).

Everything here is in-process: no subprocess, no git, no sandbox, no network.
The two functions that would touch the real filesystem outside ``tmp_path``
(``_recent_projects_path`` consumers) have ``config_dir`` redirected, and the
one gate that is anchored on the *real* ``$HOME`` (``is_sensitive_path``) is
injected rather than satisfied with a real credential directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.acp.client import AcpModelUnavailable
from kiro_crew.dashboard import chat_handlers as ch
from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_values
from kiro_crew.dashboard.state import _MAX_PENDING_CONTEXT, DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider

MOD = "kiro_crew.dashboard.chat_handlers"


# ── shared fixtures / builders ───────────────────────────────────────────────


@pytest.fixture
def _sel():
    """Neutralize the security event log (it otherwise opens the real store)."""
    fake = MagicMock()
    with patch(f"{MOD}.sel", return_value=fake):
        yield fake


def _state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot is not None:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.broadcast_ws = MagicMock()
    state.broadcast_context_usage = MagicMock()
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    return state


def _app(state: DashboardState, method: str, path: str, handler) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_route(method, path, handler)
    return app


def _acp(**attrs):
    """An AcpProvider double that still satisfies ``isinstance``."""
    provider = MagicMock(spec=AcpProvider)
    provider.is_claude_backend = False
    provider.has_active_turn = MagicMock(return_value=False)
    provider.available_models = MagicMock(return_value=[])
    provider.supports_effort = MagicMock(return_value=False)
    provider.change_effort = AsyncMock(return_value=True)
    provider.clear_effort = AsyncMock(return_value=True)
    provider.client = MagicMock()
    provider.client.set_model = AsyncMock(return_value=None)
    for key, value in attrs.items():
        setattr(provider, key, value)
    return provider


# ── _finite_number ───────────────────────────────────────────────────────────


class TestFiniteNumber:
    def test_int_and_float_pass_through_as_float(self):
        assert ch._finite_number(3) == 3.0
        assert isinstance(ch._finite_number(3), float)
        assert ch._finite_number(0.5) == 0.5

    def test_bool_is_rejected_despite_being_an_int(self):
        # bool would otherwise serialize as 1/0 and render as a real reading.
        assert ch._finite_number(True) is None
        assert ch._finite_number(False) is None

    def test_non_numeric_rejected(self):
        assert ch._finite_number("42") is None
        assert ch._finite_number(None) is None
        assert ch._finite_number([1]) is None

    def test_nan_and_inf_rejected(self):
        assert ch._finite_number(float("nan")) is None
        assert ch._finite_number(float("inf")) is None
        assert ch._finite_number(float("-inf")) is None


# ── _context_reading ─────────────────────────────────────────────────────────


class TestContextReading:
    def test_unusable_pct_yields_no_reading(self):
        assert ch._context_reading("x", 10, 200, stale=False) == {}

    def test_zero_pct_without_window_is_not_a_measurement(self):
        assert ch._context_reading(0, 0, 0, stale=False) == {}

    def test_zero_pct_with_window_is_reported(self):
        out = ch._context_reading(0, 0, 200_000, stale=False)
        assert out["context_pct"] == 0.0
        assert out["context_window_tokens"] == 200_000
        assert "context_used_tokens" not in out

    def test_pct_only_reading_omits_token_fields(self):
        out = ch._context_reading(11.5, 0, 0, stale=False)
        assert out == {"context_pct": 11.5, "context_stale": False}

    def test_full_reading_carries_both_counts(self):
        out = ch._context_reading(44, 88_000, 200_000, stale=False)
        assert out["context_used_tokens"] == 88_000
        assert out["context_window_tokens"] == 200_000
        assert out["context_stale"] is False

    def test_stale_reading_drops_used_but_keeps_window(self):
        out = ch._context_reading(44, 88_000, 200_000, stale=True)
        assert out["context_stale"] is True
        assert out["context_window_tokens"] == 200_000
        assert "context_used_tokens" not in out


# ── _context_snapshot_fields / _inner ────────────────────────────────────────


class TestContextSnapshotFields:
    @pytest.mark.asyncio
    async def test_live_provider_is_authoritative(self):
        slot = _ChatSlot("s1")
        state = _state(slot)
        provider = MagicMock()
        provider.context_usage_pct = MagicMock(return_value=33.0)
        provider.context_used_tokens = MagicMock(return_value=66_000)
        provider.context_window_tokens = MagicMock(return_value=200_000)
        state.sessions.get_provider = MagicMock(return_value=provider)

        out = await ch._context_snapshot_fields(state, slot)

        assert out["context_pct"] == 33.0
        assert out["context_used_tokens"] == 66_000
        assert out["context_stale"] is False
        # A live reading must not pay for the snapshot file.
        state.ensure_context_snapshots_loaded.assert_not_called()

    @pytest.mark.asyncio
    async def test_cold_session_falls_back_to_snapshot_marked_stale(self):
        slot = _ChatSlot("s1")
        slot.model = "opus-4.8-1m"
        state = _state(slot)
        state.context_snapshot_for = MagicMock(
            return_value={
                "model": "opus-4.8-1m",
                "pct": 12.0,
                "used_tokens": 24_000,
                "window_tokens": 200_000,
            }
        )

        out = await ch._context_snapshot_fields(state, slot)

        assert out["context_pct"] == 12.0
        assert out["context_stale"] is True
        assert "context_used_tokens" not in out
        state.ensure_context_snapshots_loaded.assert_called_once()

    @pytest.mark.asyncio
    async def test_snapshot_from_a_different_model_is_discarded(self):
        slot = _ChatSlot("s1")
        slot.model = "sonnet-4.5"
        state = _state(slot)
        state.context_snapshot_for = MagicMock(
            return_value={"model": "opus-4.8-1m", "pct": 90.0, "window_tokens": 200_000}
        )

        assert await ch._context_snapshot_fields(state, slot) == {}

    @pytest.mark.asyncio
    async def test_missing_snapshot_yields_empty(self):
        slot = _ChatSlot("s1")
        state = _state(slot)
        state.context_snapshot_for = MagicMock(return_value=None)

        assert await ch._context_snapshot_fields(state, slot) == {}

    @pytest.mark.asyncio
    async def test_failure_degrades_to_empty_instead_of_raising(self):
        slot = _ChatSlot("s1")
        state = _state(slot)
        state.sessions.get_provider = MagicMock(side_effect=RuntimeError("pool gone"))

        assert await ch._context_snapshot_fields(state, slot) == {}


# ── _has_conversation ────────────────────────────────────────────────────────


class TestHasConversation:
    def test_empty_slot_has_nothing_to_continue_from(self):
        assert ch._has_conversation(_ChatSlot("s1")) is False

    def test_compaction_notice_alone_is_scaffolding(self):
        slot = _ChatSlot("s1")
        slot.messages.append(
            {"role": "assistant", "content": "compacted", "meta": {"kind": "compaction"}}
        )
        assert ch._has_conversation(slot) is False

    def test_empty_content_does_not_count(self):
        slot = _ChatSlot("s1")
        slot.messages.append({"role": "user", "content": ""})
        assert ch._has_conversation(slot) is False

    def test_a_real_user_row_qualifies(self):
        slot = _ChatSlot("s1")
        slot.messages.append({"role": "system", "content": "boot"})
        slot.messages.append({"role": "user", "content": "hi"})
        assert ch._has_conversation(slot) is True


# ── _wire_model_id ───────────────────────────────────────────────────────────


class TestWireModelId:
    def test_claude_backend_cannot_express_default(self):
        assert ch._wire_model_id(_acp(is_claude_backend=True), "sonnet-4.5") == "sonnet-4.5"
        assert ch._wire_model_id(_acp(is_claude_backend=True), "") == ""
        assert ch._wire_model_id(_acp(is_claude_backend=True), "auto") == ""

    def test_claude_backend_translates_canonical_key(self):
        wire = ch._wire_model_id(_acp(is_claude_backend=True), "opus-4.8-1m")
        assert wire == "global.anthropic.claude-opus-4-8[1m]"

    def test_kiro_default_needs_auto_to_be_advertised(self):
        without = _acp(available_models=MagicMock(return_value=[{"modelId": "claude-opus-4.8"}]))
        assert ch._wire_model_id(without, "") == ""
        with_auto = _acp(
            available_models=MagicMock(
                return_value=[{"modelId": "auto"}, {"modelId": "claude-opus-4.8"}]
            )
        )
        assert ch._wire_model_id(with_auto, "auto") == "auto"

    def test_kiro_translates_canonical_key_to_dotted_id(self):
        assert ch._wire_model_id(_acp(), "opus-4.8-1m") == "claude-opus-4.8"


# ── _reapply_effort_after_live_switch ────────────────────────────────────────


class TestReapplyEffortAfterLiveSwitch:
    @pytest.mark.asyncio
    async def test_model_without_effort_selector_is_a_persisted_noop(self):
        slot = _ChatSlot("s1")
        slot.reasoning_effort = "high"
        provider = _acp(supports_effort=MagicMock(return_value=False))

        assert await ch._reapply_effort_after_live_switch("s1", slot, provider) is True
        provider.change_effort.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slot_override_is_pushed_and_its_result_returned(self):
        slot = _ChatSlot("s1")
        slot.reasoning_effort = "high"
        provider = _acp(
            supports_effort=MagicMock(return_value=True),
            change_effort=AsyncMock(return_value=False),
        )

        assert await ch._reapply_effort_after_live_switch("s1", slot, provider) is False
        provider.change_effort.assert_awaited_once_with("high")

    @pytest.mark.asyncio
    async def test_no_override_reresolves_default_and_ignores_its_result(self):
        slot = _ChatSlot("s1")
        slot.reasoning_effort = ""
        provider = _acp(
            supports_effort=MagicMock(return_value=True),
            clear_effort=AsyncMock(return_value=False),
        )

        # False from clear_effort is benign: no default existed to push.
        assert await ch._reapply_effort_after_live_switch("s1", slot, provider) is True
        provider.clear_effort.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_provider_error_asks_for_a_reset(self):
        slot = _ChatSlot("s1")
        slot.reasoning_effort = "max"
        provider = _acp(
            supports_effort=MagicMock(return_value=True),
            change_effort=AsyncMock(side_effect=RuntimeError("stdout busy")),
        )

        assert await ch._reapply_effort_after_live_switch("s1", slot, provider) is False


# ── _try_live_model_switch ───────────────────────────────────────────────────


class TestTryLiveModelSwitch:
    @pytest.mark.asyncio
    async def test_non_acp_provider_falls_back_to_reset(self):
        slot = _ChatSlot("s1")
        assert await ch._try_live_model_switch("s1", slot, None, "opus-4.8-1m") is False
        other = MagicMock(spec=LLMProvider)
        assert await ch._try_live_model_switch("s1", slot, other, "opus-4.8-1m") is False

    @pytest.mark.asyncio
    async def test_active_turn_falls_back_to_reset(self):
        provider = _acp(has_active_turn=MagicMock(return_value=True))
        assert (
            await ch._try_live_model_switch("s1", _ChatSlot("s1"), provider, "opus-4.8-1m") is False
        )
        provider.client.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unrepresentable_target_falls_back_to_reset(self):
        # kiro backend that never advertised "auto" cannot express the default.
        provider = _acp(available_models=MagicMock(return_value=[{"modelId": "claude-opus-4.8"}]))
        assert await ch._try_live_model_switch("s1", _ChatSlot("s1"), provider, "") is False
        provider.client.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_model_unavailable_propagates_instead_of_resetting(self):
        provider = _acp()
        provider.client.set_model = AsyncMock(side_effect=AcpModelUnavailable("nope"))
        with pytest.raises(AcpModelUnavailable):
            await ch._try_live_model_switch("s1", _ChatSlot("s1"), provider, "opus-4.8-1m")

    @pytest.mark.asyncio
    async def test_generic_set_model_failure_falls_back_to_reset(self):
        provider = _acp()
        provider.client.set_model = AsyncMock(side_effect=RuntimeError("broken pipe"))
        assert (
            await ch._try_live_model_switch("s1", _ChatSlot("s1"), provider, "opus-4.8-1m") is False
        )

    @pytest.mark.asyncio
    async def test_effort_reapply_failure_falls_back_to_reset(self):
        provider = _acp(
            supports_effort=MagicMock(return_value=True),
            change_effort=AsyncMock(side_effect=RuntimeError("x")),
        )
        slot = _ChatSlot("s1")
        slot.reasoning_effort = "high"

        assert await ch._try_live_model_switch("s1", slot, provider, "opus-4.8-1m") is False
        provider.client.set_model.assert_awaited_once_with("claude-opus-4.8")

    @pytest.mark.asyncio
    async def test_success_sends_the_wire_id(self):
        provider = _acp()
        assert await ch._try_live_model_switch("s1", _ChatSlot("s1"), provider, "sonnet-4.5") is True
        provider.client.set_model.assert_awaited_once_with("claude-sonnet-4.5")


# ── _broadcast_context_reset ─────────────────────────────────────────────────


class TestBroadcastContextReset:
    def test_no_provider_sends_a_zeroed_reset(self):
        state = _state()
        ch._broadcast_context_reset(state, "s1", None)
        state.broadcast_context_usage.assert_called_once()
        _, payload = state.broadcast_context_usage.call_args.args
        assert payload == {"slot": "s1", "pct": 0.0, "reset": True}

    def test_live_provider_payload_carries_rebased_stats(self):
        state = _state()
        provider = MagicMock()
        provider.context_usage_pct = MagicMock(return_value=7.25)
        provider.context_window_tokens = MagicMock(return_value=200_000)
        provider.context_used_tokens = MagicMock(return_value=14_000)

        ch._broadcast_context_reset(state, "s1", provider)

        _, payload = state.broadcast_context_usage.call_args.args
        assert payload["slot"] == "s1"
        assert payload["pct"] == 7.2
        assert payload["reset"] is True

    def test_broadcast_failure_is_swallowed(self):
        state = _state()
        state.broadcast_context_usage = MagicMock(side_effect=RuntimeError("no clients"))
        # Must not raise: a failed broadcast cannot fail the model switch.
        ch._broadcast_context_reset(state, "s1", None)


# ── _redact_followup_item ────────────────────────────────────────────────────


class TestRedactFollowupItem:
    def test_missing_fields_become_empty_strings(self):
        assert ch._redact_followup_item({}) == {"title": "", "description": "", "prompt": ""}

    def test_text_fields_are_redacted(self):
        secret = "ghp_" + "A" * 36
        out = ch._redact_followup_item({"title": f"use {secret}", "prompt": "ok"})
        assert secret not in out["title"]
        assert "REDACTED" in out["title"]
        assert out["prompt"] == "ok"

    def test_clean_branch_is_kept(self):
        out = ch._redact_followup_item({"title": "t", "branch": "feat/thing"})
        assert out["branch"] == "feat/thing"

    def test_branch_is_dropped_when_redaction_changes_it(self):
        # A mangled ref is worse than no ref: the frontend derives one instead.
        out = ch._redact_followup_item({"title": "t", "branch": "feat/" + "ghp_" + "A" * 36})
        assert "branch" not in out

    def test_non_string_branch_is_ignored(self):
        assert "branch" not in ch._redact_followup_item({"title": "t", "branch": 7})


# ── deny_non_dashboard_caller ────────────────────────────────────────────────


class TestDenyNonDashboardCaller:
    def test_internal_secret_caller_is_allowed_without_an_app_claim(self):
        request = make_mocked_request("POST", "/x")
        request["internal_auth"] = True
        # No owner predicate should even be consulted on this path.
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request"
        ) as owner:
            assert ch.deny_non_dashboard_caller(request, "op") is None
            owner.assert_not_called()

    def test_owner_request_is_allowed(self, _sel):
        request = make_mocked_request("POST", "/x")
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=True,
        ):
            assert ch.deny_non_dashboard_caller(request, "op") is None

    def test_non_owner_is_refused_and_audited(self, _sel):
        request = make_mocked_request("POST", "/x")
        request["user"] = "someone-else"
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            resp = ch.deny_non_dashboard_caller(request, "chat_slot_followup")
        assert resp is not None
        assert resp.status == 403
        _sel.log_api_access.assert_called_once()
        assert _sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    def test_audit_failure_still_refuses(self, _sel):
        _sel.log_api_access.side_effect = RuntimeError("sel down")
        request = make_mocked_request("POST", "/x")
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            resp = ch.deny_non_dashboard_caller(request, "op")
        assert resp is not None and resp.status == 403


# ── recent projects store ────────────────────────────────────────────────────


@pytest.fixture
def _recent_dir(tmp_path: Path):
    """Point the recent-projects file at tmp_path."""
    with patch(f"{MOD}.config_dir", return_value=tmp_path):
        yield tmp_path


class TestRecentProjects:
    def test_path_is_derived_from_config_dir(self, _recent_dir):
        assert ch._recent_projects_path() == _recent_dir / "recent_projects.json"

    def test_save_prepends_and_dedupes(self, _recent_dir):
        ch._save_recent_project("/a")
        ch._save_recent_project("/b")
        ch._save_recent_project("/a")
        stored = json.loads((_recent_dir / "recent_projects.json").read_text(encoding="utf-8"))
        assert stored == ["/a", "/b"]

    def test_save_caps_the_list(self, _recent_dir):
        fp = _recent_dir / "recent_projects.json"
        fp.write_text(json.dumps([f"/p{i}" for i in range(ch._MAX_RECENT_PROJECTS + 20)]), "utf-8")
        ch._save_recent_project("/new")
        stored = json.loads(fp.read_text(encoding="utf-8"))
        assert len(stored) == ch._MAX_RECENT_PROJECTS
        assert stored[0] == "/new"

    def test_corrupt_file_is_replaced_not_raised(self, _recent_dir):
        fp = _recent_dir / "recent_projects.json"
        fp.write_text("{not json", encoding="utf-8")
        ch._save_recent_project("/a")
        assert json.loads(fp.read_text(encoding="utf-8")) == ["/a"]

    def test_non_list_payload_is_discarded(self, _recent_dir):
        fp = _recent_dir / "recent_projects.json"
        fp.write_text(json.dumps({"dirs": ["/a"]}), encoding="utf-8")
        ch._save_recent_project("/b")
        assert json.loads(fp.read_text(encoding="utf-8")) == ["/b"]

    def test_no_temp_file_is_left_behind_on_write_failure(self, _recent_dir):
        with patch(f"{MOD}.os.replace", side_effect=OSError("read-only")):
            with pytest.raises(OSError):
                ch._save_recent_project("/a")
        assert list(_recent_dir.glob("*.tmp")) == []

    @pytest.mark.asyncio
    async def test_endpoint_drops_missing_and_non_string_entries(self, _recent_dir, _sel):
        real = _recent_dir / "proj"
        real.mkdir()
        (_recent_dir / "recent_projects.json").write_text(
            json.dumps([str(real), str(_recent_dir / "gone"), 7]), encoding="utf-8"
        )
        state = _state()
        app = _app(state, "GET", "/api/recent-projects", ch.api_recent_projects)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/recent-projects")
            assert resp.status == 200
            assert (await resp.json())["dirs"] == [str(real)]

    @pytest.mark.asyncio
    async def test_endpoint_drops_sensitive_directories(self, _recent_dir, _sel):
        real = _recent_dir / "proj"
        real.mkdir()
        (_recent_dir / "recent_projects.json").write_text(json.dumps([str(real)]), encoding="utf-8")
        state = _state()
        app = _app(state, "GET", "/api/recent-projects", ch.api_recent_projects)
        # is_sensitive_path is anchored on the real $HOME, so inject the verdict
        # rather than materialize a credential directory on this machine.
        with patch(f"{MOD}.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/recent-projects")
                assert (await resp.json())["dirs"] == []

    @pytest.mark.asyncio
    async def test_endpoint_tolerates_an_unreadable_store(self, _recent_dir, _sel):
        (_recent_dir / "recent_projects.json").write_text("[[[", encoding="utf-8")
        state = _state()
        app = _app(state, "GET", "/api/recent-projects", ch.api_recent_projects)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/recent-projects")
            assert resp.status == 200
            assert (await resp.json())["dirs"] == []


# ── _resume_session_identity ─────────────────────────────────────────────────


class TestResumeSessionIdentity:
    def test_dashboard_key_gets_the_dashboard_spelling(self):
        state = _state()
        assert ch._resume_session_identity(state, "mytab") == "dashboard:mytab"

    def test_channel_key_resolves_through_the_session_map(self):
        state = _state()
        state.sessions.channel_key_for_stem = MagicMock(return_value="slack:1712.44")
        assert ch._resume_session_identity(state, "slack_1712_44") == "slack:1712.44"

    def test_unmapped_channel_key_falls_back_to_dashboard_spelling(self):
        state = _state()
        state.sessions.channel_key_for_stem = MagicMock(return_value="")
        assert ch._resume_session_identity(state, "slack_1712_44") == "dashboard:slack_1712_44"


# ── _get_pattern_from_pending ────────────────────────────────────────────────


class TestGetPatternFromPending:
    def test_empty_request_id_short_circuits(self):
        assert ch._get_pattern_from_pending(_ChatSlot("s1"), "", "pattern") == ""

    def test_matching_permission_row_yields_the_field(self):
        slot = _ChatSlot("s1")
        slot.messages.append(
            {"role": "permission", "cls": json.dumps({"request_id": "r1", "pattern": "rm -rf"})}
        )
        assert ch._get_pattern_from_pending(slot, "r1", "pattern") == "rm -rf"

    def test_absent_field_yields_empty_string(self):
        slot = _ChatSlot("s1")
        slot.messages.append({"role": "permission", "cls": json.dumps({"request_id": "r1"})})
        assert ch._get_pattern_from_pending(slot, "r1", "pattern") == ""

    def test_non_object_and_malformed_cls_are_skipped_not_raised(self):
        slot = _ChatSlot("s1")
        slot.messages.append({"role": "permission", "cls": "[1, 2]"})
        slot.messages.append({"role": "permission", "cls": "{not json"})
        slot.messages.append({"role": "assistant", "cls": json.dumps({"request_id": "r1"})})
        assert ch._get_pattern_from_pending(slot, "r1", "pattern") == ""

    def test_newest_matching_row_wins(self):
        slot = _ChatSlot("s1")
        slot.messages.append(
            {"role": "permission", "cls": json.dumps({"request_id": "r1", "pattern": "old"})}
        )
        slot.messages.append(
            {"role": "permission", "cls": json.dumps({"request_id": "r1", "pattern": "new"})}
        )
        assert ch._get_pattern_from_pending(slot, "r1", "pattern") == "new"


# ── PATCH /api/chat/slots/{slot}/color ───────────────────────────────────────


class TestSlotColor:
    async def _patch(self, state, name, payload):
        app = _app(state, "PATCH", "/api/chat/slots/{slot}/color", ch.api_chat_slot_color)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(f"/api/chat/slots/{name}/color", json=payload)
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self):
        status, _ = await self._patch(_state(), "missing", {"color_index": 1})
        assert status == 404

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self):
        slot = _ChatSlot("s1")
        state = _state(slot)
        app = _app(state, "PATCH", "/api/chat/slots/{slot}/color", ch.api_chat_slot_color)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/s1/color",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_out_of_range_index_is_rejected(self):
        slot = _ChatSlot("s1")
        status, body = await self._patch(_state(slot), "s1", {"color_index": ch.MAX_COLOR_INDEX + 1})
        assert status == 400
        assert str(ch.MAX_COLOR_INDEX) in body["error"]
        assert slot.color_index is None

    @pytest.mark.asyncio
    async def test_negative_index_is_rejected(self):
        slot = _ChatSlot("s1")
        status, _ = await self._patch(_state(slot), "s1", {"color_index": -1})
        assert status == 400

    @pytest.mark.asyncio
    async def test_bool_index_is_rejected(self):
        slot = _ChatSlot("s1")
        status, _ = await self._patch(_state(slot), "s1", {"color_index": True})
        assert status == 400

    @pytest.mark.asyncio
    async def test_valid_index_is_stored_and_pushed(self):
        slot = _ChatSlot("s1")
        state = _state(slot)
        status, body = await self._patch(state, "s1", {"color_index": 3})
        assert (status, body) == (200, {"ok": True, "color_index": 3, "color_hex": None})
        assert slot.color_index == 3
        assert slot._dirty is True
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_null_clears_the_color(self):
        slot = _ChatSlot("s1")
        slot.color_index = 5
        status, body = await self._patch(_state(slot), "s1", {"color_index": None})
        assert (status, body) == (200, {"ok": True, "color_index": None, "color_hex": None})
        assert slot.color_index is None


# ── POST /api/chat/slots/{slot}/context ──────────────────────────────────────


class TestSlotContextInject:
    async def _post(self, state, name, payload, *, app_claim: str | None = None):
        async def route(request: web.Request) -> web.Response:
            if app_claim is not None:
                request["app"] = app_claim
            return await ch.api_chat_slot_context(request)

        app = _app(state, "POST", "/api/chat/slots/{slot}/context", route)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{name}/context", json=payload)
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self, _sel):
        status, body = await self._post(_state(), "missing", {"content": "x"})
        assert status == 404
        assert body["error"] == "slot not found"

    @pytest.mark.asyncio
    async def test_app_token_cannot_reach_an_unscoped_slot(self, _sel):
        slot = _ChatSlot("s1")
        status, _ = await self._post(_state(slot), "s1", {"content": "x"}, app_claim="notes")
        assert status == 404
        assert _sel.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_app_token_cannot_reach_another_apps_slot(self, _sel):
        slot = _ChatSlot("s1")
        slot._app = "meetings"
        status, _ = await self._post(_state(slot), "s1", {"content": "x"}, app_claim="notes")
        assert status == 404
        assert "does not own" in _sel.log_api_access.call_args.kwargs["error"]

    @pytest.mark.asyncio
    async def test_owning_app_is_allowed(self, _sel):
        slot = _ChatSlot("s1")
        slot._app = "notes"
        status, body = await self._post(_state(slot), "s1", {"content": "x"}, app_claim="notes")
        assert (status, body) == (200, {"ok": True, "pending": 1})

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, _sel):
        slot = _ChatSlot("s1")
        app = _app(_state(slot), "POST", "/api/chat/slots/{slot}/context", ch.api_chat_slot_context)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/s1/context",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_content_is_400(self, _sel):
        slot = _ChatSlot("s1")
        status, body = await self._post(_state(slot), "s1", {"source": "watch"})
        assert status == 400
        assert body["error"] == "content is required"

    @pytest.mark.asyncio
    async def test_oversize_content_is_400(self, _sel):
        slot = _ChatSlot("s1")
        status, body = await self._post(_state(slot), "s1", {"content": "x" * 40_001})
        assert status == 400
        assert "40000" in body["error"]
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_entry_records_source_ephemeral_and_max_age(self, _sel):
        slot = _ChatSlot("s1")
        status, body = await self._post(
            _state(slot),
            "s1",
            {"content": "note", "source": "watch", "ephemeral": False, "maxAge": 300},
        )
        assert (status, body) == (200, {"ok": True, "pending": 1})
        entry = slot._pending_context[0]
        assert entry["content"] == "note"
        assert entry["source"] == "watch"
        assert entry["ephemeral"] is False
        assert entry["maxAge"] == 300
        assert isinstance(entry["injectedAt"], float)

    @pytest.mark.asyncio
    async def test_max_age_is_omitted_when_not_supplied(self, _sel):
        slot = _ChatSlot("s1")
        await self._post(_state(slot), "s1", {"content": "note"})
        assert "maxAge" not in slot._pending_context[0]
        assert slot._pending_context[0]["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_per_source_cap_is_429(self, _sel):
        slot = _ChatSlot("s1")
        slot._pending_context = [
            {"content": "c", "source": "watch"} for _ in range(ch._MAX_CONTEXT_PER_SOURCE)
        ]
        status, body = await self._post(_state(slot), "s1", {"content": "x", "source": "watch"})
        assert status == 429
        assert "watch" in body["error"]
        # A different source is unaffected by another's cap.
        status2, _ = await self._post(_state(slot), "s1", {"content": "x", "source": "other"})
        assert status2 == 200

    @pytest.mark.asyncio
    async def test_queue_is_fifo_evicted_at_the_shared_ceiling(self, _sel):
        slot = _ChatSlot("s1")
        slot._pending_context = [{"content": f"c{i}", "source": ""} for i in range(50)]
        assert len(slot._pending_context) == _MAX_PENDING_CONTEXT
        status, body = await self._post(_state(slot), "s1", {"content": "newest"})
        assert (status, body) == (200, {"ok": True, "pending": _MAX_PENDING_CONTEXT})
        assert slot._pending_context[0]["content"] == "c1"
        assert slot._pending_context[-1]["content"] == "newest"


# ── queue mutation routes ────────────────────────────────────────────────────


class TestQueueCancel:
    async def _delete(self, state, name, qid):
        app = _app(
            state,
            "DELETE",
            "/api/chat/slots/{slot}/queue/{queue_id}",
            ch.api_chat_slot_queue_cancel,
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete(f"/api/chat/slots/{name}/queue/{qid}")
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self, _sel):
        status, _ = await self._delete(_state(), "missing", "q1")
        assert status == 404

    @pytest.mark.asyncio
    async def test_unknown_queue_id_is_404(self, _sel):
        slot = _ChatSlot("s1")
        status, body = await self._delete(_state(slot), "s1", "nope")
        assert status == 404
        assert body["error"] == "queue item not found"

    @pytest.mark.asyncio
    async def test_cancel_removes_the_item_and_broadcasts(self, _sel):
        slot = _ChatSlot("s1")
        qid = slot.queue_append("second thoughts")
        state = _state(slot)

        status, body = await self._delete(state, "s1", qid)

        assert status == 200
        assert body["content"] == "second thoughts"
        assert slot._queue == []
        event, payload = state.broadcast_ws.call_args.args
        assert event == "queue_cancel"
        assert payload["queue_id"] == qid
        state.push_slots_update.assert_called_once()


class TestQueueEdit:
    async def _patch(self, state, name, qid, payload):
        app = _app(
            state, "PATCH", "/api/chat/slots/{slot}/queue/{queue_id}", ch.api_chat_slot_queue_edit
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(f"/api/chat/slots/{name}/queue/{qid}", json=payload)
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self, _sel):
        status, _ = await self._patch(_state(), "missing", "q1", {"content": "x"})
        assert status == 404

    @pytest.mark.asyncio
    async def test_blank_content_is_rejected(self, _sel):
        slot = _ChatSlot("s1")
        qid = slot.queue_append("orig")
        status, body = await self._patch(_state(slot), "s1", qid, {"content": "   "})
        assert status == 400
        assert "non-empty" in body["error"]
        assert slot._queue[0]["content"] == "orig"

    @pytest.mark.asyncio
    async def test_non_string_content_is_rejected(self, _sel):
        slot = _ChatSlot("s1")
        qid = slot.queue_append("orig")
        status, _ = await self._patch(_state(slot), "s1", qid, {"content": 5})
        assert status == 400

    @pytest.mark.asyncio
    async def test_unknown_queue_id_is_404(self, _sel):
        slot = _ChatSlot("s1")
        status, body = await self._patch(_state(slot), "s1", "nope", {"content": "x"})
        assert status == 404
        assert body["error"] == "queue item not found"

    @pytest.mark.asyncio
    async def test_edit_replaces_content_in_place(self, _sel):
        slot = _ChatSlot("s1")
        first = slot.queue_append("one")
        second = slot.queue_append("two")
        state = _state(slot)

        status, body = await self._patch(state, "s1", first, {"content": "ONE"})

        assert status == 200
        assert body["content"] == "ONE"
        assert [item["id"] for item in slot._queue] == [first, second]
        assert slot._queue[0]["content"] == "ONE"
        assert state.broadcast_ws.call_args.args[0] == "queue_edit"


class TestQueueReorder:
    async def _put(self, state, name, payload):
        app = _app(
            state, "PUT", "/api/chat/slots/{slot}/queue/order", ch.api_chat_slot_queue_reorder
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(f"/api/chat/slots/{name}/queue/order", json=payload)
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self, _sel):
        status, _ = await self._put(_state(), "missing", {"order": []})
        assert status == 404

    @pytest.mark.asyncio
    async def test_non_list_order_is_400(self, _sel):
        slot = _ChatSlot("s1")
        status, body = await self._put(_state(slot), "s1", {"order": "q1"})
        assert status == 400
        assert "list of queue id strings" in body["error"]

    @pytest.mark.asyncio
    async def test_non_string_ids_are_400(self, _sel):
        slot = _ChatSlot("s1")
        status, _ = await self._put(_state(slot), "s1", {"order": [1, 2]})
        assert status == 400

    @pytest.mark.asyncio
    async def test_unknown_ids_are_400(self, _sel):
        slot = _ChatSlot("s1")
        slot.queue_append("a")
        status, body = await self._put(_state(slot), "s1", {"order": ["ghost"]})
        assert status == 400
        assert "ghost" in body["error"]

    @pytest.mark.asyncio
    async def test_partial_order_puts_named_ids_first(self, _sel):
        slot = _ChatSlot("s1")
        a = slot.queue_append("a")
        b = slot.queue_append("b")
        c = slot.queue_append("c")
        state = _state(slot)

        status, _ = await self._put(state, "s1", {"order": [c, a]})

        assert status == 200
        assert [item["id"] for item in slot._queue] == [c, a, b]
        event, payload = state.broadcast_ws.call_args.args
        assert event == "queue_reorder"
        assert payload["order"] == [c, a, b]

    @pytest.mark.asyncio
    async def test_queued_rows_are_reordered_and_other_rows_kept(self, _sel):
        slot = _ChatSlot("s1")
        a = slot.queue_append("a")
        b = slot.queue_append("b")
        slot.messages.append({"role": "user", "content": "hi", "cls": ""})
        slot.messages.append({"role": "queued", "content": "a", "cls": json.dumps({"queue_id": a})})
        slot.messages.append({"role": "queued", "content": "b", "cls": json.dumps({"queue_id": b})})
        # A queued row with unparseable cls must survive rather than vanish.
        slot.messages.append({"role": "queued", "content": "orphan", "cls": "{bad"})

        status, _ = await self._put(_state(slot), "s1", {"order": [b, a]})

        assert status == 200
        assert [m["content"] for m in slot.messages] == ["hi", "b", "a", "orphan"]


# ── POST /api/chat/slots/{slot}/workspace ────────────────────────────────────


class TestSlotWorkspace:
    async def _post(self, state, name, payload):
        app = _app(state, "POST", "/api/chat/slots/{slot}/workspace", ch.api_chat_slot_workspace)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{name}/workspace", json=payload)
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self):
        status, _ = await self._post(_state(), "missing", {"workspace": "w"})
        assert status == 404

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self):
        slot = _ChatSlot("s1")
        app = _app(
            _state(slot), "POST", "/api/chat/slots/{slot}/workspace", ch.api_chat_slot_workspace
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/s1/workspace",
                data="nope",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_is_refused_once_the_conversation_started(self):
        slot = _ChatSlot("s1")
        slot.workspace = "default"
        slot.total_messages = 3
        status, body = await self._post(_state(slot), "s1", {"workspace": "other"})
        assert status == 409
        assert "new session" in body["error"]
        assert slot.workspace == "default"

    @pytest.mark.asyncio
    async def test_switch_resets_the_session_and_repoints_the_project(self):
        slot = _ChatSlot("s1")
        state = _state(slot)
        with patch(f"{MOD}._reset_slot_session", new=AsyncMock()) as reset:
            with patch(f"{MOD}.default_project_dir", return_value="/tmp/ws-other"):
                status, body = await self._post(state, "s1", {"workspace": "other"})

        assert (status, body) == (200, {"ok": True, "workspace": "other"})
        assert slot.workspace == "other"
        assert slot.project == "/tmp/ws-other"
        reset.assert_awaited_once()
        assert reset.await_args.args[2] == "dashboard:s1"
        state.push_slots_update.assert_called_once()


# ── POST /api/chat/slots/{slot}/reasoning-effort ──────────────────────────────


def _valid_effort() -> str:
    """A level the endpoint currently accepts.

    The set is mutated at runtime by ACP (``update_reasoning_effort_values``),
    so it is read rather than hard-coded — a literal would rot the day kiro
    renames a level.
    """
    levels = get_reasoning_effort_values() - {""}
    assert levels, "no non-default effort level advertised"
    return sorted(levels)[0]


class TestSlotReasoningEffort:
    async def _post(self, state, name, payload):
        app = _app(
            state,
            "POST",
            "/api/chat/slots/{slot}/reasoning-effort",
            ch.api_chat_slot_reasoning_effort,
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{name}/reasoning-effort", json=payload)
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self):
        status, _ = await self._post(_state(), "missing", {"reasoning_effort": ""})
        assert status == 404

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self):
        slot = _ChatSlot("s1")
        app = _app(
            _state(slot),
            "POST",
            "/api/chat/slots/{slot}/reasoning-effort",
            ch.api_chat_slot_reasoning_effort,
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/s1/reasoning-effort",
                data="~",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_level_is_rejected(self):
        slot = _ChatSlot("s1")
        bogus = "turbo"
        assert bogus not in get_reasoning_effort_values()
        status, body = await self._post(_state(slot), "s1", {"reasoning_effort": bogus})
        assert status == 400
        assert "must be one of" in body["error"]
        assert slot.reasoning_effort == ""

    @pytest.mark.asyncio
    async def test_non_string_level_is_rejected(self):
        slot = _ChatSlot("s1")
        status, _ = await self._post(_state(slot), "s1", {"reasoning_effort": 3})
        assert status == 400

    @pytest.mark.asyncio
    async def test_unchanged_level_short_circuits(self):
        level = _valid_effort()
        slot = _ChatSlot("s1")
        slot.reasoning_effort = level
        state = _state(slot)
        status, body = await self._post(state, "s1", {"reasoning_effort": level})
        assert (status, body) == (200, {"ok": True, "reasoning_effort": level})
        state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_turn_defers_the_live_push(self):
        level = _valid_effort()
        slot = _ChatSlot("s1")
        state = _state(slot)
        provider = _acp(
            supports_effort=MagicMock(return_value=True),
            has_active_turn=MagicMock(return_value=True),
        )
        state.sessions.get_provider = MagicMock(return_value=provider)

        with patch(f"{MOD}._reset_slot_session", new=AsyncMock()) as reset:
            status, body = await self._post(state, "s1", {"reasoning_effort": level})

        assert status == 200
        assert body["deferred"] is True
        assert slot.reasoning_effort == level
        provider.change_effort.assert_not_awaited()
        reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_push_avoids_the_reset(self):
        level = _valid_effort()
        slot = _ChatSlot("s1")
        state = _state(slot)
        provider = _acp(
            supports_effort=MagicMock(return_value=True),
            change_effort=AsyncMock(return_value=True),
        )
        state.sessions.get_provider = MagicMock(return_value=provider)

        with patch(f"{MOD}._reset_slot_session", new=AsyncMock()) as reset:
            status, body = await self._post(state, "s1", {"reasoning_effort": level})

        assert (status, body) == (200, {"ok": True, "reasoning_effort": level})
        provider.change_effort.assert_awaited_once_with(level)
        reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clearing_the_level_calls_clear_effort(self):
        slot = _ChatSlot("s1")
        slot.reasoning_effort = _valid_effort()
        state = _state(slot)
        provider = _acp(
            supports_effort=MagicMock(return_value=True),
            clear_effort=AsyncMock(return_value=True),
        )
        state.sessions.get_provider = MagicMock(return_value=provider)

        with patch(f"{MOD}._reset_slot_session", new=AsyncMock()) as reset:
            status, body = await self._post(state, "s1", {"reasoning_effort": ""})

        assert (status, body) == (200, {"ok": True, "reasoning_effort": ""})
        provider.clear_effort.assert_awaited_once()
        reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_push_failure_falls_back_to_a_reset(self):
        level = _valid_effort()
        slot = _ChatSlot("s1")
        state = _state(slot)
        provider = _acp(
            supports_effort=MagicMock(return_value=True),
            change_effort=AsyncMock(side_effect=RuntimeError("stdout busy")),
        )
        state.sessions.get_provider = MagicMock(return_value=provider)

        with patch(f"{MOD}._reset_slot_session", new=AsyncMock()) as reset:
            status, _ = await self._post(state, "s1", {"reasoning_effort": level})

        assert status == 200
        assert slot.reasoning_effort == level
        reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_effort_incapable_model_persists_without_a_reset(self):
        level = _valid_effort()
        slot = _ChatSlot("s1")
        state = _state(slot)
        provider = _acp(supports_effort=MagicMock(return_value=False))
        state.sessions.get_provider = MagicMock(return_value=provider)

        with patch(f"{MOD}._reset_slot_session", new=AsyncMock()) as reset:
            status, _ = await self._post(state, "s1", {"reasoning_effort": level})

        assert status == 200
        assert slot.reasoning_effort == level
        reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_live_session_resets_so_the_cold_start_picks_it_up(self):
        level = _valid_effort()
        slot = _ChatSlot("s1")
        state = _state(slot)
        state.sessions.get_provider = MagicMock(return_value=None)

        with patch(f"{MOD}._reset_slot_session", new=AsyncMock()) as reset:
            status, _ = await self._post(state, "s1", {"reasoning_effort": level})

        assert status == 200
        reset.assert_awaited_once()


# ── POST /api/chat/slots/{slot}/followup ─────────────────────────────────────


def _item(**over) -> dict:
    base = {"title": "Ship it", "description": "open the PR", "prompt": "open a PR for this"}
    base.update(over)
    return base


class TestSlotFollowup:
    async def _post(self, state, name, payload, *, owner: bool = True):
        async def route(request: web.Request) -> web.Response:
            # The MCP relay arrives with the internal-secret grant, which is the
            # branch this endpoint is actually reached on.
            request["internal_auth"] = owner
            return await ch.api_chat_slot_followup(request)

        app = _app(state, "POST", "/api/chat/slots/{slot}/followup", route)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{name}/followup", json=payload)
            return resp.status, await resp.json()

    def _state_with(self, slot, delivered: int = 1):
        state = _state(slot)
        state.deliver_ws_owners = AsyncMock(return_value=delivered)
        return state

    @pytest.mark.asyncio
    async def test_non_owner_is_refused_before_the_slot_lookup(self, _sel):
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            status, body = await self._post(
                self._state_with(None), "missing", {"items": [_item()]}, owner=False
            )
        assert status == 403
        assert body["error"] == "forbidden"

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self):
        status, _ = await self._post(self._state_with(None), "missing", {"items": [_item()]})
        assert status == 404

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self):
        slot = _ChatSlot("s1")

        async def route(request: web.Request) -> web.Response:
            request["internal_auth"] = True
            return await ch.api_chat_slot_followup(request)

        app = _app(self._state_with(slot), "POST", "/api/chat/slots/{slot}/followup", route)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/s1/followup",
                data="%",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_object_body_is_400(self):
        slot = _ChatSlot("s1")
        status, _ = await self._post(self._state_with(slot), "s1", [_item()])
        assert status == 400

    @pytest.mark.asyncio
    async def test_schema_violation_is_400(self):
        slot = _ChatSlot("s1")
        # An unknown per-item field is fail-closed by the shared schema.
        status, body = await self._post(
            self._state_with(slot), "s1", {"items": [_item(colour="red")]}
        )
        assert status == 400
        assert "colour" in body["error"]

    @pytest.mark.asyncio
    async def test_empty_items_is_400(self):
        slot = _ChatSlot("s1")
        status, _ = await self._post(self._state_with(slot), "s1", {"items": []})
        assert status == 400

    @pytest.mark.asyncio
    async def test_delivered_count_comes_from_the_owner_send(self):
        slot = _ChatSlot("s1")
        slot.project = "/tmp/proj"
        state = self._state_with(slot, delivered=2)

        status, body = await self._post(state, "s1", {"items": [_item(), _item(title="Later")]})

        assert status == 200
        assert body == {"ok": True, "count": 2, "delivered": 2}
        event, payload = state.deliver_ws_owners.await_args.args
        assert event == "followup_card"
        assert payload["slot"] == "s1"
        assert [i["title"] for i in payload["items"]] == ["Ship it", "Later"]

    @pytest.mark.asyncio
    async def test_unscoped_slot_gets_the_worktree_warning(self):
        slot = _ChatSlot("s1")
        slot.project = ""
        status, body = await self._post(self._state_with(slot), "s1", {"items": [_item()]})
        assert status == 200
        assert "no project directory" in body["warning"]

    @pytest.mark.asyncio
    async def test_a_failed_delivery_reports_zero_not_a_500(self):
        slot = _ChatSlot("s1")
        slot.project = "/tmp/proj"
        state = _state(slot)
        state.deliver_ws_owners = AsyncMock(side_effect=RuntimeError("socket gone"))

        status, body = await self._post(state, "s1", {"items": [_item()]})

        assert status == 200
        assert body["delivered"] == 0
