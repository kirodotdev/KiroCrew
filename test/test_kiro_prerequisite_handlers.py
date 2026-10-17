"""Kiro CLI prerequisite tests: the dashboard routes and the detect-only contract.

The status, update and repair routes and who may read what, and the contract that Kiro
Crew detects Kiro CLI but never installs it or signs in for the user (including the
read-only-probe tripwire over the source of ``kiro_prerequisite.py`` and its owners).
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _FakeRuntime, _make_executable, _no_audit

from kiro_crew import kiro_prerequisite as prerequisite_module
from kiro_crew.dashboard.chat_handlers import api_chat_slot_create
from kiro_crew.dashboard.chat_regenerate import api_chat_slot_edit_resend, api_chat_slot_regenerate
from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.handlers.kiro_prerequisite import (
    api_kiro_prerequisite_repair_specs,
    api_kiro_prerequisite_status,
    api_kiro_prerequisite_update_cli,
)
from kiro_crew.kiro_prerequisite import (
    KIRO_CLI_LOGIN_COMMAND,
    KIRO_CLI_SSO_LOGIN_COMMAND,
    KIRO_CLI_UPDATE_COMMAND,
    OFFICIAL_INSTALL_DOCS_URL,
    KiroPrerequisiteService,
    PrerequisiteStatus,
    ProcessResult,
    _run_process,
)


class TestKiroPrerequisiteHandlers:
    @staticmethod
    def _app(
        service: KiroPrerequisiteService,
        *,
        app_claim: str,
        user: str = "test-user",
        owner_id: str = "test-user",
    ) -> web.Application:
        @web.middleware
        async def identity(
            request: web.Request,
            handler: Any,
        ) -> web.StreamResponse:
            request["user"] = user
            request["app"] = app_claim
            return await handler(request)

        app = web.Application(middlewares=[identity])
        app["state"] = SimpleNamespace(owner_id=owner_id)
        app["kiro_prerequisite_service"] = service
        app.router.add_get("/api/kiro-prerequisite", api_kiro_prerequisite_status)
        app.router.add_post(
            "/api/kiro-prerequisite/repair-specs",
            api_kiro_prerequisite_repair_specs,
        )
        app.router.add_post(
            "/api/kiro-prerequisite/update-cli",
            api_kiro_prerequisite_update_cli,
        )
        return app

    @pytest.mark.asyncio
    async def test_dashboard_user_reads_status_and_no_setup_verb_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The owner can READ readiness. Neither setup step is a Kiro Crew verb:
        # obtaining the CLI and signing in both belong to Kiro CLI, so both
        # routes are absent by construction rather than guarded.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        snapshot = {
            "platform": "Linux",
            "installed": False,
            "authenticated": False,
            "ready": False,
            "initial_setup_complete": False,
            "repair_required": False,
            "docs_url": OFFICIAL_INSTALL_DOCS_URL,
            "login_command": KIRO_CLI_LOGIN_COMMAND,
            "sso_login_command": KIRO_CLI_SSO_LOGIN_COMMAND,
        }

        async def fake_snapshot(*, force: bool = False, coalesce: bool = False) -> dict[str, Any]:
            del force, coalesce
            return snapshot

        monkeypatch.setattr(service, "snapshot", fake_snapshot)

        async with TestClient(TestServer(self._app(service, app_claim=""))) as client:
            read = await client.get("/api/kiro-prerequisite")
            assert read.status == 200
            body = await read.json()
            assert body["login_command"] == KIRO_CLI_LOGIN_COMMAND
            # The owner branch passes the snapshot through verbatim, so both
            # sign-in commands reach the gate that has to offer the tier choice.
            assert body["sso_login_command"] == KIRO_CLI_SSO_LOGIN_COMMAND
            assert (await client.post("/api/kiro-prerequisite/login")).status == 404
            assert (await client.post("/api/kiro-prerequisite/install")).status == 404

    @pytest.mark.asyncio
    async def test_owner_update_cli_runs_self_update_and_returns_snapshot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The owner may POST update-cli to run the CLI's in-place self-update.
        # The handler awaits the service, then returns the post-update snapshot
        # (cli_update_error empty on success) with setup_allowed for the owner.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        called_with: list[str] = []

        async def fake_update_cli(caller: str) -> dict[str, Any]:
            called_with.append(caller)
            return {
                "platform": "Linux",
                "installed": True,
                "authenticated": True,
                "ready": True,
                "acp_supported": True,
                "update_command": KIRO_CLI_UPDATE_COMMAND,
                "cli_update_error": "",
            }

        monkeypatch.setattr(service, "update_cli", fake_update_cli)

        async with TestClient(TestServer(self._app(service, app_claim=""))) as client:
            resp = await client.post("/api/kiro-prerequisite/update-cli")
            assert resp.status == 200
            body = await resp.json()

        # The success path re-probes and reports acp support restored with no
        # update error, and the owner branch stamps setup_allowed.
        assert body["acp_supported"] is True
        assert body["cli_update_error"] == ""
        assert body["setup_allowed"] is True
        # The handler forwards the resolved caller identity to the service.
        assert called_with == ["test-user"]

    @pytest.mark.asyncio
    async def test_status_endpoint_returns_not_ready_instead_of_500_on_probe_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A transient probe failure must not surface as an HTTP 500 (which
        # flashes the full-screen "could not check Kiro CLI" gate on reload).
        # The handler returns a retryable not-ready snapshot instead.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        async def boom(**_kwargs: Any) -> dict[str, Any]:
            raise OSError("probe wedged")

        monkeypatch.setattr(service, "snapshot", boom)

        async with TestClient(TestServer(self._app(service, app_claim=""))) as client:
            resp = await client.get("/api/kiro-prerequisite")
            assert resp.status == 200
            body = await resp.json()

        assert body["ready"] is False
        # Reported as a retryable not-ready 200, never a 500 that would flash the
        # full-screen "could not check Kiro CLI" gate on reload.
        assert body["installed"] is True
        assert body["setup_allowed"] is True
        # The screen can say WHY, not only that it could not check.
        assert body["probe_error"] == "OSError: probe wedged"
        assert body["probe_status"] is None

    @pytest.mark.asyncio
    async def test_probe_error_is_owner_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-owner reads the readiness bit and nothing about the host probe."""
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        async def boom(**_kwargs: Any) -> dict[str, Any]:
            raise OSError("probe wedged")

        monkeypatch.setattr(service, "snapshot", boom)

        app = self._app(service, app_claim="", user="someone-else", owner_id="owner")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/kiro-prerequisite")
            assert resp.status == 200
            body = await resp.json()

        assert "probe_error" not in body
        assert "probe_status" not in body

    @pytest.mark.asyncio
    async def test_session_create_and_send_are_admitted_when_latch_is_stale(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Turn-starting routes do not 503 on a latched not-ready value.

        Readiness is probed at boot and on explicit action only, so a stale latch
        must not reject a request the CLI would have served — that was the stuck
        case (sign in from a terminal, stay locked out). The ACP attempt reports
        the real auth state as a chat error instead.
        """

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        async def not_ready_snapshot(*, force: bool = False) -> dict[str, Any]:
            del force
            return {"ready": False}

        monkeypatch.setattr(service, "snapshot", not_ready_snapshot)
        assert await service.session_ready() is False
        app = web.Application()
        app["state"] = SimpleNamespace()
        app["kiro_prerequisite_service"] = service
        app.router.add_post("/api/chat/slots", api_chat_slot_create)

        async with TestClient(TestServer(app)) as client:
            create_response = await client.post("/api/chat/slots", json={})
            create_text = await create_response.text()

        # Admitted past the (now removed) prerequisite gate: the request reaches
        # the handler body and only then fails on this bare SimpleNamespace state
        # (500), rather than being turned away with the prerequisite 503.
        assert create_response.status != 503
        assert "kiro_prerequisite_required" not in create_text

    @pytest.mark.asyncio
    async def test_central_chat_runner_posts_auth_error_to_linked_slack(
        self,
        tmp_path: Path,
    ) -> None:
        """An ACP auth failure still reaches a linked Slack thread.

        The pre-turn readiness gate does not own this delivery; the
        ``AcpAuthRequired`` handler now does, so a user driving the session from
        Slack is not left without a response when the CLI is signed out.
        """

        from kiro_crew.acp.client import AcpAuthRequired

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._has_probed = True
        service._status.ready = True
        service._status.authenticated = True
        state = _make_state(tmp_path)
        state.kiro_prerequisite_service = service
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False

        async def stream(_stream_message: str):
            raise AcpAuthRequired("kiro-cli is not logged in.")
            yield  # pragma: no cover — generator shape only

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_failure = AsyncMock()
        # No inbound mirror link: this test asserts the AUTH-error delivery, so
        # the ordinary user-message mirror must stay out of post_message's calls.
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))

        slot = state.get_or_create_slot("linked-readiness")
        slot._titled = True
        slot._slack_linked = True
        slot._slack_thread_ts = "1712345.6789"
        slot._slack_channel = "C123"

        await _run_chat(state, slot, "message from linked thread")

        state.slack_client.post_message.assert_awaited_once_with(
            "C123",
            "kiro-cli is not logged in.",
            "1712345.6789",
        )
        # The observed failure latches readiness so the SPA banner appears
        # without waiting for the user to hit Refresh.
        assert service._status.ready is False
        assert service._status.authenticated is False

    @pytest.mark.asyncio
    async def test_destructive_chat_routes_reject_before_mutating_history(
        self,
        tmp_path: Path,
    ) -> None:
        """regenerate / edit-resend / rewind MUST fail closed on a stale latch.

        These three truncate `slot.messages` and PERSIST the result before the
        background turn runs, so "let the ACP attempt be the authority" does not
        hold: by the time the turn raises AcpAuthRequired the history is already
        rewritten, and no error card can undo it. They therefore keep the
        blocking gate even though an ordinary send does not.
        """

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
            clock=lambda: 1.0,
        )
        service._has_probed = True
        service._last_probe_at = 1.0
        assert await service.session_ready() is False
        messages = [
            {"role": "user", "content": "question", "ts": "u1"},
            {"role": "assistant", "content": "answer", "ts": "a1"},
        ]
        original_messages = copy.deepcopy(messages)
        slot = SimpleNamespace(messages=messages)
        sessions = MagicMock()
        persistence = MagicMock()
        state = SimpleNamespace(
            _slots={"paused": slot},
            sessions=sessions,
            conversation_log=persistence,
        )
        app = web.Application()
        app["state"] = state
        app["kiro_prerequisite_service"] = service
        app.router.add_post(
            "/api/chat/slots/{slot}/regenerate",
            api_chat_slot_regenerate,
        )
        app.router.add_post(
            "/api/chat/slots/{slot}/edit-resend",
            api_chat_slot_edit_resend,
        )
        app.router.add_post(
            "/api/chat/slots/{slot}/rewind",
            api_chat_slot_rewind,
        )

        async with TestClient(TestServer(app)) as client:
            responses = [
                await client.post("/api/chat/slots/paused/regenerate", json={}),
                await client.post(
                    "/api/chat/slots/paused/edit-resend",
                    json={"index": 0, "content": "edited"},
                ),
                await client.post(
                    "/api/chat/slots/paused/rewind",
                    json={"at_message_index": 0, "content": "edited"},
                ),
            ]
            bodies = [await response.json() for response in responses]

        assert [response.status for response in responses] == [503, 503, 503]
        assert [body["code"] for body in bodies] == ["kiro_prerequisite_required"] * 3
        # The refusal happens BEFORE any mutation: history is untouched and no
        # session/persistence call was made.
        assert messages == original_messages
        assert sessions.mock_calls == []
        assert persistence.mock_calls == []

    @pytest.mark.asyncio
    async def test_auth_failure_holds_the_queue_instead_of_draining_it(
        self,
        tmp_path: Path,
    ) -> None:
        """An auth failure must not pop every queued prompt into the same wall.

        Without this, teardown hands the queue to a successor turn that fails
        identically, repeats, and drains the whole queue — leaving nothing to
        resume after the user signs in. The queue is held intact instead (cards
        stay visible and individually cancellable).
        """

        from kiro_crew.acp.client import AcpAuthRequired

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._has_probed = True
        service._status.ready = True
        service._status.authenticated = True

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            raise AcpAuthRequired("kiro-cli is not logged in.")
            yield  # pragma: no cover — generator shape only

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state = _make_state(tmp_path)
        state.kiro_prerequisite_service = service
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_failure = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))

        slot = state.get_or_create_slot("auth-queue")
        slot._titled = True
        slot.queue_append("first queued")
        slot.queue_append("second queued")

        await _run_chat(state, slot, "message that hits the auth wall")

        # Only the original turn ran; neither queued prompt was consumed.
        assert delivered == ["message that hits the auth wall"]
        assert len(slot._queue) == 2
        # And the actionable message reached the transcript.
        assert any(
            message.get("role") == "error" and "not logged in" in message.get("content", "")
            for message in slot.messages
        )

    @pytest.mark.asyncio
    async def test_stale_not_ready_does_not_park_the_queue(
        self,
        tmp_path: Path,
    ) -> None:
        """A latched not-ready value must never strand a queued message.

        Readiness is only refreshed at boot and on explicit action, so parking
        the queue on it would wait forever. The successor turn runs and the ACP
        attempt reports the real auth state.
        """

        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        # Deliberately stale/not-ready: the boot probe found a signed-out CLI and
        # the user has since signed in from a terminal.
        service._has_probed = True
        service._status.ready = False

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"response to {stream_message}")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state = _make_state(tmp_path)
        state.kiro_prerequisite_service = service
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("queued")
        slot._titled = True
        queue_id = slot.queue_append("keep this queued")

        await _run_chat(state, slot, "first message")
        queued_turn = slot.task
        assert queued_turn is not None
        await queued_turn

        # Both turns ran despite the stale not-ready latch.
        assert delivered[0] == "first message"
        assert delivered[1].endswith("keep this queued")
        assert slot._queue == []
        assert slot.task is None
        assert any(
            call.args[0] == "queue_pop" and call.args[1]["queue_id"] == queue_id
            for call in state.broadcast_ws.call_args_list
        )
        # No "setup or sign-in is required" card — nothing was blocked.
        assert not any(
            message.get("role") == "error" and "sign-in is required" in message.get("content", "")
            for message in slot.messages
        )

    @pytest.mark.asyncio
    async def test_stale_not_ready_does_not_park_synthesis(
        self,
        tmp_path: Path,
    ) -> None:
        """Post-fan-out synthesis runs rather than waiting on latched readiness."""

        from kiro_crew.dashboard.state import SUBAGENT_SYNTHESIS_PROMPT
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._has_probed = True
        service._status.ready = False

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"response to {stream_message}")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state = _make_state(tmp_path)
        state.kiro_prerequisite_service = service
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = []
        # No child queued behind the spawn gate either, and none pending in
        # memory: the synthesis fire gate counts them.
        state.subagents.queued_count_for_async = AsyncMock(return_value=0)
        state.subagents.queued_count_or_none_async = AsyncMock(return_value=0)
        state.subagents.has_in_memory_pending_work_for.return_value = False
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("synthesis-readiness")
        slot._titled = True
        slot._pending_synthesis = True

        await _run_chat(state, slot, "first message")
        synthesis_task = slot.task
        assert synthesis_task is not None
        await synthesis_task

        assert delivered[0] == "first message"
        assert any(message.endswith(SUBAGENT_SYNTHESIS_PROMPT) for message in delivered[1:])
        assert slot._pending_synthesis is False

    @pytest.mark.asyncio
    async def test_app_token_is_denied_even_with_route_access(
        self,
        tmp_path: Path,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        async with TestClient(TestServer(self._app(service, app_claim="untrusted-app"))) as client:
            for method, path in (
                ("get", "/api/kiro-prerequisite"),
                ("post", "/api/kiro-prerequisite/repair-specs"),
                ("post", "/api/kiro-prerequisite/update-cli"),
            ):
                response = await getattr(client, method)(path)
                assert response.status == 403

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_user_gets_only_redacted_readiness(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        app = self._app(
            service,
            app_claim="",
            user="allowed-slack-user",
            owner_id="configured-owner",
        )

        async def ready_snapshot(*, force: bool = False, coalesce: bool = False) -> dict[str, Any]:
            del force, coalesce
            return {
                "platform": "Linux",
                "installed": True,
                "authenticated": True,
                "ready": True,
                "initial_setup_complete": True,
                "repair_required": False,
                "docs_url": OFFICIAL_INSTALL_DOCS_URL,
                "operation": {
                    "kind": "login",
                    "status": "succeeded",
                    "message": "host detail",
                    "detail": "host output",
                    "url": "https://app.kiro.dev/device",
                    "error": "",
                },
            }

        monkeypatch.setattr(service, "snapshot", ready_snapshot)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/kiro-prerequisite")
            assert response.status == 200
            body = await response.json()
            assert body["ready"] is True
            assert body["initial_setup_complete"] is True
            assert body["setup_allowed"] is False
            assert body["platform"] == "gateway"
            # Present but empty: the payload shape must not vary by caller, and
            # only the owner can act on a missing spec.
            assert body["missing_agent_specs"] == []
            assert body["agent_spec_repair_error"] == ""
            # Both sign-in commands are served here too. The redacted branch
            # hardcodes its fields, so a field added only to the dataclass would
            # reach the owner and leave this caller's client reading undefined.
            assert body["login_command"] == KIRO_CLI_LOGIN_COMMAND
            assert body["sso_login_command"] == KIRO_CLI_SSO_LOGIN_COMMAND
            assert body["bundled_cli"] is False
            # Redacted-but-present for the same reason as the sandbox keys: whether
            # the probe timed out describes how slow the HOST is. Asserted here
            # because the hazard the comment above names is not hypothetical -- this
            # branch is hand-built, so the key has to be added by hand with it.
            assert body["probe_timed_out"] is False
            # The pre-upgrade-tab shim is served to every caller, so the payload
            # shape does not vary by who asks.
            assert body["operation"]["status"] == "idle"

            # The repair route is a mutation on the agent home, so it is
            # owner-gated. The update-cli route spawns the CLI's self-update, an
            # equally owner-only host mutation.
            for method, path in (
                ("post", "/api/kiro-prerequisite/repair-specs"),
                ("post", "/api/kiro-prerequisite/update-cli"),
            ):
                response = await getattr(client, method)(path)
                assert response.status == 403

    @pytest.mark.asyncio
    async def test_probe_failure_backstop_preserves_returning_user_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed probe must not demote a returning user to first-run.

        A last-resort backstop reporting ``initial_setup_complete=False`` makes
        the SPA restore the
        full-screen first-run setup gate for someone who has used the app for
        months. Carry the construction-time bit through instead.
        """

        data_home = tmp_path / "data-home"
        session = data_home / "sessions" / "existing.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text('{"role":"user"}\n', encoding="utf-8")
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=data_home,
            audit_writer=_no_audit,
        )

        async def exploding_snapshot(*, force: bool = False) -> dict[str, Any]:
            del force
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(service, "snapshot", exploding_snapshot)

        app = self._app(service, app_claim="")
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/kiro-prerequisite")
            assert response.status == 200
            body = await response.json()
            assert body["ready"] is False
            assert body["initial_setup_complete"] is True

    @pytest.mark.asyncio
    async def test_local_bootstrap_identity_is_owner_when_unconfigured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        async def empty_snapshot(*, force: bool = False) -> dict[str, Any]:
            del force
            return {}

        monkeypatch.setattr(service, "snapshot", empty_snapshot)

        app = self._app(
            service,
            app_claim="",
            user="local-app",
            owner_id="",
        )
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/kiro-prerequisite")).status == 200


class TestKiroCrewNeverSetsUpKiroCli:
    """Kiro Crew DETECTS Kiro CLI. It never installs it and never signs in.

    These are contract tests, not behavior tests: they pin the ABSENCE of both
    setup capabilities. The removed install path downloaded a remote shell script
    and executed it unsandboxed, with a digest pin that silently broke setup
    whenever Kiro republished the script. The removed login path spawned a
    credential-writing child process on the user's behalf. Both belong to Kiro
    CLI, and a change that reintroduces either — or an unsandboxed spawn hook to
    build one on — fails here.
    """

    def test_no_login_execution_surface_exists(self) -> None:
        for attribute in (
            "extract_secure_login_url",
            "_TRUSTED_LOGIN_HOSTS",
            "_LOGIN_TIMEOUT_SECS",
            "OperationStatus",
        ):
            assert not hasattr(prerequisite_module, attribute), attribute
        for method in ("start_login", "_login", "_capture_operation_output"):
            assert not hasattr(KiroPrerequisiteService, method), method

    @pytest.mark.asyncio
    async def test_identity_probe_gets_idp_budget_and_version_stays_short(
        self,
        tmp_path: Path,
    ) -> None:
        # whoami may refresh an OIDC token against an organization IdP (IAM
        # Identity Center), a 12-20s round trip, so it carries its own budget.
        # --version is the FIRST execution of an unknown candidate and must
        # keep the short leash, or a missing/hung binary blocks the gate for
        # the full identity budget.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.authenticated = True

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
        )
        await service.snapshot(force=True)

        budgets = {
            tuple(args): kwargs["timeout_secs"]
            for (_, args), kwargs in zip(runtime.calls, runtime.kwargs)
        }
        assert budgets[("--version",)] == prerequisite_module._PROBE_TIMEOUT_SECS
        assert budgets[("whoami",)] == prerequisite_module._IDENTITY_PROBE_TIMEOUT_SECS
        assert (
            prerequisite_module._IDENTITY_PROBE_TIMEOUT_SECS
            > prerequisite_module._PROBE_TIMEOUT_SECS
        )

    @pytest.mark.asyncio
    async def test_slow_idp_whoami_inside_new_budget_resolves_authenticated(
        self,
        tmp_path: Path,
    ) -> None:
        # A whoami that needs longer than the old shared ceiling but fits the
        # identity budget must resolve as authenticated. The runner times out
        # iff the granted budget cannot cover an Identity Center token refresh
        # (measured at 12-20s), which is exactly what the real subprocess does.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        idp_refresh_secs = 20.0

        async def run(command: str, args: list[str], **kwargs: Any) -> ProcessResult:
            del command
            if args == ["--version"]:
                return ProcessResult(ok=True)
            if args == ["whoami"]:
                if kwargs["timeout_secs"] <= idp_refresh_secs:
                    return ProcessResult(ok=False, timed_out=True, error="process timed out")
                return ProcessResult(ok=True)
            return ProcessResult(ok=False)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )
        status = await service.snapshot(force=True)
        assert status["authenticated"] is True
        assert status["ready"] is True

    @pytest.mark.asyncio
    async def test_auto_poll_serves_latched_state_while_slow_probe_runs(
        self,
        tmp_path: Path,
    ) -> None:
        # The identity probe can legitimately run for the full IdP budget. A
        # machine poll arriving mid-probe must answer immediately from the
        # latched state — queueing behind _probe_lock would freeze the gate's
        # pane for the probe's remaining duration, in every open tab.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        probe_entered = asyncio.Event()
        release_probe = asyncio.Event()
        slow = {"active": False}
        calls: list[list[str]] = []
        clock = {"now": 1_000.0}

        async def run(command: str, args: list[str], **kwargs: Any) -> ProcessResult:
            del command, kwargs
            calls.append(args)
            if slow["active"] and args == ["whoami"]:
                probe_entered.set()
                await release_probe.wait()
            return ProcessResult(ok=True)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
            clock=lambda: clock["now"],
        )
        # Latch a fast, healthy answer first.
        first = await service.snapshot(force=True)
        assert first["ready"] is True

        # Past the floor, a slow probe starts and hangs inside whoami.
        clock["now"] += prerequisite_module._FORCED_PROBE_FLOOR_SECS + 1
        slow["active"] = True
        in_flight = asyncio.create_task(service.snapshot(force=True, coalesce=True))
        await asyncio.wait_for(probe_entered.wait(), timeout=2)

        # Machine polls answer promptly from the latch, spawning nothing.
        spawns_before = len(calls)
        polled = await asyncio.wait_for(service.snapshot(force=True, coalesce=True), timeout=1)
        assert polled["ready"] is True
        assert len(calls) == spawns_before

        release_probe.set()
        await asyncio.wait_for(in_flight, timeout=2)

    @pytest.mark.asyncio
    async def test_auto_poll_is_coalesced_but_check_again_always_probes(
        self,
        tmp_path: Path,
    ) -> None:
        # The blocking first-run gate polls with refresh=1 every 5s, and each
        # browser tab polls independently. Without a floor, N tabs mean N times
        # the kiro-cli spawns; with it they collapse onto one probe per interval.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.installed = True
        runtime.authenticated = True
        clock = {"now": 1_000.0}

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: clock["now"],
        )

        await service.snapshot(force=True, coalesce=True)
        first = len(runtime.calls)
        assert first > 0, "the first forced probe must actually run"

        # Three more tabs poll within the floor: no new spawns.
        for _ in range(3):
            await service.snapshot(force=True, coalesce=True)
        assert len(runtime.calls) == first

        # A HUMAN Check again is never coalesced — a button that returns a cached
        # answer looks broken, so it probes even inside the floor.
        await service.snapshot(force=True)
        explicit = len(runtime.calls)
        assert explicit > first

        # Past the floor, the machine poll runs again too.
        clock["now"] += prerequisite_module._FORCED_PROBE_FLOOR_SECS
        await service.snapshot(force=True, coalesce=True)
        assert len(runtime.calls) > explicit

    @pytest.mark.asyncio
    async def test_simultaneous_auto_polls_collapse_to_one_probe(
        self,
        tmp_path: Path,
    ) -> None:
        # The floor in snapshot() is read OUTSIDE _probe_lock, so tabs polling in
        # the same instant all see the same stale timestamp and all pass it. What
        # stops N tabs from becoming N probes is handing the machine poll
        # force=False, so _probe's cache recheck -- which runs inside the lock,
        # after the winner refreshed the timestamp -- drops the queued callers.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.installed = True
        runtime.authenticated = True
        # Frozen clock: every caller reads an identical, floor-passing age, which
        # is the burst this guards against.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: 5_000.0,
        )

        await asyncio.gather(*(service.snapshot(force=True, coalesce=True) for _ in range(6)))

        # Exactly one probe's worth of spawns: --version, whoami, then the
        # acp-subcommand support check.
        assert [args for _, args in runtime.calls] == [
            ["--version"],
            ["whoami"],
            ["acp", "--help"],
        ]

    def test_status_names_the_command_the_user_runs(self) -> None:
        # The UI needs the command to show, and the user runs it themselves.
        assert KIRO_CLI_LOGIN_COMMAND == "kiro-cli login"
        assert PrerequisiteStatus(platform="Linux").login_command == KIRO_CLI_LOGIN_COMMAND

    def test_status_names_the_organization_sso_command(self) -> None:
        # Both flags are pinned because either one alone silently does nothing:
        # kiro-cli discards every login flag unless --use-device-flow is set (or
        # the environment is already remote), so --license pro on its own falls
        # through to the browser portal where a free Builder ID is a peer option
        # -- the exact wrong-tier sign-in this command exists to rule out.
        assert KIRO_CLI_SSO_LOGIN_COMMAND == "kiro-cli login --use-device-flow --license pro"
        assert "--use-device-flow" in KIRO_CLI_SSO_LOGIN_COMMAND
        assert "--license pro" in KIRO_CLI_SSO_LOGIN_COMMAND
        assert PrerequisiteStatus(platform="Linux").sso_login_command == KIRO_CLI_SSO_LOGIN_COMMAND

    @pytest.mark.asyncio
    async def test_payload_keeps_an_idle_operation_for_pre_upgrade_tabs(
        self,
        tmp_path: Path,
    ) -> None:
        # No operation exists any more, but a dashboard loaded BEFORE this change
        # reads status.operation.status unconditionally (its optional chain guards
        # `status`, not `operation`) in a callback that runs for EVERY user. A tab
        # open across a gateway upgrade must not throw on its next poll, so the
        # payload keeps a permanently idle object.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
            assume_ready=True,
        )

        payload = await service.snapshot()

        assert payload["operation"] == {
            "kind": "",
            "status": "idle",
            "message": "",
            "detail": "",
            "url": "",
            "error": "",
        }
        # A fresh dict per call: a caller mutating one payload cannot poison the next.
        other = await service.snapshot()
        assert other["operation"] is not payload["operation"]

    def test_status_advertises_no_startable_action(self) -> None:
        status = PrerequisiteStatus(platform="Linux")
        for gone in ("can_login", "can_auto_install"):
            assert not hasattr(status, gone), gone

    def test_every_spawn_is_a_read_only_probe(self) -> None:
        # Only --version and whoami remain, so no spawn can mutate the user's
        # machine or write a credential.
        # The facade composes its owners, so its code is the facade's text plus theirs.
        source = "\n".join(
            Path(module.__file__).read_text(encoding="utf-8")
            for module in (
                prerequisite_module,
                *map(importlib.import_module, prerequisite_module._OWNERS),
            )
        )
        assert '"--version"' in source
        assert '"whoami"' in source
        # Matched as a STANDALONE QUOTED TOKEN, the shape a flag has when it is an
        # element of a spawn argv (and the same shape the two assertions above
        # use). The bare substring would also hit the sign-in command this module
        # SERVES for the user to type, which is a display string that is never
        # executed -- so matching it would forbid naming the flag while leaving
        # the actual "what gets spawned" question untested. The behavioral
        # guarantee is pinned separately by the argv-equality assertion in
        # test_repeated_forced_snapshots_probe_once.
        assert '"--use-device-flow"' not in source
        assert '"login"' not in source

    def test_no_installer_download_or_execution_surface_exists(self) -> None:
        for attribute in (
            "OFFICIAL_INSTALL_URL",
            "OFFICIAL_WINDOWS_INSTALL_URL",
            "_INSTALLER_SHA256",
            "_download_installer",
            "validate_installer_script",
            "official_installer_command",
            "_trusted_installer_url",
            "_trusted_installer_path",
            "_installer_proxy",
            "InstallerDownloader",
        ):
            assert not hasattr(prerequisite_module, attribute), attribute

    def test_service_exposes_no_install_operation(self) -> None:
        assert not hasattr(KiroPrerequisiteService, "start_install")
        assert not hasattr(KiroPrerequisiteService, "_install")
        assert not hasattr(KiroPrerequisiteService, "_attest_candidate")

    def test_status_carries_no_auto_install_capability(self) -> None:
        assert not hasattr(PrerequisiteStatus(platform="Linux"), "can_auto_install")

    def test_docs_url_points_at_kiros_official_setup_page(self) -> None:
        assert OFFICIAL_INSTALL_DOCS_URL == "https://kiro.dev/cli/"
        assert PrerequisiteStatus(platform="Linux").docs_url == OFFICIAL_INSTALL_DOCS_URL

    def test_process_runner_cannot_be_asked_for_an_unsandboxed_spawn(self) -> None:
        # `sandboxed=False` and `stdin_data` existed only to feed the installer
        # script to an unsandboxed interpreter. Every spawn left is sandboxed, so
        # there is no parameter through which to request otherwise.
        parameters = inspect.signature(_run_process).parameters
        assert "sandboxed" not in parameters
        assert "stdin_data" not in parameters
