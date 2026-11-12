"""The host placeholders any app's agent template may name (``apps/agent_template_values.py``).

Every value is computed in the gateway from the data home, its own environment and the
installed package; one the host cannot compute is left out. How a template naming them
registers, for an installed app and for a released built-in, is pinned in
``test_app_bridges_agent_configs.py`` (app-kit-platform.md § 34).
"""

from __future__ import annotations

import os

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401

#: The placeholder names, spelled as a template spells them: they are the contract.
APP_ROOT, APP_DATA, UV_BIN, HOST_PATH, PATH_SEP = (
    "{APP_ROOT}",
    "{APP_DATA}",
    "{UV_BIN}",
    "{HOST_PATH}",
    "{PATH_SEP}",
)


@pytest.fixture(name="values_mod")
def _values_mod():
    from kiro_crew.apps import agent_template_values

    return agent_template_values


def _values(values_mod, root):
    return values_mod.host_placeholder_values("some-app", root)


class TestHostValues:
    def test_every_value_is_derived_from_the_data_home_and_the_root(
        self, tmp_path, app_env, values_mod
    ):
        root = tmp_path / "registered-root"
        values = _values(values_mod, root)

        assert values[APP_ROOT] == str(root)
        assert values[APP_DATA] == str(app_env["home"] / "apps" / "some-app" / "data")
        assert values[PATH_SEP] == os.pathsep
        assert set(values) <= {APP_ROOT, APP_DATA, UV_BIN, HOST_PATH, PATH_SEP}
        assert set(values_mod.HOST_PLACEHOLDERS) == {
            APP_ROOT,
            APP_DATA,
            UV_BIN,
            HOST_PATH,
            PATH_SEP,
        }

    def test_the_data_dir_is_named_not_created(self, tmp_path, app_env, values_mod):
        """Rendering a template is not a reason to create a directory."""
        _values(values_mod, tmp_path)
        assert not (app_env["home"] / "apps" / "some-app").exists()

    def test_the_data_dir_is_the_one_the_hooks_receive(self, tmp_path, app_env, values_mod):
        from kiro_crew.apps.manager import app_data_dir

        assert _values(values_mod, tmp_path)[APP_DATA] == str(app_data_dir("some-app"))

    def test_a_host_without_uv_offers_no_uv_bin(self, tmp_path, app_env, values_mod, monkeypatch):
        monkeypatch.setattr(values_mod, "resolve_uv", lambda: None)
        assert UV_BIN not in _values(values_mod, tmp_path)

    def test_the_resolved_uv_is_offered(self, tmp_path, app_env, values_mod, monkeypatch):
        monkeypatch.setattr(values_mod, "resolve_uv", lambda: "/opt/uv/bin/uv")
        assert _values(values_mod, tmp_path)[UV_BIN] == "/opt/uv/bin/uv"

    def test_an_empty_path_offers_no_host_path(self, tmp_path, app_env, values_mod, monkeypatch):
        """``{HOST_PATH}{PATH_SEP}<dir>`` from an empty value would start with an empty
        element, which on POSIX is the current directory."""
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(values_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(values_mod.soffice_discovery, "windows_install_path", lambda: None)
        assert HOST_PATH not in _values(values_mod, tmp_path)

    def test_an_off_path_soffice_dir_is_appended(self, tmp_path, app_env, values_mod, monkeypatch):
        """LibreOffice's Windows installer leaves ``PATH`` alone, so a child looking
        ``soffice`` up by name needs its fixed install dir appended."""
        program = tmp_path / "LibreOffice" / "program"
        monkeypatch.setenv("PATH", "inherited")
        monkeypatch.setattr(values_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            values_mod.soffice_discovery,
            "windows_install_path",
            lambda: str(program / "soffice.exe"),
        )
        assert _values(values_mod, tmp_path)[HOST_PATH] == os.pathsep.join(
            ["inherited", str(program)]
        )

    def test_an_on_path_soffice_adds_nothing(self, tmp_path, app_env, values_mod, monkeypatch):
        monkeypatch.setenv("PATH", "inherited")
        monkeypatch.setattr(values_mod.shutil, "which", lambda name: "/usr/bin/soffice")
        monkeypatch.setattr(
            values_mod.soffice_discovery, "windows_install_path", lambda: r"C:\x\soffice.exe"
        )
        assert _values(values_mod, tmp_path)[HOST_PATH] == "inherited"
