"""Tests for the per-app third-party trust-grant REST API.

Covers the 4 endpoints in ``dashboard/handlers/security_trusted_apps.py`` that write
``agent.apps_trusted`` / ``agent.apps_allow_third_party`` in ``config.json``,
plus the end-to-end property that actually matters: a grant flips
``apps.execution.app_execution_denied`` for THAT app and leaves every other
third-party app denied.

The aiohttp handlers are exercised through an in-test ``TestClient`` opened with
``async with`` (matching ``test_denied_commands_api.py``) rather than an
async-gen fixture, following the suite's convention for teardown that must await on the test's own loop.

Grants keyed by a registry name, and the snapshot's report of entries the gate
cannot enforce, are covered in ``test_trusted_apps_api_registry.py``; uninstall,
config.local overlay and blanket-switch withdrawals that cannot finish in
``test_trusted_apps_api_withdrawal.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient
from trusted_apps_api_helpers import home_fixture  # noqa: F401
from trusted_apps_api_helpers import mock_sel_fixture  # noqa: F401
from trusted_apps_api_helpers import _APP, _OTHER, _client, _install, _stored

from kiro_crew.apps.manager import (
    _read_installed,
    _write_installed,
)
from kiro_crew.dashboard.handlers import security
from kiro_crew.dashboard.handlers.security import (
    build_trusted_apps_snapshot,
)
from kiro_crew.testing.wait import async_wait_until

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


# ── snapshot ──


def test_snapshot_defaults_to_empty_and_denied(home: Path):
    assert build_trusted_apps_snapshot() == {"apps": [], "ineffective": [], "allowAll": False}


def test_snapshot_sorts_and_dedupes_and_drops_junk(home: Path):
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": ["zebra", "apple", "apple", "", 5, {}]}}),
        encoding="utf-8",
    )
    snapshot = build_trusted_apps_snapshot()
    assert snapshot["apps"] == []
    assert snapshot["ineffective"] == ["apple", "zebra"]


def test_snapshot_allow_all_string_true_is_not_truthy(home: Path):
    # Mirrors the gate's strict identity check: "true" must render (and enforce)
    # as False, never coerced.
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": "true"}}), encoding="utf-8"
    )
    assert build_trusted_apps_snapshot()["allowAll"] is False


def test_snapshot_marks_legacy_repository_grant_ineffective(
    home: Path, tmp_path: Path
):
    _install(tmp_path, _APP)
    meta = _read_installed(_APP)
    assert meta is not None
    meta.source = f"registry:{_APP}"
    meta.sourceUrl = "https://example.test/owner/repository-app"
    _write_installed(_APP, meta)
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP]}}), encoding="utf-8"
    )

    assert build_trusted_apps_snapshot() == {
        "apps": [],
        "ineffective": [_APP],
        "allowAll": False,
    }


def test_snapshot_keeps_installed_legacy_local_grant_effective(
    home: Path, tmp_path: Path
):
    _install(tmp_path, _APP)
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP]}}), encoding="utf-8"
    )

    assert build_trusted_apps_snapshot()["apps"] == [_APP]


# ── GET ──


@pytest.mark.asyncio
async def test_get_returns_snapshot_shape(home: Path, mock_sel):
    async with _client() as client:
        resp = await client.get("/api/security/trusted-apps")
        assert resp.status == 200, await resp.text()
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
        assert first.status == 200, await first.text()
        assert (await first.json()) == {"apps": [_APP], "ineffective": [], "allowAll": False}

        second = await client.post(f"/api/security/trusted-apps/{_APP}")
        assert second.status == 200, await second.text()
        # Idempotent: no duplicate entry in the response or on disk.
        assert (await second.json())["apps"] == [_APP]

    assert _stored(home)["apps_trusted"] == [_APP]
    assert _stored(home)["apps_trusted_local"] == [_APP]
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
        assert first.status == 200, await first.text()
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
        assert second.status == 200, await second.text()
        assert (await second.json())["apps"] == []

        never = await client.delete("/api/security/trusted-apps/never-granted")
        assert never.status == 200, await never.text()
        assert (await never.json())["disabled"] is False

    assert _stored(home)["apps_trusted"] == []
    assert _stored(home)["apps_trusted_local"] == []
    assert _stored(home)["apps_trusted_repositories"] == {}


@pytest.mark.asyncio
async def test_revoke_disables_a_currently_enabled_app(home: Path, tmp_path: Path, mock_sel):
    _install(tmp_path, _APP, enabled=True)
    async with _client() as client:
        await client.post(f"/api/security/trusted-apps/{_APP}")
        resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
        assert resp.status == 200, await resp.text()
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


@pytest.mark.asyncio
async def test_revoke_tears_down_ineffective_legacy_repository_grant(
    home: Path, tmp_path: Path, mock_sel
):
    import kiro_crew.apps.teardown as appteardown

    torn: list[str] = []

    async def _teardown(name: str, record: dict, **_kw):
        torn.append(name)
        return appteardown.TeardownResult(warnings=[], failures=[])

    _install(tmp_path, _APP, enabled=True)
    meta = _read_installed(_APP)
    assert meta is not None
    meta.source = f"registry:{_APP}"
    meta.sourceUrl = "https://example.test/owner/repository-app"
    _write_installed(_APP, meta)
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_trusted": [_APP]}}), encoding="utf-8"
    )
    assert build_trusted_apps_snapshot()["ineffective"] == [_APP]

    with patch.object(security, "teardown_app_runtime", _teardown):
        async with _client() as client:
            response = await client.delete(f"/api/security/trusted-apps/{_APP}")
            assert response.status == 200, await response.text()

    assert torn == [_APP]
    assert _stored(home)["apps_trusted"] == []


# ── allow-all ──


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False])
async def test_allow_all_sets_the_blanket_flag(home: Path, mock_sel, value: bool):
    async with _client() as client:
        resp = await client.put("/api/security/trusted-apps/allow-all", json={"value": value})
        assert resp.status == 200, await resp.text()
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
        assert resp.status == 200, await resp.text()
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


async def _await_beacon(beacon: Path, timeout: float = 5.0) -> str:
    def _text() -> str:
        return beacon.read_text(encoding="utf-8") if beacon.exists() else ""

    return await async_wait_until(
        _text,
        timeout=timeout,
        off_loop=True,
        describe=lambda: f"backend never wrote {beacon}",
    )


@pytest.mark.asyncio
async def test_revoke_stops_the_backend_process_and_the_beacon(
    home: Path, tmp_path: Path, mock_sel
):
    # REGRESSION: revoke must not be only a METADATA write (``disable_app`` only). The
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
        before = await _await_beacon(beacon)

        async with _client() as client:
            resp = await client.delete(f"/api/security/trusted-apps/{_LOOP_APP}")
            assert resp.status == 200, await resp.text()
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
        assert _kw["bounded_startup_cleanup"] is True
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
async def test_trust_withdrawal_refuses_before_teardown_when_startup_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Residual startup ownership leaves every runtime surface unchanged."""
    import kiro_crew.apps.teardown as appteardown

    calls: list[str] = []

    async def _startup_preflight(name: str, *, bounded: bool = False) -> bool:
        assert name == _APP
        assert bounded is True
        calls.append("startup_preflight")
        return False

    async def _must_not_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("teardown mutated runtime state after ownership refusal")

    monkeypatch.setattr(appteardown, "stop_app_startup_hooks", _startup_preflight)
    monkeypatch.setattr(appteardown, "notify_app_disabled", _must_not_run)
    monkeypatch.setattr(appteardown, "on_app_disable", _must_not_run)
    monkeypatch.setattr(
        appteardown,
        "stop_app_backend",
        lambda _name: pytest.fail("backend stop ran after ownership refusal"),
    )
    monkeypatch.setattr(
        appteardown,
        "deregister_app",
        lambda _name: pytest.fail("deregistration ran after ownership refusal"),
    )

    result = await appteardown.teardown_app_runtime(
        _APP, {"manifest": {}, "enabled": True}, withdrawing_trust=True
    )

    assert result.ok is False
    assert calls == ["startup_preflight"]
    assert result.warnings == []
    assert result.failures == [
        "startup cleanup incomplete: detached startup hook is still running; "
        "teardown made no runtime changes"
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
            assert resp.status == 200, await resp.text()
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
            assert resp.status == 200, await resp.text()
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
            assert resp.status == 200, await resp.text()

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
            assert off.status == 200, await off.text()
            body = await off.json()

    # Swept exactly once: the pre-write pass runs unconditionally, and the bounded
    # second pass stays `require_enabled=True` so it cannot re-run a shutdown hook
    # the loader would now deny.
    assert torn == [_APP]
    # It was already off, so it is not claimed as an app this action stopped.
    assert body["stopped"] == []
    assert body["stillRunning"] == []


@pytest.mark.asyncio
async def test_blanket_off_sweeps_legacy_repository_grant(
    home: Path, tmp_path: Path, mock_sel
):
    import kiro_crew.apps.teardown as appteardown

    torn: list[str] = []

    async def _teardown(name: str, record: dict, **_kw):
        torn.append(name)
        return appteardown.TeardownResult(warnings=[], failures=[])

    _install(tmp_path, _APP, enabled=True)
    meta = _read_installed(_APP)
    assert meta is not None
    meta.source = f"registry:{_APP}"
    meta.sourceUrl = "https://example.test/owner/repository-app"
    _write_installed(_APP, meta)
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "apps_allow_third_party": True,
                    "apps_trusted": [_APP],
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.object(security, "teardown_app_runtime", _teardown):
        async with _client() as client:
            off = await client.put(
                "/api/security/trusted-apps/allow-all", json={"value": False}
            )
            assert off.status == 200, await off.text()

    assert torn == [_APP]


@pytest.mark.asyncio
async def test_blanket_off_keeps_explicit_local_grant_running(
    home: Path, tmp_path: Path, mock_sel
):
    _install(tmp_path, _APP, enabled=True)
    async with _client() as client:
        granted = await client.post(f"/api/security/trusted-apps/{_APP}")
        assert granted.status == 200, await granted.text()
        assert (
            await client.put(
                "/api/security/trusted-apps/allow-all", json={"value": True}
            )
        ).status == 200
        with patch.object(security, "teardown_app_runtime") as teardown:
            off = await client.put(
                "/api/security/trusted-apps/allow-all", json={"value": False}
            )
            assert off.status == 200, await off.text()
            teardown.assert_not_called()


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
            assert off.status == 200, await off.text()

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
    # Forge the self-report the gate must not blindly trust.
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
            assert resp.status == 200, await resp.text()

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
            assert resp.status == 200, await resp.text()
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
    # REGRESSION: the teardown must not fire on ANY revoke. Revoke is deliberately
    # NOT name-validated (the user must be able to delete junk config entries the
    # snapshot shows them), so an unconditional teardown turned this endpoint into
    # "disable any installed app" for a caller holding no grant at all.
    _install(tmp_path, _APP, enabled=True)
    assert build_trusted_apps_snapshot()["apps"] == []  # never granted

    async with _client() as client:
        resp = await client.delete(f"/api/security/trusted-apps/{_APP}")
        assert resp.status == 200, await resp.text()
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
        assert resp.status == 200, await resp.text()
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
