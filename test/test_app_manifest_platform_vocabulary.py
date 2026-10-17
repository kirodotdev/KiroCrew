"""``platform.os`` and ``platform.installMode`` are closed vocabularies that ``validate`` enforces.

``PlatformConfig.supports_platform`` resolves an ``os`` name through ``_OS_TO_PLATFORM``, and
the install gates compare ``installMode`` with ``== "client"``. A value outside those sets
matches nothing, so a manifest saying ``"Windows"`` or ``"Client"`` validates and then behaves
as the opposite of what its author wrote: the platform gate answers no for Windows, or the
app installs as a server app. The rule lives in ``PlatformConfig.validate`` and is reached
through ``AppManifest.validate``, which ``install_app``, ``update_app`` and builtin discovery
call.

Nothing here needs a Windows host. ``supports_platform`` takes the platform string as an
argument, so ``"win32"`` is passed in directly, and the suite runs on every OS in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.execution import _BUILTINS_DIR
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _validate_source_path,
    get_app_manifest,
    install_app,
    update_app,
)
from kiro_crew.apps.manifest import AppManifest, PlatformConfig
from kiro_crew.apps.routes import _client_install_manifest

#: The names a manifest may use for ``platform.os``, with the ``sys.platform`` each reaches.
_CANONICAL_OS = (("macos", "darwin"), ("linux", "linux"), ("windows", "win32"))

#: Plausible hand-written spellings that are NOT in the vocabulary. ``win32`` and ``darwin``
#: are here because they are the platform strings, not the names a manifest uses.
_MISSPELLED_OS = (
    "Windows",
    "WINDOWS",
    "win",
    "windows ",
    " windows",
    "win32",
    "macOS",
    "Linux",
    "darwin",
    "osx",
    "",
)

_MISSPELLED_MODES = ("Client", "CLIENT", "client ", " client", "clients", "local", "Server", "")


def _manifest(**platform: object) -> AppManifest:
    """A manifest that is valid apart from the ``platform`` block it is given."""
    return AppManifest.from_dict(
        {
            "name": "probe-app",
            "version": "1.0.0",
            "displayName": "Probe App",
            "description": "A probe app",
            "platform": platform,
        }
    )


def _make_source(root: Path, version: str = "1.0.0", **platform: object) -> Path:
    """An app source directory whose ``app.json`` carries the given ``platform`` block."""
    src = root / f"source-{version}"
    src.mkdir(parents=True)
    body: dict[str, object] = {
        "name": "probe-app",
        "version": version,
        "displayName": "Probe App",
        "description": "A probe app",
        "author": "tester",
    }
    if platform:
        body["platform"] = platform
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(body), encoding="utf-8")
    return src


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """An isolated data home that admits the synthetic third-party app these tests install."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home


class TestOsNames:
    """Rule 1: every ``platform.os`` entry is one of the names ``_OS_TO_PLATFORM`` has a row for."""

    @pytest.mark.parametrize("name", _MISSPELLED_OS)
    def test_a_name_outside_the_vocabulary_is_refused_and_the_message_names_the_choices(self, name):
        errors = _manifest(os=[name]).validate()

        assert len(errors) == 1, f"{name!r} validated without an error"
        assert f"platform.os: unknown value {name!r}" in errors[0]
        for accepted, _sys_platform in _CANONICAL_OS:
            assert accepted in errors[0]

    @pytest.mark.parametrize("name", ["Windows", "WINDOWS", "win", "windows ", " windows"])
    def test_a_refused_windows_spelling_is_one_the_gate_excludes_from_windows(self, name):
        """Why refusing it matters: the spelling validated cleanly and the gate said no."""
        assert PlatformConfig(os=[name]).supports_platform("win32") is False
        assert _manifest(os=[name]).validate() != []

    @pytest.mark.parametrize("name", [*_MISSPELLED_OS, *(n for n, _p in _CANONICAL_OS)])
    def test_a_name_that_validates_is_one_the_gate_resolves(self, name):
        """No spelling may both pass validation and match no platform at all."""
        accepted = _manifest(os=[name]).validate() == []
        resolved = any(
            PlatformConfig(os=[name]).supports_platform(sys_platform)
            for _name, sys_platform in _CANONICAL_OS
        )
        assert resolved or not accepted

    @pytest.mark.parametrize(("name", "sys_platform"), _CANONICAL_OS)
    def test_each_canonical_name_validates_and_reaches_its_platform(self, name, sys_platform):
        manifest = _manifest(os=[name])

        assert manifest.validate() == []
        assert manifest.platform.supports_platform(sys_platform) is True

    def test_all_three_names_together_validate(self):
        assert _manifest(os=["macos", "linux", "windows"]).validate() == []

    def test_only_the_misspelled_entries_of_a_mixed_list_are_reported(self):
        errors = _manifest(os=["macos", "Linux", "windows"]).validate()

        assert len(errors) == 1
        assert "unknown value 'Linux'" in errors[0]

    def test_a_bare_string_is_not_read_as_the_name_it_spells(self):
        """``"os": "windows"`` is not a list, so it is refused rather than matched."""
        errors = _manifest(os="windows").validate()

        assert errors
        assert all(e.startswith("platform.os: unknown value") for e in errors)

    def test_an_absent_platform_block_keeps_the_default_and_validates(self):
        manifest = AppManifest.from_dict(
            {"name": "probe-app", "version": "1.0.0", "displayName": "P", "description": "d"}
        )

        assert manifest.validate() == []
        assert manifest.platform.os == ["macos", "linux"]

    def test_arch_is_not_a_vocabulary_no_gate_reads(self):
        """``arch`` has no reader, so an unfamiliar value is not an error."""
        assert _manifest(arch=["riscv64"]).validate() == []


class TestInstallMode:
    """Rule 2: ``installMode`` is ``"server"`` or ``"client"``, the two values the gates know."""

    @pytest.mark.parametrize("mode", _MISSPELLED_MODES)
    def test_a_mode_outside_the_vocabulary_is_refused_and_the_message_names_the_choices(self, mode):
        errors = _manifest(installMode=mode).validate()

        assert len(errors) == 1, f"{mode!r} validated without an error"
        assert f"platform.installMode: unknown value {mode!r}" in errors[0]
        assert "server" in errors[0] and "client" in errors[0]

    @pytest.mark.parametrize("mode", ["server", "client"])
    def test_each_mode_validates(self, mode):
        assert _manifest(installMode=mode).validate() == []

    def test_the_gate_reads_a_misspelled_mode_as_a_server_install(self):
        """Why refusing it matters: nothing downstream flags ``"Client"``, it is just a server app."""
        assert _client_install_manifest({"platform": {"installMode": "Client"}}) is None
        assert _client_install_manifest({"platform": {"installMode": "client"}}) is not None
        assert _manifest(installMode="Client").validate() != []


class TestEveryPlatformErrorIsReported:
    def test_a_bad_name_and_a_bad_mode_are_both_reported(self):
        errors = _manifest(os=["Windows"], installMode="Client").validate()

        assert len(errors) == 2
        assert any("platform.os" in e for e in errors)
        assert any("platform.installMode" in e for e in errors)


class TestReadersOfValidate:
    """Each caller of ``AppManifest.validate`` still works, and applies the rule where it applies."""

    def test_install_refuses_a_misspelled_name_and_installs_nothing(self, tmp_path, app_home):
        result = install_app(_make_source(tmp_path, os=["Windows"]))

        assert not result.ok
        assert "platform.os: unknown value 'Windows'" in result.error
        assert get_app_manifest("probe-app") is None

    def test_install_still_accepts_every_canonical_name(self, tmp_path, app_home):
        result = install_app(_make_source(tmp_path, os=["macos", "linux", "windows"]))

        assert result.ok, result.error
        installed = get_app_manifest("probe-app")
        assert installed is not None
        assert installed.platform.supports_platform("win32") is True

    def test_install_validation_reports_a_misspelled_mode(self, tmp_path):
        errors = _validate_source_path(_make_source(tmp_path, installMode="Client"))

        assert any("platform.installMode: unknown value 'Client'" in e for e in errors)

    def test_update_refuses_a_misspelled_name_and_leaves_the_installed_app_alone(
        self, tmp_path, app_home
    ):
        assert install_app(_make_source(tmp_path, os=["macos", "linux", "windows"])).ok

        refused = update_app(_make_source(tmp_path, version="2.0.0", os=["Windows"]))

        assert not refused.ok
        assert "platform.os: unknown value 'Windows'" in refused.error
        installed = get_app_manifest("probe-app")
        assert installed is not None
        assert installed.version == "1.0.0"
        assert installed.platform.os == ["macos", "linux", "windows"]
        assert installed.platform.supports_platform("win32") is True

    def test_update_still_accepts_a_corrected_spelling(self, tmp_path, app_home):
        assert install_app(_make_source(tmp_path, os=["macos", "linux", "windows"])).ok

        result = update_app(_make_source(tmp_path, version="2.0.0", os=["windows"]))

        assert result.ok, result.error

    def test_discovery_still_admits_every_shipped_builtin(self):
        shipped = [
            p
            for p in _BUILTINS_DIR.iterdir()
            if p.is_dir() and not p.name.startswith((".", "_")) and (p / "app.json").is_file()
        ]

        assert shipped
        assert len(discover_builtin_apps()) == len(shipped)

    @pytest.mark.parametrize(
        "app_dir",
        sorted(p for p in _BUILTINS_DIR.iterdir() if (p / "app.json").is_file()),
        ids=lambda p: p.name,
    )
    def test_no_shipped_builtin_declares_a_value_the_rule_refuses(self, app_dir):
        manifest = AppManifest.from_json_file(app_dir / "app.json")

        platform_errors = [
            e for e in manifest.validate(app_root=app_dir) if e.startswith("platform.")
        ]
        assert platform_errors == []
