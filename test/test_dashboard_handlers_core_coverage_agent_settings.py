"""Coverage for ``dashboard/handlers/core.py``: agent settings writes and session sub-agent routes.

The agent-settings PUT (``/api/config/kirocrew``) validators, the PATCH validators the
editable-field table does not reach, and the session sub-agent list / result routes
with their path-id validation.

Style and isolation follow ``test_dashboard_handlers_core_coverage.py``: direct
handler calls against a ``MagicMock(spec=web.Request)``, a real aiohttp ``TestClient``
where the handler needs a genuine request body or streaming response, and every write
under the autouse-isolated ``KIROCREW_HOME`` from ``conftest.py``; nothing here touches
the network, downloads a model, or spawns a process. Shared stubs and fixtures come
from ``dashboard_handlers_core_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_handlers_core_coverage_helpers import fake_sel_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import seeded_config_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import _req

from kiro_crew.config.loader import (
    MAX_SUBAGENTS_FIXED_FLOOR,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
)
from kiro_crew.dashboard.handlers import core as core_mod

# ── Agent settings PUT (/api/config/kirocrew) ───────────────────────────


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    return await handler(request)


def _agent_cfg_app() -> web.Application:
    app = web.Application(middlewares=[_owner_identity])
    state = MagicMock()
    state.owner_id = ""
    app["state"] = state
    app.router.add_route("*", "/api/config/kirocrew", core_mod.api_kirocrew_config)
    return app


async def _put_agent(client, settings: dict):
    return await client.put("/api/config/kirocrew", json={"agent": settings})


class TestAgentSettingsPut:
    @pytest.mark.asyncio
    async def test_malformed_body_is_denied_and_audited(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await client.put("/api/config/kirocrew", data=b"{{{")
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON"
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_missing_agent_object_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await client.put("/api/config/kirocrew", json={"agent": "nope"})
            assert resp.status == 400
            assert (await resp.json())["error"] == "agent must be an object"

    @pytest.mark.asyncio
    async def test_corrupt_config_is_500_not_a_silent_reset(self, seeded_config, fake_sel) -> None:
        seeded_config.write_text("<<not json>>", encoding="utf-8", newline="\n")
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": 5})
            assert resp.status == 500
            assert (await resp.json())["error"] == "config.json is corrupt"
        assert seeded_config.read_text(encoding="utf-8") == "<<not json>>"

    @pytest.mark.asyncio
    async def test_a_stale_plaintext_on_disk_does_not_brick_an_unrelated_put(
        self, seeded_config, fake_sel
    ) -> None:
        """A plaintext ``agent.deepseek_env`` value an older build landed is not this
        write's doing: the publish floor lets a write that leaves the mapping as it
        found it through (naming the stale key on the log), so a settings change
        that never touched that field is a 200, not a 500 with a traceback."""
        stale = {"DEEPSEEK_API_KEY": "sk-live-not-a-reference"}
        seeded_config.write_text(
            json.dumps({"agent": {"approval_mode": "auto", "deepseek_env": stale}}) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": 5})
            assert resp.status == 200
        written = json.loads(seeded_config.read_text(encoding="utf-8"))
        assert written["agent"]["subagent_max_turns"] == 5
        assert written["agent"]["deepseek_env"] == stale, "the write left the mapping as it was"

    @pytest.mark.asyncio
    async def test_a_write_the_publish_floor_refuses_is_a_clean_coded_400(
        self, seeded_config, fake_sel, monkeypatch
    ) -> None:
        """When the floor does refuse a document, the PUT arm answers the operator's
        one-line instruction with a machine-readable code -- never an escaped
        ``ValueError``. The field is not editable through this surface today, so the
        refusal is raised the way the floor raises it rather than provoked through
        the body."""
        from kiro_crew.config import loader as loader_mod
        from kiro_crew.config.loader import ConfigWriteRefused

        message = (
            "agent.deepseek_env entry 'DEEPSEEK_API_KEY' holds a literal value, so the "
            "config write was refused: this mapping takes a 'secret://<vault name>' "
            "reference only."
        )

        def refuse(*_args, **_kwargs):
            raise ConfigWriteRefused(message)

        monkeypatch.setattr(loader_mod, "update_config_locked", refuse)
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": 5})
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "config_write_refused"
            assert body["error"] == message
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_out_of_range_turns_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": SUBAGENT_MAX_TURNS_CEILING + 1})
            assert resp.status == 400
            assert "between 1 and" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_boolean_is_not_an_integer(self, seeded_config, fake_sel) -> None:
        """``True`` is an ``int`` subclass in Python — the validator must still
        refuse it, or a JSON ``true`` would silently persist as 1."""
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": True})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_auto_max_above_ceiling_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_auto_max": SUBAGENT_AUTO_MAX_CEILING + 1})
            assert resp.status == 400
            assert "subagent_auto_max must be an integer" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_same_request_ceiling_raise_cannot_widen_the_pin(
        self, seeded_config, fake_sel
    ) -> None:
        """Deny-by-default: ``{subagent_auto_max: N, max_subagents: N}`` must not
        let one request raise the ceiling and immediately spend it."""
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(
                client,
                {
                    "subagent_auto_max": SUBAGENT_AUTO_MAX_CEILING,
                    "max_subagents": SUBAGENT_AUTO_MAX_CEILING,
                },
            )
            assert resp.status == 400
            assert "max_subagents must be 0 (auto)" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_corrupt_persisted_ceiling_is_clamped(self, seeded_config, fake_sel) -> None:
        """A hand-edited ceiling must not be trusted to widen the bound."""
        seeded_config.write_text(
            json.dumps({"agent": {"subagent_auto_max": 9999}}),
            encoding="utf-8",
            newline="\n",
        )
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"max_subagents": 9999})
            assert resp.status == 400
            error = (await resp.json())["error"]
            assert f"and {SUBAGENT_AUTO_MAX_CEILING}" in error

    @pytest.mark.asyncio
    async def test_fixed_pin_below_floor_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"max_subagents": MAX_SUBAGENTS_FIXED_FLOOR - 1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_auto_sentinel_is_accepted(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"max_subagents": 0})
            assert resp.status == 200
            # The cap follows config live (SubagentManager.reconfigure), so the
            # auto sentinel is applied at the next reload rather than at restart.
            assert (await resp.json())["restart_required"] is False
        assert json.loads(seeded_config.read_text(encoding="utf-8"))["agent"]["max_subagents"] == 0

    @pytest.mark.asyncio
    async def test_empty_settings_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"unknown_key": 1})
            assert resp.status == 400
            assert (await resp.json())["error"] == "no recognized settings provided"

    @pytest.mark.asyncio
    async def test_resent_unchanged_value_does_not_ask_for_a_restart(
        self, seeded_config, fake_sel
    ) -> None:
        """The dashboard sends all settings on every save, so "was applied" is
        not "was changed" — the restart hint has to stay trustworthy."""
        seeded_config.write_text(
            json.dumps({"agent": {"subagent_max_turns": 9}}),
            encoding="utf-8",
            newline="\n",
        )
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": 9})
            assert resp.status == 200
            assert (await resp.json()) == {"ok": True, "restart_required": False}

    @pytest.mark.asyncio
    async def test_get_drops_edition_contributed_sections(self, seeded_config) -> None:
        """Unknown top-level sections exist only for the save round-trip; the
        browser-facing view must omit them so an edition secret cannot leak."""
        seeded_config.write_text(
            json.dumps({"agent": {}, "some_edition": {"api_key": "s3cret"}}),
            encoding="utf-8",
            newline="\n",
        )
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            body = await (await client.get("/api/config/kirocrew")).json()
        assert "some_edition" not in body
        assert "s3cret" not in json.dumps(body)
        assert "agent" in body


class TestAgentSettingsPutLockOffload:
    """Regression tests for the locked + offloaded read-modify-write path.

    Before the fix the PUT handler called ``path.read_text`` / ``os.replace``
    directly on the event loop, racing concurrent writers (lost-write) and
    blocking the loop.  After the fix the write goes through
    ``update_config_locked`` under ``_get_config_lock``, making two
    concurrent PUTs serialize rather than clobber each other.
    """

    @pytest.mark.asyncio
    async def test_concurrent_puts_serialize_not_clobber(self, seeded_config, fake_sel) -> None:
        """Two concurrent PUTs must both land — neither write clobbers the other.

        We fire two coroutines simultaneously: one sets ``subagent_max_turns=3``
        and one sets ``max_subagents=4``.  Because the RMW is now serialized
        under the config lock, the config file must end up with BOTH values
        after both coroutines complete, not just the last one written.
        """
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            r1, r2 = await asyncio.gather(
                _put_agent(client, {"subagent_max_turns": 3}),
                _put_agent(client, {"max_subagents": 0}),
            )
        assert r1.status == 200
        assert r2.status == 200
        persisted = json.loads(seeded_config.read_text(encoding="utf-8"))["agent"]
        # Both writes must have survived — a lost-write would drop one of them.
        assert persisted.get("subagent_max_turns") == 3
        assert persisted.get("max_subagents") == 0

    @pytest.mark.asyncio
    async def test_write_goes_through_atomic_helper(
        self, seeded_config, fake_sel, monkeypatch
    ) -> None:
        """The write path must call ``update_config_locked``, not a bare
        ``write_text`` / ``os.replace``.  Patching the helper to a spy lets us
        assert it was invoked while still allowing the real write to complete."""
        import kiro_crew.config.loader as _loader

        calls: list[str] = []
        _real = _loader.update_config_locked

        def _spy(path=None, *, mutate, **kw):
            calls.append("update_config_locked")
            return _real(path, mutate=mutate, **kw)

        monkeypatch.setattr(_loader, "update_config_locked", _spy)
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": 5})
        assert resp.status == 200
        assert calls == [
            "update_config_locked"
        ], "PUT did not route through update_config_locked — lost-write race still present"
        assert (
            json.loads(seeded_config.read_text(encoding="utf-8"))["agent"]["subagent_max_turns"]
            == 5
        )


# ── PATCH validators not reachable through the editable-field table ─────


class TestPatchGuards:
    @pytest.mark.asyncio
    async def test_moved_field_names_its_replacement_endpoint(
        self, seeded_config, fake_sel
    ) -> None:
        """A dead end ("not editable") becomes a next step for fields whose
        side effects the generic write cannot reproduce."""
        app = web.Application(middlewares=[_owner_identity])
        app["state"] = SimpleNamespace(owner_id="")
        app.router.add_patch("/api/config/kirocrew", core_mod.api_kirocrew_config_patch)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.apps_allow_third_party", "value": False},
            )
            assert resp.status == 400
            assert "trusted-apps/allow-all" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_unknown_field_is_refused(self, seeded_config, fake_sel) -> None:
        app = web.Application(middlewares=[_owner_identity])
        app["state"] = SimpleNamespace(owner_id="")
        app.router.add_patch("/api/config/kirocrew", core_mod.api_kirocrew_config_patch)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/config/kirocrew", json={"path": "agent.nope", "value": 1}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "field not editable: agent.nope"


class TestFallbackModelPatch:
    """agent.fallback_model — single-value str spec with role-model validation."""

    def _app(self) -> web.Application:
        # PATCH /api/config/kirocrew is owner-gated: supply the same signed
        # local-owner identity the sibling patch tests use.
        app = web.Application(middlewares=[_owner_identity])
        app["state"] = SimpleNamespace(owner_id="")
        app.router.add_patch("/api/config/kirocrew", core_mod.api_kirocrew_config_patch)
        return app

    @pytest.mark.asyncio
    async def test_accepts_a_model_id(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": "claude-opus-4.8"},
            )
            assert resp.status == 200
        assert (
            json.loads(seeded_config.read_text(encoding="utf-8"))["agent"]["fallback_model"]
            == "claude-opus-4.8"
        )

    @pytest.mark.asyncio
    async def test_accepts_auto(self, seeded_config, fake_sel) -> None:
        # "auto" always allows — _validate_role_model's defer-to-default case.
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": "auto"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_accepts_empty_feature_off(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": ""},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_rejects_non_string(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": ["claude-opus-5"]},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_bad_grammar(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": "model; rm -rf /"},
            )
            assert resp.status == 400
            assert "invalid value" in (await resp.json())["error"]

    def _app_with_session(self, provider: object) -> web.Application:
        app = web.Application(middlewares=[_owner_identity])
        app["state"] = SimpleNamespace(
            owner_id="", sessions=SimpleNamespace(active_providers=lambda: [provider])
        )
        app.router.add_patch("/api/config/kirocrew", core_mod.api_kirocrew_config_patch)
        return app

    @pytest.mark.asyncio
    async def test_pin_denied_by_a_stale_snapshot_is_revalidated_first(
        self, seeded_config, fake_sel
    ) -> None:
        """The PATCH revalidates the live snapshot before the synchronous
        validator reads it, so a startup-race answer cannot deny an entitled pin."""
        rows = [{"modelId": "auto"}]

        class _Healing:
            client = SimpleNamespace(backend="")  # the default harness the pin runs on

            async def maybe_refresh_available_models(self, catalog_ids):
                assert catalog_ids[0] == "claude-opus-5"
                rows.append({"modelId": "claude-opus-5"})
                return list(rows)

            def available_models(self):
                return list(rows)

        async with TestClient(TestServer(self._app_with_session(_Healing()))) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": "claude-opus-5"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_pin_is_refused_while_its_revalidation_is_in_flight(
        self, seeded_config, fake_sel
    ) -> None:
        from kiro_crew.agent_sdk.drivers.acp import EntitlementRevalidating

        class _Pending:
            client = SimpleNamespace(backend="")

            async def maybe_refresh_available_models(self, _catalog_ids):
                raise EntitlementRevalidating

            def available_models(self):
                return [{"modelId": "auto"}]

        async with TestClient(TestServer(self._app_with_session(_Pending()))) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": "claude-opus-5"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == core_mod._ROLE_PIN_REVALIDATING

    @pytest.mark.asyncio
    async def test_newer_session_on_another_harness_never_judges_the_pin(
        self, seeded_config, fake_sel
    ) -> None:
        """Newest-first evidence is scoped to the harness the pin runs on (the
        default backend, kiro): a Claude member DM created later is the newest
        live session, but its catalog cannot reject a kiro id the account has."""

        class _Session:
            def __init__(self, backend: str, ids: list[str]) -> None:
                self.client = SimpleNamespace(backend=backend)
                self._rows = [{"modelId": m} for m in ids]

            def available_models(self):
                return list(self._rows)

            async def maybe_refresh_available_models(self, _catalog_ids):
                return list(self._rows)

        kiro = _Session("", ["auto", "claude-opus-5"])
        claude_member = _Session("claude", ["claude-opus-4-1", "claude-sonnet-4-5"])
        app = web.Application(middlewares=[_owner_identity])
        app["state"] = SimpleNamespace(
            owner_id="",
            sessions=SimpleNamespace(active_providers=lambda: [kiro, claude_member]),
        )
        app.router.add_patch("/api/config/kirocrew", core_mod.api_kirocrew_config_patch)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": "claude-opus-5"},
            )
            assert resp.status == 200, await resp.text()


class TestAdvertisedModelGuards:
    def test_unknown_when_no_session_has_initialised(self) -> None:
        assert core_mod._active_advertised_ids(_req(app={})) is None

    def test_provider_without_a_model_getter_is_skipped(self) -> None:
        state = SimpleNamespace(
            sessions=SimpleNamespace(active_providers=lambda: [SimpleNamespace()])
        )
        assert core_mod._active_advertised_ids(_req(app={"state": state})) is None

    def test_raising_getter_does_not_propagate(self) -> None:
        def _boom():
            raise RuntimeError("provider is mid-restart")

        state = SimpleNamespace(
            sessions=SimpleNamespace(
                active_providers=lambda: [SimpleNamespace(available_models=_boom)]
            )
        )
        assert core_mod._active_advertised_ids(_req(app={"state": state})) is None

    def test_first_provider_with_ids_wins(self) -> None:
        state = SimpleNamespace(
            sessions=SimpleNamespace(
                active_providers=lambda: [
                    SimpleNamespace(available_models=lambda: []),
                    SimpleNamespace(available_models=lambda: [{"modelId": "claude-sonnet-4.6"}]),
                ]
            )
        )
        ids = core_mod._active_advertised_ids(_req(app={"state": state}))
        assert ids == ["claude-sonnet-4.6"]

    @pytest.mark.parametrize("value", ["", "auto"])
    def test_defer_values_always_allowed(self, value) -> None:
        assert core_mod._validate_role_model(value, _req()) is None

    def test_provider_rejection_is_surfaced(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
            lambda _v, provider=None: "display-only key",
        )
        assert core_mod._validate_role_model("fable-5-1m", _req()) == "display-only key"

    def test_unknown_entitlement_does_not_accuse(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
            lambda _v, provider=None: None,
        )
        assert core_mod._validate_role_model("some-model", _req(app={})) is None

    def test_unentitled_model_lists_usable_alternatives(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
            lambda _v, provider=None: None,
        )
        state = SimpleNamespace(
            sessions=SimpleNamespace(
                active_providers=lambda: [
                    SimpleNamespace(available_models=lambda: [{"modelId": "allowed-1"}])
                ]
            )
        )
        reason = core_mod._validate_role_model("denied-1", _req(app={"state": state}))
        assert reason is not None
        assert "allowed-1" in reason

    def test_pool_agent_values_include_the_clear_sentinel(self) -> None:
        assert "" in core_mod._agent_values()


# ── Session sub-agent routes ────────────────────────────────────────────


class TestSessionAgentRoutes:
    @pytest.mark.asyncio
    async def test_list_returns_workspace_results(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr(
            "kiro_crew.session_workspace.list_results",
            lambda _s: [{"agent_id": "a1", "bytes": 12}],
        )
        resp = await core_mod.api_session_agents_list(_req(match_info={"id": "s1"}))
        assert json.loads(resp.body) == {"results": [{"agent_id": "a1", "bytes": 12}]}
        assert fake_sel.log_api_access.call_args.kwargs["resources"] == "s1"

    @pytest.mark.asyncio
    async def test_missing_result_is_404(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.session_workspace.read_result", lambda *_a: "")
        resp = await core_mod.api_session_agent_result(
            _req(match_info={"id": "s1", "agent_id": "a1"})
        )
        assert resp.status == 404
        assert json.loads(resp.body)["error"] == "not found"

    @pytest.mark.asyncio
    async def test_result_is_returned_after_redaction(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr(
            "kiro_crew.session_workspace.read_result", lambda *_a: "finished the audit"
        )
        resp = await core_mod.api_session_agent_result(
            _req(match_info={"id": "s1", "agent_id": "a1"})
        )
        body = json.loads(resp.body)
        assert body == {"agent_id": "a1", "content": "finished the audit"}

    @pytest.mark.asyncio
    async def test_stream_emits_the_tail_then_a_done_event(
        self, monkeypatch, fake_sel, tmp_path
    ) -> None:
        result = tmp_path / "agent-a1.md"
        result.write_text("partial output\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr("kiro_crew.session_workspace.result_path", lambda *_a: result)

        app = web.Application()
        app["state"] = SimpleNamespace(
            subagents=SimpleNamespace(get=lambda _a: SimpleNamespace(done=True))
        )
        app.router.add_get(
            "/api/sessions/{id}/agents/{agent_id}/stream", core_mod.api_session_agent_stream
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/sessions/s1/agents/a1/stream")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            text = await resp.text()
        assert "partial output" in text
        assert "event: done" in text

    @pytest.mark.asyncio
    async def test_stream_stops_when_the_client_disconnects(self, monkeypatch, fake_sel) -> None:
        """A reset peer must end the loop, not spin for the full 20 minutes."""

        def _reset(_self):
            raise ConnectionResetError("peer went away")

        monkeypatch.setattr("kiro_crew.session_workspace.result_path", lambda *_a: Path("unused"))
        monkeypatch.setattr("kiro_crew.session_workspace_tail.ResultTail.read_new", _reset)
        app = web.Application()
        app["state"] = SimpleNamespace(subagents=None)
        app.router.add_get(
            "/api/sessions/{id}/agents/{agent_id}/stream", core_mod.api_session_agent_stream
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/sessions/s1/agents/a1/stream")
            assert resp.status == 200
            assert await resp.text() == ""


class TestSessionAgentPathIdValidation:
    """A malformed path id must answer 400, not crash with a 500.

    ``session_workspace`` validates both ids with ``_validate_id``, which RAISES
    ``ValueError``. These three handlers called into it with no ``try``, so an id
    outside ``^[a-zA-Z0-9_.:-]+$`` -- or the literal ``..`` -- escaped as an
    unhandled exception. Every other test in this file monkeypatches
    ``list_results`` / ``read_result`` / ``result_path``, so the real validator
    never ran and the gap stayed invisible; nothing here patches them.

    ``messaging.py`` is the in-tree pattern for the same seam: ``read_state``
    wraps ``_agent_dir`` in ``try/except ValueError`` and answers ``None``.
    """

    #: Rejected by ``_validate_id``: a space is outside the charset, and ``..``
    #: is refused by name. Both survive aiohttp routing -- the default pattern
    #: for ``{id}`` is ``[^{}/]+``, which admits a space and admits ``..``.
    BAD_IDS = ("bad id", "..", "a\\b", "a%b")

    @pytest.mark.parametrize("bad", BAD_IDS)
    @pytest.mark.asyncio
    async def test_list_refuses_a_malformed_session_id(self, bad, fake_sel) -> None:
        resp = await core_mod.api_session_agents_list(_req(match_info={"id": bad}))
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_session_id"

    @pytest.mark.parametrize("bad", BAD_IDS)
    @pytest.mark.asyncio
    async def test_result_refuses_a_malformed_session_id(self, bad, fake_sel) -> None:
        resp = await core_mod.api_session_agent_result(
            _req(match_info={"id": bad, "agent_id": "a1"})
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_session_id"

    @pytest.mark.parametrize("bad", BAD_IDS)
    @pytest.mark.asyncio
    async def test_result_refuses_a_malformed_agent_id(self, bad, fake_sel) -> None:
        """The second param needs its own code -- ``read_result`` validates the
        session id first, so a caller told only "invalid session id" would go
        fix the wrong half of the URL."""
        resp = await core_mod.api_session_agent_result(
            _req(match_info={"id": "s1", "agent_id": bad})
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_agent_id"

    @pytest.mark.asyncio
    async def test_stream_refuses_before_it_prepares_an_sse_response(self, fake_sel) -> None:
        """The refusal has to land while a normal response is still sendable.

        ``result_path`` is called BEFORE ``resp.prepare(request)``, so the guard
        can still answer 400. Once prepared, the status is on the wire and the
        only way to signal a bad id would be an SSE payload no client reads.
        """
        resp = await core_mod.api_session_agent_stream(
            _req(match_info={"id": "bad id", "agent_id": "a1"})
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_session_id"

    # ── over-fixing guards: these pass on the base and must keep passing ──

    @pytest.mark.parametrize("good", ("s1", "dashboard:slot-3", "chat-735-1788268870", "a.b_c"))
    @pytest.mark.asyncio
    async def test_the_accepted_charset_is_not_narrowed(self, good, monkeypatch, fake_sel) -> None:
        """``_SAFE_ID_RE`` admits ``:``, ``.``, ``_`` and ``-``, and real session
        keys use them (``dashboard:slot-3``). A guard that rejected any of these
        would break working URLs, so pin the accepted set rather than only the
        refused one."""
        monkeypatch.setattr("kiro_crew.session_workspace.list_results", lambda _s: [])
        resp = await core_mod.api_session_agents_list(_req(match_info={"id": good}))
        assert resp.status == 200
