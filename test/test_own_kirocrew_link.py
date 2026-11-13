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
import kiro_crew.env_own_kirocrew as own_mod
from kiro_crew import sandbox_leaves, sandbox_precreate, security
from kiro_crew.env import augmented_path
from kiro_crew.env_own_kirocrew import OWN_KIROCREW_DIR

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the POSIX entry is a symlink; Windows writes a .cmd forwarder"
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
    monkeypatch.setattr(own_mod, "peek_data_home", lambda: crew_home)
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


@_POSIX_ONLY
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


@_POSIX_ONLY
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


@_POSIX_ONLY
@pytest.mark.parametrize(
    "path",
    [
        "~/.kiro/crew/kirocrew-bin/kirocrew",
        "~/.kirocrew/kirocrew-bin/kirocrew",
        "~/.kiro/crew/kirocrew-bin/kirocrew.cmd",
    ],
)
def test_the_file_tool_gate_refuses_a_write_to_the_link(path: str) -> None:
    assert security.is_sensitive_write_path(path) is True
    assert security.is_sensitive_path(path) is False


@_POSIX_ONLY
def test_the_link_directory_is_sealed_read_only_in_the_sandbox() -> None:
    """Writable, it would let an agent choose the ``kirocrew`` every lookup runs."""
    assert OWN_KIROCREW_DIR in sandbox_leaves._CREW_READONLY_LEAVES
    assert OWN_KIROCREW_DIR in sandbox_leaves._CREW_CHILD_READABLE_LEAVES
    assert OWN_KIROCREW_DIR in sandbox_precreate._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert OWN_KIROCREW_DIR in sandbox_precreate._CREW_NOFOLLOW_READONLY_DIR_LEAVES


class TestTheLinkDirectoryIsListedOnlyWhenItCanBeNamed:
    """``own_kirocrew_dirs`` lists nothing it cannot name safely."""

    def test_windows_lists_the_directory_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(own_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(own_mod, "peek_data_home", lambda: tmp_path)
        (tmp_path / OWN_KIROCREW_DIR).mkdir()

        assert own_mod.own_kirocrew_dirs(lambda value: value) == [str(tmp_path / OWN_KIROCREW_DIR)]

    def test_a_junction_at_the_directory_name_is_not_listed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``islink`` is False for a Windows junction; the listing must still refuse it."""
        monkeypatch.setattr(own_mod, "peek_data_home", lambda: tmp_path)
        (tmp_path / OWN_KIROCREW_DIR).mkdir()
        named = str(tmp_path / OWN_KIROCREW_DIR)
        monkeypatch.setattr(
            own_mod.platform_compat, "is_link_or_junction", lambda value: value == named
        )

        assert own_mod.own_kirocrew_dirs(lambda value: value) == []

    def test_an_unresolvable_crew_home_lists_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Path:
            raise RuntimeError("no crew home")

        monkeypatch.setattr(own_mod, "peek_data_home", _boom)
        assert own_mod.own_kirocrew_dirs(lambda value: value) == []


class TestTheWindowsForwarderBody:
    """``windows_forwarder``: the one line ``kirocrew.cmd`` holds."""

    def test_it_runs_the_quoted_target_with_every_argument(self) -> None:
        body = own_mod.windows_forwarder(r"C:\Program Files\Kiro Crew\Scripts\kirocrew.cmd")

        assert body == b'@"C:\\Program Files\\Kiro Crew\\Scripts\\kirocrew.cmd" %*\r\n'

    def test_a_percent_in_the_path_is_not_expanded(self) -> None:
        body = own_mod.windows_forwarder(r"C:\100%PATH%\kirocrew.exe")

        assert body == b'@"C:\\100%%PATH%%\\kirocrew.exe" %*\r\n'

    def test_metacharacters_stay_inside_the_quotes(self) -> None:
        body = own_mod.windows_forwarder(r"C:\a&b^c\kirocrew.exe")

        assert body == b'@"C:\\a&b^c\\kirocrew.exe" %*\r\n'

    @pytest.mark.parametrize(
        "bad",
        [
            'C:\\a"b\\kirocrew.exe',
            "C:\\a\r\nb\\kirocrew.exe",
            "C:\\a\0b",
            "C:\\Kiro!Crew\\kirocrew.exe",
            "C:\\Users\\Jos\u00e9\\kirocrew.exe",
        ],
        ids=["quote", "line-break", "nul", "delayed-expansion", "non-ascii"],
    )
    def test_a_target_every_console_cannot_read_alike_gets_no_body(self, bad: str) -> None:
        """``!`` is eaten under ``cmd /V:ON``; a non-ASCII byte depends on ``chcp``."""
        assert own_mod.windows_forwarder(bad) is None


class TestReadingAForwarderBack:
    """``forwarder_target``: which install a forwarder runs, for readers ``realpath`` misses."""

    @pytest.mark.parametrize(
        "target", [r"C:\install\Scripts\kirocrew.cmd", r"C:\100%PATH%\kirocrew.exe"]
    )
    def test_it_reads_back_the_target_it_was_written_for(self, tmp_path, target: str) -> None:
        forwarder = tmp_path / "kirocrew.cmd"
        body = own_mod.windows_forwarder(target)
        assert body is not None
        forwarder.write_bytes(body)

        assert own_mod.forwarder_target(str(forwarder)) == target

    @pytest.mark.parametrize(
        ("name", "body"),
        [
            ("kirocrew.exe", b'@"C:\\x\\kirocrew.exe" %*\r\n'),
            ("kirocrew.cmd", b"@echo off\r\nC:\\x\\kirocrew.exe %*\r\n"),
            ("kirocrew.cmd", b'@"C:\\x\\kirocrew.exe" %*\r\n@"C:\\y\\kirocrew.exe" %*\r\n'),
            ("kirocrew.cmd", b'@"C:\\Jos\xe9\\kirocrew.exe" %*\r\n'),
        ],
        ids=["not-a-cmd", "other-shape", "two-lines", "not-ascii"],
    )
    def test_anything_else_reads_as_no_forwarder(self, tmp_path, name: str, body: bytes) -> None:
        path = tmp_path / name
        path.write_bytes(body)

        assert own_mod.forwarder_target(str(path)) is None

    def test_a_missing_file_reads_as_no_forwarder(self, tmp_path) -> None:
        assert own_mod.forwarder_target(str(tmp_path / "kirocrew.cmd")) is None


@pytest.fixture
def forwarder_dir(tmp_path: Path) -> Path:
    return tmp_path / "crew" / OWN_KIROCREW_DIR


class TestTheGatewayWritesTheWindowsForwarder:
    """``_refresh_windows_forwarder``, driven directly so it runs on every OS."""

    def test_it_writes_one_forwarder_naming_the_running_launcher(self, forwarder_dir) -> None:
        target = r"C:\install\Scripts\kirocrew.cmd"

        written = agent_mod._refresh_windows_forwarder(forwarder_dir, target)

        assert written == str(forwarder_dir / "kirocrew.cmd")
        assert sorted(os.listdir(forwarder_dir)) == ["kirocrew.cmd"]
        assert (forwarder_dir / "kirocrew.cmd").read_bytes() == own_mod.windows_forwarder(target)

    def test_a_second_refresh_changes_nothing(self, forwarder_dir) -> None:
        target = r"C:\install\Scripts\kirocrew.cmd"
        agent_mod._refresh_windows_forwarder(forwarder_dir, target)

        assert agent_mod._refresh_windows_forwarder(forwarder_dir, target) is None

    def test_a_forwarder_to_another_install_is_rewritten(self, forwarder_dir) -> None:
        agent_mod._refresh_windows_forwarder(forwarder_dir, r"C:\old\kirocrew.exe")
        target = r"C:\new\Scripts\kirocrew.cmd"

        assert agent_mod._refresh_windows_forwarder(forwarder_dir, target)

        assert (forwarder_dir / "kirocrew.cmd").read_bytes() == own_mod.windows_forwarder(target)

    @pytest.mark.parametrize(
        "unusable",
        [None, "C:\\Users\\Jos\u00e9\\kirocrew.exe", "C:\\Kiro!Crew\\kirocrew.exe", "INSIDE"],
        ids=["unresolved", "non-ascii-without-short-name", "delayed-expansion", "itself"],
    )
    def test_an_unwritable_forwarder_removes_the_stale_one(
        self, forwarder_dir, monkeypatch, unusable: str | None
    ) -> None:
        """A stale forwarder would still win the lookup and run another install."""
        monkeypatch.setattr(agent_mod.platform_compat, "short_path_name", lambda _p: "")
        agent_mod._refresh_windows_forwarder(forwarder_dir, r"C:\old\kirocrew.exe")
        if unusable == "INSIDE":
            unusable = str(forwarder_dir / "kirocrew.cmd")

        assert agent_mod._refresh_windows_forwarder(forwarder_dir, unusable) is None
        assert not (forwarder_dir / "kirocrew.cmd").exists()

    def test_a_non_ascii_target_is_written_by_its_short_name(
        self, forwarder_dir, monkeypatch
    ) -> None:
        """``C:\\Users\\José`` must not lose the fix; its 8.3 name is ASCII."""
        target = "C:\\Users\\Jos\u00e9\\AppData\\Local\\kirocrew.cmd"
        short = r"C:\Users\JOS~1\AppData\Local\kirocrew.cmd"
        monkeypatch.setattr(agent_mod.platform_compat, "short_path_name", {target: short}.get)

        assert agent_mod._refresh_windows_forwarder(forwarder_dir, target)

        assert (forwarder_dir / "kirocrew.cmd").read_bytes() == own_mod.windows_forwarder(short)

    def test_a_short_name_that_is_still_not_ascii_writes_nothing(
        self, forwarder_dir, monkeypatch
    ) -> None:
        """A volume with 8.3 names off hands back the long spelling unchanged."""
        target = "C:\\Users\\Jos\u00e9\\kirocrew.cmd"
        monkeypatch.setattr(agent_mod.platform_compat, "short_path_name", lambda p: p)

        assert agent_mod._refresh_windows_forwarder(forwarder_dir, target) is None
        assert not (forwarder_dir / "kirocrew.cmd").exists()

    def test_the_gateway_start_routes_windows_to_the_forwarder(self, host, monkeypatch) -> None:
        calls: list[tuple[Path, str | None]] = []
        monkeypatch.setattr(agent_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            agent_mod, "_refresh_windows_forwarder", lambda d, t: calls.append((d, t)) or None
        )

        agent_mod.refresh_own_kirocrew_link()
        monkeypatch.setattr(agent_mod, "_resolve_kirocrew_bin", lambda: "kirocrew")
        agent_mod.refresh_own_kirocrew_link()

        directory = host["crew_home"] / OWN_KIROCREW_DIR
        assert calls == [(directory, str(host["this_install"])), (directory, None)]
        assert not (directory / "kirocrew").exists()


@pytest.mark.skipif(os.name != "nt", reason="needs cmd.exe and PATHEXT lookup")
class TestOnWindowsTheForwarderWinsTheLookup:
    @pytest.fixture
    def win_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
        home = tmp_path / "home"
        crew_home = tmp_path / "crew"
        crew_home.mkdir()
        stale = home / ".local" / "bin" / "kirocrew.exe"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"")
        inherited = tmp_path / "inherited"
        inherited.mkdir()
        (inherited / "kirocrew.exe").write_bytes(b"")
        this_install = tmp_path / "install" / "Scripts" / "kirocrew.cmd"
        this_install.parent.mkdir(parents=True)
        this_install.write_bytes(b"@echo off\r\necho [%~1] [%~2]\r\nexit /b 7\r\n")
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(own_mod, "peek_data_home", lambda: crew_home)
        monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: crew_home)
        monkeypatch.setattr(agent_mod, "_resolve_kirocrew_bin", lambda: str(this_install))
        return {
            "crew_home": crew_home,
            "inherited": inherited,
            "this_install": this_install,
            "tmp": tmp_path,
        }

    def test_an_earlier_kirocrew_exe_loses_to_this_install(self, win_host) -> None:
        assert agent_mod.refresh_own_kirocrew_link() is not None

        found = shutil.which("kirocrew", path=augmented_path(str(win_host["inherited"])))

        forwarder = win_host["crew_home"] / OWN_KIROCREW_DIR / "kirocrew.cmd"
        assert found is not None
        assert os.path.normcase(found) == os.path.normcase(str(forwarder))

    def test_the_forwarder_passes_arguments_and_the_exit_code(self, win_host) -> None:
        from installer_test_helpers import run_bounded

        forwarder = agent_mod.refresh_own_kirocrew_link()
        assert forwarder is not None

        result = run_bounded([forwarder, "a b", "c"], dict(os.environ), cwd=str(win_host["tmp"]))

        assert result.returncode == 7
        assert result.stdout.strip() == "[a b] [c]"

    def test_python_resolves_exactly_as_without_the_forwarder(self, win_host, monkeypatch) -> None:
        agent_mod.refresh_own_kirocrew_link()
        base = str(win_host["inherited"])
        with_forwarder = shutil.which("python", path=augmented_path(base))
        monkeypatch.setattr(env_mod, "_own_kirocrew_dirs", lambda: [])

        assert shutil.which("python", path=augmented_path(base)) == with_forwarder
