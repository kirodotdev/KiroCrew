"""The KAS ``file://`` prompt fence covers every folder gh keeps ``hosts.yml`` in.

gh reads ``GH_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/gh``, else ``%APPDATA%\\GitHub CLI``
on Windows, else ``~/.config/gh``. Each of those, and each spelling Windows treats as
the same file (case, ``\\`` or ``/``, a trailing separator), is refused before the read.
The token files hold fake values only; they exist so a missed refusal would inline them.
"""

from __future__ import annotations

import ntpath
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import kas_prompt, kas_prompt_paths
from kiro_crew.acp.kas_agents import KasAgentTranslationError, resolve_prompt
from kiro_crew.acp.kas_prompt_paths import is_unsafe_prompt_path
from kiro_crew.security import argv_floor_hosts, paths_resolver

_FAKE = "oauth_token: gho_not_a_real_token\n"
_REFUSED = "not an allowed location"
_OWN_HOST_NAMES = frozenset({"crew-test-self", "crew-test-self.example.test"})


@pytest.fixture(autouse=True)
def _pinned_own_host_names(_floor_monkeypatch):
    # A UNC host that names this machine is refused, so the share hosts below
    # (``fileserver``, ``srv``, ``slowhost``) must never be the real hostname.
    _floor_monkeypatch.setattr(argv_floor_hosts, "_own_host_names", lambda: _OWN_HOST_NAMES)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "notes.md").write_text("notes", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(var, raising=False)
    roaming = tmp_path / "roaming"
    token = roaming / "GitHub CLI" / "hosts.yml"
    token.parent.mkdir(parents=True)
    token.write_text(_FAKE, encoding="utf-8")
    return agents, roaming, token


def _resolve(ref: str, agents: Path) -> str:
    return resolve_prompt({"prompt": ref}, agent_id="a", agents_dir=agents)


def _refused(ref: str, agents: Path) -> None:
    with pytest.raises(KasAgentTranslationError, match=_REFUSED):
        _resolve(ref, agents)


# A trailing ``\`` on APPDATA is a Windows spelling; Path folds it there, and the
# backslash case is pinned on the bare check below.
@pytest.mark.parametrize("suffix", ["", "/"])
def test_the_appdata_gh_folder_is_refused_absolute_and_through_the_variable(
    env, monkeypatch, suffix
):
    agents, roaming, token = env
    monkeypatch.setenv("APPDATA", f"{roaming}{suffix}")
    _refused(f"file://{token}", agents)
    _refused("file://$APPDATA/GitHub CLI/hosts.yml", agents)
    _refused("file://${APPDATA}/GitHub CLI/hosts.yml", agents)


def test_the_windows_percent_variable_form_is_refused(env, monkeypatch):
    # ``os.path.expandvars`` is ``ntpath.expandvars`` on Windows, which reads ``%VAR%``.
    agents, roaming, _token = env
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setattr(kas_prompt.os.path, "expandvars", ntpath.expandvars)
    _refused("file://%APPDATA%\\GitHub CLI\\hosts.yml", agents)


@pytest.mark.parametrize("upper_env", [False, True])
@pytest.mark.parametrize(
    "rel",
    [
        "GitHub CLI/hosts.yml",
        "github cli/HOSTS.YML",
        "GITHUB CLI/hosts.yml",
        "GitHub CLI\\hosts.yml",
        "GitHub CLI",
        "GitHub CLI/",
    ],
)
def test_case_and_slash_spellings_meet_the_same_anchor(env, monkeypatch, rel, upper_env):
    _agents, roaming, _token = env
    monkeypatch.setenv("APPDATA", str(roaming).upper() if upper_env else str(roaming))
    assert is_unsafe_prompt_path(Path(f"{roaming}/{rel}"))
    assert is_unsafe_prompt_path(Path(f"{roaming}\\{rel}".replace("/", "\\")))


def test_an_appdata_value_spelled_with_backslashes_still_anchors(env, monkeypatch):
    _agents, roaming, _token = env
    monkeypatch.setenv("APPDATA", str(roaming).replace("/", "\\") + "\\")
    assert is_unsafe_prompt_path(Path(f"{roaming}/GitHub CLI/hosts.yml"))


def test_the_default_roaming_folder_is_refused_with_appdata_unset(env):
    agents, _roaming, _token = env
    home = agents.parent.parent
    token = home / "AppData" / "Roaming" / "GitHub CLI" / "hosts.yml"
    token.parent.mkdir(parents=True)
    token.write_text(_FAKE, encoding="utf-8")
    _refused(f"file://{token}", agents)
    _refused("file://~/AppData/Roaming/GitHub CLI/hosts.yml", agents)


@pytest.mark.parametrize(("var", "leaf"), [("GH_CONFIG_DIR", ""), ("XDG_CONFIG_HOME", "gh")])
def test_gh_config_dir_and_xdg_config_home_are_refused(env, tmp_path, monkeypatch, var, leaf):
    agents, _roaming, _token = env
    root = tmp_path / "moved"
    token = (root / leaf if leaf else root) / "hosts.yml"
    token.parent.mkdir(parents=True)
    token.write_text(_FAKE, encoding="utf-8")
    monkeypatch.setenv(var, str(root))
    _refused(f"file://{token}", agents)
    _refused(f"file://${var}/{leaf + '/' if leaf else ''}hosts.yml", agents)


def test_ordinary_files_next_to_the_gh_folders_still_load(env, monkeypatch):
    agents, roaming, _token = env
    monkeypatch.setenv("APPDATA", str(roaming))
    for name in ("GitHub CLI Extras", "Other"):
        p = roaming / name / "p.md"
        p.parent.mkdir()
        p.write_text(name, encoding="utf-8")
        assert _resolve(f"file://{p}", agents) == name
    assert _resolve("file://./notes.md", agents) == "notes"


def _win(path: Path | str) -> str:
    return str(path).replace("/", "\\")


@pytest.mark.parametrize(
    "prefix",
    [
        "\\\\localhost\\C$",
        "\\\\127.0.0.1\\c$",
        "\\\\MYHOST\\C$",
        "\\\\?\\UNC\\localhost\\C$",
        "\\\\.\\UNC\\localhost\\C$",
        "\\??\\UNC\\localhost\\C$",
        "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1",
        "//localhost/C$",
    ],
)
def test_unc_and_device_spellings_of_the_token_are_refused(env, monkeypatch, prefix):
    _agents, roaming, token = env
    monkeypatch.setenv("APPDATA", str(roaming))
    # The share stands for the drive, so the token's drive letter is dropped.
    assert is_unsafe_prompt_path(Path(prefix + _win(token)[len(token.drive) :]))


def test_an_ordinary_share_prompt_still_loads(env):
    # A mapped drive or a roaming profile resolves to a share like this one.
    assert not is_unsafe_prompt_path(Path("\\\\fileserver\\team\\prompts\\p.md"))
    assert not is_unsafe_prompt_path(Path("//fileserver/team/prompts/p.md"))


def test_a_hidden_remote_share_prompt_still_loads(env):
    assert not is_unsafe_prompt_path(Path("\\\\srv\\team$\\prompts\\p.md"))


@pytest.mark.parametrize("path", ["\\\\fileserver\\C$\\Data\\p.md", "\\\\fileserver\\admin$\\x"])
def test_a_share_that_aliases_a_local_volume_is_refused(env, path):
    assert is_unsafe_prompt_path(Path(path))


@pytest.mark.skipif(sys.platform == "win32", reason="a leading // is a UNC path on Windows")
def test_a_posix_double_slash_gh_config_dir_is_resolved_and_fenced(env, tmp_path, monkeypatch):
    token = tmp_path / "gh" / "hosts.yml"
    token.parent.mkdir()
    token.write_text(_FAKE, encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", "/" + str(tmp_path / "gh"))
    assert is_unsafe_prompt_path(token.resolve())


def test_a_unc_appdata_still_fences_its_gh_folder_by_spelling(env, monkeypatch):
    monkeypatch.setenv("APPDATA", "\\\\fileserver\\profiles\\Roaming")
    assert is_unsafe_prompt_path(Path("\\\\fileserver\\profiles\\Roaming\\GitHub CLI\\hosts.yml"))
    assert is_unsafe_prompt_path(Path("//FILESERVER/profiles/roaming/github cli/hosts.yml"))
    assert not is_unsafe_prompt_path(Path("\\\\fileserver\\profiles\\Roaming\\Other\\p.md"))


def test_the_long_path_prefix_folds_to_the_drive_path_on_windows(env, monkeypatch):
    # The repo's own Windows alias folder (paths_resolver._fold_windows_alias) runs first,
    # so ``\\?\C:\...`` is judged as ``C:\...``: refused under a gh folder, loaded elsewhere.
    _agents, _roaming, _token = env
    monkeypatch.setattr(paths_resolver, "_ON_WINDOWS", True)
    monkeypatch.setenv("APPDATA", "C:\\Users\\me\\AppData\\Roaming")
    roaming = "C:\\Users\\me\\AppData\\Roaming"
    assert is_unsafe_prompt_path(Path(f"\\\\?\\{roaming}\\GitHub CLI\\hosts.yml"))
    assert is_unsafe_prompt_path(Path(f"\\\\?\\{roaming}\\GitHub CLI\\hosts.yml::$DATA"))
    assert not is_unsafe_prompt_path(Path(f"\\\\?\\{roaming}\\Other\\p.md"))


def test_a_long_path_gh_config_dir_meets_the_folded_candidate_on_windows(env, monkeypatch):
    monkeypatch.setattr(paths_resolver, "_ON_WINDOWS", True)
    monkeypatch.setenv("GH_CONFIG_DIR", "\\\\?\\C:\\custom-gh")
    assert is_unsafe_prompt_path(Path("C:\\custom-gh\\hosts.yml"))
    assert is_unsafe_prompt_path(Path("\\\\?\\C:\\custom-gh\\hosts.yml"))
    assert not is_unsafe_prompt_path(Path("C:\\other\\p.md"))


def test_a_unc_gh_config_dir_with_dot_dot_meets_the_canonical_candidate(env, monkeypatch):
    # A UNC anchor is never resolved, so its ``..`` is folded away lexically.
    monkeypatch.setenv("GH_CONFIG_DIR", "\\\\fileserver\\team\\configs\\..\\gh")
    assert is_unsafe_prompt_path(Path("\\\\fileserver\\team\\gh\\hosts.yml"))
    assert not is_unsafe_prompt_path(Path("\\\\fileserver\\team\\other\\p.md"))


@pytest.mark.parametrize("prefix", ["\\\\?\\UNC\\", "\\\\.\\UNC\\", "\\??\\UNC\\"])
def test_an_extended_unc_gh_config_dir_fences_the_plain_unc_candidate(env, monkeypatch, prefix):
    # The anchor is matched lexically, so its extended form also stands for the plain share.
    monkeypatch.setenv("GH_CONFIG_DIR", prefix + "fileserver\\team\\gh")
    assert is_unsafe_prompt_path(Path("\\\\fileserver\\team\\gh\\hosts.yml"))
    assert not is_unsafe_prompt_path(Path("\\\\fileserver\\team\\other\\p.md"))


@pytest.mark.parametrize("prefix", ["\\\\?\\UNC\\", "\\\\.\\UNC\\", "\\??\\UNC\\"])
def test_a_plain_unc_gh_config_dir_fences_the_extended_unc_candidate(env, monkeypatch, prefix):
    # An extended-form candidate is in the device namespace, which is refused outright.
    monkeypatch.setenv("GH_CONFIG_DIR", "\\\\fileserver\\team\\gh")
    assert is_unsafe_prompt_path(Path(prefix + "fileserver\\team\\gh\\hosts.yml"))
    assert is_unsafe_prompt_path(Path("\\\\fileserver\\team\\gh\\hosts.yml"))


def test_a_drive_relative_gh_config_dir_fences_that_drives_cwd(env, monkeypatch):
    # ``C:`` is the current directory of drive C on Windows, never ``C:\``.
    real_abspath = kas_prompt_paths.os.path.abspath
    monkeypatch.setattr(
        kas_prompt_paths.os.path,
        "abspath",
        lambda v: "C:\\work" if v == "C:" else real_abspath(v),
    )
    monkeypatch.setenv("GH_CONFIG_DIR", "C:")
    assert is_unsafe_prompt_path(Path("C:\\work\\hosts.yml"))
    assert not is_unsafe_prompt_path(Path("C:\\other\\p.md"))


def test_a_unc_appdata_is_never_resolved(env, monkeypatch):
    # A redirected %APPDATA% on a dead file server must not cost a network probe per check.
    agents, _roaming, _token = env
    monkeypatch.setenv("APPDATA", "\\\\slowhost\\share\\Roaming")
    seen: list[str] = []
    real_resolve = Path.resolve

    def spy(self, *a, **k):
        seen.append(self.as_posix())
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", spy)
    note = agents / "notes.md"
    for _ in range(5):
        assert not is_unsafe_prompt_path(note)
    assert not any("slowhost" in s.replace("\\", "/") for s in seen)


def test_an_anchor_that_cannot_resolve_still_refuses_by_its_lexical_spelling(env, monkeypatch):
    _agents, roaming, token = env
    monkeypatch.setenv("APPDATA", str(roaming))

    def boom(self, *a, **k):
        raise OSError("share down")

    monkeypatch.setattr(Path, "resolve", boom)
    assert is_unsafe_prompt_path(token)


def test_gh_config_dir_at_a_root_fences_the_token_file_not_the_drive(env, monkeypatch):
    monkeypatch.setenv("GH_CONFIG_DIR", "/")
    assert is_unsafe_prompt_path(Path("/hosts.yml"))
    assert is_unsafe_prompt_path(Path("/HOSTS.YML"))
    assert not is_unsafe_prompt_path(Path("/opt/prompts/p.md"))


@pytest.mark.parametrize(
    "path",
    ["\\\\127.0.0.1\\share\\x\\p.md", "\\\\2130706433\\share\\x\\p.md", "\\\\[::1]\\share\\p.md"],
)
def test_a_unc_path_to_this_machine_is_refused_on_any_share(env, path):
    assert is_unsafe_prompt_path(Path(path))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX root spellings")
def test_a_gh_config_dir_that_normalizes_to_the_root_fences_only_the_token_file(
    env, tmp_path, monkeypatch
):
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path) + "/.." * len(tmp_path.parts))
    assert is_unsafe_prompt_path(Path("/hosts.yml"))
    assert not is_unsafe_prompt_path(Path("/srv/prompts/p.md"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX root spellings")
def test_a_gh_config_dir_linked_to_the_root_fences_only_the_token_file(env, tmp_path, monkeypatch):
    link = tmp_path / "rootlink"
    link.symlink_to("/", target_is_directory=True)
    monkeypatch.setenv("GH_CONFIG_DIR", str(link))
    assert is_unsafe_prompt_path(Path("/hosts.yml"))
    assert not is_unsafe_prompt_path(Path("/srv/prompts/p.md"))


def test_appdata_at_a_drive_root_keeps_the_root(env, monkeypatch):
    monkeypatch.setenv("APPDATA", "C:\\")
    assert is_unsafe_prompt_path(Path("C:\\GitHub CLI\\hosts.yml"))
    assert not is_unsafe_prompt_path(Path("C:\\Other\\p.md"))


@pytest.mark.parametrize(
    ("var", "value", "refused", "loaded"),
    [
        ("GH_CONFIG_DIR", "ghrel", "/srv/a/ghrel/hosts.yml", "/srv/a/ghrelx/hosts.yml"),
        ("GH_CONFIG_DIR", "./cfg/ghrel/", "/x/cfg/ghrel/hosts.yml", "/x/ghrel/hosts.yml"),
        ("GH_CONFIG_DIR", "../ghrel", "/y/ghrel/hosts.yml", "/y/other/hosts.yml"),
        ("XDG_CONFIG_HOME", "cfg", "/z/cfg/gh/hosts.yml", "/z/cfg/p.md"),
        ("APPDATA", "Roam", "/w/Roam/GitHub CLI/hosts.yml", "/w/Roam/Other/p.md"),
        ("GH_CONFIG_DIR", ".", "/anywhere/hosts.yml", "/anywhere/p.md"),
    ],
)
def test_a_relative_env_value_fences_that_tail_wherever_gh_runs(
    env, monkeypatch, var, value, refused, loaded
):
    # gh joins a relative value to its OWN cwd, which the gateway cannot know.
    monkeypatch.setenv(var, value)
    assert is_unsafe_prompt_path(Path(refused))
    assert not is_unsafe_prompt_path(Path(loaded))


def test_a_relative_env_value_linked_under_the_gateway_cwd_is_fenced_at_its_target(
    env, tmp_path, monkeypatch
):
    store = tmp_path / "token-store"
    store.mkdir()
    (store / "hosts.yml").write_text(_FAKE, encoding="utf-8")
    (tmp_path / "cfg").mkdir()
    platform_compat.symlink_or_junction(str(store), str(tmp_path / "cfg" / "gh"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_CONFIG_DIR", "cfg/gh")
    assert is_unsafe_prompt_path((store / "hosts.yml").resolve())


def test_a_gh_config_dir_of_dot_fences_only_the_token_file_in_the_gateway_cwd(
    env, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_CONFIG_DIR", ".")
    assert is_unsafe_prompt_path((tmp_path / "hosts.yml").resolve())
    assert not is_unsafe_prompt_path((tmp_path / "prompts" / "p.md").resolve())


def test_an_env_value_is_kept_as_written_so_a_stray_space_fences_what_gh_reads(env, monkeypatch):
    # gh does not trim the value: "/x/cfg " is a folder whose name ends in a space.
    monkeypatch.setenv("GH_CONFIG_DIR", "/x/cfg ")
    assert is_unsafe_prompt_path(Path("/x/cfg /hosts.yml"))
    assert not is_unsafe_prompt_path(Path("/x/cfg/p.md"))


@pytest.mark.parametrize(
    ("var", "refused"),
    [("GH_CONFIG_DIR", "/a/  /hosts.yml"), ("XDG_CONFIG_HOME", "/a/  /gh/hosts.yml")],
)
def test_a_value_of_only_spaces_is_fenced_as_a_relative_tail(env, monkeypatch, var, refused):
    # Only "" is unset for gh; "  " is a relative folder joined to gh's own cwd.
    monkeypatch.setenv(var, "  ")
    assert is_unsafe_prompt_path(Path(refused))
    assert not is_unsafe_prompt_path(Path("/a/b/p.md"))


def test_a_short_name_alias_of_the_gh_folder_is_refused(env, monkeypatch):
    # An 8.3 name (``GITHUB~1``) is a second name for the same folder; the candidate
    # is resolved to the long name before the check. A link stands in for it here.
    agents, roaming, _token = env
    monkeypatch.setenv("APPDATA", str(roaming))
    # On Windows with 8.3 names on, ``GITHUB~1`` already is that second name.
    alias = roaming / "GITHUB~1"
    if not alias.exists():
        alias.symlink_to(roaming / "GitHub CLI", target_is_directory=True)
    _refused(f"file://{roaming}/GITHUB~1/hosts.yml", agents)


@pytest.mark.skipif(sys.platform != "win32", reason="real 8.3 short names are a Windows feature")
def test_a_real_windows_short_name_is_refused(env, monkeypatch):
    import ctypes

    agents, roaming, token = env
    monkeypatch.setenv("APPDATA", str(roaming))
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(token), buf, 1024):
        pytest.skip("GetShortPathNameW failed")
    short = buf.value
    if "~" not in short:
        pytest.skip("8.3 names are off on this volume")
    _refused(f"file://{short}", agents)
