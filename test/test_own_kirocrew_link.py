"""An agent's ``kirocrew`` resolves to the install the gateway runs.

An older install's launcher in ``~/.local/bin`` must not win the PATH lookup: an
old CLI's config save strips every crewmate's member identity. The gateway links
its own ``kirocrew`` into a crew-home directory that holds nothing else, and
:func:`kiro_crew.env.augmented_path` lists that directory ahead of
``~/.local/bin``. The interpreter's directory stays last, so ``python`` and
``pip`` keep resolving where they did.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.env as env_mod
from kiro_crew import sandbox_leaves, sandbox_precreate, security
from kiro_crew.env import OWN_KIROCREW_DIR, augmented_path

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the own-kirocrew link is POSIX only; Windows runs the .exe"
)


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A home with a stale ``~/.local/bin/kirocrew``, a crew home, and this install."""
    home = tmp_path / "home"
    crew_home = tmp_path / "crew"
    crew_home.mkdir()
    stale = _executable(home / ".local" / "bin" / "kirocrew")
    this_install = _executable(tmp_path / "install" / "bin" / "kirocrew")
    base = tmp_path / "base"
    base_python = _executable(base / "python")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(env_mod, "peek_data_home", lambda: crew_home)
    monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: crew_home)
    monkeypatch.setattr(agent_mod, "_resolve_kirocrew_bin", lambda: str(this_install))
    promoted = _executable(tmp_path / "install-next" / "bin" / "kirocrew")
    monkeypatch.setattr(agent_mod, "_through_stable_link", lambda _p: str(promoted))
    return {
        "promoted": promoted,
        "home": home,
        "crew_home": crew_home,
        "stale": stale,
        "this_install": this_install,
        "base": base,
        "base_python": base_python,
    }


def _which(name: str, base: Path) -> str | None:
    return shutil.which(name, path=augmented_path(str(base)))


class TestAnAgentReachesThisInstall:
    def test_a_stale_launcher_in_local_bin_loses_the_lookup(self, host) -> None:
        assert agent_mod.refresh_own_kirocrew_link() is not None

        found = _which("kirocrew", host["base"])

        assert found is not None
        assert os.path.realpath(found) == os.path.realpath(host["this_install"])

    def test_the_link_names_the_running_tree_not_the_promoted_stable_link(self, host) -> None:
        """An update promotes the stable link before the gateway restarts."""
        agent_mod.refresh_own_kirocrew_link()

        link = host["crew_home"] / OWN_KIROCREW_DIR / "kirocrew"
        assert os.readlink(link) == str(host["this_install"])
        assert os.readlink(link) != str(host["promoted"])

    def test_the_link_directory_holds_only_kirocrew(self, host) -> None:
        agent_mod.refresh_own_kirocrew_link()

        directory = host["crew_home"] / OWN_KIROCREW_DIR
        assert sorted(os.listdir(directory)) == ["kirocrew"]
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_python_resolves_exactly_as_without_the_link(self, host, monkeypatch) -> None:
        agent_mod.refresh_own_kirocrew_link()
        with_link = _which("python", host["base"])
        monkeypatch.setattr(env_mod, "_own_kirocrew_dirs", lambda: [])
        without_link = _which("python", host["base"])

        assert with_link == without_link
        assert with_link == str(host["base_python"])

    def test_the_link_dir_leads_local_bin_and_the_interpreter_dir_stays_last(self, host) -> None:
        dirs = augmented_path(str(host["base"])).split(os.pathsep)

        own = dirs.index(str(host["crew_home"] / OWN_KIROCREW_DIR))
        assert own < dirs.index(str(host["home"] / ".local" / "bin"))
        tail = env_mod._interpreter_script_dirs()
        assert dirs[-len(tail) :] == tail

    def test_before_the_gateway_links_it_the_lookup_is_unchanged(self, host) -> None:
        """An empty or absent directory is a PATH entry nothing resolves through."""
        found = _which("kirocrew", host["base"])

        assert found is not None
        assert os.path.realpath(found) == os.path.realpath(host["stale"])

    def test_a_symlinked_link_directory_is_not_listed_or_written(self, host, tmp_path) -> None:
        """A link at the directory NAME would put a tree it chose ahead of ~/.local/bin."""
        planted = tmp_path / "planted"
        _executable(planted / "kirocrew")
        (host["crew_home"] / OWN_KIROCREW_DIR).symlink_to(planted)

        assert agent_mod.refresh_own_kirocrew_link() is None
        found = _which("kirocrew", host["base"])

        assert found is not None
        assert os.path.realpath(found) == os.path.realpath(host["stale"])
        assert sorted(os.listdir(planted)) == ["kirocrew"]

    def test_a_pinned_home_gets_no_link_directory(self, host) -> None:
        """A caller resolving another account's tools must not get this crew's link."""
        dirs = augmented_path(str(host["base"]), home=str(host["home"])).split(os.pathsep)

        assert str(host["crew_home"] / OWN_KIROCREW_DIR) not in dirs


class TestTheGatewayRefreshesTheLink:
    def test_a_second_refresh_changes_nothing(self, host) -> None:
        assert agent_mod.refresh_own_kirocrew_link() is not None
        assert agent_mod.refresh_own_kirocrew_link() is None

    def test_a_link_to_another_install_is_repointed(self, host) -> None:
        directory = host["crew_home"] / OWN_KIROCREW_DIR
        directory.mkdir()
        (directory / "kirocrew").symlink_to(host["stale"])

        assert agent_mod.refresh_own_kirocrew_link() is not None

        assert os.readlink(directory / "kirocrew") == str(host["this_install"])
        assert sorted(os.listdir(directory)) == ["kirocrew"]

    def test_a_bare_name_writes_no_link(self, host, monkeypatch) -> None:
        monkeypatch.setattr(agent_mod, "_resolve_kirocrew_bin", lambda: "kirocrew")

        assert agent_mod.refresh_own_kirocrew_link() is None
        assert not (host["crew_home"] / OWN_KIROCREW_DIR / "kirocrew").exists()

    def test_an_unwritable_crew_home_is_logged_not_raised(self, host, monkeypatch) -> None:
        def _refuse(*_a, **_k):
            raise PermissionError("read-only")

        monkeypatch.setattr(agent_mod, "mkdirs_owner_only", _refuse)

        assert agent_mod.refresh_own_kirocrew_link() is None


@pytest.mark.parametrize(
    "path", ["~/.kiro/crew/kirocrew-bin/kirocrew", "~/.kirocrew/kirocrew-bin/kirocrew"]
)
def test_the_file_tool_gate_refuses_a_write_to_the_link(path: str) -> None:
    assert security.is_sensitive_write_path(path) is True
    assert security.is_sensitive_path(path) is False


def test_the_link_directory_is_sealed_read_only_in_the_sandbox() -> None:
    """Writable, it would let an agent choose the ``kirocrew`` every lookup runs."""
    assert OWN_KIROCREW_DIR in sandbox_leaves._CREW_READONLY_LEAVES
    assert OWN_KIROCREW_DIR in sandbox_leaves._CREW_CHILD_READABLE_LEAVES
    assert OWN_KIROCREW_DIR in sandbox_precreate._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert OWN_KIROCREW_DIR in sandbox_precreate._CREW_NOFOLLOW_READONLY_DIR_LEAVES
