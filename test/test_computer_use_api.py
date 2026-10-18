"""Tests for the computer-use REST API (``dashboard/handlers/computer_use.py``).

Three endpoints: the browser-called ``GET``/``PUT /api/computer-use/config`` pair
and the machine-only ``POST /api/computer-use/invoke`` leg the
``kirocrew-computer`` stdio shim forwards to.

Two things this file pins that no other test can:

* the primary enable is written to the KEYSTONE ``computer_use.json`` and NOT to
  ``config.json`` — the whole reason the agent cannot enable its own desktop
  automation;
* the permission probe is an out-of-process shell-out that DEGRADES rather than
  raising, and is never invoked at all on a platform with no driver.

The permission probe is monkeypatched in every case, so no test spawns a real
subprocess. Handlers are exercised through an in-test ``TestClient`` opened with
``async with`` (matching ``test_denied_commands_api.py``) rather than an
async-gen fixture: the CI-pinned ``pytest-asyncio`` is incompatible with the
pinned ``pytest`` for async fixtures.

This module pins the config GET and PUT, the permission probe, the invoke leg and its
loopback secret, the route wiring, the config section and the machine-readable error
codes. The live-view frame ingress and relay live in ``test_computer_use_api_frame.py``;
the keystone save's guards (app tokens, an all-or-nothing mixed save, the session restart
and spec rebuild an enable flip triggers, a malformed keystone) in
``test_computer_use_api_keystone.py``; the shared fixtures and app builders in
``computer_use_api_helpers.py``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from computer_use_api_helpers import (  # noqa: F401 -- fixtures are requested by name
    _GRANTED,
    _VALID_FRAME,
    _client,
    _frame_app,
    _reset_ctx,
    config_file_fixture,
    home_fixture,
    mock_sel_fixture,
    no_agent_spec_rewrite,
    no_real_probe,
    profiles_dir_fixture,
    state_file_fixture,
)

from kiro_crew.computer_use.types import (
    PERMISSION_GRANTED,
    PERMISSION_UNKNOWN,
    PERMISSION_UNSUPPORTED,
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    STATE_KEY_ALLOWED_APPS,
    STATE_KEY_ENABLED,
)
from kiro_crew.dashboard.handlers import computer_use as cu_api

# The REAL probe, captured at import time — the autouse ``no_real_probe`` fixture
# replaces the module attribute, and ``TestPermissionProbe`` needs the genuine
# implementation. Captured rather than reloaded: ``importlib.reload`` would rebind
# the module object the handlers package already holds a reference to.
_REAL_PROBE = cu_api._probe_permissions


# ── GET ──


class TestConfigGet:
    @pytest.mark.asyncio
    async def test_reports_disabled_with_defaults_on_a_fresh_home(self, home):
        async with _client() as client:
            body = await (await client.get("/api/computer-use/config")).json()
        assert body["enabled"] is False
        assert body["max_tree_nodes"] == 1200
        assert body["screenshot_max_px"] == 1280
        assert body["attach_screenshot"] is True
        # CI runs the shipped FAKE backend, whose ``platform_id`` is "fake", so the
        # probe correctly reports "unsupported" (there is no macOS TCC state to
        # read) and the panel hides its Permissions section. The macOS path is
        # covered by ``test_macos_platform_reports_the_probed_grants`` below.
        assert body["platform"] == "fake"
        assert body["permissions"]["accessibility"] == PERMISSION_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_macos_platform_reports_the_probed_grants(self, home):
        from kiro_crew.computer_use.types import BackendStatus

        backend = MagicMock()
        backend.status.return_value = BackendStatus(supported=True, platform_id=PLATFORM_MACOS)
        with patch("kiro_crew.computer_use.backend.get_shared_backend", return_value=backend):
            async with _client() as client:
                body = await (await client.get("/api/computer-use/config")).json()
        assert body["platform"] == PLATFORM_MACOS
        assert body["permissions"] == _GRANTED

    @pytest.mark.asyncio
    async def test_reads_the_enable_from_the_keystone_not_config_json(
        self, state_file: Path, config_file: Path
    ):
        # The enable is ONLY honoured from the keystone: a config.json that claims
        # ``enabled`` must not turn the feature on (that file is agent-writable).
        state_file.write_text(json.dumps({STATE_KEY_ENABLED: True}), encoding="utf-8")
        config_file.write_text(json.dumps({"computer_use": {"enabled": False}}), encoding="utf-8")
        async with _client() as client:
            body = await (await client.get("/api/computer-use/config")).json()
        assert body["enabled"] is True

    @pytest.mark.asyncio
    async def test_a_corrupt_keystone_renders_as_disabled(self, state_file: Path):
        state_file.write_text("{not json", encoding="utf-8")
        async with _client() as client:
            resp = await client.get("/api/computer-use/config")
            body = await resp.json()
        assert resp.status == 200
        assert body["enabled"] is False

    @pytest.mark.asyncio
    async def test_unsupported_platform_reports_reason_and_skips_the_probe(self, home, monkeypatch):
        from kiro_crew.computer_use.backend import UnsupportedBackend

        calls: list[str] = []

        async def _spy(platform_id: str) -> dict:
            calls.append(platform_id)
            return dict(cu_api._permission_block(PERMISSION_UNSUPPORTED))

        monkeypatch.setattr(cu_api, "_probe_permissions", _spy)
        backend = UnsupportedBackend(PLATFORM_LINUX, "the Linux AT-SPI driver is not implemented")
        with patch("kiro_crew.computer_use.backend.get_shared_backend", return_value=backend):
            async with _client() as client:
                body = await (await client.get("/api/computer-use/config")).json()
        assert body["supported"] is False
        assert body["platform"] == PLATFORM_LINUX
        assert "AT-SPI" in body["reason"]
        # The probe still runs (it is the single splice point) but is told the
        # platform, so it neither spawns nor claims a grant is missing.
        assert calls == [PLATFORM_LINUX]
        assert body["permissions"]["accessibility"] == PERMISSION_UNSUPPORTED


class TestPermissionProbe:
    @pytest.mark.asyncio
    async def test_non_macos_never_spawns(self, monkeypatch):
        def _boom(*_a, **_kw):
            raise AssertionError("the probe must not spawn off macOS")

        monkeypatch.setattr("asyncio.create_subprocess_exec", _boom)
        result = await _REAL_PROBE(PLATFORM_LINUX)
        assert result["accessibility"] == PERMISSION_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_a_failed_probe_degrades_to_unknown(self, monkeypatch):
        async def _fail(*_a, **_kw):
            raise OSError("no such binary")

        monkeypatch.setattr("asyncio.create_subprocess_exec", _fail)
        result = await _REAL_PROBE(PLATFORM_MACOS)
        assert result["accessibility"] == PERMISSION_UNKNOWN
        assert result["screen_recording"] == PERMISSION_UNKNOWN

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_degrades_to_unknown(self, monkeypatch):
        proc = MagicMock()
        proc.returncode = 2

        async def _communicate():
            return (b"", b"")

        proc.communicate = _communicate

        async def _spawn(*_a, **_kw):
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", _spawn)
        result = await _REAL_PROBE(PLATFORM_MACOS)
        assert result["accessibility"] == PERMISSION_UNKNOWN

    @pytest.mark.asyncio
    async def test_a_successful_probe_is_parsed(self, monkeypatch):
        payload = json.dumps(
            {
                "permissions": {
                    "accessibility": "missing",
                    "screen_recording": PERMISSION_GRANTED,
                    "responsible_hint": "Grant them to Terminal",
                }
            }
        ).encode()
        proc = MagicMock()
        proc.returncode = 0

        async def _communicate():
            return (payload, b"")

        proc.communicate = _communicate

        async def _spawn(*argv, **_kw):
            # Fixed argv, no shell, nothing request-derived.
            expected = (cu_api.DOCTOR_SUBCOMMAND, *cu_api.DOCTOR_ARGS)
            assert argv[-len(expected) :] == expected
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", _spawn)
        result = await _REAL_PROBE(PLATFORM_MACOS)
        assert result == {
            "accessibility": "missing",
            "screen_recording": PERMISSION_GRANTED,
            "responsible_hint": "Grant them to Terminal",
        }

    @pytest.mark.asyncio
    async def test_a_timed_out_probe_is_killed_and_degrades(self, monkeypatch):
        killed: list[bool] = []
        proc = MagicMock()
        proc.returncode = None
        proc.kill = lambda: killed.append(True)

        async def _hang():
            raise TimeoutError

        proc.communicate = _hang

        async def _spawn(*_a, **_kw):
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", _spawn)
        result = await _REAL_PROBE(PLATFORM_MACOS)
        assert result["accessibility"] == PERMISSION_UNKNOWN
        # A stalled probe MUST be reaped: the panel polls every 5s while a grant
        # is outstanding, so leaking one child per poll would pile up fast.
        assert killed == [True]

    def test_normalize_accepts_both_shapes_and_rejects_junk(self):
        nested = cu_api._normalize_permissions({"permissions": {"accessibility": "missing"}})
        assert nested["accessibility"] == "missing"
        flat = cu_api._normalize_permissions({"accessibility": "granted"})
        assert flat["accessibility"] == PERMISSION_GRANTED
        assert cu_api._normalize_permissions("nope")["accessibility"] == PERMISSION_UNKNOWN
        assert cu_api._normalize_permissions({"accessibility": 7})["accessibility"] == (
            PERMISSION_UNKNOWN
        )


# ── PUT ──


class TestConfigSave:
    @pytest.mark.asyncio
    async def test_enable_writes_the_keystone_and_never_config_json(
        self, state_file: Path, config_file: Path, mock_sel
    ):
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert body["enabled"] is True
        assert json.loads(state_file.read_text(encoding="utf-8"))[STATE_KEY_ENABLED] is True
        # THE assertion: the enable never lands in the agent-writable config.
        if config_file.exists():
            assert "enabled" not in json.loads(config_file.read_text(encoding="utf-8")).get(
                "computer_use", {}
            )
        assert mock_sel.log_api_access.called

    @pytest.mark.asyncio
    async def test_limits_write_config_json_and_not_the_keystone(
        self, state_file: Path, config_file: Path
    ):
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={"max_tree_nodes": 400, "attach_screenshot": False},
            )
            body = await resp.json()
        assert resp.status == 200
        assert body["max_tree_nodes"] == 400
        assert body["attach_screenshot"] is False
        section = json.loads(config_file.read_text(encoding="utf-8"))["computer_use"]
        assert section["max_tree_nodes"] == 400
        assert section["attach_screenshot"] is False
        # THE assertion: the persisted section never grows an ``enabled`` key, not
        # even through the loader's own migration write-back (which serializes
        # ``ComputerUseConfig``, a dataclass that deliberately has no such field).
        assert "enabled" not in section
        assert not state_file.exists()

    @pytest.mark.asyncio
    async def test_limits_write_preserves_unrelated_config_sections(self, config_file: Path):
        # ``_write_limits`` merges into the RAW JSON rather than round-tripping
        # ``KiroCrewConfig.to_dict()``, so an unrelated section keeps whatever the
        # user (or an edition) put there.
        config_file.write_text(
            json.dumps({"agent": {"model": "keep-me"}, "her_section": {"x": 1}}),
            encoding="utf-8",
        )
        async with _client() as client:
            await client.put("/api/computer-use/config", json={"text_limit": 42})
        data = json.loads(config_file.read_text(encoding="utf-8"))
        assert data["agent"]["model"] == "keep-me"
        assert data["her_section"] == {"x": 1}
        assert data["computer_use"]["text_limit"] == 42

    @pytest.mark.asyncio
    async def test_app_lists_are_lowercased_and_deduped(self, state_file: Path):
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={STATE_KEY_ALLOWED_APPS: ["Com.Apple.Preview", "com.apple.preview", " "]},
            )
        assert resp.status == 200
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state[STATE_KEY_ALLOWED_APPS] == ["com.apple.preview"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"enabled": "yes"},
            {"max_tree_nodes": "400"},
            # ``isinstance(True, int)`` is True in Python — a JSON bool must not
            # be accepted as a budget.
            {"max_tree_nodes": True},
            {"max_tree_nodes": 0},
            {"max_tree_nodes": 99999},
            {"screenshot_max_px": 3},
            {"attach_screenshot": 1},
            {STATE_KEY_ALLOWED_APPS: "com.apple.preview"},
            {STATE_KEY_ALLOWED_APPS: [7]},
            {STATE_KEY_ALLOWED_APPS: ["x" * 300]},
            {},
            {"unknown_field": 1},
        ],
    )
    async def test_invalid_bodies_are_400_and_write_nothing(
        self, body, state_file: Path, config_file: Path
    ):
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json=body)
        assert resp.status == 400
        assert not state_file.exists()
        assert not config_file.exists()

    @pytest.mark.asyncio
    async def test_too_many_app_patterns_is_rejected(self, state_file: Path):
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={STATE_KEY_ALLOWED_APPS: [f"app{i}" for i in range(200)]},
            )
        assert resp.status == 400
        assert not state_file.exists()

    @pytest.mark.asyncio
    async def test_a_corrupt_keystone_refuses_the_mutation_without_clobbering(
        self, state_file: Path, mock_sel
    ):
        state_file.write_text("{not json", encoding="utf-8")
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 500
        # The populated-but-unparseable file is left exactly as it was.
        assert state_file.read_text(encoding="utf-8") == "{not json"

    @pytest.mark.asyncio
    async def test_a_corrupt_config_json_refuses_the_limits_write(self, config_file: Path):
        config_file.write_text("{not json", encoding="utf-8")
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"text_limit": 42})
        assert resp.status == 500
        assert config_file.read_text(encoding="utf-8") == "{not json"

    @pytest.mark.asyncio
    async def test_invalid_json_body_is_400(self, home):
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400


class TestInvoke:
    @pytest.mark.asyncio
    async def test_forwards_the_call_and_returns_the_dispatcher_text(self, home, monkeypatch):
        seen: dict = {}

        def _fake_dispatch(tool_name, args, *, session_key, agent="", app="", **_kw):
            seen.update(
                {
                    "tool": tool_name,
                    "args": dict(args),
                    "session_key": session_key,
                    "agent": agent,
                    "app": app,
                }
            )
            return '0 window "Documents"'

        monkeypatch.setattr("kiro_crew.computer_use.tools.dispatch_tool", _fake_dispatch)
        async with _client() as client:
            resp = await client.post(
                "/api/computer-use/invoke",
                json={
                    "tool": "computer_get_state",
                    "args": {"app": "Finder"},
                    "session_key": "dashboard:slot1",
                    "agent": "kirocrew",
                },
            )
            body = await resp.json()
        assert resp.status == 200
        assert body["text"] == '0 window "Documents"'
        assert seen == {
            "tool": "computer_get_state",
            "args": {"app": "Finder"},
            "session_key": "dashboard:slot1",
            "agent": "kirocrew",
            "app": "",
        }

    @pytest.mark.asyncio
    async def test_a_missing_session_key_is_forwarded_as_empty_not_inferred(
        self, home, monkeypatch
    ):
        # The handler must never invent an identity: the gate treats "" as an
        # unattended surface and denies, which is the intended outcome.
        seen: dict = {}

        def _fake_dispatch(tool_name, args, *, session_key, agent="", app="", **_kw):
            seen["session_key"] = session_key
            return "Error: Blocked: the calling session could not be identified"

        monkeypatch.setattr("kiro_crew.computer_use.tools.dispatch_tool", _fake_dispatch)
        async with _client() as client:
            resp = await client.post(
                "/api/computer-use/invoke", json={"tool": "computer_list_apps"}
            )
            body = await resp.json()
        assert seen["session_key"] == ""
        assert body["text"].startswith("Error: ")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{}, {"tool": ""}, {"tool": 7}, {"tool": "computer_click", "args": "nope"}],
    )
    async def test_malformed_requests_are_400(self, body, home, monkeypatch):
        def _boom(*_a, **_kw):
            raise AssertionError("the dispatcher must not be reached")

        monkeypatch.setattr("kiro_crew.computer_use.tools.dispatch_tool", _boom)
        async with _client() as client:
            resp = await client.post("/api/computer-use/invoke", json=body)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_a_raising_dispatcher_becomes_error_text_not_a_5xx(self, home, monkeypatch):
        # The shim relays a TOOL RESULT: a non-200 surfaces as a transport
        # failure the model cannot reason about, whereas "Error: ..." is what the
        # SEL classifier reads as a failed outcome.
        def _raise(*_a, **_kw):
            raise RuntimeError("driver exploded")

        monkeypatch.setattr("kiro_crew.computer_use.tools.dispatch_tool", _raise)
        async with _client() as client:
            resp = await client.post(
                "/api/computer-use/invoke",
                json={"tool": "computer_click", "session_key": "dashboard:slot1"},
            )
            body = await resp.json()
        assert resp.status == 200
        assert body["text"] == cu_api.ERR_DISPATCH_FAILED
        assert "driver exploded" not in body["text"]

    @pytest.mark.asyncio
    async def test_a_body_field_can_never_mint_an_approval(self, home, monkeypatch):
        """``approval_recorded`` is not readable from the request body — ever.

        A body field would be a claim the shim issues to itself. The handler now
        passes a hard ``False`` (the handshake proves kiro-cli is upstream, NOT that
        it prompted — see ``TestApprovalIsNeverInferred``), so this asserts the
        stronger property: sending ``"approval_recorded": true`` changes nothing.
        """
        seen: dict = {}

        def _fake_dispatch(tool_name, args, *, session_key, agent="", app="", **kw):
            seen.update(kw)
            return "ok"

        monkeypatch.setattr("kiro_crew.computer_use.tools.dispatch_tool", _fake_dispatch)
        async with _client() as client:
            await client.post(
                "/api/computer-use/invoke",
                json={
                    "tool": "computer_click",
                    "session_key": "dashboard:slot1",
                    "approval_recorded": True,
                },
            )
        assert seen["approval_recorded"] is False


class TestRouteWiring:
    def test_invoke_is_a_strict_internal_path_and_config_is_not(self):
        """The machine leg is loopback+secret only; the Settings pair is not.

        A cookie fall-through on ``invoke`` would turn any authenticated browser
        page into an entry point for accessibility reads and input synthesis, so
        it must stay in the strict set — while the browser-called config pair must
        stay OUT of it or the panel could not load at all.
        """
        from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

        assert "/api/computer-use/invoke" in _STRICT_INTERNAL_API_PATHS
        assert "/api/computer-use/config" not in _STRICT_INTERNAL_API_PATHS

    def test_the_frame_ingress_is_a_strict_internal_path(self):
        """The PiP ingress is machine-only too.

        Its body is a frame of the operator's own desktop and its only caller is
        this gateway's own capture thread, so a cookie fall-through would let any
        authenticated page (or an app-scoped token) inject frames into every owner
        window's live view.
        """
        from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

        assert "/api/computer-use/frame" in _STRICT_INTERNAL_API_PATHS

    def test_handlers_are_re_exported_from_the_handlers_package(self):
        import kiro_crew.dashboard.handlers as handlers

        assert callable(handlers.api_computer_use_config_get)
        assert callable(handlers.api_computer_use_config_save)
        assert callable(handlers.api_computer_use_frame)
        assert callable(handlers.api_computer_use_invoke)

    def test_editable_config_exposes_budgets_but_never_an_enable(self):
        """The generic config PATCH route must not be able to enable the feature.

        It writes ``config.json``; the enable lives on the keystone. An
        ``computer_use.enabled`` key here would reintroduce exactly the hole the
        keystone exists to close.
        """
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert "computer_use.max_tree_nodes" in _EDITABLE_CONFIG
        assert "computer_use.screenshot_max_px" in _EDITABLE_CONFIG
        assert not any(key.startswith("computer_use.enabled") for key in _EDITABLE_CONFIG)


class TestInvokeSecretEnforcement:
    """The loopback secret is ENFORCED, not merely configured.

    ``TestRouteWiring`` above asserts the route is listed in
    ``_STRICT_INTERNAL_API_PATHS``; membership alone does not prove a request
    without the secret is actually refused. These cases mount the route behind the
    REAL ``token_auth_middleware``, using the production path set, and drive it
    end-to-end — so a middleware change that stopped enforcing the header (or a
    future move of this route into the cookie-fall-through "mixed" bucket) fails
    here rather than silently opening an entry point to accessibility reads and
    input synthesis.
    """

    @staticmethod
    def _authed_client(secret: str) -> TestClient:
        from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS
        from kiro_crew.dashboard.token_auth import token_auth_middleware

        app = web.Application(
            middlewares=[
                token_auth_middleware(
                    internal_paths=_STRICT_INTERNAL_API_PATHS,
                    internal_secret=secret,
                    local_only=True,
                )
            ]
        )
        app.router.add_post("/api/computer-use/invoke", cu_api.api_computer_use_invoke)
        return TestClient(TestServer(app))

    @pytest.fixture(autouse=True)
    def _never_dispatch(self, monkeypatch):
        """The dispatcher must never run for a rejected request.

        Asserted structurally rather than by inspecting the response: a handler
        that ran and then had its result discarded would still have walked an
        accessibility tree.
        """
        reached: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.computer_use.tools.dispatch_tool",
            lambda tool_name, *a, **k: reached.append(tool_name) or "ok",
        )
        self.reached = reached

    @pytest.mark.asyncio
    async def test_a_missing_secret_is_refused(self, home):
        """No ``X-Internal-Secret`` at all — the browser-originated case.

        This is precisely the request a "mixed" internal path WOULD admit (it falls
        through to cookie auth); the strict classification is what makes a
        browser-originated call to this route impossible.
        """
        async with self._authed_client("the-real-secret") as client:
            resp = await client.post(
                "/api/computer-use/invoke",
                json={"tool": "computer_list_apps", "session_key": "dashboard:slot1"},
            )
        assert resp.status in (401, 403)
        assert self.reached == []

    @pytest.mark.asyncio
    async def test_a_wrong_secret_is_refused(self, home):
        async with self._authed_client("the-real-secret") as client:
            resp = await client.post(
                "/api/computer-use/invoke",
                json={"tool": "computer_list_apps", "session_key": "dashboard:slot1"},
                headers={"X-Internal-Secret": "guessed-wrong"},
            )
        assert resp.status in (401, 403)
        assert self.reached == []

    @pytest.mark.asyncio
    async def test_an_almost_right_secret_is_refused(self, home):
        """A prefix of the real secret must not pass (constant-time compare).

        Cheap to assert and it pins that the comparison is over the WHOLE value,
        not a ``startswith``.
        """
        async with self._authed_client("the-real-secret") as client:
            resp = await client.post(
                "/api/computer-use/invoke",
                json={"tool": "computer_list_apps", "session_key": "dashboard:slot1"},
                headers={"X-Internal-Secret": "the-real-secre"},
            )
        assert resp.status in (401, 403)
        assert self.reached == []

    @pytest.mark.asyncio
    async def test_the_correct_secret_is_accepted(self, home):
        """The control case, so the three refusals above mean something."""
        async with self._authed_client("the-real-secret") as client:
            resp = await client.post(
                "/api/computer-use/invoke",
                json={"tool": "computer_list_apps", "session_key": "dashboard:slot1"},
                headers={"X-Internal-Secret": "the-real-secret"},
            )
            body = await resp.json()
        assert resp.status == 200
        assert body["text"] == "ok"
        assert self.reached == ["computer_list_apps"]

    @pytest.mark.asyncio
    async def test_a_valid_dashboard_token_alone_is_refused(self, home):
        """THE bypass this handler's own check exists to close.

        Being on the strict path list does NOT mean "secret required": when the
        ``X-Internal-Secret`` header is ABSENT the middleware deliberately falls
        through to normal cookie/token auth so dashboard pages can call internal
        routes. A caller holding only a valid dashboard token would therefore reach
        the handler — AND choose its own ``session_key``, which is what selects the
        governance profile. Claiming ``dashboard:main`` from a subagent context
        would escape a ``cu-off-subagent`` profile entirely.

        So the handler re-asserts ``request["internal_auth"]`` itself, and this case
        pins that: a good token, no secret, must still be refused and must never
        reach the dispatcher.
        """
        from kiro_crew.dashboard.token_auth import generate_token

        token = generate_token("tester", 3600)
        async with self._authed_client("the-real-secret") as client:
            resp = await client.post(
                f"/api/computer-use/invoke?token={token}",
                json={"tool": "computer_list_apps", "session_key": "dashboard:main"},
            )
        assert resp.status in (401, 403)
        assert self.reached == []

    @pytest.mark.asyncio
    async def test_a_gateway_with_no_secret_configured_refuses(self, home):
        """An unconfigured secret must DENY, never accept anything.

        Fail-closed: a gateway that could not read its own local secret has no way
        to authenticate the shim, and treating "no secret" as "no check" would open
        the route to every loopback process on the machine.
        """
        async with self._authed_client("") as client:
            resp = await client.post(
                "/api/computer-use/invoke",
                json={"tool": "computer_list_apps", "session_key": "dashboard:slot1"},
                headers={"X-Internal-Secret": "anything"},
            )
        assert resp.status in (401, 403)
        assert self.reached == []


class TestConfigSection:
    def test_config_section_has_no_enabled_field(self):
        """``ComputerUseConfig`` must never grow an ``enabled`` field.

        ``config.json`` is writable by an auto-approved agent shell
        (``is_sensitive_bash_command`` deliberately does not block it), so an
        enable stored there would be flippable by prompt injection.
        """
        from kiro_crew.config.loader import ComputerUseConfig

        names = {f.name for f in dataclasses.fields(ComputerUseConfig)}
        assert "enabled" not in names
        # Exhaustive on purpose: a new field here has to be a deliberate edit, so
        # nobody can add a security-relevant one to the agent-writable file by
        # accident. ``cursor_motion`` is purely visual (a drawn overlay), which is
        # why it is allowed to live here rather than on the keystone.
        assert names == {
            "max_tree_nodes",
            "max_tree_depth",
            "text_limit",
            "attach_screenshot",
            "screenshot_max_px",
            "screenshot_jpeg_quality",
            "cursor_motion",
        }

    def test_section_is_known_and_round_trips(self, home):
        from kiro_crew.config.loader import _KNOWN_CONFIG_SECTIONS, KiroCrewConfig

        assert "computer_use" in _KNOWN_CONFIG_SECTIONS
        assert "computer_use" in KiroCrewConfig.load().to_dict()

    def test_state_path_is_on_the_keystone_floor(self):
        from kiro_crew import sandbox
        from kiro_crew.config.loader import computer_use_state_path
        from kiro_crew.security import _CREW_SECRET_LEAVES, is_sensitive_path

        assert "computer_use.json" in _CREW_SECRET_LEAVES
        assert computer_use_state_path().name == "computer_use.json"
        assert is_sensitive_path("~/.kiro/crew/computer_use.json") is True
        # The shell plane is sealed by the sandbox, not matched by text.
        assert "computer_use.json" in sandbox._CREW_READONLY_LEAVES


# ── Machine-readable error codes ──


class TestErrorCodes:
    """Every refusal from this module carries a machine-readable ``code``.

    The contract gate in ``test/test_error_code_contract.py`` inventories
    prose-only refusals in ``error-code-baseline.json``; this module held 20 of
    them. The prose is kept and keeps its meaning -- it is demoted to advisory,
    not removed -- so a client that only reads ``error`` is unaffected.

    Both of this module's consumers are already built for the code, which is why
    this conversion is backend-only:

    * the browser panel (``website/src/pages/settings/ComputerUsePanel.tsx``)
      never renders the server's prose -- its ``onError`` shows a fixed localized
      string -- so there is no copy to move and no catalog to translate;
    * the stdio shim reaches ``/invoke`` through ``mcp_core._http_error_body``,
      which ALREADY parses a top-level ``code``, validates it as
      ``[a-z0-9_.-]{1,64}`` and passes it on, and the capture thread's
      ``screencast._post_frame`` discards the response body entirely.
    """

    # -- the per-file ratchet --
    #
    # The repo-wide gate only fails on a NET regression, so it would let a new
    # prose-only refusal land here behind an unrelated deletion. These two run
    # the gate's OWN scanner, so the per-file rule cannot drift from it.

    @staticmethod
    def _findings():
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_error_code_contract as gate

        return [f for f in gate.scan() if f.path == "dashboard/handlers/computer_use.py"]

    def test_no_refusal_in_this_module_is_prose_only(self) -> None:
        missing = sorted(f.lineno for f in self._findings() if f.bucket == "missing_code")
        assert missing == [], (
            "these src/kiro_crew/dashboard/handlers/computer_use.py lines refuse with "
            f"prose and no machine-readable code: {missing}"
        )

    def test_the_ratchet_can_actually_fail(self) -> None:
        """Self-check: a scan matching nothing would pass the assertion above vacuously."""
        coded = [f for f in self._findings() if f.bucket == "compliant"]
        assert len(coded) == 20, f"scanner reached {len(coded)} coded sites, expected 20"
        assert all(f.code_value for f in coded)

    # -- PUT /api/computer-use/config --

    @staticmethod
    async def _put(client: TestClient, body) -> tuple[int, dict]:
        resp = await client.put("/api/computer-use/config", json=body)
        return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_body_is_not_a_json_object(self, home: Path) -> None:
        async with _client() as client:
            status, body = await self._put(client, ["enabled"])
        assert status == 400
        assert body["code"] == "body_not_object"
        assert body["error"]

    @pytest.mark.asyncio
    async def test_enabled_of_the_wrong_type(self, home: Path) -> None:
        async with _client() as client:
            status, body = await self._put(client, {"enabled": "yes"})
        assert status == 400
        assert body["code"] == "invalid_field_type"

    @pytest.mark.asyncio
    async def test_an_integer_limit_of_the_wrong_type(self, home: Path) -> None:
        async with _client() as client:
            status, body = await self._put(client, {"max_tree_nodes": "1200"})
        assert status == 400
        assert body["code"] == "invalid_field_type"

    @pytest.mark.asyncio
    async def test_an_integer_limit_out_of_range_is_a_different_code(self, home: Path) -> None:
        """The distinction a caller cannot make without matching English.

        A value of the right type that is simply too large is a different refusal
        from a value of the wrong type, and only the code separates them.
        """
        async with _client() as client:
            status, body = await self._put(client, {"max_tree_nodes": 10**9})
        assert status == 400
        assert body["code"] == "value_out_of_range"

    @pytest.mark.asyncio
    async def test_a_boolean_limit_of_the_wrong_type(self, home: Path) -> None:
        async with _client() as client:
            status, body = await self._put(client, {"attach_screenshot": 1})
        assert status == 400
        assert body["code"] == "invalid_field_type"

    @pytest.mark.asyncio
    async def test_a_malformed_app_pattern_list(self, home: Path) -> None:
        async with _client() as client:
            status, body = await self._put(client, {"allowed_apps": "Preview"})
        assert status == 400
        assert body["code"] == "invalid_app_patterns"

    @pytest.mark.asyncio
    async def test_a_body_naming_no_known_field(self, home: Path) -> None:
        async with _client() as client:
            status, body = await self._put(client, {"nothing_we_know": True})
        assert status == 400
        assert body["code"] == "no_known_fields"

    # -- POST /api/computer-use/invoke (machine leg) --

    @pytest.mark.asyncio
    async def test_invoke_without_a_tool(self, home: Path) -> None:
        async with _client() as client:
            resp = await client.post("/api/computer-use/invoke", json={"args": {}})
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "tool_required"

    @pytest.mark.asyncio
    async def test_invoke_with_non_object_args(self, home: Path) -> None:
        async with _client() as client:
            resp = await client.post(
                "/api/computer-use/invoke", json={"tool": "screenshot", "args": ["a"]}
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "invalid_args"

    @pytest.mark.asyncio
    async def test_the_code_survives_the_shims_own_error_decoder(self, home: Path) -> None:
        """The code is only worth adding if the shim's decoder keeps it.

        ``mcp_core._http_error_body`` drops any ``code`` that is not a short
        identifier, so this drives the REAL decoder over this handler's REAL body
        rather than asserting that the two shapes look compatible.
        """
        import io as _io
        import urllib.error

        from kiro_crew.mcp_core import _http_error_body

        async with _client() as client:
            resp = await client.post("/api/computer-use/invoke", json={"args": {}})
            raw = await resp.read()

        decoded = _http_error_body(
            urllib.error.HTTPError(
                "http://127.0.0.1/api/computer-use/invoke",
                400,
                "Bad Request",
                {},
                _io.BytesIO(raw),
            )
        )
        assert decoded["code"] == "tool_required"

    # -- POST /api/computer-use/frame (machine leg) --

    @pytest.mark.asyncio
    async def test_frame_without_the_internal_secret(self, home: Path) -> None:
        async with TestClient(TestServer(_frame_app(grant_internal_auth=False))) as client:
            resp = await client.post("/api/computer-use/frame", json=_VALID_FRAME)
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "internal_secret_required"

    @pytest.mark.asyncio
    async def test_frame_carrying_no_frame_data(self, home: Path) -> None:
        async with TestClient(TestServer(_frame_app())) as client:
            resp = await client.post("/api/computer-use/frame", json={"format": "jpeg"})
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "no_frame_data"

    # -- the behavioural ratchet --

    @pytest.mark.asyncio
    async def test_every_config_refusal_carries_both_a_code_and_its_prose(self, home: Path) -> None:
        """No refusal path may regress to prose-only, and none may drop the advisory
        text an existing client still reads."""
        for payload in (
            ["enabled"],
            {"enabled": "yes"},
            {"max_tree_nodes": "1200"},
            {"max_tree_nodes": 10**9},
            {"attach_screenshot": 1},
            {"allowed_apps": "Preview"},
            {"nothing_we_know": True},
        ):
            async with _client() as client:
                status, body = await self._put(client, payload)
            assert status == 400, payload
            assert isinstance(body.get("code"), str) and body["code"], payload
            assert isinstance(body.get("error"), str) and body["error"], payload
