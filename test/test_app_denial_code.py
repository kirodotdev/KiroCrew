"""The third-party execution denial must be machine-identifiable.

The frontend renders an actionable affordance ("open Security settings") for
exactly one failure: the execution gate refusing to run non-builtin app code.
It has to key that off a stable ``code``, because the accompanying ``error``
prose is English, unlocalizable, and free to be reworded at any time -- string
matching it would break silently on a copy edit.

Two denial sites reach the dashboard and BOTH are pinned here, because they
fail through different shapes: the registry install returns a payload dict, and
``enable_app`` returns an ``AppResult`` whose ``to_dict`` is what the HTTP layer
serializes. That second one is the regression this file exists for --
``AppResult.error_code`` was set by callers but never serialized, so every
structured code was silently dropped on the way out.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.manager import AppResult

_REPO_ROOT = Path(__file__).resolve().parents[1]


DENIAL_CODE = "app_execution_denied"


def _deny_third_party(monkeypatch) -> None:
    """Force the execution gate closed, the way a default install ships.

    ``registries`` is stubbed too: the registry path consults it for the
    trusted-clone host allowlist, so a namespace carrying only the agent block
    fails with an AttributeError instead of reaching the denial under test.
    """
    from kiro_crew.config import loader as cfg_loader

    monkeypatch.setattr(
        cfg_loader.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(
                agent=SimpleNamespace(apps_allow_third_party=False),
                registries=[],
            )
        ),
    )


class TestAppResultSerializesCode:
    """``to_dict`` is the wire boundary -- a code it drops does not exist."""

    def test_error_code_is_serialized_as_code(self):
        d = AppResult(ok=False, name="x", error="prose", error_code=DENIAL_CODE).to_dict()
        assert d["code"] == DENIAL_CODE
        # `error` stays, as advisory prose for an unknown-code fallback.
        assert d["error"] == "prose"

    def test_absent_error_code_adds_no_key(self):
        # An empty code must not ship `"code": ""`, which a frontend switch would
        # treat as a real (unrecognized) value.
        assert "code" not in AppResult(ok=False, name="x", error="prose").to_dict()

    def test_success_result_is_unchanged(self):
        d = AppResult(ok=True, name="x", message="done").to_dict()
        assert d == {"ok": True, "name": "x", "message": "done"}


class TestEnableDenialCarriesCode:
    def test_enable_app_denial_is_identifiable(self, monkeypatch, tmp_path):
        _deny_third_party(monkeypatch)
        from kiro_crew.apps import manager

        monkeypatch.setattr(manager, "config_dir", lambda: tmp_path)
        # A record must exist, or enable_app fails on "not installed" first and
        # never reaches the execution gate -- which would make this test pass
        # for the wrong reason.
        app_dir = tmp_path / "apps" / "thirdparty"
        app_dir.mkdir(parents=True)
        (app_dir / "installed.json").write_text(
            json.dumps(
                {
                    "name": "thirdparty",
                    "version": "1.0.0",
                    "displayName": "Third Party",
                    "enabled": False,
                    "manifest": {"name": "thirdparty", "version": "1.0.0"},
                }
            ),
            encoding="utf-8",
        )

        result = manager.enable_app("thirdparty")
        assert not result.ok
        assert result.error_code == DENIAL_CODE
        assert result.to_dict()["code"] == DENIAL_CODE


class TestRegistryInstallDenialCarriesCode:
    @pytest.mark.asyncio
    async def test_registry_install_denial_is_identifiable(self, monkeypatch, tmp_path):
        _deny_third_party(monkeypatch)
        from kiro_crew.apps import admission, registry

        # `install_from_registry` runs `app_admission_denied()` BEFORE the execution
        # gate, and that reads the real `config_dir()/app_admission.json`. On a host
        # with any enforcing policy (banned / non-empty approved / require_signature)
        # the call returns an ADMISSION denial -- a dict with no `code` key -- so
        # `result["code"]` would KeyError. Patch the module that actually reads it:
        # `admission.py` has its own `config_dir` import, so patching
        # `registry.config_dir` would not redirect the policy read.
        monkeypatch.setattr(admission, "config_dir", lambda: tmp_path)

        # The resolution path consults the official catalog FIRST, with a fresh
        # uncached HTTPS fetch (#4236); pin "catalog reachable, app absent" so
        # the seeded row below is what resolves, deterministically.
        monkeypatch.setattr(
            "kiro_crew.apps.official_catalog.inventory_for_install",
            lambda name: None,
        )

        monkeypatch.setattr(
            registry,
            "_load_registry_file",
            lambda: [
                {
                    "name": "thirdparty",
                    "gitUrl": "https://github.com/acme/thirdparty",
                    "repo": "https://github.com/acme/thirdparty",
                    "branch": "main",
                }
            ],
        )
        # `install_from_registry` fetches the app.json BEFORE it reaches the
        # execution gate, and that fetch shells out to `git clone` through the OS
        # sandbox. On a host with no sandbox backend (Windows CI, and any Linux
        # without user namespaces) that raises SandboxUnavailableError, so the
        # unstubbed test passes on macOS and fails everywhere else. Stub the fetch:
        # this test is about the denial's wire shape, not about cloning.

        async def _no_manifest(*_args, **_kwargs):
            return None

        monkeypatch.setattr(registry, "_fetch_app_manifest", _no_manifest)

        result = await registry.install_from_registry("thirdparty")
        assert result["ok"] is False
        assert result["code"] == DENIAL_CODE
        # The gate must refuse BEFORE any clone or install script runs.
        assert "blocked by execution policy" in result["error"]


def test_denial_code_matches_the_open_command_route():
    """One code for one condition, across every path that reports it.

    ``routes.py`` already shipped this code on the ``openCommand`` denial; the
    install and enable paths joining it is what lets the frontend handle the
    condition once instead of per-endpoint. If someone renames the code in one
    place, this fails rather than leaving the affordance silently dead on the
    other paths.
    """
    src = (_REPO_ROOT / "src/kiro_crew/apps/routes.py").read_text(encoding="utf-8")
    assert f'"code": "{DENIAL_CODE}"' in src
