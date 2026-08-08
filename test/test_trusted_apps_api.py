"""Tests for the per-app third-party trust-grant REST API.

Covers the 4 endpoints in ``dashboard/handlers/security.py`` that write
``agent.apps_trusted`` / ``agent.apps_allow_third_party`` in ``config.json``,
plus the end-to-end property that actually matters: a grant flips
``apps.execution.app_execution_denied`` for THAT app and leaves every other
third-party app denied.

The aiohttp handlers are exercised through an in-test ``TestClient`` opened with
``async with`` (matching ``test_denied_commands_api.py``) rather than an
async-gen fixture: the CI-pinned ``pytest-asyncio==0.20.3`` is incompatible with
the pinned ``pytest==8.4.1`` for async fixtures, so the whole suite avoids
``@pytest_asyncio.fixture`` by convention.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    install_app,
)
from kiro_crew.dashboard.handlers import security
from kiro_crew.dashboard.handlers.security import (
    api_trusted_app_grant,
    api_trusted_app_revoke,
    api_trusted_apps_allow_all,
    api_trusted_apps_list,
    build_trusted_apps_snapshot,
)

_APP = "trust-test-app"
_OTHER = "other-test-app"

# A real builtin: its name is declared by a SHIPPED app.json, so it is exempt at
# the gate and must never be granted or torn down by this surface.
_BUILTIN = "file-explorer"


async def _noop_hook(name: str, record: dict, **_kw: object) -> dict:
    """Stand-in for ``on_app_disable`` when the test is about a later step.

    Accepts arbitrary keywords so a new one (``run_app_hooks``) does not turn every
    caller into a spurious "hooks disable failed" and a 409.
    """
    return {}

# A POPULATED config.json with a syntax error (trailing comma after
# max_subagents). Every distinctive value below is one ``KiroCrewConfig.load()``
# would silently replace with a default — so a mutation that loaded this file and
# saved it back would erase the model, the subagent cap, the external registry
# and the dashboard settings. The regression assertion is byte-identity, not the
# presence of the 409.
_CORRUPT_CONFIG = """{
  "agent": {
    "model": "distinctive-sentinel-model",
    "max_subagents": 7,
  },
  "registries": [
    {"name": "distinctive-registry", "repo": "acme/apps", "branch": "release"}
  ],
  "dashboard": {"port": 41999, "host": "127.0.0.101"}
}"""

# Values that must survive a refused mutation verbatim.
_CORRUPT_SENTINELS = (
    "distinctive-sentinel-model",
    '"max_subagents": 7',
    "distinctive-registry",
    "acme/apps",
    '"port": 41999',
    "127.0.0.101",
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect KIROCREW_HOME so config.json + installed apps land in tmp."""
    h = tmp_path / "kirocrew-home"
    h.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    return h


@pytest.fixture
def mock_sel():
    """Patch the late-bound ``_sel()`` so SEL audit calls are observable."""
    with patch("kiro_crew.dashboard.handlers.security._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


def _install(
    tmp_path: Path,
    name: str,
    *,
    enabled: bool = False,
    builtin: bool = False,
    entry_point: str | None = None,
    port: int | str | None = None,
    body: str = "",
) -> None:
    """Install a minimal app record under the tmp KIROCREW_HOME.

    ``builtin=True`` marks the install builtin-OWNED (``source``/``origin`` ==
    ``builtin``), which is what ``manager.builtin_owns_installed`` checks. Combined
    with a *name* a shipped ``app.json`` declares (``_BUILTIN``), that is the only
    way a name enters ``execution.builtin_app_names()``.

    ``entry_point`` declares a gateway-spawned backend so the app has real code to
    stop; *body* is written to that file. ``port`` declares that backend's port —
    a fixed number is observable after a stop attempt, ``"auto"`` is not.
    """
    source = tmp_path / "source" / name
    source.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "displayName": name,
        "description": "trust grant fixture",
        "author": "tester",
    }
    if entry_point:
        manifest["backend"] = {"entryPoint": entry_point, "runtime": "python"}
        if port is not None:
            manifest["backend"]["port"] = port
        (source / entry_point).write_text(body, encoding="utf-8")
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert install_app(source).ok
    meta = _read_installed(name)
    assert meta is not None
    meta.enabled = enabled
    meta.origin = "builtin" if builtin else "registry"
    if builtin:
        meta.source = "builtin"
    meta.resources = "gateway"
    _write_installed(name, meta)


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/security/trusted-apps", api_trusted_apps_list)
    app.router.add_put("/api/security/trusted-apps/allow-all", api_trusted_apps_allow_all)
    app.router.add_post("/api/security/trusted-apps/{name}", api_trusted_app_grant)
    app.router.add_delete("/api/security/trusted-apps/{name}", api_trusted_app_revoke)
    return app


def _client() -> TestClient:
    """Build an aiohttp TestClient — open with ``async with`` inside each test."""
    return TestClient(TestServer(_make_app()))


def _stored(home: Path) -> dict[str, Any]:
    """The persisted ``agent`` section of config.json (``{}`` before first save)."""
    path = home / "config.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    agent = data.get("agent")
    return agent if isinstance(agent, dict) else {}


# ── snapshot ──


def test_snapshot_defaults_to_empty_and_denied(home: Path):
    assert build_trusted_apps_snapshot() == {"apps": [], "ineffective": [], "allowAll": False}


def test_snapshot_sorts_and_dedupes_and_drops_junk(home: Path):
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": ["zebra", "apple", "apple", "", 5, {}]}}),
        encoding="utf-8",
    )
    assert build_trusted_apps_snapshot()["apps"] == ["apple", "zebra"]


def test_snapshot_allow_all_string_true_is_not_truthy(home: Path):
    # Mirrors the gate's strict identity check: "true" must render (and enforce)
    # as False, never coerced.
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": "true"}}), encoding="utf-8"
    )
    assert build_trusted_apps_snapshot()["allowAll"] is False


# ── GET ──


@pytest.mark.asyncio
async def test_get_returns_snapshot_shape(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.get("/api/security/trusted-apps")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"apps": [], "ineffective": [], "allowAll": False}
    # reads do not audit
    mock_sel.log_api_access.assert_not_called()


# ── grant ──


@pytest.mark.asyncio
async def test_grant_persists_and_is_idempotent(home: Path, tmp_path: Path, mock_sel):
    _install(tmp_path, _APP)
    async with _client() as client:
        first = await client.post(f"/api/security/trusted-apps/{_APP}")
        assert first.status == 200
        assert (await first.json()) == {"apps": [_APP], "ineffective": [], "allowAll": False}

        second = await client.post(f"/api/security/trusted-apps/{_APP}")
        assert second.status == 200
        # Idempotent: no duplicate entry in the response or on disk.
        assert (await second.json())["apps"] == [_APP]

    assert _stored(home)["apps_trusted"] == [_APP]
    ops = [c.kwargs["operation"] for c in mock_sel.log_api_access.call_args_list]
    assert ops == ["security.trusted_apps.grant"] * 2
    assert all(c.kwargs["outcome"] == "ok" for c in mock_sel.log_api_access.call_args_list)


@pytest.mark.asyncio
async def test_grant_of_uninstalled_app_404s_with_code(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.post("/api/security/trusted-apps/ghost-app")
        assert resp.status == 404
        body = await resp.json()
        assert body["code"] == "app_not_installed"
        assert body["error"]
    assert build_trusted_apps_snapshot()["apps"] == []
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["-leading", "Upper", "has.dot", "has%20space"])
async def test_grant_invalid_name_400s_with_code(home: Path, mock_sel, name: str):
    async with _client() as client:
        resp = await client.post(f"/api/security/trusted-apps/{name}")
        assert resp.status == 400
        body = await resp.json()
        assert body["code"] == "invalid_app_name"
    assert build_trusted_apps_snapshot()["apps"] == []


@pytest.mark.asyncio
async def test_grant_preserves_existing_grants(home: Path, tmp_path: Path, mock_sel):
    _install(tmp_path, _APP)
    _install(tmp_path, _OTHER)
    async with _client() as client:
        await client.post(f"/api/security/trusted-apps/{_APP}")
        resp = await client.post(f"/api/security/trusted-apps/{_OTHER}")
        assert (await resp.json())["apps"] == sorted([_APP, _OTHER])


# ── revoke ──


@pytest.mark.asyncio
async def test_revoke_removes_grant_and_is_idempotent(home: Path, tmp_path: Path, mock_sel):
    _install(tmp_path, _APP)
    async with _client() as client:
        await client.post(f"/api/security/trusted-apps/{_APP}")

        first = await client.delete(f"/api/security/trusted-apps/{_APP}")
        assert first.status == 200
        # `warnings` carries anything the teardown wants the operator to LEARN while
        # still reporting success — the leading case is the app's own on_shutdown
        # hook failing, which loses buffered state without leaving code running.
        # Empty here: this app was never enabled, so nothing was torn down.
        assert (await first.json()) == {
            "apps": [],
            "ineffective": [],
            "allowAll": False,
            "disabled": False,
            "warnings": [],
        }

        # Revoking a name that holds no grant is a 200, not a 404.
        second = await client.delete(f"/api/security/trusted-apps/{_APP}")
        assert second.status == 200
        assert (await second.json())["apps"] == []

        never = await client.delete("/api/security/trusted-apps/never-granted")
        assert never.status == 200
        assert (await never.json())["disabled"] is False

    assert _stored(home)["apps_trusted"] == []


@pytest.mark.asyncio
async def test_revoke_disables_a_currently_enabled_app(home: Path, tmp_path: Path, mock_sel):
    _install(tmp_path, _APP, enabled=True)
    async with _client() as client:
        await client.post(f"/api/security/trusted-apps/{_APP}")
        resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
        assert resp.status == 200
        body = await resp.json()

    # Revocation is EFFECTIVE, not merely declarative: the app's code stops.
    assert body["disabled"] is True
    meta = _read_installed(_APP)
    assert meta is not None and meta.enabled is False

    revokes = [
        c
        for c in mock_sel.log_api_access.call_args_list
        if c.kwargs["operation"] == "security.trusted_apps.revoke"
    ]
    assert revokes and "disabled=True" in revokes[-1].kwargs["resources"]


@pytest.mark.asyncio
async def test_revoke_of_disabled_app_reports_not_disabled(home: Path, tmp_path: Path, mock_sel):
    _install(tmp_path, _APP, enabled=False)
    async with _client() as client:
        await client.post(f"/api/security/trusted-apps/{_APP}")
        resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
        assert (await resp.json())["disabled"] is False


# ── allow-all ──


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False])
async def test_allow_all_sets_the_blanket_flag(home: Path, mock_sel, value: bool):
    async with _client() as client:
        resp = await client.put("/api/security/trusted-apps/allow-all", json={"value": value})
        assert resp.status == 200
        assert (await resp.json())["allowAll"] is value
    assert _stored(home)["apps_allow_third_party"] is value


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"value": "true"}, {"value": 1}, {}, [], "nope"])
async def test_allow_all_rejects_non_bool_with_code(home: Path, mock_sel, body: Any):
    async with _client() as client:
        resp = await client.put("/api/security/trusted-apps/allow-all", json=body)
        assert resp.status == 400
        payload = await resp.json()
        assert payload["code"] == "invalid_value"
        assert payload["error"]
    assert build_trusted_apps_snapshot()["allowAll"] is False


@pytest.mark.asyncio
async def test_allow_all_rejects_unparseable_body_with_code(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.put(
            "/api/security/trusted-apps/allow-all",
            data="{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_value"


# ── the property that matters: a grant changes enforcement ──


@pytest.mark.asyncio
async def test_grant_admits_only_that_app(home: Path, tmp_path: Path, mock_sel):
    from kiro_crew.apps.execution import app_execution_denied

    _install(tmp_path, _APP)
    _install(tmp_path, _OTHER)

    # Baseline: both third-party apps are denied.
    assert app_execution_denied(_APP, action="enable") is not None
    assert app_execution_denied(_OTHER, action="enable") is not None

    async with _client() as client:
        assert (await client.post(f"/api/security/trusted-apps/{_APP}")).status == 200

    # The grant is narrow: it admits exactly the app named, nothing else.
    assert app_execution_denied(_APP, action="enable") is None
    assert app_execution_denied(_OTHER, action="enable") is not None

    async with _client() as client:
        assert (await client.delete(f"/api/security/trusted-apps/{_APP}")).status == 200

    # And revoking it takes effect immediately — no restart.
    assert app_execution_denied(_APP, action="enable") is not None


# ══════════════════════════════════════════════════════════════════════════
# Regression tests — five defects an adversarial review proved, each of which
# the suite above did NOT catch. Every test below asserts a POSTCONDITION
# (bytes on disk, a process's liveness, what the gate admits), not that a write
# happened, because "the metadata was written" is exactly what let these
# through.
# ══════════════════════════════════════════════════════════════════════════


# ── 1. a corrupt config.json is never clobbered ──


def _drive(client: TestClient, op: str, name: str):
    """Issue one of the three MUTATING trusted-apps requests."""
    if op == "grant":
        return client.post(f"/api/security/trusted-apps/{name}")
    if op == "revoke":
        return client.delete(f"/api/security/trusted-apps/{name}")
    return client.put("/api/security/trusted-apps/allow-all", json={"value": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["grant", "revoke", "allow_all"])
async def test_mutation_on_corrupt_config_409s_without_clobbering(
    home: Path, tmp_path: Path, mock_sel, op: str
):
    # REGRESSION: ``KiroCrewConfig.load()`` degrades an unparseable config.json to
    # DEFAULTS. A blind load→mutate→save would therefore write those defaults over
    # a populated file and silently erase the model, the subagent cap, the external
    # registry and the dashboard settings — total config loss from a trailing
    # comma. ``_mutate_agent_config`` pre-flight-parses inside the config lock and
    # raises ConfigCorruptError instead. Byte-identity is the assertion that
    # matters; the 409 is only how the refusal is reported.
    _install(tmp_path, _APP)  # so grant fails on corruption, not on existence
    path = home / "config.json"
    path.write_text(_CORRUPT_CONFIG, encoding="utf-8")

    async with _client() as client:
        resp = await _drive(client, op, _APP)
        body = await resp.json()

    # POSTCONDITION FIRST: the file is untouched, byte-for-byte — not merely
    # "still parses". Asserted before the status code so a regression reports the
    # data loss it caused rather than a status mismatch.
    after = path.read_text(encoding="utf-8")
    # Spelled out per value so a partial rewrite names what it destroyed.
    for sentinel in _CORRUPT_SENTINELS:
        assert sentinel in after, f"mutation destroyed {sentinel!r} in config.json"
    assert after == _CORRUPT_CONFIG, "config.json was rewritten"

    # Only then: the refusal is reported as a coded 409 and audited.
    assert resp.status == 409
    assert body["code"] == "config_corrupt"
    assert body["error"]
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_corrupt_config_refusal_does_not_create_the_file(home: Path, mock_sel):
    # The pre-flight only guards an EXISTING file: with no config.json at all a
    # mutation must still succeed (first-ever write), so the guard cannot be
    # implemented as "refuse whenever the parse fails".
    async with _client() as client:
        resp = await client.put("/api/security/trusted-apps/allow-all", json={"value": True})
        assert resp.status == 200
    assert (home / "config.json").is_file()
    assert _stored(home)["apps_allow_third_party"] is True


# ── 2. revoke actually stops the running code ──

_LOOP_APP = "trust-loop-app"
_BEACON = "beacon.txt"


def _beacon_body(beacon: Path) -> str:
    """A backend that proves it is still executing by advancing a counter."""
    return (
        "import pathlib\n"
        "import time\n"
        f"p = pathlib.Path({str(beacon)!r})\n"
        "i = 0\n"
        "while True:\n"
        "    i += 1\n"
        "    p.write_text(str(i))\n"
        "    time.sleep(0.05)\n"
    )


def _await_beacon(beacon: Path, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if beacon.exists():
            text = beacon.read_text(encoding="utf-8")
            if text:
                return text
        time.sleep(0.05)
    raise AssertionError(f"backend never wrote {beacon}")


@pytest.mark.asyncio
async def test_revoke_stops_the_backend_process_and_the_beacon(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: revoke used to be a METADATA write (``disable_app`` only). The
    # enabled flag flipped and the endpoint reported success while the app's
    # backend process kept running with its app secret, its routes stayed proxied
    # and its crons stayed armed — i.e. third-party code the operator had just
    # un-trusted was still executing. The assertion is the PROCESS, not the flag.
    from kiro_crew.apps import backend as appbackend

    # This test spawns a REAL backend, and spawning goes through the sandbox. CI
    # runners have no backend for it (Linux ``unshare`` is EPERM in the container,
    # Windows has none at all), so without this the test only passes on a macOS
    # dev box. The subject here is teardown, not isolation, so the isolated home
    # opts out explicitly rather than the test silently skipping in CI — a skipped
    # regression test for a shipped exploit is not a regression test.
    (home / "config.json").write_text(
        json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": True}}),
        encoding="utf-8",
    )

    beacon = home / _BEACON
    _install(
        tmp_path,
        _LOOP_APP,
        enabled=True,
        entry_point="server.py",
        body=_beacon_body(beacon),
    )

    async with _client() as client:
        assert (await client.post(f"/api/security/trusted-apps/{_LOOP_APP}")).status == 200

    loop = asyncio.get_running_loop()
    proc = await loop.run_in_executor(None, appbackend.start_app_backend, _LOOP_APP)
    if proc is None:  # pragma: no cover - environment could not spawn a backend
        pytest.skip("backend process could not be started in this environment")

    try:
        # Precondition: the third-party code is alive and demonstrably running.
        assert proc.proc.poll() is None
        assert _LOOP_APP in appbackend._processes
        before = _await_beacon(beacon)

        async with _client() as client:
            resp = await client.delete(f"/api/security/trusted-apps/{_LOOP_APP}")
            assert resp.status == 200
            assert (await resp.json())["disabled"] is True

        # POSTCONDITION: the process is gone, the registry entry with it, and the
        # beacon has stopped advancing (no orphan still writing).
        assert proc.proc.poll() is not None, "backend survived the revoke"
        assert _LOOP_APP not in appbackend._processes
        after_revoke = beacon.read_text(encoding="utf-8")
        time.sleep(0.5)
        assert beacon.read_text(encoding="utf-8") == after_revoke, (
            "beacon advanced after revoke — third-party code is still executing"
        )
        assert before is not None
    finally:
        await loop.run_in_executor(None, appbackend.stop_app_backend, _LOOP_APP)


@pytest.mark.asyncio
async def test_revoke_runs_the_full_teardown_in_order(home: Path, tmp_path: Path, mock_sel):
    # Companion to the process test: pins the ORDER of the teardown so a refactor
    # cannot reintroduce a metadata-only revoke, or reorder it so the flag flips
    # before the code stops (a window in which the app is disabled-but-running).
    # Patch on apps.teardown, the module that LOOKS THESE UP: it binds them at
    # import so patching their defining modules would not affect the sequence
    # under test ("patch where it's used, not where it's defined").
    import kiro_crew.apps.teardown as appteardown
    from kiro_crew.apps.manager import AppResult

    calls: list[str] = []

    async def _on_app_disable(name: str, record: dict, **_kw: object) -> dict:
        calls.append(f"on_app_disable:{name}")
        return {}

    with (
        patch.object(appteardown, "on_app_disable", _on_app_disable),
        patch.object(
            appteardown,
            "stop_app_backend",
            lambda name: calls.append(f"stop_app_backend:{name}") or True,
        ),
        patch.object(
            appteardown,
            "deregister_app",
            lambda name: calls.append(f"deregister_app:{name}"),
        ),
        patch.object(
            security,
            "disable_app",
            lambda name: (
                calls.append(f"disable_app:{name}"),
                AppResult(ok=True, name=name),
            )[1],
        ),
    ):
        _install(tmp_path, _APP, enabled=True)
        async with _client() as client:
            await client.post(f"/api/security/trusted-apps/{_APP}")
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert (await resp.json())["disabled"] is True

    # Shutdown hooks → backend process → bridge deregistration → enabled flag.
    # ``disable_app`` LAST: the flag is the cheapest step and the least
    # security-relevant, so it must not gate the ones that stop real code.
    assert calls == [
        f"on_app_disable:{_APP}",
        f"stop_app_backend:{_APP}",
        f"deregister_app:{_APP}",
        f"disable_app:{_APP}",
    ]


@pytest.mark.asyncio
async def test_revoke_fails_when_an_untracked_backend_is_still_listening(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: `stop_app_backend` returns False both for "nothing to stop" and
    # for "something is running that I did not stop", and the teardown discarded the
    # flag entirely — so a fixed-port backend the gateway never adopted at boot
    # (`lsof` unavailable, so adoption was skipped) survived the revoke while the
    # endpoint dropped the grant and answered 200. Third variant of the
    # "revocation that revokes nothing" defect this module exists to prevent.
    import kiro_crew.apps.teardown as appteardown

    with (
        patch.object(appteardown, "stop_app_backend", lambda name: False),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: 9137),
    ):
        _install(tmp_path, _APP, enabled=True)
        async with _client() as client:
            await client.post(f"/api/security/trusted-apps/{_APP}")
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "teardown_incomplete"
            assert any("9137" in f for f in body["failures"])

    # The grant is deliberately LEFT so the client can retry — a revoke that could
    # not stop the code must not report the permission as withdrawn.
    assert _APP in build_trusted_apps_snapshot()["apps"]


@pytest.mark.asyncio
async def test_revoke_succeeds_when_the_backend_was_merely_dead(
    home: Path, tmp_path: Path, mock_sel
):
    # The other half of the asymmetry, and the reason the fix observes the port
    # instead of trusting the flag: an ENABLED app whose backend had already
    # crashed also yields False. Treating that as a teardown failure would make the
    # grant unrevokable forever — refusing to withdraw a permission is worse than
    # the window it would close.
    import kiro_crew.apps.teardown as appteardown

    with (
        patch.object(appteardown, "stop_app_backend", lambda name: False),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: None),
    ):
        _install(tmp_path, _APP, enabled=True)
        async with _client() as client:
            await client.post(f"/api/security/trusted-apps/{_APP}")
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 200
            assert (await resp.json())["disabled"] is True

    assert _APP not in build_trusted_apps_snapshot()["apps"]


def test_unstopped_backend_port_only_answers_for_a_declared_fixed_port(
    home: Path, tmp_path: Path
):
    # The probe must not guess. `port: auto` lived only in the tracking entry
    # `stop_app_backend` just dropped, so there is nothing left to observe and the
    # answer is "cannot tell" (None), never a fabricated port.
    from kiro_crew.apps import backend as appbackend

    _install(tmp_path, "auto-port-app", entry_point="main.py", port="auto")
    with patch.object(appbackend, "_port_is_listening", lambda port: True):
        assert appbackend.unstopped_backend_port("auto-port-app") is None

    # A declared fixed port IS observable — and only a listening one is reported.
    _install(tmp_path, "fixed-port-app", entry_point="main.py", port=9137)
    with patch.object(appbackend, "_port_is_listening", lambda port: port == 9137):
        assert appbackend.unstopped_backend_port("fixed-port-app") == 9137
    with patch.object(appbackend, "_port_is_listening", lambda port: False):
        assert appbackend.unstopped_backend_port("fixed-port-app") is None

    # No declared backend at all: nothing the gateway spawned, nothing to claim.
    _install(tmp_path, "no-backend-app")
    with patch.object(appbackend, "_port_is_listening", lambda port: True):
        assert appbackend.unstopped_backend_port("no-backend-app") is None


@pytest.mark.asyncio
async def test_revoke_tears_down_even_when_metadata_says_disabled(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: teardown was gated on the PERSISTED `enabled` flag, but that flag
    # is metadata rather than evidence about the runtime. `manager.disable_app` is a
    # pure metadata write, and `kirocrew app disable` calls it from a DIFFERENT
    # PROCESS that cannot reach the gateway's backend child — so "recorded as
    # disabled" and "its code is running" are routinely both true. The gate
    # therefore skipped teardown for exactly the apps whose recorded state was
    # least trustworthy, and revoke answered 200 while third-party code kept going.
    import kiro_crew.apps.teardown as appteardown

    torn: list[str] = []

    async def _teardown(name: str, record: dict, **_kw):
        torn.append(name)
        return appteardown.TeardownResult(warnings=[], failures=[])

    _install(tmp_path, _APP, enabled=False)
    with patch.object(security, "teardown_app_runtime", _teardown):
        async with _client() as client:
            await client.post(f"/api/security/trusted-apps/{_APP}")
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 200
            # `disabled` still answers "did WE switch it off" — the app was already
            # recorded off, so claiming a disable would report a change we did not
            # make. The teardown ran regardless; that is the whole point.
            assert (await resp.json())["disabled"] is False

    assert torn == [_APP]


@pytest.mark.asyncio
async def test_revoke_of_a_builtin_still_skips_teardown_when_disabled(
    home: Path, tmp_path: Path, mock_sel
):
    # The un-gating must NOT widen which apps get torn down. A builtin is exempt at
    # the execution gate, so no grant governs it and revoking a junk entry naming
    # one must never stop shipped code — enabled or not.
    torn: list[str] = []

    async def _teardown(name: str, record: dict, **_kw):
        torn.append(name)
        raise AssertionError("a builtin must never be torn down by revoke")

    _install(tmp_path, _BUILTIN, enabled=False, builtin=True)
    with patch.object(security, "teardown_app_runtime", _teardown):
        async with _client() as client:
            resp = await client.delete(f"/api/security/trusted-apps/{_BUILTIN}")
            assert resp.status == 200

    assert torn == []


@pytest.mark.asyncio
async def test_blanket_off_sweeps_an_app_whose_metadata_says_disabled(
    home: Path, tmp_path: Path, mock_sel
):
    # Same hazard on the falling edge: the candidate query filtered on `enabled`,
    # so an app the CLI had "disabled" while its backend kept running was excluded
    # from the sweep that exists to stop it.
    import kiro_crew.apps.teardown as appteardown

    torn: list[str] = []

    async def _teardown(name: str, record: dict, **_kw):
        torn.append(name)
        return appteardown.TeardownResult(warnings=[], failures=[])

    _install(tmp_path, _APP, enabled=False)
    with patch.object(security, "teardown_app_runtime", _teardown):
        async with _client() as client:
            assert (
                await client.put(
                    "/api/security/trusted-apps/allow-all", json={"value": True}
                )
            ).status == 200
            off = await client.put(
                "/api/security/trusted-apps/allow-all", json={"value": False}
            )
            assert off.status == 200
            body = await off.json()

    # Swept exactly once: the pre-write pass runs unconditionally, and the bounded
    # second pass stays `require_enabled=True` so it cannot re-run a shutdown hook
    # the loader would now deny.
    assert torn == [_APP]
    # It was already off, so it is not claimed as an app this action stopped.
    assert body["stopped"] == []
    assert body["stillRunning"] == []


@pytest.mark.asyncio
async def test_revoke_probes_the_port_even_when_the_stop_reported_success(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: the port probe was gated on `stop_app_backend` returning False,
    # which re-introduced the very mistake the surrounding comment argues against —
    # trusting a claim about the runtime instead of looking at it. A `True` return
    # only says "the process I was TRACKING is gone"; it says nothing about a
    # detached worker the app spawned itself, which keeps the declared fixed port
    # and keeps executing. Revoke then reported success over live code.
    import kiro_crew.apps.teardown as appteardown

    probed: list[str] = []

    def _probe(name: str, **_kw):
        probed.append(name)
        return 9137

    _install(tmp_path, _APP, enabled=True)
    with (
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "unstopped_backend_port", _probe),
    ):
        async with _client() as client:
            await client.post(f"/api/security/trusted-apps/{_APP}")
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "teardown_incomplete"
            assert any("9137" in f for f in body["failures"])

    assert probed == [_APP]
    # Grant deliberately left so the client can retry.
    assert _APP in build_trusted_apps_snapshot()["apps"]


@pytest.mark.asyncio
async def test_blanket_off_sweeps_an_app_that_calls_itself_builtin(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: the candidate query skipped `origin == "builtin"`, but `origin`
    # lives in the app's own `installed.json` record — writable by any app trusted
    # to run code. A trusted app could therefore stamp itself first-party and walk
    # out of the sweep that exists to stop it. `builtin_app_names()` is the only
    # forgery-proof source (it requires a SHIPPED app.json), and its own contract
    # says installed.json may narrow trust but never widen it.
    import kiro_crew.apps.teardown as appteardown
    from kiro_crew.apps.manager import _read_installed, _write_installed

    torn: list[str] = []

    async def _teardown(name: str, record: dict, **_kw):
        torn.append(name)
        return appteardown.TeardownResult(warnings=[], failures=[])

    _install(tmp_path, _APP, enabled=True)
    # Forge first-party provenance the way a trusted app could.
    meta = _read_installed(_APP)
    assert meta is not None
    meta.origin = "builtin"
    _write_installed(_APP, meta)

    with patch.object(security, "teardown_app_runtime", _teardown):
        async with _client() as client:
            assert (
                await client.put(
                    "/api/security/trusted-apps/allow-all", json={"value": True}
                )
            ).status == 200
            off = await client.put(
                "/api/security/trusted-apps/allow-all", json={"value": False}
            )
            assert off.status == 200

    # The forged claim buys nothing: the app is still swept.
    assert torn == [_APP]


@pytest.mark.asyncio
async def test_teardown_deregisters_even_when_the_app_calls_itself_self_managed(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: deregistration was gated on `record.get("resources") == "gateway"`,
    # but `resources` is a field of the app's own `installed.json` record — writable
    # by any app trusted to run code. An app could declare itself self-managed and
    # skip the branch entirely, leaving its agents, skills, crons and MCP servers
    # registered after trust was withdrawn: the same "stale execution surface left
    # behind" defect the branch's own comment describes, through a different door.
    import kiro_crew.apps.teardown as appteardown
    from kiro_crew.apps.manager import _read_installed, _write_installed

    dereg: list[str] = []

    _install(tmp_path, _APP, enabled=True)
    meta = _read_installed(_APP)
    assert meta is not None
    # Forge the self-report the gate used to trust.
    meta.lifecycle = "app"
    _write_installed(_APP, meta)
    from kiro_crew.apps.manager import INSTALLED_META_FILENAME, app_dir

    meta_path = app_dir(_APP) / INSTALLED_META_FILENAME
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    raw["resources"] = "app"
    meta_path.write_text(json.dumps(raw), encoding="utf-8")

    with (
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: None),
        patch.object(appteardown, "recorded_backend_port", lambda name: None),
        patch.object(appteardown, "deregister_app", lambda name: dereg.append(name)),
    ):
        async with _client() as client:
            await client.post(f"/api/security/trusted-apps/{_APP}")
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 200

    # The forged claim buys nothing: registered resources are still torn down.
    assert dereg == [_APP]


def test_unstopped_backend_port_prefers_the_gateway_recorded_port(
    home: Path, tmp_path: Path
):
    # REGRESSION: the probe read the port from `get_app_manifest`, i.e. `app.json`
    # INSIDE the app directory — writable by any app trusted to run code. An app
    # could relabel its port (or claim `auto`) and hide from the liveness check that
    # decides whether revoke may report success. The gateway's own record wins.
    from kiro_crew.apps import backend as appbackend

    # The app declares `auto`, so the manifest path alone can observe nothing...
    _install(tmp_path, _APP, enabled=True, entry_point="main.py", port="auto")
    with patch.object(appbackend, "_port_is_listening", lambda port: True):
        assert appbackend.unstopped_backend_port(_APP) is None
        # ...but the port the gateway actually recorded is still checkable.
        assert appbackend.unstopped_backend_port(_APP, port_hint=9137) == 9137
    with patch.object(appbackend, "_port_is_listening", lambda port: False):
        assert appbackend.unstopped_backend_port(_APP, port_hint=9137) is None


@pytest.mark.asyncio
async def test_disable_honors_the_resources_contract_but_revoke_does_not(
    home: Path, tmp_path: Path, mock_sel
):
    # Two callers, two jobs, and the difference is the point.
    #
    # An ordinary DISABLE is a lifecycle operation: `resources: "app"` means the app
    # registered its own agents/skills/crons/MCP servers and owns them, and
    # `deregister_app` removes by app-name prefix without asking who created an
    # entry — so honoring the field is required or a routine off-switch destroys
    # app-owned state.
    #
    # REVOKING TRUST is a security operation: `resources` lives in the app's own
    # installed.json, so honoring it there would hand a trusted app a switch for
    # evading its own teardown. Same teardown, opposite treatment of the same field.
    import kiro_crew.apps.teardown as appteardown
    from kiro_crew.apps.manager import INSTALLED_META_FILENAME, app_dir

    dereg: list[str] = []

    _install(tmp_path, _APP, enabled=True)
    meta_path = app_dir(_APP) / INSTALLED_META_FILENAME
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    raw["resources"] = "app"
    meta_path.write_text(json.dumps(raw), encoding="utf-8")
    record = {"resources": "app"}

    with (
        patch.object(appteardown, "on_app_disable", _noop_hook),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "recorded_backend_port", lambda name: None),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: None),
        patch.object(appteardown, "deregister_app", lambda name: dereg.append(name)),
    ):
        # Ordinary disable: the app's own registrations survive.
        assert (await appteardown.teardown_app_runtime(_APP, record)).ok
        assert dereg == []

        # Withdrawing trust: the self-report buys nothing.
        assert (
            await appteardown.teardown_app_runtime(
                _APP, record, withdrawing_trust=True
            )
        ).ok
        assert dereg == [_APP]


@pytest.mark.asyncio
async def test_teardown_runs_the_apps_own_on_disable_when_withdrawing_trust(
    home: Path, tmp_path: Path
):
    # REGRESSION: `onDisable` lived only in the disable HANDLER, so revoking a grant
    # went straight to the shared teardown and skipped it. `onEnable` can start
    # something the gateway never tracked — a detached helper — and `onDisable` is
    # the only thing that knows how to stop it. So revoke stopped the tracked
    # backend, returned 200, and left that helper executing after its permission to
    # execute had been withdrawn: revoke was WEAKER than a plain off-switch, which
    # is backwards.
    import kiro_crew.apps.teardown as appteardown

    ran: list[tuple[str, int]] = []

    async def _script(name, script, *, timeout=30, action="", **kw):
        ran.append((script, timeout))
        return {"output": "", "failed": False}

    _install(tmp_path, _APP, enabled=True)
    record = {
        "resources": "app",
        "enabled": True,
        "manifest": {"setup": {"onDisable": "stop-my-helper", "onDisableTimeout": 7}},
    }

    with (
        patch.object(appteardown, "on_app_disable", _noop_hook),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "recorded_backend_port", lambda name: None),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: None),
        patch.object(appteardown, "deregister_app", lambda name: None),
        patch.object(appteardown, "run_lifecycle_script", _script),
    ):
        result = await appteardown.teardown_app_runtime(
            _APP, record, withdrawing_trust=True
        )

    assert result.ok, result.failures
    assert ran == [("stop-my-helper", 7)], "revoke did not run the app's own onDisable"


@pytest.mark.asyncio
async def test_a_disabled_apps_own_code_is_not_launched_by_an_ordinary_disable(
    home: Path, tmp_path: Path
):
    # An ORDINARY disable of an app that is off and has no observed port must not
    # launch the app's own code: there is nothing to stop, and no security urgency
    # that would justify running a third-party script.
    #
    # Deliberately NOT asserted for the revoke path — see
    # test_a_revoke_always_runs_the_shutdown_code_even_when_disabled. A detached
    # helper survives `kirocrew app disable` (metadata-only, cross-process) with no
    # observed port, and `onDisable` is the only thing that can stop it, so a revoke
    # cannot afford to skip it.
    import kiro_crew.apps.teardown as appteardown

    ran: list[str] = []

    async def _script(name, script, **kw):
        ran.append(script)
        return {"output": "", "failed": False}

    async def _hook(name, record, **kw):
        ran.append(f"hook:run_app_hooks={kw.get('run_app_hooks')}")
        return {}

    dereg: list[str] = []
    stopped: list[str] = []
    _install(tmp_path, _APP, enabled=False)
    record = {
        "resources": "app",
        "enabled": False,
        "manifest": {"setup": {"onDisable": "should-not-run"}},
    }

    with (
        patch.object(appteardown, "on_app_disable", _hook),
        patch.object(appteardown, "stop_app_backend", lambda n: stopped.append(n)),
        patch.object(appteardown, "recorded_backend_port", lambda n: None),
        patch.object(appteardown, "unstopped_backend_port", lambda n, **kw: None),
        patch.object(appteardown, "deregister_app", lambda n: dereg.append(n)),
        patch.object(appteardown, "run_lifecycle_script", _script),
    ):
        result = await appteardown.teardown_app_runtime(_APP, record)

    assert result.ok, result.failures
    assert "should-not-run" not in ran, (
        "an ordinary disable launched an idle app's own script"
    )
    assert "hook:run_app_hooks=False" in ran, ran
    # Everything that STOPS something still runs — the flag never skips those.
    assert stopped == [_APP], "the backend stop was skipped"
    # Deregistration correctly does NOT happen on an ordinary disable: that honours
    # the app's `resources` contract, since `deregister_app` removes by app-name
    # prefix without asking who created the entry. Only a trust withdrawal ignores
    # that contract.
    assert dereg == [], "an ordinary disable destroyed app-owned registrations"


@pytest.mark.asyncio
async def test_a_revoke_always_runs_the_shutdown_code_even_when_disabled(
    home: Path, tmp_path: Path
):
    # REGRESSION: gating the app's own shutdown code on `enabled or observed port`
    # reopened a hole on the REVOKE path. `kirocrew app disable` is metadata-only and
    # runs in another process, so `enabled` can read false while a helper the app
    # detached is still alive. That helper is not the tracked backend, so no port is
    # observed either — and `onDisable` is the only thing that knows how to stop it.
    # Skipping it left third-party code running after its permission was revoked.
    import kiro_crew.apps.teardown as appteardown

    ran: list[str] = []

    async def _script(name, script, **kw):
        ran.append(script)
        return {"output": "", "failed": False}

    hook_kwargs: list[object] = []

    async def _hook(name, record, **kw):
        hook_kwargs.append(kw.get("run_app_hooks"))
        return {}

    _install(tmp_path, _APP, enabled=False)
    record = {
        "resources": "app",
        "enabled": False,
        "manifest": {"setup": {"onDisable": "stop-my-helper"}},
    }

    with (
        patch.object(appteardown, "on_app_disable", _hook),
        patch.object(appteardown, "stop_app_backend", lambda n: True),
        patch.object(appteardown, "recorded_backend_port", lambda n: None),
        patch.object(appteardown, "unstopped_backend_port", lambda n, **kw: None),
        patch.object(appteardown, "deregister_app", lambda n: None),
        patch.object(appteardown, "run_lifecycle_script", _script),
    ):
        result = await appteardown.teardown_app_runtime(
            _APP, record, withdrawing_trust=True
        )

    assert result.ok, result.failures
    assert ran == ["stop-my-helper"], (
        "a revoke skipped the only thing that can stop a detached helper"
    )
    assert hook_kwargs == [True], hook_kwargs


@pytest.mark.asyncio
async def test_an_observed_port_overrides_a_disabled_flag(home: Path, tmp_path: Path):
    # The flag can only ever run LESS of the app's code, never suppress on its own:
    # `installed.json` is app-writable, so an app could mark itself disabled while its
    # backend keeps serving. An OBSERVED port therefore overrides the metadata and the
    # shutdown path runs anyway.
    import kiro_crew.apps.teardown as appteardown

    ran: list[str] = []

    async def _script(name, script, **kw):
        ran.append(script)
        return {"output": "", "failed": False}

    _install(tmp_path, _APP, enabled=False)
    record = {
        "resources": "app",
        "enabled": False,
        "manifest": {"setup": {"onDisable": "stop-my-helper"}},
    }

    with (
        patch.object(appteardown, "on_app_disable", _noop_hook),
        patch.object(appteardown, "stop_app_backend", lambda n: True),
        patch.object(appteardown, "recorded_backend_port", lambda n: 51234),
        patch.object(appteardown, "unstopped_backend_port", lambda n, **kw: None),
        patch.object(appteardown, "deregister_app", lambda n: None),
        patch.object(appteardown, "run_lifecycle_script", _script),
    ):
        result = await appteardown.teardown_app_runtime(
            _APP, record, withdrawing_trust=True
        )

    assert result.ok, result.failures
    assert ran == ["stop-my-helper"], (
        "an app serving on an observed port was believed when it claimed to be off"
    )


@pytest.mark.asyncio
async def test_a_failing_on_disable_warns_but_never_refuses_the_withdrawal(
    home: Path, tmp_path: Path
):
    # `onDisable` is the APP'S OWN code, so treating its failure as fatal would hand
    # any app a switch for blocking the withdrawal of its own trust — exit non-zero
    # and keep the grant. Same call as `hooks_shutdown`, for a sharper reason: a
    # warning the operator sees, and the rest of the teardown still runs.
    import kiro_crew.apps.teardown as appteardown

    async def _script(name, script, **kw):
        return {"output": "refusing to stop", "failed": True}

    stopped: list[str] = []
    _install(tmp_path, _APP, enabled=True)
    # `enabled: True` matters: teardown only runs the app's OWN shutdown code
    # when there is reason to believe it is running. The not-running case is
    # covered by test_a_disabled_apps_own_code_is_not_launched_by_a_revoke.
    record = {
        "resources": "app",
        "enabled": True,
        "manifest": {"setup": {"onDisable": "boom"}},
    }

    with (
        patch.object(appteardown, "on_app_disable", _noop_hook),
        patch.object(appteardown, "stop_app_backend", lambda name: stopped.append(name)),
        patch.object(appteardown, "recorded_backend_port", lambda name: None),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: None),
        patch.object(appteardown, "deregister_app", lambda name: None),
        patch.object(appteardown, "run_lifecycle_script", _script),
    ):
        result = await appteardown.teardown_app_runtime(
            _APP, record, withdrawing_trust=True
        )

    assert result.ok is True, "a broken onDisable made trust unrevocable"
    assert result.failures == []
    assert any("onDisable" in w for w in result.warnings), result.warnings
    assert stopped == [_APP], "teardown stopped early instead of pushing through"


@pytest.mark.asyncio
async def test_teardown_notes_are_scrubbed_of_exfiltration_urls(
    home: Path, tmp_path: Path
):
    # REGRESSION: these notes were scrubbed with `redact_credentials` alone, which
    # masks the key but leaves the DESTINATION intact — `https://evil.example.com/`
    # survived, so a failing onDisable could name where it shipped data to. The
    # disable handler happened to apply both passes on the way out via its own
    # `_redact_warning`, but the revoke handler returns `warnings` straight on its
    # 200 with no redaction, so this is the only place it happens for a revoke.
    import kiro_crew.apps.teardown as appteardown

    async def _script(name, script, **kw):
        return {
            "output": "posted to https://evil.example.com/AKIAIOSFODNN7EXAMPLE",
            "failed": True,
        }

    _install(tmp_path, _APP, enabled=True)
    record = {
        "resources": "app",
        "enabled": True,
        "manifest": {"setup": {"onDisable": "x"}},
    }

    with (
        patch.object(appteardown, "on_app_disable", _noop_hook),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "recorded_backend_port", lambda name: None),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: None),
        patch.object(appteardown, "deregister_app", lambda name: None),
        patch.object(appteardown, "run_lifecycle_script", _script),
    ):
        result = await appteardown.teardown_app_runtime(
            _APP, record, withdrawing_trust=True
        )

    joined = " ".join(result.warnings)
    # The placeholder deliberately NAMES the domain — that is operator diagnostics,
    # not a leak, since what is gone is the live URL and the credential in it. So
    # assert on the thing that actually differs: with `redact_credentials` alone the
    # note still read `https://evil.example.com/[REDACTED: credential]`, i.e. an
    # intact, reusable destination. Now the whole URL is replaced.
    assert "https://evil.example.com" not in joined, (
        f"a live exfiltration URL reached the response: {joined}"
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in joined, joined
    assert "[REDACTED: suspicious URL" in joined, joined
    assert "onDisable" in joined, "the warning itself was lost, not just scrubbed"


@pytest.mark.asyncio
async def test_an_on_disable_that_raises_is_contained(home: Path, tmp_path: Path):
    # The runner itself blowing up (not the script exiting non-zero) must not abort
    # the teardown either — same reasoning, and a teardown that stops halfway leaves
    # the app in a worse state than one that pushes through and reports.
    import kiro_crew.apps.teardown as appteardown

    async def _script(name, script, **kw):
        raise RuntimeError("sandbox unavailable")

    _install(tmp_path, _APP, enabled=True)
    record = {
        "resources": "app",
        "enabled": True,
        "manifest": {"setup": {"onDisable": "x"}},
    }

    with (
        patch.object(appteardown, "on_app_disable", _noop_hook),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "recorded_backend_port", lambda name: None),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: None),
        patch.object(appteardown, "deregister_app", lambda name: None),
        patch.object(appteardown, "run_lifecycle_script", _script),
    ):
        result = await appteardown.teardown_app_runtime(
            _APP, record, withdrawing_trust=True
        )

    assert result.ok is True
    assert any("could not be run" in w for w in result.warnings), result.warnings


@pytest.mark.asyncio
async def test_a_failed_shutdown_hook_is_reported_without_blocking_the_revoke(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: the hook-result loop only inspected `cron_cleanup`, so
    # `hooks_shutdown: "failed"` was neither reported nor acted on — the process was
    # killed and revoke answered 200 while whatever the app's own on_shutdown hook
    # was buffering went with it, silently.
    #
    # It is a WARNING, not a failure, and the distinction is the point. A failed
    # cron cleanup means jobs may still FIRE (code still executes) so it must refuse.
    # A failed shutdown hook means the app did not FLUSH — the backend stop and
    # deregistration still run, so the code does end up stopped. Refusing here would
    # make trust unrevokable for any app whose cleanup hook is simply broken.
    import kiro_crew.apps.teardown as appteardown

    async def _hook_fails(name: str, record: dict, **_kw: object) -> dict:
        return {"hooks_shutdown": "failed"}

    _install(tmp_path, _APP, enabled=True)
    with (
        patch.object(appteardown, "on_app_disable", _hook_fails),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "recorded_backend_port", lambda name: None),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: None),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            await client.post(f"/api/security/trusted-apps/{_APP}")
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            # Revocation SUCCEEDS — the permission is withdrawn and the code stopped.
            assert resp.status == 200
            body = await resp.json()

    # ...and the operator can still learn that state may have been dropped.
    assert any("on_shutdown" in w for w in body["warnings"])
    assert _APP not in body["apps"]


@pytest.mark.asyncio
async def test_grant_is_withdrawn_if_the_app_is_uninstalled_mid_write(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: the grant path validated that the app exists, then wrote. The
    # handler holds `app_lifecycle_lock(name)`, but `kirocrew app uninstall` runs in a
    # DIFFERENT PROCESS that no asyncio lock reaches — so the app could vanish
    # between the check and the write, leaving a grant over a name no app occupies.
    # Grants are name-keyed, so the next app installed under it would run its own
    # code with no consent prompt.
    from kiro_crew.apps import manager as appmanager

    real_get_app = appmanager.get_app
    calls: list[str] = []

    def _get_app_then_vanish(name: str):
        calls.append(name)
        # Installed for the validation read; gone by the post-write re-check, which
        # is exactly what a concurrent CLI uninstall looks like from in here.
        if len(calls) <= 2:
            return real_get_app(name)
        return None

    _install(tmp_path, _APP)
    with patch.object(security, "get_app", _get_app_then_vanish):
        async with _client() as client:
            resp = await client.post(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 409
            assert (await resp.json())["code"] == "app_uninstalled_mid_grant"

    # The orphan is gone from the persisted list, not merely reported.
    assert _stored(home)["apps_trusted"] == []


# ── 3. revoke does not over-disable ──


@pytest.mark.asyncio
async def test_revoke_without_a_grant_leaves_an_enabled_app_running(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: the teardown used to fire on ANY revoke. Revoke is deliberately
    # NOT name-validated (the user must be able to delete junk config entries the
    # snapshot shows them), so an unconditional teardown turned this endpoint into
    # "disable any installed app" for a caller holding no grant at all.
    _install(tmp_path, _APP, enabled=True)
    assert build_trusted_apps_snapshot()["apps"] == []  # never granted

    async with _client() as client:
        resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
        assert resp.status == 200
        body = await resp.json()

    # POSTCONDITION FIRST: the app is untouched. A caller holding no grant over
    # this app must not be able to disable it through the revoke endpoint.
    meta = _read_installed(_APP)
    assert meta is not None and meta.enabled is True, "revoke disabled an app it never granted"
    assert body["disabled"] is False, "revoke reported a teardown it had no grant to perform"
    revokes = [
        c
        for c in mock_sel.log_api_access.call_args_list
        if c.kwargs["operation"] == "security.trusted_apps.revoke"
    ]
    assert revokes and "was_granted=False" in revokes[-1].kwargs["resources"]


@pytest.mark.asyncio
async def test_revoke_of_a_builtin_name_leaves_the_builtin_enabled(
    home: Path, tmp_path: Path, mock_sel
):
    # A builtin is exempt at the gate, so no grant governs it and revoking one
    # must not disable it. The grant list is force-seeded (the POST endpoint
    # refuses builtins — see test 4), which is exactly the hand-edited-config
    # shape the un-validated revoke path exists to clean up.
    _install(tmp_path, _BUILTIN, enabled=True, builtin=True)
    from kiro_crew.apps.execution import builtin_app_names

    assert _BUILTIN in builtin_app_names(), "fixture failed to make a real builtin"
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_BUILTIN]}}), encoding="utf-8"
    )

    async with _client() as client:
        resp = await client.delete(f"/api/security/trusted-apps/{_BUILTIN}")
        assert resp.status == 200
        body = await resp.json()

    # POSTCONDITION FIRST: the builtin's code is left alone. Shipped code is
    # exempt at the gate, so no grant governs it and revoking one cannot disable it.
    meta = _read_installed(_BUILTIN)
    assert meta is not None and meta.enabled is True, "revoke disabled a builtin"
    # The stale entry is still removed — that is what the un-validated name is for.
    assert body["apps"] == [] and body["ineffective"] == []
    assert body["disabled"] is False, "revoke claimed to tear down a builtin"


# ── 4. a builtin name can never hold a grant ──


@pytest.mark.asyncio
async def test_grant_of_a_builtin_name_409s_and_persists_nothing(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: a builtin passed the "is this a real app?" existence check
    # (builtins have an installed.json too), so a grant persisted for it. It sat
    # inert while the builtin owned the slot — and went LIVE the moment a
    # third-party app claimed that name, inheriting a grant nobody made for it.
    _install(tmp_path, _BUILTIN, enabled=True, builtin=True)
    from kiro_crew.apps.execution import builtin_app_names

    assert _BUILTIN in builtin_app_names()

    async with _client() as client:
        resp = await client.post(f"/api/security/trusted-apps/{_BUILTIN}")
        body = await resp.json()

    # POSTCONDITION FIRST: nothing was written — not to the effective set and not
    # to the raw file. The persisted entry is the defect; the status is the report.
    assert _stored(home).get("apps_trusted", []) == [], "a grant persisted for a builtin"
    snapshot = build_trusted_apps_snapshot()
    assert snapshot["apps"] == [] and snapshot["ineffective"] == []

    assert resp.status == 409
    assert body["code"] == "app_is_builtin"
    assert body["error"]
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_refused_builtin_grant_is_not_inherited_by_a_later_takeover(
    home: Path, tmp_path: Path, mock_sel
):
    # WHY the refusal matters: after the builtin stops owning the name, a
    # third-party app claiming it must still be denied. Had the grant persisted,
    # this app would execute on a grant the operator never made for it.
    # The POST's status is deliberately not asserted here — the load-bearing
    # assertion is the gate's verdict AFTER the takeover.
    from kiro_crew.apps.execution import app_execution_denied, builtin_app_names

    _install(tmp_path, _BUILTIN, enabled=True, builtin=True)
    async with _client() as client:
        await client.post(f"/api/security/trusted-apps/{_BUILTIN}")

    # The name is now occupied by a USER-installed app (registry-owned install,
    # so builtin_owns_installed is False and the gate treats it as third party).
    meta = _read_installed(_BUILTIN)
    assert meta is not None
    meta.source = "registry:takeover"
    meta.origin = "registry"
    _write_installed(_BUILTIN, meta)
    assert _BUILTIN not in builtin_app_names(), "takeover fixture did not take effect"

    assert app_execution_denied(_BUILTIN, action="enable") is not None, (
        "a third-party app inherited a grant made against the builtin's name"
    )


# ── 5. a registry name that is not installed yet is grantable ──

_REG_ONLY = "registry-only-app"


@pytest.fixture
def seeded_registry(monkeypatch: pytest.MonkeyPatch):
    """Seed the BUNDLED registry index in-process (never touches the network)."""
    import kiro_crew.apps.registry as registry

    monkeypatch.setattr(
        registry,
        "_load_registry_file",
        lambda: [{"name": _REG_ONLY, "repo": "acme/registry-only-app", "branch": "main"}],
    )
    return _REG_ONLY


@pytest.mark.asyncio
async def test_grant_accepts_a_registry_name_that_is_not_installed(
    home: Path, mock_sel, seeded_registry: str
):
    # REGRESSION: requiring an INSTALL deadlocked the feature. ``install_from_
    # registry`` checks the execution gate BEFORE cloning (clone/build/onInstall
    # are themselves third-party code), so "no grant without an install, no
    # install without a grant" left the operator only the blanket
    # ``apps_allow_third_party`` this endpoint exists to avoid.
    from kiro_crew.apps.manager import get_app

    assert get_app(_REG_ONLY) is None, "fixture must NOT install the app"

    async with _client() as client:
        resp = await client.post(f"/api/security/trusted-apps/{_REG_ONLY}")
        assert resp.status == 200
        assert (await resp.json())["apps"] == [_REG_ONLY]

    # Persisted AND effective, so the install that follows is admitted.
    assert _stored(home)["apps_trusted"] == [_REG_ONLY]
    from kiro_crew.apps.execution import app_execution_denied

    assert app_execution_denied(_REG_ONLY, action="install") is None


@pytest.mark.asyncio
async def test_grant_of_a_name_in_neither_registry_nor_installs_404s(
    home: Path, mock_sel, seeded_registry: str
):
    # The registry leg widens WHICH names are acceptable, not whether the check
    # runs: a name in neither the index nor the installs is still a 404, so an
    # arbitrary name cannot sit in config waiting for an app to claim it.
    async with _client() as client:
        resp = await client.post("/api/security/trusted-apps/not-in-any-registry")
        assert resp.status == 404
        assert (await resp.json())["code"] == "app_not_installed"

    assert _stored(home).get("apps_trusted", []) == []
    assert build_trusted_apps_snapshot()["apps"] == []


# ── 6. the snapshot tells the truth about what is enforced ──

# Stored entries the ENFORCEMENT reader (``trusted_app_names``) drops: wrong case,
# a trailing space, a fullwidth homoglyph that is not ASCII at all, traversal, and
# a glob. Every one of these renders as a grant unless the snapshot splits them
# out — a security panel claiming trust that admits nothing, with no way for the
# user to see why their app is still blocked.
_INEFFECTIVE_ENTRIES = ["LD-App", "ld-app ", "ｌd-app", "..", "*"]


@pytest.mark.asyncio
async def test_snapshot_reports_unenforceable_entries_as_ineffective(
    home: Path, tmp_path: Path, mock_sel
):
    from kiro_crew.apps.execution import app_execution_denied, trusted_app_names

    _install(tmp_path, _APP)
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP, *_INEFFECTIVE_ENTRIES]}}),
        encoding="utf-8",
    )

    snapshot = build_trusted_apps_snapshot()

    # NONE of the junk is reported as an enforced grant; ALL of it is surfaced as
    # ineffective so the user can see the entry exists and does nothing.
    assert snapshot["apps"] == [_APP]
    for entry in _INEFFECTIVE_ENTRIES:
        assert entry not in snapshot["apps"], f"{entry!r} reported as enforced"
        assert entry in snapshot["ineffective"], f"{entry!r} silently dropped"
    assert sorted(_INEFFECTIVE_ENTRIES) == snapshot["ineffective"]

    # The invariant behind the split: ``apps`` is EXACTLY what the gate enforces.
    stored = set(json.loads((home / "config.json").read_text())["agent"]["apps_trusted"])
    assert set(snapshot["apps"]) == stored & set(trusted_app_names())
    # And the gate agrees with the report, entry by entry.
    assert app_execution_denied(_APP, action="enable") is None
    assert app_execution_denied("LD-App", action="enable") is not None


@pytest.mark.asyncio
async def test_snapshot_reflected_over_the_api_matches_the_gate(
    home: Path, tmp_path: Path, mock_sel
):
    # The split must survive the wire, not just the helper: the GET is what the
    # Security panel actually renders.
    _install(tmp_path, _APP)
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP, *_INEFFECTIVE_ENTRIES]}}),
        encoding="utf-8",
    )
    async with _client() as client:
        body = await (await client.get("/api/security/trusted-apps")).json()
    assert body["apps"] == [_APP]
    assert body["ineffective"] == sorted(_INEFFECTIVE_ENTRIES)


@pytest.mark.asyncio
async def test_failed_cron_cleanup_does_not_report_a_successful_revoke(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: ``on_app_disable`` reports cron cleanup as PROSE in one field —
    # "removed N job(s)" on success, "failed: cron store busy — jobs may still be
    # enabled" when the store is contended. Treating both as an informational
    # warning let a contended store leave the app's scheduled commands ARMED while
    # the endpoint returned 200 and said the app was switched off: third-party code
    # still executing on a timer, with trust reported as revoked.
    import kiro_crew.apps.teardown as appteardown

    async def _busy_store(name: str, record: dict, **_kw: object) -> dict:
        return {"cron_cleanup": "failed: cron store busy — jobs may still be enabled"}

    _install(tmp_path, _APP, enabled=True)
    with (
        patch.object(appteardown, "on_app_disable", _busy_store),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            assert (await client.post(f"/api/security/trusted-apps/{_APP}")).status == 200
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "teardown_incomplete"
            assert any("cron cleanup incomplete" in f for f in body["failures"])

    # RETRYABLE: the grant must still be there, otherwise a retry would skip the
    # teardown and the app would keep running with trust already gone. This is why
    # the teardown runs BEFORE the config write.
    assert _APP in _stored(home).get("apps_trusted", [])

    # And the retry succeeds once cleanup can complete.
    async def _clean(name: str, record: dict, **_kw: object) -> dict:
        return {"cron_cleanup": "removed 2 job(s)"}

    with (
        patch.object(appteardown, "on_app_disable", _clean),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 200
            assert (await resp.json())["disabled"] is True
    assert _APP not in _stored(home).get("apps_trusted", [])


@pytest.mark.asyncio
async def test_blanket_off_reports_apps_it_could_not_stop(home: Path, tmp_path: Path, mock_sel):
    # REGRESSION: turning the blanket flag OFF sweeps every enabled third-party app
    # that holds no grant. A failed teardown used to `continue` silently, so the
    # response carried only `stopped` — the operator could not tell that code they
    # had just un-trusted was STILL RUNNING. Same shape as the metadata-only revoke
    # this feature already had to fix, one layer up.
    import kiro_crew.apps.teardown as appteardown

    async def _busy_store(name: str, record: dict, **_kw: object) -> dict:
        return {"cron_cleanup": "failed: cron store busy — jobs may still be enabled"}

    _install(tmp_path, _APP, enabled=True)
    with (
        patch.object(appteardown, "on_app_disable", _busy_store),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            on = await client.put(
                "/api/security/trusted-apps/allow-all", json={"value": True}
            )
            assert on.status == 200
            off = await client.put(
                "/api/security/trusted-apps/allow-all", json={"value": False}
            )
            # 409, not 200: the flag IS off (and the snapshot says so) but code the
            # flag was admitting survived, and the caller must be able to tell.
            assert off.status == 409
            body = await off.json()
            assert body["allowAll"] is False
            assert body["code"] == "blanket_trust_sweep_incomplete"
            # The app is NOT in `stopped`, and the response SAYS it is still running.
            assert _APP not in body["stopped"]
            assert _APP in body["stillRunning"]


def test_uninstall_reports_a_trust_grant_it_could_not_drop(home: Path, tmp_path: Path):
    # REGRESSION: uninstall drops the app's grant because a grant is keyed on NAME
    # alone — a leftover would admit a DIFFERENT app installed under that name with
    # no consent prompt. When the drop FAILED the old code logged and returned
    # ok=True, so the app vanished while its code-execution trust stayed on file and
    # reusable. The uninstall still succeeds (the files are gone either way) but it
    # must say so.
    from kiro_crew.apps import manager as appmanager

    _install(tmp_path, _APP, enabled=False)

    def _boom(name: str) -> None:
        raise RuntimeError("config.json is unreadable")

    with patch.object(appmanager, "_drop_trust_grant", _boom):
        result = appmanager.uninstall_app(_APP)

    # REFUSES rather than half-uninstalling. Deleting the files first and reporting
    # the surviving grant as a warning left a state the user could not recover from:
    # the app is gone, so nothing is left to uninstall and no retry clears the
    # grant, while the name stays armed for whatever is installed under it next.
    assert result.ok is False
    assert result.error_code == "trust_grant_not_removed"
    assert "not uninstalling" in result.error
    assert "code" in result.to_dict()
    # Nothing was destroyed, so the user can fix the cause and retry.
    assert appmanager.get_app(_APP) is not None


@pytest.mark.asyncio
async def test_mutations_refuse_when_config_local_owns_the_trust_setting(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: KiroCrewConfig deep-merges config.local.json OVER config.json and
    # save() STRIPS overlay-owned values from what it writes. So when the overlay
    # owns a trust setting, mutating config.json is doubly ineffective — and the old
    # code returned 200, telling the operator a grant was revoked while the overlay
    # re-admitted the app's code on the very next load. Third variant of
    # "revocation that revokes nothing". Refusing is the honest answer: the overlay
    # is user-owned and Kiro Crew never writes it.
    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": ["pinned-app"]}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)
    async with _client() as client:
        for call in (
            client.post(f"/api/security/trusted-apps/{_APP}"),
            client.delete(f"/api/security/trusted-apps/{_APP}"),
            client.put("/api/security/trusted-apps/allow-all", json={"value": True}),
        ):
            resp = await call
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "trust_setting_overlay_owned"
            assert body["overlaySettings"] == ["apps_trusted"]
            assert "config.local.json" in body["error"]


@pytest.mark.asyncio
async def test_refused_revoke_does_not_switch_the_app_off(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: revoke tore the app down FIRST and only then wrote the config, so
    # an overlay-owned trust setting produced the worst of both — a 409 saying "that
    # change did not take effect" over an app that had just been stopped and
    # disabled, with the grant it was supposed to lose still standing. The user
    # asked to withdraw permission and instead lost the working app and kept the
    # permission. Config mutability is now pre-flighted before anything is stopped.
    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP]}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=True)
    with patch(
        "kiro_crew.dashboard.handlers.security.teardown_app_runtime"
    ) as teardown:
        async with _client() as client:
            resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 409
            assert (await resp.json())["code"] == "trust_setting_overlay_owned"

    # Nothing was stopped, and the app is still switched on.
    teardown.assert_not_called()
    meta = _read_installed(_APP)
    assert meta is not None and meta.enabled is True


def test_uninstall_holds_the_shared_config_lock_across_the_grant_drop(
    home: Path, tmp_path: Path
):
    # REGRESSION: `_drop_trust_grant` is a read-modify-write of config.json, but the
    # uninstall route held only `app_lifecycle_lock(name)` — keyed on the APP, so it
    # serializes nothing against a settings/agent write, which takes the SHARED
    # config lock and rewrites the same file. The two interleaved into a lost
    # update: either the user's settings vanished, or the removed grant came back —
    # and a restored name-keyed grant is a consent bypass for whatever is installed
    # under that name next. Asserted structurally (the route acquires the shared
    # lock around the executor call) because reproducing the interleaving requires
    # winning a race.
    from kiro_crew.apps import routes as approutes

    src = Path(approutes.__file__).read_text(encoding="utf-8")
    handler = src[src.index("async def handle_uninstall_app") :]
    handler = handler[: handler.index("\nasync def ", 1)]
    lock_at = handler.index("_get_config_lock()")
    call_at = handler.index("uninstall_app(name, keep_data=keep_data)")
    assert lock_at < call_at, "the shared config lock must be held across uninstall_app"


def test_uninstall_reports_an_overlay_owned_grant_it_cannot_drop(
    home: Path, tmp_path: Path
):
    # Same hazard on the uninstall path: the grant is name-keyed, so one left in the
    # overlay would admit a DIFFERENT app installed under this name later. The
    # uninstall still succeeds (files are gone) but must SAY the grant survived.
    from kiro_crew.apps import manager as appmanager

    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP]}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)
    result = appmanager.uninstall_app(_APP)

    assert result.ok is False
    assert result.error_code == "trust_grant_not_removed"
    assert "config.local.json" in result.error
    assert appmanager.get_app(_APP) is not None  # still installed, so retryable


def test_an_overlay_grant_for_another_app_does_not_block_this_uninstall(
    home: Path, tmp_path: Path
):
    # The overlay refusal used to fire on the mere PRESENCE of `apps_trusted`,
    # regardless of which apps it named — so any operator who set it at all could
    # never uninstall ANY app. Scoped to a grant this app actually holds.
    from kiro_crew.apps import manager as appmanager

    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": ["some-other-app"]}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    assert appmanager.trust_grant_removal_blocked(_APP) is None
    result = appmanager.uninstall_app(_APP)
    assert result.ok is True, result.error
    assert appmanager.get_app(_APP) is None


def test_uninstall_drops_the_base_grant_even_when_an_overlay_replaces_the_list(
    home: Path, tmp_path: Path
):
    # The removal used to decide from the MERGED config. A list merge REPLACES,
    # so base ["<app>"] + overlay ["other"] merges to ["other"], the merged view
    # sees no grant for <app>, and nothing is removed — leaving the BASE entry
    # behind. It is inert only while that overlay key stands; edit or drop the
    # key and a different app installed under this name inherits the grant.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP], "model": "sonnet"}}),
        encoding="utf-8",
    )
    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": ["some-other-app"]}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    # Not blocked: the OVERLAY does not grant this app, so the effective grant is
    # removable even though the overlay owns the setting's merged value.
    assert appmanager.trust_grant_removal_blocked(_APP) is None
    result = appmanager.uninstall_app(_APP)
    assert result.ok is True, result.error

    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == []
    # Everything else in the base file survives the targeted edit.
    assert base["agent"]["model"] == "sonnet"
    # And the overlay is never written by us.
    overlay = json.loads((home / "config.local.json").read_text(encoding="utf-8"))
    assert overlay["agent"]["apps_trusted"] == ["some-other-app"]


def test_uninstall_withdraws_a_grant_that_landed_during_the_delete(
    home: Path, tmp_path: Path
):
    # The cross-process race GPT named: `kirocrew app uninstall` drops the (absent)
    # grant, and only THEN deletes the files. In that gap the dashboard's grant
    # handler sees the app on disk, writes the grant, and its own post-write
    # existence re-check ALSO still sees the app — so every guard on both sides
    # passes and the grant is left standing over a name no app occupies, ready to
    # admit a different app under that name with no consent prompt.
    #
    # Injected deterministically here: the concurrent write is performed from inside
    # `rmtree`, which is exactly the window (after the first withdrawal, before the
    # files are gone). No cross-process lock is needed to close it — only the second
    # withdrawal AFTER the delete, which by ordering must run after that write.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [], "model": "sonnet"}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    real_rmtree = appmanager.shutil.rmtree

    def _rmtree_racing_a_grant(path, *a, **kw):
        # Stand in for the other process's grant handler winning the gap.
        cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
        cfg["agent"]["apps_trusted"] = [_APP]
        (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return real_rmtree(path, *a, **kw)

    with patch.object(appmanager.shutil, "rmtree", _rmtree_racing_a_grant):
        result = appmanager.uninstall_app(_APP)

    assert result.ok is True, result.error
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == [], (
        "a grant that landed mid-delete survived the uninstall — the name is armed "
        "with no app to own it"
    )
    assert base["agent"]["model"] == "sonnet"


def test_a_failed_delete_puts_the_trust_grant_back(home: Path, tmp_path: Path):
    # REGRESSION: the withdrawal runs BEFORE the delete (so a withdrawal failure is
    # retryable with nothing destroyed). But if the delete then failed, the app was
    # still installed with its grant already gone — an operation that did not even
    # succeed silently stripped a permission the operator had granted.
    #
    # Fixed by restoring rather than by deferring the withdrawal: deferring it until
    # after a successful delete re-opens the hole the current ordering exists to
    # close, where a withdrawal that fails leaves the app GONE with its name still
    # armed and no app left to uninstall, so no retry can ever clear it.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP], "model": "sonnet"}}),
        encoding="utf-8",
    )
    _install(tmp_path, _APP, enabled=False)

    def _boom(*a, **kw):
        raise OSError("device busy")

    with patch.object(appmanager.shutil, "rmtree", _boom):
        result = appmanager.uninstall_app(_APP)

    assert result.ok is False
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == [_APP], (
        "a failed uninstall left the still-installed app stripped of its grant"
    )
    assert base["agent"]["model"] == "sonnet"


def test_a_failed_delete_does_not_grant_an_app_that_had_no_grant(
    home: Path, tmp_path: Path
):
    # The other half, and the one that would be a real vulnerability: restoring must
    # never CREATE a grant. An untrusted app whose uninstall fails must stay
    # untrusted, or a failed uninstall becomes a way to gain execution permission.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": []}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    def _boom(*a, **kw):
        raise OSError("device busy")

    with patch.object(appmanager.shutil, "rmtree", _boom):
        result = appmanager.uninstall_app(_APP)

    assert result.ok is False
    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert base["agent"]["apps_trusted"] == [], "a failed uninstall granted execution"


def test_the_cli_trust_withdrawal_is_audited(home: Path, tmp_path: Path):
    # A permission boundary that moves silently is what SEL exists to make visible.
    # The dashboard revoke endpoint audits its own withdrawal, but this path runs
    # from `kirocrew app uninstall`, so without this the grant could disappear with
    # nothing in the security event log to reconstruct the trust timeline from.
    #
    # Patched on `manager`, not via the `mock_sel` fixture: that one patches the
    # DASHBOARD handler's `_sel`, which this code path never touches.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP]}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    recorder = MagicMock()
    with patch.object(appmanager, "sel", lambda: recorder):
        result = appmanager.uninstall_app(_APP)
    assert result.ok is True, result.error

    ops = [c.kwargs.get("operation") for c in recorder.log_api_access.call_args_list]
    assert "app_trust_revoke" in ops, f"no SEL event for the withdrawal: {ops}"


def test_uninstall_writes_no_config_at_all_when_there_is_no_grant(
    home: Path, tmp_path: Path
):
    # Bounds what the two withdrawals cost. `config.json` read-modify-write is
    # unlocked across processes REPO-WIDE (24 writers on the base branch, including
    # `kirocrew config set`, whose own reader documents the shape), so every writer
    # carries some lost-update exposure. This asserts the trust-grant cleanup adds
    # NONE of it in the ordinary case: with no grant to withdraw, both calls read,
    # find nothing, and return WITHOUT writing. A write happens only when there is
    # actually an orphaned grant to remove — the case the withdrawal exists for.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps({"agent": {"model": "sonnet"}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    with patch.object(appmanager, "write_config_atomically") as writes:
        result = appmanager.uninstall_app(_APP)

    assert result.ok is True, result.error
    assert writes.call_count == 0, (
        f"uninstalling an ungranted app rewrote config.json {writes.call_count}x"
    )


def test_uninstall_reports_a_grant_it_could_not_withdraw_after_the_delete(
    home: Path, tmp_path: Path
):
    # Same window, but the second withdrawal fails. Refusing the uninstall is not
    # available (the files are already gone) and claiming plain success would hide a
    # live grant over a name nothing occupies, invisible in the app list because
    # there is no app left to list. So it must succeed AND say so.
    from kiro_crew.apps import manager as appmanager

    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": []}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    calls = {"n": 0}
    real_drop = appmanager._drop_trust_grant

    def _drop(name: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_drop(name)  # the pre-delete withdrawal succeeds
        raise RuntimeError("config.json went unreadable")

    with patch.object(appmanager, "_drop_trust_grant", _drop):
        result = appmanager.uninstall_app(_APP)

    assert calls["n"] == 2, "the post-delete withdrawal did not run"
    assert result.ok is True, result.error
    assert "still in" in result.message and _APP in result.message, result.message
    assert "Settings" in result.message, "no recovery step offered for a live grant"


@pytest.mark.asyncio
async def test_grant_preserves_base_settings_shadowed_by_the_overlay(
    home: Path, tmp_path: Path, mock_sel
):
    # The mutation used to run KiroCrewConfig.load() -> cfg.save(), and save()
    # strips every value config.local.json also defines so overlay settings do not
    # leak into the base file. Routing a trust write through it rewrote the WHOLE
    # base document minus all overlay-owned keys, so granting one app trust
    # permanently deleted the user's base `model` whenever an overlay also set it.
    # Nothing looked wrong at the time -- the merged config still read the overlay
    # value -- until the overlay was removed and the base setting was simply gone.
    # Install FIRST: `install_app` writes the whole config through the model, so
    # seeding before it would measure main's writer, not the trust endpoint.
    _install(tmp_path, _APP, enabled=False)
    # Migration-complete on purpose: `KiroCrewConfig.load()` write-BACKS a legacy
    # config (missing `agents`/`default_agent`), and that write-back is itself a
    # full model save. A minimal hand-written config would trip it and measure the
    # migration instead of the mutation under test.
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {"model": "sonnet", "max_subagents": 3},
                "agents": {
                    "default": {
                        "kiro_agent": "kirocrew",
                        "workspace": "default",
                        "memory_store": "default",
                    }
                },
                "default_agent": "default",
                "workspaces": {"default": {"dir": "workspace"}},
                "default_workspace": "default",
            }
        ),
        encoding="utf-8",
    )
    (home / "config.local.json").write_text(
        json.dumps({"agent": {"model": "opus"}}), encoding="utf-8"
    )

    async with _client() as client:
        resp = await client.post(f"/api/security/trusted-apps/{_APP}")
        assert resp.status == 200

    base = json.loads((home / "config.json").read_text(encoding="utf-8"))
    # The grant landed...
    assert base["agent"]["apps_trusted"] == [_APP]
    # ...and the overlay-shadowed base setting survived, as did its neighbour.
    assert base["agent"]["model"] == "sonnet"
    assert base["agent"]["max_subagents"] == 3
    # The overlay itself is never written by us.
    overlay = json.loads((home / "config.local.json").read_text(encoding="utf-8"))
    assert overlay == {"agent": {"model": "opus"}}


@pytest.mark.asyncio
async def test_uninstall_route_refuses_before_running_anything_destructive(
    home: Path, tmp_path: Path, mock_sel
):
    # The abort used to live inside uninstall_app, which the handler reaches only
    # at Step 5 — AFTER cron deregistration, the app's non-idempotent onUninstall
    # script, the backend stop and dependency cleanup. The refusal therefore
    # stranded a half-removed app and re-ran onUninstall on every retry. It is now
    # a precondition: nothing destructive may have run when it fires.
    from kiro_crew.apps import routes as approutes

    (home / "config.local.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP]}}), encoding="utf-8"
    )
    _install(tmp_path, _APP, enabled=False)

    uninstall_app_router = web.Application()
    uninstall_app_router.router.add_delete(
        "/api/apps/{name}", approutes.handle_uninstall_app
    )

    destructive: list[str] = []
    with (
        patch.object(
            approutes,
            "deregister_app",
            lambda *a, **k: destructive.append("deregister_app"),
        ),
        patch.object(
            approutes,
            "uninstall_app",
            lambda *a, **k: destructive.append("uninstall_app"),
        ),
    ):
        async with TestClient(TestServer(uninstall_app_router)) as client:
            resp = await client.delete(f"/api/apps/{_APP}")
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "trust_grant_not_removed"
            assert body["retryable"] is True
            assert "Nothing has been changed" in body["error"]

    assert destructive == []


@pytest.mark.asyncio
async def test_blanket_off_runs_shutdown_hooks_before_persisting_false(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: the flag is what authorises loading an app's Python at all —
    # `load_app_module` consults `app_execution_denied`. Persisting `false` FIRST
    # therefore denied the app's own `on_shutdown` hook, so it was stopped without
    # ever being told to flush state or release resources. The sweep has to run
    # while trust still stands. Pinned by observing the config as the hook sees it.
    import kiro_crew.apps.teardown as appteardown

    observed: list[bool] = []

    async def _on_app_disable(name: str, record: dict, **_kw: object) -> dict:
        # What a shutdown hook would see when the loader gates it.
        from kiro_crew.apps.execution import third_party_execution_allowed

        observed.append(third_party_execution_allowed())
        return {}

    _install(tmp_path, _APP, enabled=True)
    with (
        patch.object(appteardown, "on_app_disable", _on_app_disable),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            assert (
                await client.put(
                    "/api/security/trusted-apps/allow-all", json={"value": True}
                )
            ).status == 200
            off = await client.put(
                "/api/security/trusted-apps/allow-all", json={"value": False}
            )
            assert off.status == 200
            assert (await off.json())["allowAll"] is False

    # The hook ran, and it ran while the app was still permitted to execute.
    assert observed == [True], (
        "the shutdown hook must run BEFORE the flag is persisted; observed "
        f"allow-all={observed} at hook time"
    )


@pytest.mark.asyncio
async def test_blanket_off_catches_an_app_enabled_during_the_sweep(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: the falling edge sweeps BEFORE persisting `false` so shutdown hooks
    # can still load. That opened a window — the sweep enumerates candidates and then
    # spends real time stopping backends, and an app enabled inside that window is
    # never in the enumeration, so it kept executing under trust the operator
    # believed they had withdrawn. Per-app locks cannot cover it: there is no name to
    # lock for an app that was not enumerated. A second sweep after the write closes
    # it. Simulated by enabling a second app from inside the first app's teardown.
    import kiro_crew.apps.teardown as appteardown
    from kiro_crew.apps.manager import enable_app

    late = "trust-late-app"
    _install(tmp_path, _APP, enabled=True)
    _install(tmp_path, late, enabled=False)

    async def _enable_the_other_app(name: str, record: dict, **_kw: object) -> dict:
        # The racing enable: lands after `late` was already passed over.
        if name == _APP:
            enable_app(late)
        return {}

    with (
        patch.object(appteardown, "on_app_disable", _enable_the_other_app),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "deregister_app", lambda name: None),
    ):
        async with _client() as client:
            assert (
                await client.put(
                    "/api/security/trusted-apps/allow-all", json={"value": True}
                )
            ).status == 200
            off = await client.put(
                "/api/security/trusted-apps/allow-all", json={"value": False}
            )
            body = await off.json()

    # The app that raced in must NOT be left enabled-and-running on withdrawn trust:
    # either it was stopped, or the response says it may still be executing.
    from kiro_crew.apps.manager import get_app

    record = get_app(late) or {}
    accounted = late in body.get("stopped", []) or late in body.get("stillRunning", [])
    assert accounted, (
        f"{late!r} was enabled during the sweep and is unaccounted for: "
        f"stopped={body.get('stopped')} stillRunning={body.get('stillRunning')}"
    )
    assert not (record.get("enabled") and not accounted)


def test_blanket_flag_is_not_editable_through_the_generic_config_patch():
    # REGRESSION: `agent.apps_allow_third_party` was PATCHable through
    # /api/config/kirocrew, and that path performs NO teardown — so flipping it off
    # there withdrew trust on paper while every app it had admitted kept executing,
    # crons included, until a gateway restart. A dashboard card shipped against that
    # endpoint (#1414), which is how the hole became reachable from the UI.
    #
    # The key is deliberately absent from the editable set so the ONLY writer is the
    # endpoint that runs the sweep, and the refusal names it rather than dead-ending
    # on "field not editable".
    from kiro_crew.dashboard.handlers.core import (
        _EDITABLE_CONFIG,
        _MOVED_CONFIG_FIELDS,
    )

    assert "agent.apps_allow_third_party" not in _EDITABLE_CONFIG
    hint = _MOVED_CONFIG_FIELDS["agent.apps_allow_third_party"]
    assert "/api/security/trusted-apps/allow-all" in hint
    # The sibling grant list is not editable there either, for the same reason.
    assert "agent.apps_trusted" not in _EDITABLE_CONFIG


@pytest.mark.asyncio
async def test_grant_holds_the_app_lifecycle_lock_across_validate_and_write(
    home: Path, tmp_path: Path, mock_sel
):
    # Validation and the write have to be one critical section. Unserialized, a
    # concurrent uninstall sees no grant to drop, tears the app down, and THIS
    # grant lands afterwards — leaving a grant on a name nothing owns, which is
    # exactly what lets a later app claiming that name run code unprompted.
    # Revoke already serializes this way; grant did not.
    from kiro_crew.apps import manager as appmanager
    from kiro_crew.dashboard.handlers import security as sec

    _install(tmp_path, _APP, enabled=False)
    lock = appmanager.app_lifecycle_lock(_APP)

    held_during_write: list[bool] = []
    real_mutate = sec._mutate_agent_config

    async def _spy(mutate):
        held_during_write.append(lock.locked())
        return await real_mutate(mutate)

    with patch.object(sec, "_mutate_agent_config", _spy):
        async with _client() as client:
            resp = await client.post(f"/api/security/trusted-apps/{_APP}")
            assert resp.status == 200

    # The write ran while the per-app lifecycle lock was held, so an uninstall
    # cannot interleave between the existence check and the persisted grant.
    assert held_during_write == [True]
    # And the lock is released afterwards — a held lock would wedge every later
    # lifecycle op on this app.
    assert lock.locked() is False
