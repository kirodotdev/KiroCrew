"""The user's hook registrations (``~/.kiro/hooks``) are read-only to every agent.

A file there is a hook kiro-cli loads and Crew fires on its permission path, so an
agent that could delete or edit one would switch the person's gate off from its next
turn. The file tools are refused on the directory (``security.paths_lists``) and both
launchers seal it read-only beside the kiro agents tree. Reads stay open.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import hypothesis
import pytest
from hypothesis import strategies as st

import kiro_crew.agent as agent_mod
from kiro_crew import sandbox, sandbox_plan, sandbox_seatbelt
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.sandbox_plan import (
    BACKEND_NAMESPACE,
    BACKEND_SEATBELT,
    SandboxRequest,
    plan_confinement,
)
from kiro_crew.security import is_sensitive_path, is_sensitive_write_path
from kiro_crew.security import paths as security_paths

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

#: The real resolver, captured before the ``home`` fixture replaces it.
_REAL_RESOLVED_KIRO_HOOKS_DIR = agent_mod.resolved_kiro_hooks_dir


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A throwaway ``$HOME`` holding a hooks directory, with no kiro-cli home override."""
    home = (tmp_path / "home").resolve()
    (home / ".kiro" / "hooks").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    # Crew's configured hooks directory is the default unless a test overrides it.
    monkeypatch.setattr(agent_mod, "_DEFAULT_KIRO_HOOKS_DIR", home / ".kiro" / "hooks")
    monkeypatch.setattr(agent_mod, "resolved_kiro_hooks_dir", lambda: home / ".kiro" / "hooks")
    return home


@_POSIX_ONLY
def test_the_default_and_kiro_home_spellings_are_targets(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    targets = sandbox._resolved_kiro_hooks_targets()
    assert str(home / ".kiro" / "hooks") in targets
    assert str(tmp_path / "kiro" / "hooks") in targets
    assert len(targets) == len(set(targets))


@_POSIX_ONLY
def test_the_configured_hooks_dir_is_a_target(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = home / "my-hooks"
    _set_configured_hooks_dir(monkeypatch, str(configured))
    assert sandbox._resolved_kiro_hooks_targets() == [
        str(home / ".kiro" / "hooks"),
        str(configured),
    ]


@_POSIX_ONLY
def test_a_symlinked_hooks_dir_seals_the_link_and_its_target(home: Path, tmp_path: Path) -> None:
    hooks = home / ".kiro" / "hooks"
    hooks.rmdir()
    real = (tmp_path / "dotfiles" / "hooks").resolve()
    real.mkdir(parents=True)
    hooks.symlink_to(real, target_is_directory=True)
    targets = sandbox._resolved_kiro_hooks_targets()
    assert str(hooks) in targets
    assert str(real) in targets


@_POSIX_ONLY
def test_the_resolver_never_raises(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Path:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(agent_mod, "resolved_kiro_hooks_dir", boom)
    assert sandbox._resolved_kiro_hooks_targets() == [str(home / ".kiro" / "hooks")]


@pytest.mark.parametrize(
    ("path", "links", "reached"),
    [
        (r"C:\Users\me\dotfiles\hooks", {}, r"C:\Users\me\dotfiles\hooks"),
        ("C:/Users/me/dotfiles/hooks", {}, r"C:\Users\me\dotfiles\hooks"),
        (r"\\server\share\hooks", {}, r"\\server\share\hooks"),
        (r"C:\Users\me\a\..\hooks", {}, r"C:\Users\me\hooks"),
        (r"C:\Users\me\link\x", {r"C:\Users\me\link": r"\\?\D:\real"}, r"D:\real\x"),
        (r"C:\Users\me\link\x", {r"C:\Users\me\link": r"\\?\UNC\srv\sh"}, r"\\srv\sh\x"),
        (r"C:\Users\me\link\x", {r"C:\Users\me\link": r"rel\d"}, r"C:\Users\me\rel\d\x"),
    ],
)
def test_a_windows_spelling_keeps_its_drive(
    monkeypatch: pytest.MonkeyPatch, path: str, links: dict[str, str], reached: str
) -> None:
    import errno
    import ntpath
    import types

    from kiro_crew import kiro_hooks_dir

    def readlink(name: str) -> str:
        if name in links:
            return links[name]
        raise OSError(errno.EINVAL, "not a link", name)

    windows_os = types.SimpleNamespace(
        sep="\\",
        altsep="/",
        path=types.SimpleNamespace(
            isabs=ntpath.isabs,
            join=ntpath.join,
            dirname=ntpath.dirname,
            splitroot=ntpath.splitroot,
            lexists=lambda name: True,
        ),
        readlink=readlink,
        getcwd=lambda: "C:\\",
    )
    monkeypatch.setattr(kiro_hooks_dir, "os", windows_os)
    assert kiro_hooks_dir.walk(path).resolved == reached


@_POSIX_ONLY
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize("tier", ["strict", "cc", "standard"])
def test_every_tier_of_both_launchers_seals_it(home: Path, backend: str, tier: str) -> None:
    request = SandboxRequest(tier=tier, backend=backend)
    host = sandbox._live_plan_host(request)
    hooks = str(home / ".kiro" / "hooks")
    assert hooks in host.kiro_agents_targets
    assert hooks in plan_confinement(request, host).readonly


@_POSIX_ONLY
def test_the_namespace_launcher_receives_it_as_a_readonly_dir(home: Path) -> None:
    request = SandboxRequest(tier="standard", backend=BACKEND_NAMESPACE)
    plan = plan_confinement(request, sandbox._live_plan_host(request))
    assert str(home / ".kiro" / "hooks") in sandbox_plan.namespace_payload(plan)["readonly_dirs"]


@_POSIX_ONLY
def test_the_seatbelt_profile_denies_writing_below_it(home: Path) -> None:
    request = SandboxRequest(tier="standard", backend=BACKEND_SEATBELT)
    plan = plan_confinement(request, sandbox._live_plan_host(request))
    profile = sandbox_seatbelt.render_seatbelt_profile(plan)
    assert f'(deny file-write* (subpath "{home / ".kiro" / "hooks"}"))' in profile


@_POSIX_ONLY
def test_a_visibility_lift_does_not_unseal_it(home: Path) -> None:
    hooks = str(home / ".kiro" / "hooks")
    request = SandboxRequest(tier="standard", extra_visible_dirs=(hooks,))
    assert hooks in plan_confinement(request, sandbox._live_plan_host(request)).readonly


@_POSIX_ONLY
def test_an_absent_hooks_dir_is_created_so_the_linux_seal_has_a_dir(home: Path) -> None:
    hooks = home / ".kiro" / "hooks"
    hooks.rmdir()
    assert sandbox._materialize_kiro_hooks_dirs() == [str(hooks)]
    assert stat.S_IMODE(hooks.stat().st_mode) == 0o700
    assert list(hooks.iterdir()) == []


@_POSIX_ONLY
def test_an_absent_kiro_home_gets_its_hooks_dir_so_none_can_be_planted(home: Path) -> None:
    (home / ".kiro" / "hooks").rmdir()
    (home / ".kiro").rmdir()
    assert sandbox._materialize_kiro_hooks_dirs() == [
        str(home / ".kiro"),
        str(home / ".kiro" / "hooks"),
    ]
    assert stat.S_IMODE((home / ".kiro" / "hooks").stat().st_mode) == 0o700


@_POSIX_ONLY
def test_an_existing_dir_and_a_link_are_left_alone(home: Path, tmp_path: Path) -> None:
    hooks = home / ".kiro" / "hooks"
    (hooks / "guard.json").write_text("{}", encoding="utf-8")
    assert sandbox._materialize_kiro_hooks_dirs() == []
    assert (hooks / "guard.json").read_text(encoding="utf-8") == "{}"

    for child in hooks.iterdir():
        child.unlink()
    hooks.rmdir()
    elsewhere = tmp_path / "missing-parent" / "hooks"
    hooks.symlink_to(elsewhere, target_is_directory=True)
    assert sandbox._materialize_kiro_hooks_dirs() == [
        os.path.realpath(elsewhere.parent),
        os.path.realpath(elsewhere),
    ]
    assert os.readlink(hooks) == str(elsewhere)
    assert elsewhere.is_dir()


@_POSIX_ONLY
def test_a_configured_hooks_dir_is_created_with_its_missing_parents(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = home / "dotfiles" / "kiro" / "hooks"
    _set_configured_hooks_dir(monkeypatch, str(configured))
    assert sandbox._materialize_kiro_hooks_dirs() == [
        str(home / "dotfiles"),
        str(home / "dotfiles" / "kiro"),
        str(configured),
    ]
    assert stat.S_IMODE(configured.stat().st_mode) == 0o700


@_POSIX_ONLY
def test_a_dangling_link_gets_its_target_created_so_both_ends_are_sealed(
    home: Path, tmp_path: Path
) -> None:
    hooks = home / ".kiro" / "hooks"
    hooks.rmdir()
    target = (tmp_path / "dotfiles").resolve() / "hooks"
    target.parent.mkdir()
    hooks.symlink_to(target, target_is_directory=True)
    assert sandbox._materialize_kiro_hooks_dirs() == [str(target)]
    assert os.readlink(hooks) == str(target)
    assert target.is_dir()


def _edit(path: str, kind: str = "edit"):
    return HookManager(HooksConfig.from_dict({})).on_tool_call(
        "Editing", session_key="cli_chat", tool_kind=kind, raw_params={"path": path}
    )


def test_file_edit_writes_into_the_hooks_dir_are_refused(home: Path) -> None:
    hooks = home / ".kiro" / "hooks"
    for target in (hooks / "guard.json", hooks / "guard.sh", hooks / "sub" / "x.json", hooks):
        assert is_sensitive_write_path(str(target)) is True, target
    assert is_sensitive_write_path("~/.kiro/hooks/guard.json") is True
    decision = _edit(str(hooks / "guard.json"))
    assert decision.action == TOOL_DENY


def test_the_file_tool_fence_follows_kiro_home(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    moved = tmp_path / "elsewhere"
    monkeypatch.setenv("KIRO_HOME", str(moved))
    assert is_sensitive_write_path(str(moved / "hooks" / "guard.json")) is True


def _set_configured_hooks_dir(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    """Write ``agent.kiro_hooks_dir`` and let Crew's own resolver read it."""
    from kiro_crew.config import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": {"kiro_hooks_dir": value}}), encoding="utf-8")
    monkeypatch.setattr(agent_mod, "resolved_kiro_hooks_dir", _REAL_RESOLVED_KIRO_HOOKS_DIR)


def test_file_edit_writes_into_a_configured_hooks_dir_are_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = home / "dotfiles" / "kiro" / "hooks"
    _set_configured_hooks_dir(monkeypatch, str(configured))
    for target in (configured / "guard.json", configured / "guard.sh", configured):
        assert is_sensitive_write_path(str(target)) is True, target
    assert _edit(str(configured / "guard.json")).action == TOOL_DENY
    assert is_sensitive_path(str(configured / "guard.json")) is False
    assert is_sensitive_write_path(str(home / "dotfiles" / "kiro" / "notes.txt")) is False


def test_a_configured_hooks_dir_in_a_bom_saved_config_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config import config_path

    configured = home / "dotfiles" / "kiro" / "hooks"
    _set_configured_hooks_dir(monkeypatch, str(configured))
    path = config_path()
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert security_paths._configured_kiro_hooks_dir_rows() == ["dotfiles/kiro/hooks"]
    assert is_sensitive_write_path(str(configured / "guard.json")) is True


@pytest.mark.parametrize("value", ["", None, 7, "/elsewhere/hooks", "~"])
def test_a_configured_hooks_dir_the_resolver_rejects_fences_nothing_more(
    home: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    _set_configured_hooks_dir(monkeypatch, value)
    assert is_sensitive_write_path(str(home / "dotfiles" / "x.json")) is False
    assert is_sensitive_write_path(str(home / ".kiro" / "hooks" / "guard.json")) is True
    # Its walk is guarded against rename, but nothing it names is sealed read-only.
    assert sandbox._resolved_kiro_hooks_targets() == [str(home / ".kiro" / "hooks")]


def test_the_hooks_rows_widen_no_parent_mask(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers of ``write_protected_home_paths`` mask each row's parent directory.

    A configured ``~/hooks`` must not reach that view, or its parent, the whole home,
    would be masked; and the static ``.kiro/hooks`` row's parent is already masked through
    ``.kiro/agents``, so the hooks rows add no mask at all.
    """
    from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
        write_protected_targets,
    )
    from kiro_crew.security import write_protected_home_paths

    (home / "hooks").mkdir()
    _set_configured_hooks_dir(monkeypatch, str(home / "hooks"))
    assert is_sensitive_write_path(str(home / "hooks" / "guard.json")) is True
    rows = write_protected_home_paths()
    assert "hooks" not in rows
    assert str(home) not in write_protected_targets()
    parents = {(home / row).parent for row in rows}
    assert parents == {(home / row).parent for row in rows if row != ".kiro/hooks"}


def test_reads_stay_allowed(home: Path) -> None:
    target = str(home / ".kiro" / "hooks" / "guard.json")
    assert is_sensitive_path(target) is False
    assert _edit(target, kind="read").action != TOOL_DENY


def test_neighbours_are_not_over_blocked(home: Path) -> None:
    assert is_sensitive_write_path("~/.kiro/hooks-backup/x.json") is False
    assert is_sensitive_write_path("~/.kiro/notes.json") is False


# --------------------------------------------------------------------------- #
# Every state of the hooks directory, one case per row of the PR's state table.
# --------------------------------------------------------------------------- #

#: (row id, which directory Crew reads, how it is laid out). ``dot`` is a sibling
#: tree under ``$HOME`` that links point into.
_STATES = [
    ("D1", "default", "real dir"),
    ("D2", "default", "absent, parent exists"),
    ("D3", "default", "absent, parent absent"),
    ("D4", "default", "link to existing dir"),
    ("D5", "default", "link to missing dir, its parent exists"),
    ("D6", "default", "link to missing dir, its parent absent"),
    ("D7", "default", "parent is a link to an existing dir"),
    ("D8", "default", "parent is a link to a missing dir"),
    ("C1", "configured", "real dir"),
    ("C2", "configured", "absent, parent exists"),
    ("C3", "configured", "absent, parent absent"),
    ("C4", "configured", "link to existing dir"),
    ("C5", "configured", "link to missing dir, its parent exists"),
    ("C6", "configured", "link to missing dir, its parent absent"),
    ("C7", "configured", "parent is a link to an existing dir"),
    ("C8", "configured", "parent is a link to a missing dir"),
    ("B1", "configured-bom", "real dir"),
    ("B2", "configured-bom", "link to missing dir, its parent absent"),
    ("K1", "kiro-home", "real dir"),
    ("K2", "kiro-home", "absent, parent absent"),
    ("K3", "kiro-home", "link to missing dir, its parent absent"),
    ("T1", "traversal", "the setting is a link to the default dir"),
    ("T2", "traversal", "a link, then .., then the dir"),
    ("T3", "traversal", "a real dir, then .., then the dir"),
    ("T4", "traversal", "two links on the path"),
    ("T5", "traversal", "a link, then .., reaching the default dir"),
    ("T6", "traversal", "a link inside a link's target"),
    ("T7", "traversal", "a missing name, then .., then the dir"),
    ("T8", "traversal", "a relative value"),
    ("T9", "traversal", "a relative value through a link"),
    ("K4", "kiro-home-traversal", "a link, then .., in KIRO_HOME"),
    ("K5", "kiro-home-traversal", "a missing name, then .., in KIRO_HOME"),
]


def _lay_out_traversal(home: Path, shape: str) -> str:
    """Build the layout for a T row and return the setting as the person wrote it."""
    if shape == "the setting is a link to the default dir":
        (home / ".kiro" / "hooks").mkdir(parents=True)
        (home / "link").symlink_to(home / ".kiro" / "hooks", target_is_directory=True)
        return "~/link"
    if shape == "a link, then .., then the dir":
        (home / "real" / "sub").mkdir(parents=True)
        (home / "real" / "hooks").mkdir()
        (home / "a").symlink_to(home / "real" / "sub", target_is_directory=True)
        return "~/a/../hooks"
    if shape == "a real dir, then .., then the dir":
        (home / "a").mkdir()
        (home / "hooks").mkdir()
        return "~/a/../hooks"
    if shape == "two links on the path":
        (home / "y" / "hooks").mkdir(parents=True)
        (home / "x").mkdir()
        (home / "x" / "l2").symlink_to(home / "y", target_is_directory=True)
        (home / "l1").symlink_to(home / "x", target_is_directory=True)
        return "~/l1/l2/hooks"
    if shape == "a link, then .., reaching the default dir":
        (home / ".kiro" / "hooks").mkdir(parents=True)
        (home / "real").mkdir()
        (home / "a").symlink_to(home / "real", target_is_directory=True)
        return "~/a/../.kiro/hooks"
    if shape == "a link inside a link's target":
        (home / "t" / "inner-target" / "hooks").mkdir(parents=True)
        (home / "t" / "inner").symlink_to("inner-target", target_is_directory=True)
        (home / "outer").symlink_to(home / "t", target_is_directory=True)
        return "~/outer/inner/hooks"
    if shape == "a missing name, then .., then the dir":
        (home / "hooks").mkdir()
        return "~/a/../hooks"
    if shape == "a relative value":
        (home / "rel" / "hooks").mkdir(parents=True)
        return "rel/hooks"
    if shape == "a relative value through a link":
        (home / "real" / "hooks").mkdir(parents=True)
        (home / "rl").symlink_to(home / "real", target_is_directory=True)
        return "rl/hooks"
    if shape == "a link, then .., in KIRO_HOME":
        (home / "real" / "sub").mkdir(parents=True)
        (home / "real" / "kh" / "hooks").mkdir(parents=True)
        (home / "a").symlink_to(home / "real" / "sub", target_is_directory=True)
        return f"{home}/a/../kh"
    if shape == "a missing name, then .., in KIRO_HOME":
        (home / "kh" / "hooks").mkdir(parents=True)
        return f"{home}/a/../kh"
    raise AssertionError(shape)  # pragma: no cover - a typo in _STATES


def _walk_entries(path: str) -> tuple[list[str], list[str]]:
    """The links and the ``..``-climbed directories the kernel meets walking *path*.

    An independent walk for the test: one name at a time, a link's value spliced in
    before the names after it, a ``..`` applied to the directory reached so far.
    """
    links: list[str] = []
    climbed: list[str] = []
    resolved = os.sep
    pending = path.split(os.sep)
    while pending and len(links) <= 40:  # a loop stops where the kernel gives ELOOP
        name = pending.pop(0)
        if name in ("", "."):
            continue
        if name == "..":
            climbed.append(resolved)
            resolved = os.path.dirname(resolved)
            continue
        resolved = os.path.join(resolved, name)
        if os.path.islink(resolved):
            links.append(resolved)
            value = os.readlink(resolved)
            pending[:0] = value.split(os.sep)
            resolved = os.sep if os.path.isabs(value) else os.path.dirname(resolved)
    return links, climbed


def _lay_out(base: Path, leaf: str, shape: str, dot: Path) -> None:
    """Build ``base/leaf`` in *shape*, linking into *dot* where the shape needs a link."""
    reader = base / leaf
    if shape == "real dir":
        reader.mkdir(parents=True)
    elif shape == "absent, parent exists":
        base.mkdir(parents=True, exist_ok=True)
    elif shape == "absent, parent absent":
        pass
    elif shape.startswith("link to"):
        base.mkdir(parents=True, exist_ok=True)
        target = dot / "hooks"
        if shape == "link to existing dir":
            target.mkdir(parents=True)
        elif shape == "link to missing dir, its parent exists":
            dot.mkdir(parents=True)
        reader.symlink_to(target, target_is_directory=True)
    elif shape.startswith("parent is a link"):
        base.parent.mkdir(parents=True, exist_ok=True)
        if shape == "parent is a link to an existing dir":
            (dot / leaf).mkdir(parents=True)
        base.symlink_to(dot, target_is_directory=True)
    else:  # pragma: no cover - a typo in _STATES
        raise AssertionError(shape)


def _links_on(path: Path, home: Path) -> list[str]:
    """Every symlink between *home* and *path*, *path* included."""
    links = []
    current = home
    for part in path.relative_to(home).parts:
        current = current / part
        if current.is_symlink():
            links.append(str(current))
    return links


@_POSIX_ONLY
@pytest.mark.parametrize(("row", "which", "shape"), _STATES, ids=[s[0] for s in _STATES])
def test_every_state_of_the_hooks_dir_is_sealed_and_fenced(
    home: Path, monkeypatch: pytest.MonkeyPatch, row: str, which: str, shape: str
) -> None:
    (home / ".kiro" / "hooks").rmdir()
    (home / ".kiro").rmdir()
    dot = home / "dot"
    if which == "default":
        reader = home / ".kiro" / "hooks"
        _lay_out(home / ".kiro", "hooks", shape, dot)
    elif which == "kiro-home":
        reader = home / "kh" / "hooks"
        _lay_out(home / "kh", "hooks", shape, dot)
        monkeypatch.setenv("KIRO_HOME", str(home / "kh"))
    elif which == "traversal":
        setting = _lay_out_traversal(home, shape)
        _set_configured_hooks_dir(monkeypatch, setting)
        if not setting.startswith("~"):
            monkeypatch.chdir(home)  # a relative value resolves against Crew's cwd
        reader = Path(os.path.join(home, os.path.expanduser(setting)))
    elif which == "kiro-home-traversal":
        override = _lay_out_traversal(home, shape)
        monkeypatch.setenv("KIRO_HOME", override)
        reader = Path(override, "hooks")
    else:
        reader = home / "my" / "hooks"
        _lay_out(home / "my", "hooks", shape, dot)
        _set_configured_hooks_dir(monkeypatch, "~/my/hooks")
        if which == "configured-bom":
            from kiro_crew.config import config_path

            path = config_path()
            path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    # A hook the person already registered survives the materialiser untouched.
    hook = reader / "guard.json"
    if reader.is_dir():
        hook.write_text('{"hooks": []}', encoding="utf-8")

    sandbox._materialize_kiro_hooks_dirs()

    if hook.exists():
        assert hook.read_text(encoding="utf-8") == '{"hooks": []}', row

    # The directory Crew and kiro-cli read exists, so the Linux seal has it to bind.
    canonical = os.path.realpath(reader)
    assert os.path.isdir(canonical), row
    if which in ("traversal", "kiro-home-traversal"):
        if which == "traversal":
            # Crew reads the directory reached walking the setting as written.
            assert os.path.realpath(agent_mod.resolved_kiro_hooks_dir()) == canonical, row
        links, climbed = _walk_entries(str(reader))
        # The row builds the traversal it names (a plain relative value has none).
        assert links or climbed or shape == "a relative value", row
    else:
        links, climbed = _links_on(reader, home), []

    ns_request = SandboxRequest(tier="standard", backend=BACKEND_NAMESPACE)
    payload = sandbox_plan.namespace_payload(
        plan_confinement(ns_request, sandbox._live_plan_host(ns_request))
    )
    assert canonical in payload["readonly_dirs"], row
    # A link on the way is pinned, so it cannot be removed or pointed elsewhere.
    for entry in (*links, *climbed):
        assert entry in payload["parent_pins"], (row, entry)
        # The Linux launcher pins only a name that exists; a missing one could be
        # created later as a link.
        assert os.path.lexists(entry), (row, entry)

    sb_request = SandboxRequest(tier="standard", backend=BACKEND_SEATBELT)
    profile = sandbox_seatbelt.render_seatbelt_profile(
        plan_confinement(sb_request, sandbox._live_plan_host(sb_request))
    )
    assert f'(deny file-write* (subpath "{canonical}"))' in profile, row
    for entry in (*links, *climbed):
        assert f'(deny file-write* (literal "{entry}"))' in profile, (row, entry)

    # The file-edit gate refuses a write through every spelling.
    for spelling in (reader, Path(canonical)):
        assert is_sensitive_write_path(str(spelling / "guard.json")) is True, (row, spelling)
    assert _edit(str(reader / "guard.json")).action == TOOL_DENY, row
    assert is_sensitive_path(str(reader / "guard.json")) is False, row


#: (row id, setting or ``KIRO_HOME`` value, the name an agent would replace). Each
#: spelling is one a reader rejects, so Crew reads the default; replacing the name
#: with a link into ``$HOME`` would make the reader accept it and read the agent's
#: directory instead.
_REJECTED = [
    ("J1", "~/a/..", "a"),
    ("J2", "~/x/hooks", "x"),
    ("J3", "~/loop/hooks", "loop"),
    ("J4", "~/s/hooks", "s"),
    ("J5", "~/m/..", "m"),
    ("J6", "KIRO_HOME=~/k", "k"),
    ("J7", "~/.ssh", ".ssh"),
]


@_POSIX_ONLY
@pytest.mark.parametrize(("row", "value", "name"), _REJECTED, ids=[r[0] for r in _REJECTED])
def test_a_rejected_spelling_cannot_be_turned_into_an_accepted_one(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, row: str, value: str, name: str
) -> None:
    outside = tmp_path / "outside"
    (outside / "hooks").mkdir(parents=True)
    if row == "J1":
        (home / "a").mkdir()
    elif row == "J2":
        (home / "x").symlink_to(outside, target_is_directory=True)
    elif row == "J3":
        (home / "loop").symlink_to(home / "loop")
    elif row == "J4":
        (home / ".ssh").mkdir()
        (home / "s").symlink_to(home / ".ssh", target_is_directory=True)
    elif row == "J6":
        (home / "k").symlink_to("/usr", target_is_directory=True)
    elif row == "J7":
        (home / ".ssh").mkdir()
    if value.startswith("KIRO_HOME="):
        monkeypatch.setenv("KIRO_HOME", value.removeprefix("KIRO_HOME=").replace("~", str(home)))
        from kiro_crew.config.paths import kiro_home

        assert kiro_home() == home / ".kiro", row  # Crew rejects the override
    else:
        _set_configured_hooks_dir(monkeypatch, value)
        assert agent_mod.resolved_kiro_hooks_dir() == home / ".kiro" / "hooks", row

    sandbox._materialize_kiro_hooks_dirs()

    attacked = str(home / name)
    assert os.path.lexists(attacked), row  # created where missing, so it can be pinned
    ns_request = SandboxRequest(tier="standard", backend=BACKEND_NAMESPACE)
    payload = sandbox_plan.namespace_payload(
        plan_confinement(ns_request, sandbox._live_plan_host(ns_request))
    )
    assert attacked in payload["parent_pins"], row
    readonly = [os.path.realpath(p) for p in payload["readonly_dirs"] if os.path.exists(p)]
    assert _linux_blocks(attacked, readonly, set(payload["parent_pins"])), row
    # A rejected spelling is guarded, never sealed: $HOME stays writable.
    assert str(home) not in readonly, row
    sb_request = SandboxRequest(tier="standard", backend=BACKEND_SEATBELT)
    profile = sandbox_seatbelt.render_seatbelt_profile(
        plan_confinement(sb_request, sandbox._live_plan_host(sb_request))
    )
    assert f'(deny file-write* (literal "{attacked}"))' in profile, row
    assert f'(deny file-write* (subpath "{home}"))' not in profile, row


# --------------------------------------------------------------------------- #
# Property: no write the sandbox still allows changes what Crew or kiro-cli read.
# --------------------------------------------------------------------------- #

_NAMES = ("a", "b", "c")
#: How each top-level name under the throwaway $HOME is laid out. The last three make
#: a reader reject the spelling: a link out of $HOME, a loop, a sensitive directory.
_KINDS = (
    "absent",
    "dir",
    "link-to-dir",
    "link-to-missing",
    "link-outside",
    "loop",
    "link-sensitive",
)
_TOKENS = ("a", "b", "c", "x", "..", "hooks")
#: Subdirectories of every attacker target, so a replaced name followed by the
#: setting's later names, or by a ``..``, still reaches a directory.
_ATTACK_SUBDIRS = ("hooks", "x/hooks", "kh/hooks", "a/hooks", "b/hooks", "c/hooks", "sub")


def _build_entry(path: Path, kind: str, home: Path, tag: str) -> None:
    """Lay out one name of the layout as *kind*; *tag* keeps link targets distinct."""
    outside = home.parent / "outside"
    if kind == "dir":
        (path / "x" / "hooks").mkdir(parents=True)
        (path / "hooks").mkdir()
    elif kind == "link-to-dir":
        (home / "tgt" / tag / "x" / "hooks").mkdir(parents=True)
        (home / "tgt" / tag / "hooks").mkdir(exist_ok=True)
        path.symlink_to(home / "tgt" / tag, target_is_directory=True)
    elif kind == "link-to-missing":
        path.symlink_to(home / "gone" / tag, target_is_directory=True)
    elif kind == "link-outside":
        (outside / tag / "x" / "hooks").mkdir(parents=True)
        (outside / tag / "hooks").mkdir(exist_ok=True)
        path.symlink_to(outside / tag, target_is_directory=True)
    elif kind == "loop":
        path.symlink_to(path)
    elif kind == "link-sensitive":
        (home / ".ssh" / "x").mkdir(parents=True, exist_ok=True)
        path.symlink_to(home / ".ssh", target_is_directory=True)


def _build_layout(home: Path, kinds: tuple[str, ...], inner_kinds: tuple[str, ...]) -> None:
    """Each top-level name as its kind; under a real directory, its ``x`` as its inner kind."""
    for name, kind, inner in zip(_NAMES, kinds, inner_kinds, strict=True):
        _build_entry(home / name, kind, home, name)
        if kind == "dir" and inner != "dir":
            shutil.rmtree(home / name / "x")
            _build_entry(home / name / "x", inner, home, f"{name}-x")
    (home / "hooks").mkdir(exist_ok=True)
    (home / ".kiro").mkdir(exist_ok=True)


def _attack_targets(root: Path, home: Path) -> list[Path]:
    """Directories an agent points a replaced name at: two in $HOME, one outside it.

    A link into $HOME can turn a spelling the reader rejected into one it accepts, and
    a link to a subdirectory lets a later ``..`` land on a third directory.
    """
    targets = [home / "evil", home / "evil" / "sub", root / "evil"]
    for target in (home / "evil", root / "evil"):
        for sub in _ATTACK_SUBDIRS:
            (target / sub).mkdir(parents=True, exist_ok=True)
    return targets


def _what_readers_reach(kiro_home_value: str | None) -> tuple[str, ...]:
    """What Crew's resolver, Crew's ``kiro_home()`` and kiro-cli's ``$KIRO_HOME/hooks`` reach.

    kiro-cli reads ``$KIRO_HOME/hooks`` whether or not Crew accepts the override.
    """
    from kiro_crew.config.paths import kiro_home

    reached = [os.path.realpath(agent_mod.resolved_kiro_hooks_dir())]
    if kiro_home_value is not None:
        try:
            reached.append(os.path.realpath(kiro_home()))
        except (OSError, RuntimeError):  # Path.resolve on a link loop
            reached.append("<loop>")
        reached.append(os.path.realpath(os.path.join(kiro_home_value, "hooks")))
    return tuple(reached)


def _linux_blocks(path: str, readonly: list[str], pins: set[str]) -> bool:
    """Whether the namespace sandbox refuses replacing *path* (unlink, rename, create)."""
    parent = os.path.realpath(os.path.dirname(path))
    if any(parent == ro or parent.startswith(ro.rstrip(os.sep) + os.sep) for ro in readonly):
        return True  # the folder holding the name is a read-only bind
    canonical = os.path.join(parent, os.path.basename(path))
    if (path in pins or canonical in pins) and os.path.lexists(path):
        return True  # the name itself is pinned (a mount on it, whatever spells it)
    return not os.path.islink(path) and os.path.realpath(path) in readonly


def _attack_candidates(home: Path, readers: list[str]) -> list[str]:
    """Every name an agent could replace that a reader might step on.

    Built without the product's walker: every path of up to three names under $HOME
    from the layout's vocabulary, and every link and ``..``-climbed directory the
    test's own walk (:func:`_walk_entries`) meets on each reader's spelling.
    """
    vocabulary = (*_NAMES, "x", "hooks", "tgt", "gone", ".kiro", "kh", ".ssh")
    candidates: dict[str, None] = {}
    for depth in (1, 2, 3):
        for parts in itertools.product(vocabulary, repeat=depth):
            candidates[os.path.join(home, *parts)] = None
    for spelling in readers:
        links, climbed = _walk_entries(spelling)
        for entry in (*links, *climbed):
            if entry.startswith(str(home) + os.sep):
                candidates[entry] = None
    return list(candidates)


def _replace_with_link(path: str, evil: Path) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
    os.symlink(evil, path, target_is_directory=True)


#: Every inner name a real directory, so the layout has no link below the top level.
_ALL_DIRS = ("dir", "dir", "dir")

# Fixed examples, as (kinds, inner_kinds, setting, KIRO_HOME names). Rejected
# spellings an agent could turn into accepted ones: a real directory then ``..``
# reaching $HOME, a link out of $HOME, a loop, a link to a sensitive directory, a
# looping KIRO_HOME, and the same below the top level. Then a missing name before
# ``..``, a relative setting, a ``..`` in KIRO_HOME, and layouts where the reached
# directory is also a name a later ``..`` climbs back to.
_FIXED_EXAMPLES = [
    (("dir", "absent", "absent"), _ALL_DIRS, "~/a/..", None),
    (("absent", "link-outside", "absent"), _ALL_DIRS, "~/b/hooks", None),
    (("absent", "absent", "loop"), _ALL_DIRS, "~/c/hooks", None),
    (("link-sensitive", "absent", "absent"), _ALL_DIRS, "~/a/hooks", None),
    (("absent", "absent", "loop"), _ALL_DIRS, None, ["c"]),
    (("dir", "absent", "absent"), ("link-outside", "dir", "dir"), "~/a/x/hooks", None),
    (("dir", "absent", "absent"), ("loop", "dir", "dir"), "~/a/x/hooks", None),
    (("dir", "absent", "absent"), ("link-to-dir", "dir", "dir"), "~/a/x/..", None),
    (("absent", "absent", "absent"), _ALL_DIRS, "~/a/a/..", None),
    (("absent", "link-to-dir", "absent"), _ALL_DIRS, None, ["b", "a", ".."]),
    (("absent", "dir", "dir"), _ALL_DIRS, "~/a/../hooks", None),
    (("link-to-dir", "dir", "dir"), _ALL_DIRS, "a/hooks", None),
    (("link-to-dir", "dir", "dir"), _ALL_DIRS, None, ["a", "..", "b"]),
]


def _with_fixed_examples(test: Callable[..., None]) -> Callable[..., None]:
    for kinds, inner_kinds, setting, kiro_home_tokens in reversed(_FIXED_EXAMPLES):
        test = hypothesis.example(
            kinds=kinds,
            inner_kinds=inner_kinds,
            setting=setting,
            kiro_home_tokens=kiro_home_tokens,
        )(test)
    return test


@_POSIX_ONLY
@hypothesis.settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    suppress_health_check=[hypothesis.HealthCheck.function_scoped_fixture],
)
@hypothesis.seed(43668)
@hypothesis.given(
    kinds=st.tuples(*(st.sampled_from(_KINDS) for _ in _NAMES)),
    inner_kinds=st.tuples(*(st.sampled_from(_KINDS) for _ in _NAMES)),
    setting=st.one_of(
        st.none(),
        st.tuples(
            st.sampled_from(("~/", "")),
            st.lists(st.sampled_from(_TOKENS), min_size=1, max_size=4),
        ).map(lambda t: t[0] + "/".join(t[1])),
    ),
    kiro_home_tokens=st.one_of(
        st.none(), st.lists(st.sampled_from(_TOKENS[:-1]), min_size=1, max_size=3)
    ),
)
@_with_fixed_examples
def test_no_write_the_sandbox_allows_changes_the_hooks_dir_readers_reach(
    tmp_path: Path,
    kinds: tuple[str, ...],
    inner_kinds: tuple[str, ...],
    setting: str | None,
    kiro_home_tokens: list[str] | None,
) -> None:
    import uuid

    root = (tmp_path / uuid.uuid4().hex[:8]).resolve()
    home = root / "home"
    home.mkdir(parents=True)
    _build_layout(home, kinds, inner_kinds)
    targets = _attack_targets(root, home)
    kiro_home_value = None if not kiro_home_tokens else str(home / "/".join(kiro_home_tokens))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        mp.setenv("USERPROFILE", str(home))
        mp.chdir(home)  # a relative setting resolves against Crew's cwd
        mp.setattr(agent_mod, "_DEFAULT_KIRO_HOOKS_DIR", home / ".kiro" / "hooks")
        mp.setattr(agent_mod, "resolved_kiro_hooks_dir", _REAL_RESOLVED_KIRO_HOOKS_DIR)
        if kiro_home_value is None:
            mp.delenv("KIRO_HOME", raising=False)
        else:
            mp.setenv("KIRO_HOME", kiro_home_value)
        from kiro_crew.config import config_path

        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        agent_cfg = {} if setting is None else {"kiro_hooks_dir": setting}
        path.write_text(json.dumps({"agent": agent_cfg}), encoding="utf-8")

        sandbox._materialize_kiro_hooks_dirs()
        before = _what_readers_reach(kiro_home_value)
        request = SandboxRequest(tier="standard", backend=BACKEND_NAMESPACE)
        payload = sandbox_plan.namespace_payload(
            plan_confinement(request, sandbox._live_plan_host(request))
        )
        readonly = [os.path.realpath(p) for p in payload["readonly_dirs"] if os.path.exists(p)]
        pins = set(payload["parent_pins"])
        readers = [str(home / ".kiro" / "hooks")]
        if setting is not None:
            readers.append(os.path.join(home, os.path.expanduser(setting)))
        if kiro_home_value is not None:
            readers.append(os.path.join(kiro_home_value, "hooks"))
        candidates = _attack_candidates(home, readers)
        allowed = [c for c in candidates if not _linux_blocks(c, readonly, pins)]
        assert len(candidates) > len(allowed) or not readonly, "nothing was protected"
        for candidate in allowed:
            for target in targets:
                folder = os.path.realpath(os.path.dirname(candidate))
                if not os.path.isdir(folder) or not (
                    folder == str(home) or folder.startswith(str(home) + os.sep)
                ):
                    break  # an earlier replacement moved its folder out of $HOME
                if folder.startswith(str(home / "evil")):
                    break  # the attacker's own tree
                _replace_with_link(candidate, target)
                assert _what_readers_reach(kiro_home_value) == before, (
                    kinds,
                    setting,
                    kiro_home_tokens,
                    candidate,
                    str(target),
                )


# --------------------------------------------------------------------------- #
# A mode change on a folder the walk steps on fails every reader closed.
# --------------------------------------------------------------------------- #

#: (row id, how the person laid the hooks out, the folder an agent makes unreadable,
#: whether Crew's own readers read through that folder). On Linux a pin holds a name,
#: not its mode, so these changes are allowed inside the sandbox.
_UNREADABLE = [
    ("M1", "configured ~/my/hooks", "my", True),
    ("M2", "default ~/.kiro/hooks -> ~/dot/kiro/hooks", "dot/kiro", True),
    ("M3", "configured ~/p/q/hooks, ~/p unreadable from the start", "p", True),
    ("M4", "default real dir", ".kiro", True),
    ("M5", "default real dir, the directory itself not listable", ".kiro/hooks", True),
    ("M6", "configured ~/my/hooks, owner ACL entry cleared", "my", True),
    ("M7", "KIRO_HOME=~/kh, read by kiro-cli only", "kh", False),
]


def _make_unreadable(path: Path, row: str) -> None:
    if row == "M6":
        import subprocess

        if shutil.which("setfacl") is None:
            pytest.skip("setfacl is not installed")
        done = subprocess.run(
            ["setfacl", "-m", "u::---", str(path)], capture_output=True, cwd=path.parent
        )
        if done.returncode != 0:
            pytest.skip("this filesystem does not take a POSIX ACL")
    elif row == "M5":
        path.chmod(0o100)  # searchable, not listable
    else:
        path.chmod(0)


@_POSIX_ONLY
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
@pytest.mark.parametrize(
    ("row", "layout", "folder", "crew_reads_it"), _UNREADABLE, ids=[r[0] for r in _UNREADABLE]
)
def test_an_unreadable_folder_on_the_way_fails_every_reader_closed(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    row: str,
    layout: str,
    folder: str,
    crew_reads_it: bool,
) -> None:
    import asyncio
    from types import SimpleNamespace

    import kiro_crew.config.paths as paths_mod
    from kiro_crew.acp import kas_agents
    from kiro_crew.agent_materialization import kiro_hooks
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.kiro_hooks_dir import HooksDirUnreadable

    hook_doc = json.dumps(
        {
            "version": "v1",
            "hooks": [
                {
                    "name": "gate",
                    "trigger": "PreToolUse",
                    "action": {"type": "command", "command": "true"},
                }
            ],
        }
    )
    if row in ("M1", "M6"):
        reader = home / "my" / "hooks"
        _set_configured_hooks_dir(monkeypatch, "~/my/hooks")
    elif row == "M2":
        (home / ".kiro" / "hooks").rmdir()
        reader = home / "dot" / "kiro" / "hooks"
        reader.mkdir(parents=True)
        (home / ".kiro" / "hooks").symlink_to(reader, target_is_directory=True)
    elif row == "M3":
        reader = home / "p" / "q" / "hooks"
        _set_configured_hooks_dir(monkeypatch, "~/p/q/hooks")
    elif row == "M7":
        reader = home / "kh" / "hooks"
        monkeypatch.setenv("KIRO_HOME", str(home / "kh"))
    else:
        reader = home / ".kiro" / "hooks"
    reader.mkdir(parents=True, exist_ok=True)
    (reader / "gate.json").write_text(hook_doc, encoding="utf-8")
    (reader / "gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (reader / "gate.sh").chmod(0o700)
    spec_hooks._user_file_cache.clear()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a1.json").write_text(json.dumps({"name": "a1", "prompt": "p"}), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *a, **k: None)
    if row != "M3":
        sandbox._materialize_kiro_hooks_dirs()  # the spawn before the agent's change

    blocked = home / folder
    _make_unreadable(blocked, row)
    try:
        if crew_reads_it:
            # Crew's own readers: an error, never "no hooks" or another folder.
            with pytest.raises(HooksDirUnreadable):
                spec_hooks.user_hook_files()
            with pytest.raises(HooksDirUnreadable):
                kiro_hooks._apply_user_kiro_hooks(
                    {"hooks": {}}, json.loads(_config_text_or_empty())
                )
            # The permission path refuses every tool call of the turn.
            kas = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="")
            assert asyncio.run(spec_hooks.turn_spec_hooks(kas, "a1")).unreadable is True
            _hooks, unreadable, _cwd = asyncio.run(
                chat_runner._prepare_spec_hooks(
                    SimpleNamespace(), SimpleNamespace(), kas, "a1", is_new=False
                )
            )
            assert unreadable is True
            assert kas_agents.pre_tool_hook_matchers("a1", {}) == ("*",)
        # The next spawn refuses rather than sealing less (kiro-cli reads with no error).
        with pytest.raises(sandbox.SandboxHooksDirUnreadable) as refused:
            sandbox._materialize_kiro_hooks_dirs()
        assert str(blocked) in str(refused.value) or str(reader) in str(refused.value)
    finally:
        if row == "M6":
            import subprocess

            subprocess.run(["setfacl", "-b", str(blocked)], cwd=blocked.parent, check=False)
        blocked.chmod(0o700)


def _config_text_or_empty() -> str:
    from kiro_crew.config import config_path

    path = config_path()
    return path.read_text(encoding="utf-8") if path.exists() else "{}"


@_POSIX_ONLY
@pytest.mark.parametrize("change", ["user-xattr", "utimes"])
def test_a_metadata_change_no_reader_consults_changes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from kiro_crew.agent_sdk import spec_hooks

    reader = home / "my" / "hooks"
    reader.mkdir(parents=True)
    (reader / "gate.json").write_text(
        json.dumps(
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": "gate",
                        "trigger": "PreToolUse",
                        "action": {"type": "command", "command": "true"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _set_configured_hooks_dir(monkeypatch, "~/my/hooks")
    spec_hooks._user_file_cache.clear()
    before = [h.command for h in spec_hooks.user_hook_files()]
    for folder in (home / "my", reader):
        if change == "user-xattr":
            try:
                os.setxattr(folder, "user.kiro-crew-test", b"x")
            except (AttributeError, OSError):
                pytest.skip("user extended attributes are not supported here")
        else:
            os.utime(folder, ns=(0, 0))
    spec_hooks._user_file_cache.clear()
    assert agent_mod.resolved_kiro_hooks_dir() == reader
    assert [h.command for h in spec_hooks.user_hook_files()] == before == ["true"]
    sandbox._materialize_kiro_hooks_dirs()


@_POSIX_ONLY
def test_a_relative_setting_with_no_working_directory_does_not_break_the_spawn(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import kiro_hooks_dir

    _set_configured_hooks_dir(monkeypatch, "rel/hooks")

    def gone() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(kiro_hooks_dir.os, "getcwd", gone)
    sandbox._materialize_kiro_hooks_dirs()
    assert str(home / ".kiro" / "hooks") in sandbox._resolved_kiro_hooks_targets()
    sandbox._resolved_kiro_hooks_walk_entries()


@_POSIX_ONLY
def test_every_missing_name_of_a_long_setting_is_created(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [f"d{i}" for i in range(70)]
    _set_configured_hooks_dir(monkeypatch, "~/" + "/".join(names) + "/hooks")
    sandbox._materialize_kiro_hooks_dirs()
    assert (home.joinpath(*names) / "hooks").is_dir()
