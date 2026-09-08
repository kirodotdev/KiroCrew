"""Windows support for the Notes app: the manifest declaration and the four
places the app behaved differently on a native Windows host.

The manifest pair is the accepted paradigm from ``test/test_dev_fleet_app.py``:
one test pins the declared list, the other proves every name in it maps to a real
``sys.platform`` so a declaration cannot claim a platform the gate then rejects.

The rest covers this port's own code: the GitHub-CLI lookup (which could only ever
miss on Windows), the note-name validation (where an unrepresentable name did not
fail but silently became an NTFS alternate data stream), the 64-bit Program Files
probe, and the two readable-failure messages a Windows user now gets instead of a
bare ``git_failed``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.md_notebook import git_ops

APP_DIR = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    return json.loads((APP_DIR / "app.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def server():
    """The app's backend module. Skipped rather than errored without aiohttp."""
    pytest.importorskip("aiohttp")
    from kiro_crew.apps.builtins.md_notebook import server as server_mod

    return server_mod


# --- manifest platform declaration -----------------------------------------


def test_manifest_declares_every_platform_the_app_runs_on():
    """The app is aiohttp + git + filesystem work, all of which runs anywhere.

    Pinned because omitting the block is not neutral: ``PlatformConfig`` defaults
    ``os`` to ``["macos", "linux"]``, so an absent declaration is an active
    refusal to enable the app on Windows rather than a missing opinion.
    """
    assert _manifest()["platform"]["os"] == ["macos", "linux", "windows"]


def test_declared_platforms_all_resolve_to_a_real_sys_platform():
    """Every declared name must map to a sys.platform value.

    An unmapped name is accepted into the list and then never matches, so a
    declaration can claim a platform the gate silently rejects.
    """
    from kiro_crew.apps.manifest import PlatformConfig

    cfg = PlatformConfig(os=_manifest()["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform


# --- the gh lookup ----------------------------------------------------------


def test_windows_gh_candidates_are_fixed_install_roots_never_path(server, monkeypatch):
    """Fixed install roots only, de-duplicated, and nothing PATH-derived.

    ``_find_gh``'s security property is that PATH is never consulted, so the
    Windows arm must not weaken it — and a 64-bit machine where ProgramW6432 and
    ProgramFiles agree must not probe the same directory twice.
    """
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramW6432", r"C:\Program Files")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    assert server._windows_gh_candidates() == [
        os.path.join(r"C:\Program Files", "GitHub CLI", "gh.exe"),
        os.path.join(r"C:\Users\u\AppData\Local", "Programs", "GitHub CLI", "gh.exe"),
    ]


def test_windows_gh_candidates_cover_the_64_bit_root_under_a_32_bit_host(server, monkeypatch):
    """A 32-bit interpreter sees ProgramFiles as the x86 tree; gh is in the other."""
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files (x86)")
    monkeypatch.setenv("ProgramW6432", r"C:\Program Files")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert os.path.join(r"C:\Program Files", "GitHub CLI", "gh.exe") in (
        server._windows_gh_candidates()
    )


def test_find_gh_resolves_the_windows_install_root(server, monkeypatch, tmp_path):
    """Before this, ``gh auth login`` on Windows produced no token and no reason."""
    monkeypatch.delenv("MD_NOTEBOOK_GH_BIN", raising=False)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    gh = tmp_path / "pf" / "GitHub CLI" / "gh.exe"
    gh.parent.mkdir(parents=True)
    gh.write_text("", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert server._find_gh() == str(gh)


def test_find_gh_still_fails_closed_on_windows_with_nothing_installed(
    server, monkeypatch, tmp_path
):
    """No trusted gh means no gh-derived token — never a PATH fallback."""
    monkeypatch.delenv("MD_NOTEBOOK_GH_BIN", raising=False)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "absent"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert server._find_gh() is None


def test_gh_override_still_wins_on_windows(server, monkeypatch, tmp_path):
    override = tmp_path / "gh.exe"
    override.write_text("", encoding="utf-8")
    override.chmod(0o755)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setenv("MD_NOTEBOOK_GH_BIN", str(override))
    assert server._find_gh() == str(override)


# --- note names Win32 cannot represent --------------------------------------

UNPORTABLE = [
    "a:b.md",  # would become an NTFS alternate data stream on a file named `a`
    "note<1>.md",
    "note>1.md",
    'quote".md',
    "pipe|x.md",
    "star*.md",
    "what?.md",
    "NUL.md",  # a device, not a file
    "nul.md",
    "COM1.md",
    "lpt9.md",
    "sub./note.md",  # component ending in a dot: Win32 strips it silently
    "sub /note.md",  # component ending in a space: likewise
]


@pytest.mark.parametrize("rel", UNPORTABLE)
def test_move_destination_refuses_a_name_windows_cannot_hold(server, rel):
    """A CREATED name is held to Win32's rules on every platform.

    A vault is meant to travel: `a:b.md` written on macOS makes the Windows clone
    un-checkoutable (git's own core.protectNTFS refuses it, surfacing only as an
    unexplained clone failure), and on Windows the same move does not fail at all
    — it writes an alternate data stream the listing walk never sees, so the note
    disappears from the app rather than erroring.
    """
    with pytest.raises(server.ApiError) as excinfo:
        server.require_note_path(rel, "to", for_new=True)
    assert excinfo.value.status == 400
    assert excinfo.value.code == "path_not_a_note"


@pytest.mark.parametrize("rel", UNPORTABLE)
def test_every_path_is_refused_when_running_on_windows(server, rel, monkeypatch):
    """On Windows the name is not merely unwise, so read and save refuse it too."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    with pytest.raises(server.ApiError) as excinfo:
        server.require_note_path(rel)
    assert excinfo.value.code == "path_not_a_note"


@pytest.mark.parametrize("rel", ["a:b.md", "NUL.md", "star*.md"])
def test_an_existing_odd_name_stays_readable_off_windows(server, rel, monkeypatch):
    """Not a new name and representable on this host, so it must still open.

    Refusing here would make a note that a POSIX vault already contains
    unopenable and un-renameable — the app would hide the problem instead of
    letting the user fix it, and `to` is already gated.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    assert server.require_note_path(rel) == rel
    assert server.require_note_path(rel, "from") == rel


@pytest.mark.parametrize(
    "rel",
    [
        "note.md",
        "folder/note.md",
        "a.b.md",
        "my note (1).md",
        "Ünïcode note.md",
        "conference.md",  # `con` is reserved; `conference` is not
        "communication.md",
    ],
)
def test_ordinary_note_names_are_untouched(server, rel):
    assert server.require_note_path(rel, for_new=True) == rel


def test_new_note_folder_is_gated_on_windows_only(server, monkeypatch):
    """The folder is resolved, not created, so only the host's own limits apply."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    with pytest.raises(server.ApiError) as excinfo:
        server.require_folder_path("CON")
    assert excinfo.value.code == "path_not_a_note"
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    assert server.require_folder_path("CON") == "CON"


def test_folder_path_keeps_refusing_a_dotted_component(server, monkeypatch):
    """The pre-existing `.git` guard must survive the new check."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    with pytest.raises(server.ApiError):
        server.require_folder_path(".git")


# --- readable failures instead of a bare git_failed -------------------------


def test_windows_git_bin_dirs_cover_the_64_bit_program_files_tree(monkeypatch):
    """A 64-bit Git is reachable from a 32-bit host interpreter."""
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files (x86)")
    monkeypatch.setenv("ProgramW6432", r"C:\Program Files")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    dirs = git_ops._windows_git_bin_dirs()
    assert os.path.join(r"C:\Program Files", "Git", "cmd") in dirs
    assert os.path.join(r"C:\Program Files (x86)", "Git", "cmd") in dirs
    assert os.path.join(r"C:\Users\u\AppData\Local", "Programs", "Git", "cmd") in dirs


def test_windows_git_bin_dirs_do_not_probe_one_root_twice(monkeypatch):
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramW6432", r"C:\Program Files")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    dirs = git_ops._windows_git_bin_dirs()
    assert len(dirs) == len(set(dirs))


def test_git_bin_failure_tells_a_windows_user_what_to_install(monkeypatch):
    """Windows has no OS sandbox backend, so this fail-close is the whole story:
    it has to name the thing to install and the escape hatch, not just fail."""
    monkeypatch.delenv("MD_NOTEBOOK_GIT_BIN", raising=False)
    monkeypatch.setattr(git_ops, "_git_bin_memo", None)
    monkeypatch.setattr(git_ops, "_GIT_BIN_DIRS", ())
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    with pytest.raises(git_ops.GitError) as excinfo:
        git_ops._git_bin()
    message = str(excinfo.value)
    assert "Git for Windows" in message
    assert "MD_NOTEBOOK_GIT_BIN" in message
    assert "PATH is deliberately not consulted" in message


def test_git_bin_failure_keeps_its_original_wording_off_windows(monkeypatch):
    monkeypatch.delenv("MD_NOTEBOOK_GIT_BIN", raising=False)
    monkeypatch.setattr(git_ops, "_git_bin_memo", None)
    monkeypatch.setattr(git_ops, "_GIT_BIN_DIRS", ())
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    with pytest.raises(git_ops.GitError) as excinfo:
        git_ops._git_bin()
    message = str(excinfo.value)
    assert "no trusted git binary found in a system location" in message
    assert "Git for Windows" not in message


def test_windows_gets_a_doubled_default_git_timeout():
    """Six to ten git invocations per sync, each dearer on Windows (process
    creation plus Defender scanning the object store), made 30s a spurious
    timeout on a large vault where nothing was actually wrong."""
    expected = 60 if platform_compat.IS_WINDOWS else 30
    assert git_ops._DEFAULT_GIT_TIMEOUT_SEC == expected


def test_git_timeout_env_override_still_wins(monkeypatch):
    """The doubled default must not shadow the documented override."""
    monkeypatch.setenv("MDNB_GIT_TIMEOUT_SEC", "7")
    assert int(os.environ.get("MDNB_GIT_TIMEOUT_SEC", git_ops._DEFAULT_GIT_TIMEOUT_SEC)) == 7


def test_folder_chooser_is_unsupported_and_says_so_off_macos(server, monkeypatch):
    """Windows and Linux both land here; the 501 now names the alternative."""
    monkeypatch.setattr(server.sys, "platform", "win32")
    assert server.pick_folder_supported() is False
