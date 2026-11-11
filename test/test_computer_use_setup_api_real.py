"""The setup routes against the REAL ``kiro_crew.computer_use.setup`` module.

``test_computer_use_setup_api.py`` pins the handler against a fake module; these cases
pin that the real module's documents survive the handler's shaping unchanged, and run
install -> verify -> ready through the routes with a fake download and a fake driver.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.computer_use import enable_state, setup
from kiro_crew.computer_use.driver_runtime import install as install_mod
from kiro_crew.computer_use.driver_runtime import pin
from kiro_crew.computer_use.driver_runtime import session as session_mod
from kiro_crew.dashboard.handlers import computer_use_setup as setup_api
from kiro_crew.testing.wait import async_wait_until, default_timeout

OWNER = "U0OWNER0000"
X11 = session_mod.detect_linux({"DISPLAY": ":1", "DBUS_SESSION_BUS_ADDRESS": "x"}, uinput=False)
BINARY = b"#!/bin/sh\necho cua-driver 0.47.8\n"
SHA = "ab" * 32
INSTALLED = install_mod.InstallState(installed=True, version="0.47.8", binary_sha256=SHA)
MISSING = install_mod.InstallState(installed=False, detail="not installed")
#: Keys the real module adds for its own callers that the wire contract does not carry.
_LOCAL_ONLY = ("choice",)


def _status(**kw):  # type: ignore[no-untyped-def]
    args = dict(
        platform="linux",
        choice="auto",
        keystone_enabled=True,
        locked=False,
        install_state=INSTALLED,
        install_progress={},
        session=X11,
        progress={"verify": {"ok": True, "binary_sha256": SHA}},
        now=1_000_000.0,
    )
    args.update(kw)
    return setup.build_status(**args)


AQUA = session_mod.SessionInfo(kind=session_mod.KIND_AQUA, detail="desktop", supported=True)
WAYLAND_NO_X = session_mod.detect_linux(
    {"WAYLAND_DISPLAY": "w", "DBUS_SESSION_BUS_ADDRESS": "x"}, uinput=True
)

SCENARIOS = {
    "ready": _status(),
    "disabled": _status(keystone_enabled=False),
    "not_configured": _status(install_state=MISSING, progress={}),
    "installing": _status(install_state=MISSING, install_progress={"running": True}),
    "install_failed": _status(install_state=MISSING, install_progress={"error": "sha256 mismatch"}),
    "validating": _status(progress={}, verifying=True),
    "error": _status(progress={"verify": {"ok": False, "binary_sha256": SHA, "detail": "x"}}),
    "session0": _status(
        platform="windows", choice="upstream", session=session_mod.detect_windows(0)
    ),
    "no_desktop": _status(session=session_mod.detect_linux({}, uinput=False)),
    "wayland_no_x": _status(session=WAYLAND_NO_X),
    "mac_requesting": _status(platform="macos", choice="upstream", session=AQUA),
    "mac_revoked": _status(
        platform="macos",
        choice="upstream",
        session=AQUA,
        progress={
            "verify": {"ok": True, "binary_sha256": SHA},
            "permissions": {"accessibility": "missing"},
            "ever_granted": {"accessibility": True},
        },
    ),
    "mac_restart": _status(
        platform="macos",
        choice="upstream",
        session=AQUA,
        progress={
            "verify": {"ok": True, "binary_sha256": SHA},
            "permissions": {"accessibility": "granted", "screen_recording": "granted"},
            "restart_required": True,
        },
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_handler_keeps_every_real_document_intact(name: str) -> None:
    doc = SCENARIOS[name]
    expected = {k: v for k, v in doc.items() if k not in _LOCAL_ONLY}
    expected["progress"] = {"last_step": doc["progress"]["last_step"]}
    assert setup_api.shape_document(doc) == json.loads(json.dumps(expected))


def test_no_desktop_is_never_reported_as_error() -> None:
    assert SCENARIOS["session0"]["state"] == "temporarily_unavailable"
    assert SCENARIOS["session0"]["session"]["kind"] == "session0"
    assert SCENARIOS["no_desktop"]["state"] == "temporarily_unavailable"
    assert SCENARIOS["no_desktop"]["session"]["kind"] == "none"
    assert SCENARIOS["wayland_no_x"]["state"] == "unsupported"
    assert SCENARIOS["wayland_no_x"]["session"]["kind"] == "wayland"


def test_setup_fixable_refusals_name_the_settings_page() -> None:
    """The chat offers the setup button only when a refusal names this page."""
    from kiro_crew.computer_use.driver_runtime import backend as cua_backend
    from kiro_crew.computer_use.types import REFUSAL_DISABLED

    marker = "Settings -> Computer Use"
    assert marker in REFUSAL_DISABLED
    assert marker in cua_backend.ERR_NOT_INSTALLED


# ── install -> verify -> ready through the routes ──


def _tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("cua-driver")
        info.size, info.mode = len(BINARY), 0o755
        tf.addfile(info, io.BytesIO(BINARY))
    return buf.getvalue()


class _Resp(io.BytesIO):
    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, tool: str, args: dict, *, timeout: float, idempotent: bool = False) -> dict:
        self.calls.append(tool)
        if tool == "list_windows":
            return {"structuredContent": {"windows": [{"pid": 5, "window_id": 9}]}}
        if tool == "get_window_state":
            assert "screenshot_out_file" not in args, "the verify capture must stay off disk"
            return {
                "content": [{"type": "image", "mimeType": "image/png", "data": "cG5n"}],
                "structuredContent": {"elements": []},
            }
        if tool == "health_report":
            return {"structuredContent": {"overall": "ok"}}
        raise AssertionError(f"setup must not call {tool}")

    def restart(self) -> None:
        return None


def _app() -> web.Application:
    @web.middleware
    async def owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
        request["user"], request["app"] = OWNER, ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app.router.add_get("/api/computer-use/setup", setup_api.api_computer_use_setup_get)
    app.router.add_post("/api/computer-use/setup", setup_api.api_computer_use_setup_post)
    return app


@pytest.mark.asyncio
async def test_install_verify_ready_through_the_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _tar()
    asset = pin.PinnedAsset(
        os="linux",
        arch="x86_64",
        name="cua-driver-rs-0.47.8-linux-x86_64-binary.tar.gz",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        binary="cua-driver",
        binary_sha256=hashlib.sha256(BINARY).hexdigest(),
    )
    home = tmp_path / "home"
    real_install = install_mod.install

    def install(root, **kw):  # type: ignore[no-untyped-def]
        return real_install(
            root,
            asset=asset,
            os_id="linux",
            run=lambda argv, **k: __import__("subprocess").CompletedProcess(
                argv, 0, "cua-driver 0.47.8\n", ""
            ),
        )

    import types

    from kiro_crew import asset_downloader

    def open_url(req, timeout=None):  # type: ignore[no-untyped-def]
        resp = _Resp(data)
        resp.status = 200  # type: ignore[attr-defined]
        resp.headers = {}  # type: ignore[attr-defined]
        return resp

    monkeypatch.setattr(
        asset_downloader, "build_opener", lambda *_a, **_k: types.SimpleNamespace(open=open_url)
    )
    driver = _FakeDriver()
    monkeypatch.setattr(setup, "_home", lambda h: home)
    monkeypatch.setattr(setup, "_platform", lambda: "linux")
    monkeypatch.setattr(setup, "asset_for", lambda os_id: asset)
    monkeypatch.setattr(install_mod, "_pinned_for_host", lambda a: a or asset)
    monkeypatch.setattr(install_mod, "install", install)
    monkeypatch.setattr(install_mod, "PROGRESS", install_mod.InstallProgress())
    monkeypatch.setattr(session_mod, "detect", lambda: X11)
    monkeypatch.setattr(setup, "_supervisor", lambda root: driver)
    monkeypatch.setattr(setup_api, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(setup_api, "_inflight", None)
    enable_state.save_state({"enabled": True})

    async with TestClient(TestServer(_app())) as client:
        first = await (await client.get("/api/computer-use/setup")).json()
        assert first["state"] == "not_configured"
        assert [s["status"] for s in first["steps"]][0] == "current"
        await client.post("/api/computer-use/setup", json={"action": "install"})

        async def settled() -> "dict | None":
            doc = await (await client.get("/api/computer-use/setup")).json()
            return doc if doc["state"] != "installing" else None

        doc = await async_wait_until(settled, timeout=default_timeout())
        assert doc["state"] == "not_configured" and doc["driver"]["installed"] is True
        assert {s["id"]: s["status"] for s in doc["steps"]}["verify"] == "current"
        done = await (
            await client.post("/api/computer-use/setup", json={"action": "verify"})
        ).json()
    assert done["state"] == "ready", done
    assert driver.calls == ["list_windows", "get_window_state", "health_report"]
