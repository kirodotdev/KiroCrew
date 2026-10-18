"""The fixtures, middlewares and app builders the computer-use API tests share.

``test_computer_use_api.py`` and its ``_frame`` and ``_keystone`` siblings import these by
name. Four autouse fixtures apply to every test of an importing module: ``profiles_dir``
isolates the governance profile store, ``no_agent_spec_rewrite`` stubs the agent spec
rebuild, ``_reset_ctx`` resets the platform context, and ``no_real_probe`` replaces the
permission probe. The fixtures a test requests by parameter (``home``, ``state_file``,
``config_file``, ``mock_sel`` and ``profiles_dir``) are declared ``name=`` on
``*_fixture`` functions, so no parameter shadows an import. ``_make_app`` and ``_client``
mount the config and invoke handlers behind the two stand-in middlewares, ``_frame_app``
and ``_VALID_FRAME`` serve the frame ingress, and ``_GRANTED`` is the probe's all-granted
answer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.computer_use.types import PERMISSION_GRANTED, PERMISSION_UNSUPPORTED, PLATFORM_MACOS
from kiro_crew.dashboard.handlers import computer_use as cu_api
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp

_GRANTED = {
    "accessibility": PERMISSION_GRANTED,
    "screen_recording": PERMISSION_GRANTED,
    "responsible_hint": "",
}


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect KIROCREW_HOME so the keystone + config writes land in a tmp dir."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(name="state_file")
def state_file_fixture(home: Path) -> Path:
    """The keystone ``computer_use.json`` (the primary enable + target lists)."""
    return home / "computer_use.json"


@pytest.fixture(name="config_file")
def config_file_fixture(home: Path) -> Path:
    """``config.json`` — where the budget knobs live, and the enable never does."""
    return home / "config.json"


@pytest.fixture(name="profiles_dir", autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture, moved verbatim; no test in a module that imports it calls monkeypatch.undo()
def profiles_dir_fixture(tmp_path, monkeypatch):
    """Isolate the governance profile store (never read the developer's own)."""
    directory = tmp_path / "profiles"
    directory.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", directory)
    gp.reset_store()
    yield directory
    gp.reset_store()


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture, moved verbatim; no test in a module that imports it calls monkeypatch.undo()
def no_agent_spec_rewrite(monkeypatch):
    """Never let a test rewrite the operator's real ``~/.kiro/agents``.

    Flipping the computer-use enable rebuilds the agent spec (the enable is a
    spec-emission gate, so the tools would otherwise not appear until the next
    gateway start). That rebuild writes ``~/.kiro/agents/kirocrew.json``, which is
    machine-wide and deliberately NOT under the ``KIROCREW_HOME`` this module's
    ``home`` fixture redirects — so every enable-flipping test would touch the
    developer's live agent config, and a plain clone (CI, or a checkout that is not
    a git worktree) has no guard that stops it.

    Autouse rather than per-test: this file has a dozen tests that flip the enable
    for unrelated reasons (auth, validation, corrupt-file handling), and each new
    one would silently inherit the problem. The three tests that ASSERT on the
    rebuild re-patch it with their own recorder, which still wins — a later
    ``monkeypatch.setattr`` overrides this one and both unwind at teardown.
    """
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "rebuild_agent_config", lambda **_: Path("/dev/null"))


@pytest.fixture(autouse=True)
def _reset_ctx():
    yield
    ctx_mod.reset_context()


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture, moved verbatim; no test in a module that imports it calls monkeypatch.undo()
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


@pytest.fixture(name="mock_sel")
def mock_sel_fixture():
    """Patch the late-bound ``_sel()`` so SEL audit calls are observable."""
    with patch.object(cu_api, "_sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


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


@web.middleware
async def _owner_dashboard_identity(request: web.Request, handler):
    """Stand in for the owner's dashboard cookie on the browser-called config route.

    ``PUT /api/computer-use/config`` is owner-gated, so the Settings panel these
    cases model carries ``app == ""`` and the owner's subject. With no
    ``owner_id`` configured, the signed local bootstrap subject is the owner.
    """
    if request.path == "/api/computer-use/config":
        request["user"] = "local-app"
        request["app"] = ""
    return await handler(request)


def _make_app() -> web.Application:
    app = web.Application(middlewares=[_grant_internal_auth, _owner_dashboard_identity])
    app["state"] = SimpleNamespace(owner_id="")
    app.router.add_get("/api/computer-use/config", cu_api.api_computer_use_config_get)
    app.router.add_put("/api/computer-use/config", cu_api.api_computer_use_config_save)
    app.router.add_post("/api/computer-use/invoke", cu_api.api_computer_use_invoke)
    return app


def _client() -> TestClient:
    """Build an aiohttp TestClient (open with ``async with`` inside the test)."""
    return TestClient(TestServer(_make_app()))


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
