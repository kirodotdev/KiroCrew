"""Bounds on ``setup.onEnableTimeout`` / ``setup.onDisableTimeout``.

Both scripts run while the app's lifecycle lock is held. A manifest value of 0 or
less ended the script at once, ``true`` read as 1 second, and a huge value let one
manifest hold the enable or disable request (and every same-app operation queued
behind it) for as long as it liked.

Compatibility rule under test: every value the old bare ``int()`` accepted still
installs, and parses to the same ``int`` (``setup`` is signed, so the parsed value
must not move). Where the script runs, an out-of-range value, a bool or a fraction
is clamped (below 1 -> the 30s default, above 300 -> 300) and the clamp is logged.
Only a value ``int()`` raised on fails validation, with a message naming the field.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _make_app_source, _setup_env

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import install_app
from kiro_crew.apps.manifest import AppManifest, SetupConfig

# Literals, not the module's constants: the bounds are the contract under test, and a
# test that imported them would pass on any value they were changed to.
LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT = 30
LIFECYCLE_SCRIPT_TIMEOUT_MAX = 300

_FIELDS = ("onEnableTimeout", "onDisableTimeout")
_HUGE = 10**9

# (manifest value, seconds the script gets) for every value int() accepted.
_CLAMPED = [
    (0, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
    (-1, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
    (True, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
    (False, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
    ("0", LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
    (0.5, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
    (_HUGE, LIFECYCLE_SCRIPT_TIMEOUT_MAX),
    (LIFECYCLE_SCRIPT_TIMEOUT_MAX + 1, LIFECYCLE_SCRIPT_TIMEOUT_MAX),
    (str(_HUGE), LIFECYCLE_SCRIPT_TIMEOUT_MAX),
    (60.5, 60),
]
_KEPT = [
    (1, 1),
    (60, 60),
    ("60", 60),
    (" 60 ", 60),
    (60.0, 60),
    (LIFECYCLE_SCRIPT_TIMEOUT_MAX, LIFECYCLE_SCRIPT_TIMEOUT_MAX),
]
# int() raised on each of these, so no manifest carrying one installs today.
_REFUSED = ["abc", "", "1.5", None, [60], {"s": 60}, float("inf"), float("nan")]


def _manifest(**setup: Any) -> AppManifest:
    return AppManifest.from_dict(
        {
            "name": "timeout-app",
            "version": "1.0.0",
            "displayName": "Timeout App",
            "description": "an app with lifecycle scripts",
            "setup": {"onEnable": "true", "onDisable": "true", **setup},
        }
    )


def _timeout_errors(manifest: AppManifest) -> list[str]:
    return [e for e in manifest.validate() if "Timeout" in e]


@pytest.mark.parametrize("field_name", _FIELDS)
@pytest.mark.parametrize("value", [v for v, _ in _CLAMPED + _KEPT], ids=repr)
def test_a_value_int_accepted_still_validates_and_parses_as_before(
    field_name: str, value: Any
) -> None:
    manifest = _manifest(**{field_name: value})
    assert _timeout_errors(manifest) == []
    got = getattr(manifest.setup, field_name)
    assert got == int(value) and type(got) is int


@pytest.mark.parametrize("value", [600, _HUGE, 0, -1, True, "60", 60.5])
def test_the_signed_payload_keeps_the_value_as_int_read_it(value: Any) -> None:
    # `setup` is in AppManifest.signing_payload: a manifest published before the bound
    # must keep producing the bytes its publisher signed.
    payload = json.loads(_manifest(onEnableTimeout=value).signing_payload())
    expected = int(value)
    if expected == LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT:
        assert "onEnableTimeout" not in payload["setup"]
    else:
        assert payload["setup"]["onEnableTimeout"] == expected


@pytest.mark.parametrize("field_name", _FIELDS)
@pytest.mark.parametrize("value", _REFUSED, ids=repr)
def test_a_value_int_refused_fails_validation_by_name(field_name: str, value: Any) -> None:
    manifest = _manifest(**{field_name: value})
    errors = _timeout_errors(manifest)
    assert len(errors) == 1, errors
    assert f"setup.{field_name}" in errors[0]
    # Parsing itself never raises, and keeps the default in the field.
    assert getattr(manifest.setup, field_name) == LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT


@pytest.mark.parametrize(
    ("value", "seconds"), [(True, 1), (False, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT)]
)
def test_a_bool_timeout_runs_as_the_int_its_signature_covers(value: bool, seconds: int) -> None:
    # The run sites read the record, which carries the same int() value the
    # signature covers, so an unsigned edit from 1 to true cannot change the budget.
    from kiro_crew.apps.manifest import lifecycle_timeout_seconds

    manifest = _manifest(onEnableTimeout=value)
    record = manifest.to_dict()["setup"].get("onEnableTimeout")
    assert record is None or type(record) is int
    signed = json.loads(manifest.signing_payload())["setup"].get("onEnableTimeout")
    assert record == signed
    assert lifecycle_timeout_seconds(record) == seconds


def test_an_absent_timeout_is_the_default_and_valid() -> None:
    manifest = _manifest()
    assert _timeout_errors(manifest) == []
    assert manifest.setup.onEnableTimeout == LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT
    assert manifest.setup.onDisableTimeout == LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT


def test_the_module_bounds_match_the_documented_ones() -> None:
    from kiro_crew.apps import manifest

    assert manifest.LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT == LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT
    assert manifest.LIFECYCLE_SCRIPT_TIMEOUT_MAX == LIFECYCLE_SCRIPT_TIMEOUT_MAX
    assert SetupConfig().onEnableTimeout == LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT


@pytest.mark.parametrize("value", [0, -1, True, _HUGE, "60"], ids=repr)
def test_install_still_accepts_a_value_int_accepted(
    value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    result = install_app(_make_app_source(tmp_path, setup={"onEnableTimeout": value}))
    assert result.ok, result.error


def test_install_refuses_a_non_numeric_timeout_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    result = install_app(_make_app_source(tmp_path, setup={"onEnableTimeout": "abc"}))
    assert not result.ok
    assert "setup.onEnableTimeout" in result.error


@pytest.mark.parametrize(
    ("value", "ok"), [("abc", False), (None, False), (600, True), ("60", True)]
)
def test_self_registration_refuses_only_what_int_refused(
    value: Any, ok: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps.manager import register_external_app

    _setup_env(tmp_path, monkeypatch)
    result = register_external_app(
        APP,
        "1.0.0",
        "Timeout App",
        manifest_data={"name": APP, "version": "1.0.0", "setup": {"onEnableTimeout": value}},
    )
    assert result.ok is ok, result.error
    if not ok:
        assert "setup.onEnableTimeout" in result.error


@pytest.mark.parametrize(("raw", "expected"), _CLAMPED, ids=repr)
def test_a_run_time_clamp_is_logged_every_run(
    raw: Any, expected: int, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.apps.manifest import lifecycle_timeout_seconds

    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.manifest_sections"):
        got = lifecycle_timeout_seconds(raw, app_name="timeout-app", field_name="onEnableTimeout")
        lifecycle_timeout_seconds(raw, app_name="timeout-app", field_name="onEnableTimeout")
    assert got == expected and type(got) is int
    assert len(caplog.records) == 2, caplog.text
    assert all(
        "timeout-app" in r.getMessage() and "setup.onEnableTimeout" in r.getMessage()
        for r in caplog.records
    ), caplog.text


@pytest.mark.parametrize(("raw", "expected"), [(None, 30), *_KEPT], ids=repr)
def test_a_value_used_as_written_logs_nothing(
    raw: Any, expected: int, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.apps.manifest import lifecycle_timeout_seconds

    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.manifest_sections"):
        got = lifecycle_timeout_seconds(raw, app_name="timeout-app", field_name="onEnableTimeout")
    assert got == expected
    assert caplog.records == []


def _with_raw_setup(real_get_app: Any, **setup: Any) -> Any:
    """``get_app`` returning the stored record with raw ``setup`` values spliced in,
    as a hand-edited record (or one predating any check) carries them."""

    def _get_app(name: str) -> Any:
        info = real_get_app(name)
        if info:
            info = dict(info)
            manifest = dict(info.get("manifest") or {})
            manifest["setup"] = {**(manifest.get("setup") or {}), **setup}
            info["manifest"] = manifest
        return info

    return _get_app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
        (-1, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
        (True, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
        (_HUGE, LIFECYCLE_SCRIPT_TIMEOUT_MAX),
        ("abc", LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
        ("90", 90),
        (90, 90),
    ],
)
async def test_the_enable_route_clamps_a_stored_timeout(
    raw: Any, expected: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, setup={"onEnable": "setup.sh"})
    seen: list[int] = []

    async def _script(*args: Any, timeout: int = 0, **kwargs: Any) -> dict[str, Any]:
        seen.append(timeout)
        return {"output": "", "failed": False}

    monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _script)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    # Through the facade: get_app is bound by more than one routes owner.
    monkeypatch.setattr(
        routes_mod,
        "get_app",
        _with_raw_setup(routes_mod.get_app, onEnableTimeout=raw),
    )
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/enable")
        assert resp.status == 200, await resp.text()
    assert seen == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
        (True, 1),
        (_HUGE, LIFECYCLE_SCRIPT_TIMEOUT_MAX),
        ("90", 90),
    ],
    ids=repr,
)
async def test_an_installed_app_runs_onenable_clamped_and_logged(
    raw: Any,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The real path, nothing spliced: the manifest installs with the value as
    # written, and the clamp happens (and is logged) where the script runs.
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, setup={"onEnable": "setup.sh", "onEnableTimeout": raw})
    seen: list[int] = []

    async def _script(*args: Any, timeout: int = 0, **kwargs: Any) -> dict[str, Any]:
        seen.append(timeout)
        return {"output": "", "failed": False}

    monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _script)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.manifest_sections"):
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 200, await resp.text()
    assert seen == [expected]
    clamp_logs = [r for r in caplog.records if "setup.onEnableTimeout" in r.getMessage()]
    clamped = expected != int(raw)
    assert len(clamp_logs) == (1 if clamped else 0), caplog.text


async def _noop_hook(*args: Any, **kwargs: Any) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
        (-1, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
        (True, LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
        (_HUGE, LIFECYCLE_SCRIPT_TIMEOUT_MAX),
        ("abc", LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT),
        ("7", 7),
        (7, 7),
    ],
)
async def test_teardown_clamps_a_stored_on_disable_timeout(
    raw: Any, expected: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.apps.teardown as appteardown

    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, setup={"onDisable": "stop.sh"})
    seen: list[int] = []

    async def _script(name: str, script: str, *, timeout: int = 0, **kw: Any) -> dict[str, Any]:
        seen.append(timeout)
        return {"output": "", "failed": False}

    record = {
        "resources": "app",
        "enabled": True,
        "manifest": {"setup": {"onDisable": "stop.sh", "onDisableTimeout": raw}},
    }
    with (
        patch.object(appteardown, "on_app_disable", _noop_hook),
        patch.object(appteardown, "stop_app_backend", lambda name: True),
        patch.object(appteardown, "recorded_backend_port", lambda name: None),
        patch.object(appteardown, "unstopped_backend_port", lambda name, **kw: None),
        patch.object(appteardown, "deregister_app", lambda name: None),
        patch.object(appteardown, "run_lifecycle_script", _script),
    ):
        result = await appteardown.teardown_app_runtime(APP, record)
    assert result.ok, result.failures
    assert seen == [expected]


@pytest.mark.asyncio
async def test_a_registry_install_refuses_a_non_numeric_timeout_before_the_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pre-clone admission parse is where int() raised, so nothing was cloned and
    # no onInstall ran. The refusal stays there.
    from kiro_crew.apps import registry

    monkeypatch.setattr(
        registry,
        "_resolve_install_entry",
        lambda name: ({"name": "demo", "gitUrl": "https://github.com/o/demo.git"}, ""),
    )

    async def _manifest(*a: Any, **k: Any) -> dict[str, Any]:
        return {"name": "demo", "setup": {"onInstall": "true", "onEnableTimeout": "abc"}}

    async def _no_clone(*a: Any, **k: Any) -> dict[str, Any]:
        raise AssertionError("cloned a manifest the install always refuses")

    monkeypatch.setattr(registry, "_fetch_app_manifest", _manifest)
    monkeypatch.setattr(registry, "get_app", lambda name: None)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "_clone_build_app", _no_clone)
    result = await registry.install_from_registry("demo")
    assert result["ok"] is False
    assert "setup.onEnableTimeout" in result["error"]


@pytest.mark.asyncio
async def test_the_cloned_manifest_is_refused_before_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The repository can advance between the prefetch and the clone, so the cloned
    # manifest is checked too, before any build command or onInstall runs.
    from unittest.mock import MagicMock

    from kiro_crew.apps import registry

    src = tmp_path / "app-sources" / "demoapp"
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    async def _fake_clone(git_url: str, branch: str, dest: Path, log_lines: Any, **kw: Any) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        manifest = {"name": "demoapp", "setup": {"onDisableTimeout": "abc"}}
        (dest / "app.json").write_text(json.dumps(manifest), encoding="utf-8")

    build_calls: list[object] = []

    async def _fake_run_build(build_dir: Path, app_name: str, log_lines: Any) -> dict[str, Any]:
        build_calls.append(build_dir)
        return {"ok": True}

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "_run_app_build", _fake_run_build)

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    assert "setup.onDisableTimeout" in result["error"]
    assert build_calls == []
