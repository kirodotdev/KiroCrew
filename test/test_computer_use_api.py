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
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.computer_use import enable_state
from kiro_crew.computer_use.types import (
    PERMISSION_GRANTED,
    PERMISSION_UNKNOWN,
    PERMISSION_UNSUPPORTED,
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    STATE_KEY_ALLOWED_APPS,
    STATE_KEY_ENABLED,
    PolicyStateError,
)
from kiro_crew.dashboard.handlers import computer_use as cu_api
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy

_GRANTED = {
    "accessibility": PERMISSION_GRANTED,
    "screen_recording": PERMISSION_GRANTED,
    "responsible_hint": "",
}

# The REAL probe, captured at import time — the autouse ``no_real_probe`` fixture
# replaces the module attribute, and ``TestPermissionProbe`` needs the genuine
# implementation. Captured rather than reloaded: ``importlib.reload`` would rebind
# the module object the handlers package already holds a reference to.
_REAL_PROBE = cu_api._probe_permissions


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect KIROCREW_HOME so the keystone + config writes land in a tmp dir."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def state_file(home: Path) -> Path:
    """The keystone ``computer_use.json`` (the primary enable + target lists)."""
    return home / "computer_use.json"


@pytest.fixture
def config_file(home: Path) -> Path:
    """``config.json`` — where the budget knobs live, and the enable never does."""
    return home / "config.json"


@pytest.fixture(autouse=True)
def profiles_dir(tmp_path, monkeypatch):
    """Isolate the governance profile store (never read the developer's own)."""
    directory = tmp_path / "profiles"
    directory.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", directory)
    gp.reset_store()
    yield directory
    gp.reset_store()


@pytest.fixture(autouse=True)
def _reset_ctx():
    yield
    ctx_mod.reset_context()


@pytest.fixture(autouse=True)
def no_real_probe(monkeypatch):
    """Never spawn the real ``kirocrew computer doctor`` child in CI.

    Individual tests override this with their own stub when they care about the
    probe's own behaviour.
    """

    async def _fake(platform_id: str) -> dict:
        if platform_id != PLATFORM_MACOS:
            return dict(cu_api._permission_block(PERMISSION_UNSUPPORTED))
        return dict(_GRANTED)

    monkeypatch.setattr(cu_api, "_probe_permissions", _fake)


@pytest.fixture
def mock_sel():
    """Patch the late-bound ``_sel()`` so SEL audit calls are observable."""
    with patch.object(cu_api, "_sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


def _install_ceiling(policy_body) -> None:
    """Compose a context carrying *policy_body* as the ceiling and install it."""
    from kiro_crew.config.loader import KiroCrewConfig

    base = build_default_context(KiroCrewConfig.load())
    ceiling = parse_policy(policy_body) if policy_body is not None else None
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


@web.middleware
async def _grant_internal_auth(request: web.Request, handler):
    """Stand in for the middleware's verified-secret grant.

    ``api_computer_use_invoke`` requires ``request["internal_auth"] is True`` — the
    flag ``token_auth_middleware`` sets ONLY after a constant-time
    ``X-Internal-Secret`` match — because being listed in
    ``_STRICT_INTERNAL_API_PATHS`` does NOT by itself mean the secret was checked
    (the middleware deliberately falls through to cookie auth when the header is
    absent, and reclassifies strict paths as mixed when ``local_only=False``).

    These cases mount the handler WITHOUT the real middleware in order to test its
    own behaviour, so the grant is supplied here. The enforcement itself — that a
    request lacking the secret is refused — is covered end-to-end against the REAL
    middleware in ``TestInvokeSecretEnforcement``.
    """
    request["internal_auth"] = True
    return await handler(request)


def _make_app() -> web.Application:
    app = web.Application(middlewares=[_grant_internal_auth])
    app.router.add_get("/api/computer-use/config", cu_api.api_computer_use_config_get)
    app.router.add_put("/api/computer-use/config", cu_api.api_computer_use_config_save)
    app.router.add_post("/api/computer-use/invoke", cu_api.api_computer_use_invoke)
    return app


def _client() -> TestClient:
    """Build an aiohttp TestClient (open with ``async with`` inside the test)."""
    return TestClient(TestServer(_make_app()))


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
            assert argv[1:] == (cu_api.DOCTOR_SUBCOMMAND, *cu_api.DOCTOR_ARGS)
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
        from kiro_crew.config.loader import computer_use_state_path
        from kiro_crew.security import (
            _CREW_SECRET_LEAVES,
            is_sensitive_bash_command,
            is_sensitive_path,
        )

        assert "computer_use.json" in _CREW_SECRET_LEAVES
        assert computer_use_state_path().name == "computer_use.json"
        assert is_sensitive_path("~/.kiro/crew/computer_use.json") is True
        for command in (
            "cat ~/.kiro/crew/computer_use.json",
            "echo x > ~/.kiro/crew/computer_use.json",
            "tee ~/.kiro/crew/computer_use.json",
        ):
            assert is_sensitive_bash_command(command)


# ── POST /api/computer-use/frame — the live-view (PiP) ingress ──


def _frame_app(*, grant_internal_auth: bool = True) -> web.Application:
    """Mount ONLY the frame ingress, with a MagicMock ``state``.

    ``grant_internal_auth`` stands in for the middleware's verified-secret grant
    (``token_auth_middleware`` sets ``request["internal_auth"]`` only after a
    constant-time ``X-Internal-Secret`` match). These cases mount the handler
    without the real middleware in order to test the handler's OWN branches, so
    the flag is supplied here; the ``False`` variant proves the handler refuses
    without it.
    """
    middlewares = [_grant_internal_auth] if grant_internal_auth else []
    app = web.Application(middlewares=middlewares)
    app.router.add_post("/api/computer-use/frame", cu_api.api_computer_use_frame)
    state = MagicMock()

    async def _deliver(_event, _payload):
        return 2

    state.deliver_ws_owners = _deliver
    app["state"] = state
    return app


_VALID_FRAME = {"data": "QUJD", "format": "jpeg", "width": 1280, "height": 800}


class TestFrameIngress:
    """The ingress: loopback-gated, bounded, and OWNER-only on the way out."""

    @pytest.mark.asyncio
    async def test_a_non_loopback_post_is_refused(self, home, mock_sel):
        """The load-bearing auth control on this route.

        The body is a live frame of the operator's own desktop; an off-host POST
        has no legitimate caller, so it is refused before the body is even parsed.
        """
        app = _frame_app()
        delivered: list = []
        app["state"].deliver_ws_owners = lambda *a: delivered.append(a)
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=False):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/computer-use/frame", json=_VALID_FRAME)
        assert resp.status == 403
        assert delivered == []

    @pytest.mark.asyncio
    async def test_a_loopback_post_without_the_verified_secret_is_refused(self, home, mock_sel):
        """Loopback alone is NOT sufficient.

        Being listed in ``_STRICT_INTERNAL_API_PATHS`` does not prove the secret
        was checked: with the header absent the middleware falls through to cookie
        auth, and on a ``local_only=False`` deployment it reclassifies strict paths
        as "mixed". Either way a caller holding only a dashboard cookie or an
        app-scoped token would reach the handler — and could then inject arbitrary
        frames into every owner window's live view.
        """
        app = _frame_app(grant_internal_auth=False)
        delivered: list = []
        app["state"].deliver_ws_owners = lambda *a: delivered.append(a)
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/computer-use/frame", json=_VALID_FRAME)
        assert resp.status == 403
        assert delivered == []

    @pytest.mark.asyncio
    async def test_a_valid_frame_is_delivered_to_owner_sockets_only(self, home, mock_sel):
        """``deliver_ws_owners``, never ``broadcast_ws``.

        An App Kit credential can open ``/api/ws`` and lands in the all-clients
        set, so an all-clients broadcast of the operator's desktop would cross the
        App Kit boundary.
        """
        app = _frame_app()
        seen: list = []

        async def _deliver(event, payload):
            seen.append((event, payload))
            return 3

        app["state"].deliver_ws_owners = _deliver
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/computer-use/frame", json=_VALID_FRAME)
                body = await resp.json()
        assert resp.status == 200
        assert body == {"ok": True, "subscribers": 3}
        assert len(seen) == 1
        event, payload = seen[0]
        assert event == "computer_use_frame"
        assert payload["data"] == "QUJD" and payload["format"] == "jpeg"
        app["state"].broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, home, mock_sel):
        app = _frame_app()
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/computer-use/frame",
                    data=b"not json",
                    headers={"Content-Type": "application/json"},
                )
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"format": "jpeg"},
            {"data": "", "format": "jpeg"},
            {"data": "QUJD"},  # no format at all
            {"data": "QUJD", "format": "png"},  # a full-res PNG is refused outright
            {"data": "QUJD", "format": "webp"},
            {"data": "<svg onload=alert(1)>", "format": "jpeg"},  # markup
            {"data": "http://evil.example/x", "format": "jpeg"},  # a URL
            {"data": "QUJD QUJD", "format": "jpeg"},  # whitespace
        ],
    )
    async def test_a_body_with_no_usable_jpeg_frame_is_400(self, body, home, mock_sel):
        """Nothing but a base64 JPEG travels this path.

        The PNG/WebP rows are the important ones: the encoder only ever produces a
        downscaled JPEG, so a frame claiming another encoding did not come from it
        and must be refused rather than relabelled.
        """
        app = _frame_app()
        delivered: list = []
        app["state"].deliver_ws_owners = lambda *a: delivered.append(a)
        with patch("kiro_crew.dashboard.origin.is_loopback", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/computer-use/frame", json=body)
        assert resp.status == 400
        assert delivered == []


class TestFramePayload:
    """``build_frame_payload`` — every field bounded at the boundary."""

    def test_a_minimal_valid_frame(self):
        from kiro_crew.computer_use.screencast import build_frame_payload

        assert build_frame_payload({"data": "QUJD", "format": "jpeg"}) == {
            "data": "QUJD",
            "format": "jpeg",
        }

    def test_padded_base64_is_accepted(self):
        from kiro_crew.computer_use.screencast import build_frame_payload

        out = build_frame_payload({"data": "QUJDRA==", "format": "jpeg"})
        assert out is not None and out["data"] == "QUJDRA=="

    def test_an_oversized_frame_is_rejected(self):
        """One frame must not become a multi-megabyte websocket write."""
        from kiro_crew.computer_use.screencast import (
            MAX_FRAME_B64_CHARS,
            build_frame_payload,
        )

        big = "Q" * (MAX_FRAME_B64_CHARS + 4)
        assert build_frame_payload({"data": big, "format": "jpeg"}) is None

    def test_dimensions_are_integers_within_the_encoder_ceiling(self):
        from kiro_crew.computer_use.screencast import build_frame_payload
        from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX

        out = build_frame_payload(
            {"data": "QUJD", "format": "jpeg", "width": 1280, "height": "tall"}
        )
        assert out == {"data": "QUJD", "format": "jpeg", "width": 1280}
        # bool is an int subclass in Python, so an unguarded isinstance(int) would
        # let width=True through and the panel would treat it as a 1px frame.
        for bad in (True, False, 0, -5, MAX_SCREENSHOT_MAX_PX + 1):
            out = build_frame_payload(
                {"data": "QUJD", "format": "jpeg", "width": bad, "height": 800}
            )
            assert out is not None
            assert "width" not in out, f"width={bad!r} leaked"
            assert out["height"] == 800  # a valid sibling still passes

    def test_the_session_key_is_bounded_to_a_lookup_charset(self):
        from kiro_crew.computer_use.screencast import build_frame_payload

        out = build_frame_payload(
            {"data": "QUJD", "format": "jpeg", "session_key": "dashboard:slot1"}
        )
        assert out is not None and out["session_key"] == "dashboard:slot1"
        for bad in (123, "has space", "x" * 129, "<b>"):
            out = build_frame_payload({"data": "QUJD", "format": "jpeg", "session_key": bad})
            assert out is not None and "session_key" not in out

    def test_the_app_label_is_bounded(self):
        from kiro_crew.computer_use.screencast import build_frame_payload

        out = build_frame_payload({"data": "QUJD", "format": "jpeg", "app": "Google Chrome"})
        assert out is not None and out["app"] == "Google Chrome"
        for bad in (7, "x" * 97, "</span><script>", "line\nbreak"):
            out = build_frame_payload({"data": "QUJD", "format": "jpeg", "app": bad})
            assert out is not None and "app" not in out


class TestLiveViewSuppression:
    """The three suppressions that make this pixel egress path acceptable.

    All evaluated in ``computer_use/screencast.py`` BEFORE anything leaves the
    process, so a suppressed frame is never even POSTed — the ingress handler is
    not the boundary and must not be relied on as one.
    """

    @pytest.fixture(autouse=True)
    def no_real_post(self, monkeypatch):
        """Record POST attempts instead of making them."""
        from kiro_crew.computer_use import screencast

        posted: list[dict] = []
        monkeypatch.setattr(screencast, "_post_frame", lambda payload: posted.append(payload))
        # ``emit_snapshot_frame`` spawns the POST on a daemon thread; run it inline
        # so the assertions are deterministic.
        monkeypatch.setattr(
            screencast.threading,
            "Thread",
            lambda target=None, args=(), **_kw: _InlineThread(target, args),
        )
        return posted

    def _snapshot(self, *, secure: bool = False):
        from kiro_crew.computer_use.types import AppRef, Snapshot

        return Snapshot(
            app=AppRef(name="Finder", pid=41, bundle_id="com.apple.finder", window_id=9),
            window_title="Documents",
            has_secure=secure,
            image_jpeg=b"\xff\xd8\xff\xd9",
            image_path="/tmp/shot.jpeg",
            image_width=1280,
            image_height=800,
        )

    def test_a_secure_window_emits_no_frame(self, home, profiles_dir, no_real_post):
        """A window holding a password field is NEVER mirrored.

        Read from ``Snapshot.has_secure`` — the driver's own predicate, the same
        one ``capture_snapshot_image`` refuses on — rather than re-derived here, so
        there is exactly one definition of "this window holds a secure field".
        """
        from kiro_crew.computer_use import screencast

        _install_ceiling(None)
        with screencast.frame_scope(session_key="dashboard:slot1"):
            assert screencast.emit_snapshot_frame(self._snapshot(secure=True)) is False
        assert no_real_post == []

    def test_a_capture_with_no_published_scope_emits_no_frame(
        self, home, profiles_dir, no_real_post
    ):
        """Fail-closed by construction: an unattributable frame is not mirrored.

        The capture layer has no session identity of its own, so the invoke handler
        publishes one for the duration of a dispatch. A capture reached any other
        way (a CLI probe, a future caller that skipped the handler) cannot be
        governed for a surface, so it emits nothing rather than guessing.
        """
        from kiro_crew.computer_use import screencast

        _install_ceiling(None)
        assert screencast.active_scope() is None
        assert screencast.emit_snapshot_frame(self._snapshot()) is False
        assert no_real_post == []

    def test_a_snapshot_with_no_encoded_bytes_emits_no_frame(
        self, home, profiles_dir, no_real_post
    ):
        from kiro_crew.computer_use import screencast
        from kiro_crew.computer_use.types import AppRef, Snapshot

        _install_ceiling(None)
        empty = Snapshot(app=AppRef(name="Finder", pid=41, window_id=9))
        with screencast.frame_scope(session_key="dashboard:slot1"):
            assert screencast.emit_snapshot_frame(empty) is False
        assert no_real_post == []

    def test_the_frame_scope_is_restored_and_never_leaks_across_dispatches(self, home):
        """A pooled worker thread must not carry one surface's identity forward."""
        from kiro_crew.computer_use import screencast

        assert screencast.active_scope() is None
        with screencast.frame_scope(session_key="dashboard:slot1", agent="kirocrew"):
            outer = screencast.active_scope()
            assert outer is not None and outer.session_key == "dashboard:slot1"
            with screencast.frame_scope(session_key="cron:nightly"):
                inner = screencast.active_scope()
                assert inner is not None and inner.session_key == "cron:nightly"
            # Restored to the enclosing scope, not cleared.
            restored = screencast.active_scope()
            assert restored is not None and restored.session_key == "dashboard:slot1"
        assert screencast.active_scope() is None

    def test_a_relay_error_is_swallowed(self, home, profiles_dir, monkeypatch, no_real_post):
        """The mirror must never degrade the observation the model asked for.

        ``emit_snapshot_frame`` is called from the capture path, so an unexpected
        failure anywhere inside it (here: the scope lookup) has to become a dropped
        frame rather than a failed screenshot.
        """
        from kiro_crew.computer_use import screencast

        _install_ceiling(None)

        def _boom():
            raise RuntimeError("scope lookup on fire")

        monkeypatch.setattr(screencast, "active_scope", _boom)
        assert screencast.emit_snapshot_frame(self._snapshot()) is False
        assert no_real_post == []


class TestTheFrameRelayIsNeverProxied:
    """The relay POST carries the gateway's local secret, so it must not proxy.

    ``urlopen`` builds its opener from ``getproxies()`` and urllib has no implicit
    loopback exemption, so with ``http_proxy`` set — routine in a container or on a
    corporate desktop — a POST to ``http://127.0.0.1:<port>`` goes to the proxy in
    absolute form with ``X-Internal-Secret`` attached, in cleartext. The relay runs
    through ``loopback_urlopen``, whose opener carries an explicit
    ``ProxyHandler({})``.

    ``_post_frame`` is called DIRECTLY rather than through ``emit_snapshot_frame``:
    every other case in this file replaces ``_post_frame`` wholesale, so the
    transport inside it is the one part of the relay with no coverage at all.

    Both listeners are real sockets on port 0, following ``test_cron_trigger.py`` —
    the kernel hands out a free port, so no ``xdist_group`` marker is needed.
    """

    SECRET = "canary-not-a-real-frame-secret"

    # ``getproxies_environment`` ignores uppercase ``HTTP_PROXY`` when
    # ``REQUEST_METHOD`` is set (the httpoxy CGI guard), so that is cleared too —
    # otherwise a CI runner exporting it would make this pass vacuously.
    _PROXY_ENV_KEYS = (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
        "REQUEST_METHOD",
    )

    @staticmethod
    def _serve(sink: list):
        """A listener that records the secret header it saw, then answers 200."""

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                sink.append(
                    {
                        "requestline": self.requestline,
                        "secret": self.headers.get("X-Internal-Secret"),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, fmt, *args):
                pass  # keep pytest output clean

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, server.server_address[1]

    def test_the_secret_reaches_the_gateway_and_never_the_proxy(self, home, monkeypatch):
        from kiro_crew.computer_use import screencast

        gateway_hits: list[dict] = []
        proxy_hits: list[dict] = []
        gateway, gateway_port = self._serve(gateway_hits)
        proxy, proxy_port = self._serve(proxy_hits)
        try:
            for key in self._PROXY_ENV_KEYS:
                monkeypatch.delenv(key, raising=False)
            monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
            # The real ``_headers()`` runs, so the header the ingress authenticates
            # with is the one under test rather than a literal in this file.
            (home / ".local_secret").write_text(self.SECRET, encoding="utf-8")
            monkeypatch.setattr(
                screencast,
                "_ingress_url",
                lambda: f"http://127.0.0.1:{gateway_port}{screencast.FRAME_INGRESS_PATH}",
            )

            screencast._post_frame({"data": "QUJD", "format": "jpeg"})

            assert proxy_hits == [], f"the frame secret reached the proxy: {proxy_hits}"
            # Positive leg too: ``_post_frame`` swallows every failure, so "the proxy
            # saw nothing" alone would also pass if the POST had never happened.
            assert [h["secret"] for h in gateway_hits] == [self.SECRET]
            # Origin-form request line confirms a direct connection rather than a
            # proxied one, which is sent absolute-form.
            assert gateway_hits[0]["requestline"].startswith(
                f"POST {screencast.FRAME_INGRESS_PATH}"
            )
        finally:
            gateway.shutdown()
            proxy.shutdown()


class TestInvokePublishesTheFrameScope:
    """The invoke leg is what makes a capture attributable to a surface."""

    @pytest.mark.asyncio
    async def test_the_dispatch_runs_inside_the_calling_surfaces_frame_scope(
        self, home, monkeypatch
    ):
        from kiro_crew.computer_use import screencast

        seen: dict = {}

        def _fake_dispatch(tool_name, args, *, session_key, agent="", app="", **_kw):
            scope = screencast.active_scope()
            seen["scope"] = scope
            return "ok"

        monkeypatch.setattr("kiro_crew.computer_use.tools.dispatch_tool", _fake_dispatch)
        async with _client() as client:
            await client.post(
                "/api/computer-use/invoke",
                json={
                    "tool": "computer_get_state",
                    "args": {"app": "Finder"},
                    "session_key": "dashboard:slot1",
                    "agent": "kirocrew",
                    "app": "notes",
                },
            )
        scope = seen["scope"]
        assert scope is not None
        assert (scope.session_key, scope.agent, scope.app) == (
            "dashboard:slot1",
            "kirocrew",
            "notes",
        )
        # Restored once the dispatch returns, so the pooled worker is clean.
        assert screencast.active_scope() is None


class _InlineThread:
    """Minimal ``threading.Thread`` stand-in that runs the target on ``start()``."""

    def __init__(self, target, args):
        self._target = target
        self._args = args
        self.daemon = False
        self.name = ""

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args)


def _app_token_middleware(app_name: str):
    """Stand in for ``token_auth_middleware``'s app-token grant.

    The real middleware sets BOTH ``request["user"]`` and ``request["app"]`` for an
    app-scoped token — which is the trap: ``request["user"]`` is truthy, so a handler
    that only checks it treats an app exactly like the dashboard operator.
    """

    @web.middleware
    async def _mw(request: web.Request, handler):
        request["user"] = "dashboard"
        request["app"] = app_name
        request["internal_auth"] = True
        return await handler(request)

    return _mw


def _app_client(app_name: str = "pwn") -> TestClient:
    app = web.Application(middlewares=[_app_token_middleware(app_name)])
    app.router.add_get("/api/computer-use/config", cu_api.api_computer_use_config_get)
    app.router.add_put("/api/computer-use/config", cu_api.api_computer_use_config_save)
    return TestClient(TestServer(app))


class TestAnAppTokenCannotWriteTheKeystone:
    """The one write the keystone design exists to prevent (reviewer finding).

    ``enable_state.save_state`` deliberately bypasses ``is_sensitive_path`` — that is
    what lets the operator's own Settings panel write a file the agent cannot read or
    write with a tool. So this handler is the only thing standing between an
    App-Kit-scoped token and ``enabled: true``. It used to check nothing at all:
    ``request["user"]`` is truthy for an app token too, and an app whose manifest
    declares ``permissions.api: ["/api/computer-use"]`` passes
    ``app_token_path_allowed`` (verified: a bare ``/api/computer-use`` pattern matches
    ``/api/computer-use/config`` on the path boundary). An agent that can author an app
    manifest could therefore mint a token and turn on its own desktop automation.
    """

    @pytest.mark.asyncio
    async def test_an_app_token_put_is_refused_and_the_keystone_is_untouched(
        self, home, state_file, mock_sel
    ):
        async with _app_client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 403
        # THE assertion: refused BEFORE any write — the keystone never appears.
        assert (
            not state_file.exists()
            or json.loads(state_file.read_text()).get(STATE_KEY_ENABLED) is not True
        )
        # A security-boundary denial is audited (backend-security-controls).
        assert mock_sel.log_api_access.called

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_edit_the_target_lists_either(self, home, state_file):
        """``allowed_apps`` widening is the same boundary as the enable."""
        async with _app_client() as client:
            resp = await client.put(
                "/api/computer-use/config", json={"allowed_apps": ["com.apple.Terminal"]}
            )
        assert resp.status == 403
        assert not state_file.exists()

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_shrink_a_budget_either(self, home, config_file):
        """Refused at the door, before the widen/narrow distinction is reached.

        The 409 ceiling logic distinguishes widening from narrowing; this gate does
        not, deliberately — an app token has no business writing ANY of the
        operator's computer-use configuration, and a narrow-only exception would be a
        second code path to keep correct for no user benefit.
        """
        async with _app_client() as client:
            resp = await client.put("/api/computer-use/config", json={"max_tree_nodes": 200})
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_the_dashboard_user_is_still_allowed(self, home, state_file):
        """The gate must key on ``app``, not break the operator's own panel."""
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 200
        assert json.loads(state_file.read_text(encoding="utf-8"))[STATE_KEY_ENABLED] is True


class TestMixedSaveIsAllOrNothing:
    """A mixed state+limits PUT must not half-apply (reviewer finding).

    The keystone write lands first, so a corrupt ``config.json`` used to leave the
    SECURITY state applied — the feature enabled, or the real-pointer opt-in set —
    while the response told the operator the save had failed. The ceiling had moved
    and nothing said so.
    """

    @pytest.mark.asyncio
    async def test_a_corrupt_config_leaves_the_keystone_untouched(self, home, state_file):
        (home / "config.json").write_text("{not json")
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={"enabled": True, "max_tree_nodes": 900},
            )
        assert resp.status == 500
        # THE assertion: the enable did not survive the failed save.
        assert (
            not state_file.exists()
            or json.loads(state_file.read_text()).get(STATE_KEY_ENABLED) is not True
        )

    @pytest.mark.asyncio
    async def test_a_state_only_save_still_works_with_no_config_file(self, home, state_file):
        """The preflight must not turn an absent config.json into a failure."""
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 200
        assert json.loads(state_file.read_text())[STATE_KEY_ENABLED] is True

    @pytest.mark.asyncio
    async def test_a_healthy_mixed_save_applies_both(self, home, state_file, config_file):
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={"enabled": True, "max_tree_nodes": 900},
            )
        assert resp.status == 200
        assert json.loads(state_file.read_text())[STATE_KEY_ENABLED] is True
        assert json.loads(config_file.read_text())["computer_use"]["max_tree_nodes"] == 900

    @pytest.mark.asyncio
    async def test_a_write_time_OSError_can_never_leave_the_CEILING_applied(
        self, home, state_file, config_file, monkeypatch
    ):
        """The preflight cannot rule out a write-time failure — so ordering must.

        A full disk (or a mode change between the check and the write) raises from
        one writer after the other already landed. Two files are not atomic here,
        but the DIRECTION of a partial apply is a choice: limits first, keystone
        last, so the only reachable half-state is "budget knob moved, ceiling
        untouched" and a reported failure never hides an enable.
        """

        def boom(_patch):
            raise OSError("no space left on device")

        monkeypatch.setattr(cu_api, "_write_state", boom)
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={"enabled": True, "max_tree_nodes": 900},
            )
        assert resp.status == 500
        # THE assertion: the security state is untouched.
        assert (
            not state_file.exists()
            or json.loads(state_file.read_text()).get(STATE_KEY_ENABLED) is not True
        )

    @pytest.mark.asyncio
    async def test_the_keystone_is_written_after_the_limits(self, home, monkeypatch):
        """Pins the ORDER itself, so a refactor cannot quietly swap the two back."""
        order: list[str] = []
        real_state = cu_api._write_state
        real_limits = cu_api._write_limits

        def state(patch):
            order.append("state")
            real_state(patch)

        def limits(patch):
            order.append("limits")
            real_limits(patch)

        monkeypatch.setattr(cu_api, "_write_state", state)
        monkeypatch.setattr(cu_api, "_write_limits", limits)
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={"enabled": True, "max_tree_nodes": 900},
            )
        assert resp.status == 200
        assert order == ["limits", "state"]


class TestEnableRestartsSessions:
    """Flipping the enable must restart sessions, the way an MCP change does.

    kiro-cli caches ``tools/list`` for the LIFETIME of a session and ACP has no
    ``tools/list_changed`` notification, so without this the operator enables the
    feature, sees "0 tools" in the chat they are sitting in, and concludes it is
    broken. ``POST /api/mcp/sync`` already resets sessions for exactly this reason;
    this pins that the enable does the same.

    The three properties that matter are all about NOT restarting more than needed:
    a budget knob is read per call, and re-saving the same value must not tear down
    the user's session.
    """

    @staticmethod
    def _spy(monkeypatch) -> list:
        """Record calls to ``_reset_all_sessions`` without touching real sessions."""
        calls: list = []
        import kiro_crew.dashboard.handlers.sessions as sessions_mod

        async def _fake(request):
            calls.append(request)
            return 3

        monkeypatch.setattr(sessions_mod, "_reset_all_sessions", _fake)
        return calls

    @pytest.mark.asyncio
    async def test_turning_it_ON_restarts_and_reports_the_count(self, state_file, monkeypatch):
        calls = self._spy(monkeypatch)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert len(calls) == 1
        # Reported so the panel can explain the restart rather than leaving the
        # operator to guess why their session reset.
        assert body["sessions_reset"] == 3

    @pytest.mark.asyncio
    async def test_turning_it_OFF_also_restarts(self, state_file, monkeypatch):
        """Symmetric: a disabled feature must stop being callable in a live session."""
        state_file.write_text(json.dumps({STATE_KEY_ENABLED: True}), encoding="utf-8")
        calls = self._spy(monkeypatch)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": False})
        assert resp.status == 200
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_NO_OP_resave_does_not_restart(self, state_file, monkeypatch):
        """Re-saving the same value must not kill the operator's session."""
        state_file.write_text(json.dumps({STATE_KEY_ENABLED: True}), encoding="utf-8")
        calls = self._spy(monkeypatch)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert calls == []
        assert body["sessions_reset"] == 0

    @pytest.mark.asyncio
    async def test_a_LIMITS_only_save_does_not_restart(self, config_file, monkeypatch):
        """Budget knobs are read per call — restarting for one would be gratuitous."""
        calls = self._spy(monkeypatch)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"max_tree_nodes": 400})
        assert resp.status == 200
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_failed_restart_does_not_fail_the_SAVE(self, state_file, monkeypatch):
        """The write already landed; reporting failure would be a lie.

        Worst case is the pre-existing behaviour — the new tool surface appears on
        the next cold session — which is strictly better than telling the operator
        their save failed when the keystone did move.
        """
        import kiro_crew.dashboard.handlers.sessions as sessions_mod

        async def _boom(request):
            raise RuntimeError("no session manager on this app")

        monkeypatch.setattr(sessions_mod, "_reset_all_sessions", _boom)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert body["enabled"] is True
        assert body["sessions_reset"] == 0
        assert json.loads(state_file.read_text())[STATE_KEY_ENABLED] is True


class TestAMalformedKeystoneStillRendersSettings:
    """GPT 5.6 BLOCKING, confirmed: a hand-edited keystone 500'd the config GET.

    ``load_policy_config`` raises on a present-but-malformed ``allowed_apps`` **by
    design** — refusing is the safe direction for a value clearly meant to narrow
    something, because coercing it to the empty tuple would silently convert a
    restriction into no restriction at all. That is right on the ACTION path.

    On the READ path it was a self-inflicted lockout: the exception escaped
    ``_snapshot()``, the Settings GET returned HTTP 500, and the only UI that can
    repair the file became unreachable. The page has to render precisely BECAUSE the
    file is broken.
    """

    @pytest.mark.asyncio
    async def test_the_GET_renders_instead_of_500ing(self, state_file: Path):
        # A bare string where a list belongs — the realistic hand-edit.
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": "Preview"}))
        async with _client() as client:
            resp = await client.get("/api/computer-use/config")
            body = await resp.json()
        assert resp.status == 200
        # And it reports the enable truthfully rather than degrading to "off", which
        # would misrepresent the machine's actual state.
        assert body["enabled"] is True

    @pytest.mark.asyncio
    async def test_the_payload_SAYS_the_policy_is_unreadable(self, state_file: Path):
        """An empty list must not read as "no restriction configured".

        Without this the panel would show a confidently empty allow-list for a file
        that actually contains one — the operator would see the restriction they
        wrote silently absent, with nothing to explain why.
        """
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": "Preview"}))
        async with _client() as client:
            body = await (await client.get("/api/computer-use/config")).json()
        assert body["allowed_apps"] == []
        assert "allowed_apps" in body["policy_error"]

    @pytest.mark.asyncio
    async def test_a_LIST_of_malformed_entries_is_handled_too(self, state_file: Path):
        """``[{"name": "Preview"}]`` is a list, so the container check passes and every
        item is dropped — the same widening bug one level down, and the same 500."""
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": [{"name": "Preview"}]}))
        async with _client() as client:
            resp = await client.get("/api/computer-use/config")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_HEALTHY_policy_reports_no_error(self, state_file: Path):
        """Inverse guard: the fallback must not fire on a well-formed file, or every
        real allow-list would render as unreadable."""
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": ["Preview", "Notes"]}))
        async with _client() as client:
            body = await (await client.get("/api/computer-use/config")).json()
        assert body["allowed_apps"] == ["preview", "notes"]
        assert body["policy_error"] == ""

    def test_the_ACTION_path_still_refuses(self, state_file: Path):
        """THE assertion that keeps this a rendering fix and not a ceiling change.

        Making the GET fail soft must not make the dispatcher fail soft: a malformed
        allow-list still has to refuse the action, or the fix would have converted an
        operator's restriction into no restriction — exactly the bug the strict mode
        exists to prevent.
        """
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": "Preview"}))
        with pytest.raises(PolicyStateError):
            enable_state.load_policy_config()
