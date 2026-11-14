"""A root sealed around its windows stays sealed on top of every mask that contains it.

A data home under ``~/.aws`` or ``~/.ssh`` puts the scratch root inside another mask, before
or after it in the list, and possibly only once links are followed. The launcher places the
root ahead of its containers, holds it, and mounts it back over each container once that is
placed; once every mask is placed it confirms each sealed root is still the read-only
stand-in it mounted, with its windows (``confirm_sealed_roots``), and refuses otherwise.

These cases drive the launcher's own stages with the fake libcs of
``test_scratch_root_seal`` and ``test_sandbox_launcher_program``; the kernel half is pinned by
``test_scratch_root_seal_real_sandbox.py``.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_sandbox_launcher_program import (
    CoveringLibc,
    _resolved,
    identity,
    launch,
    payload,
    read_back_read_only_remounts,
    refusal,
)
from test_scratch_root_seal import (
    _HOME,
    _LINUX_ONLY,
    _MS_BIND,
    _MS_RDONLY,
    _MS_REC,
    _MS_REMOUNT,
    _OWN,
    _ROOT,
    _host,
    _placed,
    _plan,
    _read_only_remounts,
    _Scratch,
    _SealingLibc,
    _window_binds,
    read_back_survives_a_test_local_undo,
)

from kiro_crew import sandbox_launcher_program as program
from kiro_crew import sandbox_plan
from kiro_crew.sandbox_plan import BACKEND_NAMESPACE, BACKEND_SEATBELT

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")


@pytest.fixture(autouse=True)
def read_only_remounts(_floor_monkeypatch: pytest.MonkeyPatch) -> set[tuple[int, int]]:
    """What the fake libc remounted read-only reads back as read-only (``confirm_sealed_roots``).

    Patched on the floor's own stack, so a test's ``monkeypatch.undo()`` leaves it in place.
    """
    return read_back_read_only_remounts(_floor_monkeypatch)


# --------------------------------------------------------------------------- #
# A mask that contains the sealed root, placed before it or after it.
# --------------------------------------------------------------------------- #


def _settled(run: program.Launch) -> None:
    """The mount stages ``run_child`` runs before the agent: place, retire the stage, confirm."""
    program.place_masks(run)
    program.retire_stand_in_stage(run)
    program.confirm_sealed_roots(run)


def _unseal(tmp_path: Path) -> None:
    """Make every directory the fake seal left read-only writable again, for cleanup."""
    for parent, dirs, _files in os.walk(tmp_path):
        for name in dirs:
            path = os.path.join(parent, name)
            if not os.path.islink(path):
                os.chmod(path, 0o755)


def _contained(
    tmp_path: Path, container: str, *, windows: bool
) -> tuple[_Scratch, Path, dict[str, Any]]:
    """A scratch root inside a credential directory, as with ``KIROCREW_HOME=~/.aws/crew``.

    The plan carries the root's identity, as the gateway's alias pass records it for every
    hidden crew leaf, so the launcher refuses to mask any other object at that name.
    """
    scratch = _Scratch(tmp_path)
    aws = scratch.root.parent.parent
    (aws / "credentials").write_text("[default]\n")
    st = os.stat(scratch.root)
    root = str(scratch.root)
    fields: dict[str, Any] = {
        "private_dirs": scratch.windows if windows else [],
        "seal_windowed_masks": [root],
        "mask_occupants": {root: [st.st_dev, st.st_ino, 0, 1, st.st_dev, st.st_ino]},
    }
    fields["sensitive_dirs"] = {
        "after": [root, str(aws)],
        "before": [str(aws), root],
        "ssh": [root],
    }[container]
    if container == "ssh":
        fields.update(ssh_dir=str(aws), ssh_known_hosts=str(aws / "known_hosts"), hide_ssh=1)
    return scratch, aws, fields


@_LINUX_ONLY
@pytest.mark.parametrize("windows", [True, False], ids=["with windows", "no window"])
@pytest.mark.parametrize(
    "container",
    ["after", "before", "ssh"],
    ids=["container after the root", "container before it", "strict ~/.ssh mask"],
)
def test_a_mask_that_contains_the_sealed_root_leaves_the_seal_on_top(
    tmp_path: Path, container: str, windows: bool
) -> None:
    """Once every mask is placed, the root is still sealed and only its windows take writes.

    A containing mask binds a fresh writable stand-in over the whole tree; mounted after the
    seal it would cover it, and a stale write under the root would land in that stand-in and
    be lost. Without a window under the root the stand-in has no directory there at all, so
    a ``mkdir -p`` would create one in it.
    """
    scratch, aws, fields = _contained(tmp_path, container, windows=windows)
    libc = _MovingSealLibc()
    run = launch(tmp_path, payload(**fields), libc=libc)
    try:
        _settled(run)
        root = scratch.root
        assert root.is_dir(), "the sealed root is mounted inside the containing mask"
        for stale in (root / "runtime-stale", root / "runtime-new" / "briefs"):
            with pytest.raises(PermissionError):
                os.makedirs(stale)
        with pytest.raises(PermissionError):
            (root / "new-file").write_text("lost")
        assert not (aws / "credentials").exists(), "the containing mask still hides its tree"
        if windows:
            assert sorted(os.listdir(root)) == ["runtime-own", "runtime-tree"]
            (scratch.own / "kept.md").write_text("kept")
            assert (scratch.tree / "brief.md").read_text() == "tree brief"
        else:
            assert os.listdir(root) == []
        assert run.masked_names[str(root)] == identity(root)
        assert run.sealed_mounts == {}, "the descriptor holding the root is closed"
    finally:
        _unseal(tmp_path)
    if windows:
        assert (scratch.own / "kept.md").read_text() == "kept"


def _linked(
    tmp_path: Path, kind: str, order: str, *, windows: bool
) -> tuple[Path, list[Path], Path, dict[str, Any]]:
    """A scratch root that a container holds only through a link: ``(root, windows, real, plan)``.

    * ``aws``: ``~/.aws`` is a link to ``data-aws`` and the data home is ``data-aws/crew``,
      as ``KIROCREW_HOME=~/.aws/crew`` resolves (strict and cc list ``.aws`` before the
      root; a Codex session adds it after).
    * ``ssh``: the same with ``~/.ssh``, which the strict tier masks last.
    * ``crew home``: ``~/.kiro/crew`` is a link into a real ``~/.aws``, so the root is
      spelled through the link.
    * ``parent``: ``$HOME`` is a link, so the container is spelled through it while the
      root is spelled by its real path.

    *real* is the directory the container's mask covers. The plan carries the root's
    identity, as the gateway's alias pass records it.
    """
    home = tmp_path / "home"
    if kind == "parent":
        real = tmp_path / "real-home" / ".aws"
        real.mkdir(parents=True)
        home.symlink_to(tmp_path / "real-home")
        container, crew = home / ".aws", real / "crew"
    elif kind in ("aws", "ssh"):
        real = tmp_path / f"data-{kind}"
        real.mkdir()
        home.mkdir()
        container = home / f".{kind}"
        container.symlink_to(real)
        crew = real / "crew"
    else:
        real = home / ".aws"
        (real / "crew").mkdir(parents=True)
        (home / ".kiro").mkdir()
        (home / ".kiro" / "crew").symlink_to(real / "crew")
        container, crew = real, home / ".kiro" / "crew"
    (real / "credentials").write_text("[default]\n")
    root = crew / "scratch"
    own, tree = root / "runtime-own", root / "runtime-tree"
    own.mkdir(parents=True)
    tree.mkdir()
    st = os.stat(root)
    fields: dict[str, Any] = {
        "private_dirs": [str(own), str(tree)] if windows else [],
        "seal_windowed_masks": [str(root)],
        "mask_occupants": {str(root): [st.st_dev, st.st_ino, 0, 1, st.st_dev, st.st_ino]},
    }
    if kind == "ssh":
        fields.update(
            sensitive_dirs=[str(root)],
            ssh_dir=str(container),
            ssh_known_hosts=str(container / "known_hosts"),
            hide_ssh=1,
        )
    else:
        pair = [str(container), str(root)]
        fields["sensitive_dirs"] = pair if order == "before" else pair[::-1]
    return root, [own, tree] if windows else [], real, fields


_LINKED_LAYOUTS = [
    ("aws", "before"),
    ("aws", "after"),
    ("ssh", "last"),
    ("crew home", "before"),
    ("crew home", "after"),
    ("parent", "before"),
    ("parent", "after"),
]


@_LINUX_ONLY
@pytest.mark.parametrize("windows", [True, False], ids=["with windows", "no window"])
@pytest.mark.parametrize(
    ("kind", "order"),
    _LINKED_LAYOUTS,
    ids=[f"{kind} link, container {order}" for kind, order in _LINKED_LAYOUTS],
)
def test_a_container_reached_through_a_link_leaves_the_seal_on_top(
    tmp_path: Path, kind: str, order: str, windows: bool
) -> None:
    """The container holds the root only once links are followed, and is still found.

    Judged by spelling alone, the root would not move ahead of the container, would not be
    held, and the container's stand-in, bound over the object the link reaches, would
    cover it: the root's name then reaches nothing, or a directory a ``mkdir -p`` creates
    in that stand-in.
    """
    root, wins, real, fields = _linked(tmp_path, kind, order, windows=windows)
    libc = _MovingSealLibc()
    run = launch(tmp_path, payload(**fields), libc=libc)
    try:
        _settled(run)
        assert root.is_dir(), "the sealed root is mounted inside the containing mask"
        for stale in (root / "runtime-stale", root / "runtime-new" / "briefs"):
            with pytest.raises(PermissionError):
                os.makedirs(stale)
        with pytest.raises(PermissionError):
            (root / "new-file").write_text("lost")
        assert not (real / "credentials").exists(), "the containing mask still hides its tree"
        assert sorted(os.listdir(root)) == sorted(w.name for w in wins)
        for window in wins:
            (window / "kept.md").write_text("kept")
        assert run.sealed_mounts == {}
    finally:
        _unseal(tmp_path)
    assert all((w / "kept.md").read_text() == "kept" for w in wins)


@_LINUX_ONLY
def test_one_spelling_rule_decides_containment_and_the_part_below(tmp_path: Path) -> None:
    """Written spellings first, then resolved ones; the part below comes from the pair that matched."""
    data = tmp_path / "data-aws"
    (data / "crew" / "scratch").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    link = tmp_path / "home" / ".aws"
    link.symlink_to(data)
    real = tmp_path / "real"
    real.mkdir()
    (real / "out").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "elsewhere" / "scratch").mkdir(parents=True)
    run = launch(tmp_path)
    below = program._held_below
    assert below(run, str(link), str(link / "crew" / "scratch")) == "crew/scratch"
    assert below(run, str(link), str(data / "crew" / "scratch")) == "crew/scratch"
    assert below(run, str(link) + "/", str(data) + "//crew/./scratch") == "crew/scratch"
    # A name below the container's name is covered by it, wherever its own link points.
    assert below(run, str(real), str(real / "out" / "scratch")) == "out/scratch"
    # Both pairs match and name different parts: the name passes through the container's
    # name, so its part below is the written one.
    (data / "z" / "y").mkdir(parents=True)
    (data / "x").symlink_to(data / "z")
    assert below(run, str(link), str(link / "x" / "y")) == "x/y"
    # Neither spelling below: outside, also for the container itself.
    assert below(run, str(link), str(tmp_path / "elsewhere" / "scratch")) is None
    assert below(run, str(link), str(data)) is None
    assert below(run, str(real), str(tmp_path / "elsewhere")) is None


@_LINUX_ONLY
def test_the_spellings_are_read_before_the_first_mask_is_mounted(tmp_path: Path) -> None:
    """Once a container's stand-in covers a link's target, ``realpath`` would see the stand-in.

    A strict tier's ``~/.ssh`` that holds no root is first compared when the root is held,
    after the root's own mount, so it is the spelling a lazy read would take late.
    """
    root, _wins, _real, fields = _linked(tmp_path, "aws", "after", windows=True)
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir()
    fields.update(ssh_dir=str(ssh), ssh_known_hosts=str(ssh / "known_hosts"), hide_ssh=1)
    reads: list[int] = []
    libc = _MovingSealLibc()
    real_mount = libc.mount

    def _mount(*args: Any) -> int:
        reads.append(len(run.mask_spellings))
        return real_mount(*args)

    libc.mount = _mount  # type: ignore[method-assign]
    run = launch(tmp_path, payload(**fields), libc=libc)
    try:
        program.stage_private_windows(run)
        before = len(run.mask_spellings)
        program.mask_sensitive(run)
    finally:
        _unseal(tmp_path)
    assert before == 0
    first_mask = reads[len(run.private_dirs)]
    assert first_mask == len(run.mask_spellings), "every spelling was read before the masks"
    assert run.mask_spellings[str(root)][1] == os.path.realpath(root)
    assert str(ssh) in run.mask_spellings


@_LINUX_ONLY
def test_a_crew_leaf_after_a_linked_container_refuses_as_on_the_base(tmp_path: Path) -> None:
    """Out of scope here, and unchanged: the base launcher's own absent-name check is lexical.

    A crew leaf listed after a container that holds it only through a link vanishes beneath
    that container, and ``_covered_by_own_mask`` does not follow the link, so the spawn
    refuses at that leaf, as the base launcher refuses at the first such leaf. The sealed root
    is not that leaf: it is found inside the container and placed ahead of it.
    """
    root, _wins, real, fields = _linked(tmp_path, "aws", "before", windows=True)
    tasks = real / "crew" / "tasks"
    tasks.mkdir()
    st = os.stat(tasks)
    fields["sensitive_dirs"].append(str(tasks))
    fields["mask_occupants"][str(tasks)] = [st.st_dev, st.st_ino, 0, 1, st.st_dev, st.st_ino]
    run = launch(tmp_path, payload(**fields), libc=_MovingSealLibc())
    try:
        message = refusal(program.place_masks, run)
    finally:
        _unseal(tmp_path)
    assert message is not None and str(tasks) in message and "vanished" in message
    assert str(root) not in message


@_LINUX_ONLY
def test_a_crew_file_leaf_after_a_linked_container_refuses_as_on_the_base(tmp_path: Path) -> None:
    """The Codex layout: the linked ``.aws`` is the last directory mask, after every crew leaf.

    So every crew leaf is placed before it, and the single-file masks, which run after every
    directory mask and are offered each crew leaf again, find a leaf a pass established gone
    beneath the container. The base launcher's absent-name check goes by spelling, so the
    spawn refuses at that leaf, as the base launcher refuses at the same one; the sealed root
    is not that leaf.
    """
    root, _wins, real, fields = _linked(tmp_path, "aws", "after", windows=True)
    cache = real / "crew" / "policy_cache"
    cache.mkdir()
    fields["sensitive_dirs"].insert(0, str(cache))
    fields.update(sensitive_files=[str(cache)], required_mask_targets=[str(cache)])
    run = launch(tmp_path, payload(**fields), libc=_MovingSealLibc())
    try:
        message = refusal(program.place_masks, run)
    finally:
        _unseal(tmp_path)
    assert message is not None and str(cache) in message and "it is absent" in message, message
    assert str(root) not in message


# --------------------------------------------------------------------------- #
# The last check: each sealed root is its sealed stand-in, its windows in place.
# --------------------------------------------------------------------------- #


@_LINUX_ONLY
def test_a_test_local_undo_keeps_the_read_only_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The autouse read-back is on the floor's own stack, so a test's ``undo()`` keeps it.

    Without it the fake seal reads back as writable and the last check refuses.
    """
    read_back_survives_a_test_local_undo(__file__, monkeypatch, tmp_path)


@_LINUX_ONLY
def test_a_link_swapped_while_the_spellings_are_read_ends_in_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``~/.ssh`` is moved aside while its spelling is read, and put back before it is masked.

    The data home is under the link's target, so the read spelling says ``~/.ssh`` holds no
    root: the root is not held, the ``~/.ssh`` mask covers it, and nothing mounts it back.
    The last check finds the root's name reaches something other than its sealed stand-in
    and refuses before anything runs, where without it a write under the root would land in
    the ``~/.ssh`` stand-in and be lost.
    """
    root, _wins, _real, fields = _linked(tmp_path, "ssh", "last", windows=True)
    link = Path(fields["ssh_dir"])
    real_realpath = os.path.realpath
    swapped: list[str] = []

    def _realpath(path: Any, *args: Any, **kwargs: Any) -> str:
        if os.fspath(path) != str(link) or swapped:
            return real_realpath(path, *args, **kwargs)
        swapped.append(os.fspath(path))
        os.rename(link, link.with_name(".ssh-aside"))
        try:
            return real_realpath(path, *args, **kwargs)
        finally:
            os.rename(link.with_name(".ssh-aside"), link)

    run = launch(tmp_path, payload(**fields), libc=_MovingSealLibc())
    try:
        with monkeypatch.context() as patched:
            patched.setattr(os.path, "realpath", _realpath)
            message = refusal(_settled, run)
    finally:
        _unseal(tmp_path)
    assert swapped == [str(link)]
    assert run.mask_spellings[str(link)] == (str(link), str(link)), "the read missed the link"
    assert message is not None and str(root) in message, message
    assert "now that every mask is placed" in message


@_LINUX_ONLY
@pytest.mark.parametrize(
    ("change", "said"),
    [
        ("covered", "is not the sealed stand-in this spawn mounted there"),
        ("writable", "is not on a read-only mount"),
        ("window gone", "No such file or directory"),
        ("window replaced", "it is a different object"),
    ],
)
def test_the_last_check_refuses_a_sealed_root_this_spawn_did_not_leave(
    tmp_path: Path, read_only_remounts: set[tuple[int, int]], change: str, said: str
) -> None:
    """A root covered by another mount, a seal that is not read-only, or a window that is gone
    or another directory, each found once every mask is placed: the spawn refuses, naming the
    root."""
    scratch = _Scratch(tmp_path)
    run, libc = _placed(tmp_path, scratch, seal_windowed_masks=[str(scratch.root)])
    assert program.confirm_sealed_roots(run) is None, "as placed, the root passes"
    if change == "covered":
        cover = tmp_path / "cover"
        cover.mkdir()
        libc.mount(str(cover).encode(), str(scratch.root).encode(), None, _MS_BIND, None)
    elif change == "writable":
        read_only_remounts.clear()
    else:
        os.rename(scratch.own, tmp_path / "moved-own")
        if change == "window replaced":
            scratch.own.mkdir()
    message = refusal(program.confirm_sealed_roots, run)
    assert message is not None and str(scratch.root) in message and said in message, message
    if change.startswith("window"):
        assert str(scratch.own) in message


@_LINUX_ONLY
def test_a_stand_in_entry_that_still_reaches_the_root_writable_refuses_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stand-in's own host path reaches it on a mount nothing sealed: the spawn refuses.

    The fake places a mount by renaming, so the entry is gone once the root is masked; a
    link put back at it, opened on a mount the read-back reports writable, stands for the
    entry a real bind leaves in place, as when the bind closing it came out writable.
    """
    scratch = _Scratch(tmp_path)
    run, _libc = _placed(tmp_path, scratch, seal_windowed_masks=[str(scratch.root)])
    entry, how = run.stand_in_sources[str(scratch.root)]
    assert how == "closed" and not os.path.lexists(entry)
    os.symlink(scratch.root, entry)
    opened: dict[int, str] = {}
    real_open, read_back = os.open, os.fstatvfs

    def _open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        opened[fd] = os.fsdecode(path)
        return fd

    def _fstatvfs(fd: int) -> Any:
        flags = read_back(fd).f_flag
        if opened.get(fd) == entry:
            flags &= ~os.ST_RDONLY
        return SimpleNamespace(f_flag=flags)

    with monkeypatch.context() as patched:
        patched.setattr(os, "open", _open)
        patched.setattr(os, "fstatvfs", _fstatvfs)
        message = refusal(program.confirm_sealed_roots, run)
    assert message is not None and entry in message and str(scratch.root) in message, message
    assert "still reaches it on a writable mount" in message


@_LINUX_ONLY
@pytest.mark.parametrize("record", ["open", "missing"])
def test_a_stand_in_entry_that_was_not_closed_refuses_the_spawn(
    tmp_path: Path, record: str
) -> None:
    """Made on a host-visible root and never closed, or never recorded: the spawn refuses."""
    scratch = _Scratch(tmp_path)
    run, _libc = _placed(tmp_path, scratch, seal_windowed_masks=[str(scratch.root)])
    entry, _how = run.stand_in_sources[str(scratch.root)]
    if record == "open":
        run.stand_in_sources[str(scratch.root)] = (entry, "open")
    else:
        del run.stand_in_sources[str(scratch.root)]
    message = refusal(program.confirm_sealed_roots, run)
    assert message is not None and str(scratch.root) in message, message
    assert "was not closed" in message
    assert (entry in message) == (record == "open")


def test_run_child_confirms_the_sealed_roots_once_the_stage_is_retired() -> None:
    """The last check runs once, after the stand-in stage is retired and before the agent.

    The seals' parents are pinned before the stage is mounted, so no recursive pin copies it.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(program.run_child)))
    called = [
        node.func.id
        for node in sorted(
            (n for n in ast.walk(tree) if isinstance(n, ast.Call)), key=lambda n: n.lineno
        )
        if isinstance(node.func, ast.Name)
    ]
    assert called.count("confirm_sealed_roots") == 1
    order = [
        "pin_seal_parents",
        "stage_stand_ins_privately",
        "place_masks",
        "retire_stand_in_stage",
        "confirm_sealed_roots",
        "scrub_env",
    ]
    assert [called.index(step) for step in order] == sorted(called.index(step) for step in order)
    assert called.index("confirm_sealed_roots") == called.index("retire_stand_in_stage") + 1
    assert called.index("stage_stand_ins_privately") == called.index("pin_seal_parents") + 1
    placed = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(program.place_masks))))
    assert "confirm_sealed_roots" not in placed


@_LINUX_ONLY
def test_a_sealed_root_no_mask_was_placed_on_is_not_checked(tmp_path: Path) -> None:
    """A root that is absent was never masked, so nothing is confirmed there and the spawn runs."""
    root = tmp_path / "home" / ".kiro" / "crew" / "scratch"
    run = launch(
        tmp_path,
        payload(sensitive_dirs=[str(root)], seal_windowed_masks=[str(root)]),
    )
    program.place_masks(run)
    assert str(root) not in run.masked_names
    assert program.confirm_sealed_roots(run) is None


@_LINUX_ONLY
def test_the_sealed_root_is_placed_ahead_of_every_mask_that_contains_it(tmp_path: Path) -> None:
    """Only a sealed root with a containing mask ahead of it moves, to just before that mask."""
    root = "/h/.aws/crew/scratch"
    run = launch(
        tmp_path,
        payload(
            sensitive_dirs=["/h/.cache", "/h/.aws", "/h/.aws/crew/tasks", root, "/h/.docker"],
            seal_windowed_masks=[root],
        ),
    )
    assert program._mask_order(run) == [
        "/h/.cache",
        root,
        "/h/.aws",
        "/h/.aws/crew/tasks",
        "/h/.docker",
    ]
    unchanged = ["/h/.cache", root, "/h/.aws", "/h/.docker"]
    run.sensitive_dirs = list(unchanged)
    assert program._mask_order(run) == unchanged
    run.sensitive_dirs = ["/h/.aws", "/h/.kiro/crew/scratch"]
    run.seal_windowed_masks = frozenset({"/h/.kiro/crew/scratch"})
    assert program._mask_order(run) == ["/h/.aws", "/h/.kiro/crew/scratch"]


@_LINUX_ONLY
def test_without_a_containing_mask_nothing_is_held_or_re_attached(tmp_path: Path) -> None:
    """The ordinary layout makes the same mounts as before: no recursive bind of the root."""
    scratch = _Scratch(tmp_path)
    libc = CoveringLibc()
    run = launch(
        tmp_path,
        payload(
            sensitive_dirs=[str(scratch.root)],
            private_dirs=scratch.windows,
            seal_windowed_masks=[str(scratch.root)],
        ),
        libc=libc,
    )
    program.stage_private_windows(run)
    program.mask_sensitive(run)
    assert run.sealed_mounts == {}
    assert not [c for c in libc.calls if c.flags & _MS_REC]


@_LINUX_ONLY
@pytest.mark.parametrize("container", ["after", "before", "ssh"])
def test_the_root_is_re_attached_once_per_container_and_sealed_again(
    tmp_path: Path, container: str
) -> None:
    """Each window is bound once, the recursive bind lands on the root, then the seal again.

    The container's stand-in gets no window of the root's: they come back inside the root.
    The descriptor that held the root is closed once every mask is placed.
    """
    scratch, _aws, fields = _contained(tmp_path, container, windows=True)
    libc = _MovingSealLibc()
    run = launch(tmp_path, payload(**fields), libc=libc)
    try:
        _settled(run)
    finally:
        _unseal(tmp_path)
    root = str(scratch.root)
    assert len(_window_binds(libc, scratch.windows)) == 2
    (reattach,) = [i for i, c in enumerate(libc.calls) if c.flags & _MS_REC]
    assert libc.calls[reattach].target_path == root
    assert max(_read_only_remounts(libc)) > reattach
    assert libc.calls[max(_read_only_remounts(libc))].target_path == root
    assert run.sealed_mounts == {}


@_LINUX_ONLY
def test_a_re_attach_that_does_not_reach_the_sealed_root_refuses_the_spawn(
    tmp_path: Path,
) -> None:
    _scratch, _aws, fields = _contained(tmp_path, "after", windows=True)
    run = launch(tmp_path, payload(**fields), libc=_NoRecursiveBindLibc())
    try:
        message = refusal(program.place_masks, run)
    finally:
        _unseal(tmp_path)
    assert message is not None and "does not reach its mask" in message


@_LINUX_ONLY
def test_a_held_root_that_no_longer_reaches_its_stand_in_refuses_the_spawn(
    tmp_path: Path,
) -> None:
    scratch, _aws, fields = _contained(tmp_path, "after", windows=False)
    run = launch(tmp_path, payload(**fields), libc=CoveringLibc())
    run.masked_names[str(scratch.root)] = (0, 0)
    message = refusal(program._hold_sealed_mount, run, str(scratch.root))
    assert message is not None and "does not reach its sealed mask" in message
    assert run.sealed_mounts == {}


@_LINUX_ONLY
def test_an_opted_in_container_with_the_roots_windows_inside_it_is_not_sealed(
    tmp_path: Path,
) -> None:
    """The container still carries windows, inside the root, so its opt-in seal stays off."""
    scratch, aws, fields = _contained(tmp_path, "after", windows=True)
    fields["seal_windowless_masks"] = [str(aws)]
    libc = _MovingSealLibc()
    run = launch(tmp_path, payload(**fields), libc=libc)
    try:
        _settled(run)
    finally:
        _unseal(tmp_path)
    assert {libc.calls[i].target_path for i in _read_only_remounts(libc)} == {str(scratch.root)}


class _MovingSealLibc(_SealingLibc):
    """``_SealingLibc`` whose binds can move a directory its seal made read-only.

    The fake places a mount by renaming directories, and a directory left mode 0555 by the
    fake seal cannot be renamed, which a real mount never needs. So a sealed directory a
    bind moves is opened for the move and sealed again where it lands.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sealed_ids: set[Any] = set()

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if flags & _MS_REMOUNT and flags & _MS_RDONLY:
            result = super().bound(source, target, fstype, flags)
            self.sealed_ids.add(identity(_resolved(target)))
            return result
        if source is None or not flags & _MS_BIND or flags & _MS_REMOUNT:
            return super().bound(source, target, fstype, flags)
        src, tgt = _resolved(source), _resolved(target)
        for path in (src, tgt):
            if identity(path) in self.sealed_ids:
                os.chmod(path, 0o755)
        try:
            return super().bound(source, target, fstype, flags)
        finally:
            for path in (tgt, self.aside.get(tgt)):
                if path and identity(path) in self.sealed_ids:
                    os.chmod(path, 0o555)


class _NoRecursiveBindLibc(_MovingSealLibc):
    """A libc that reports the recursive bind done and mounts nothing."""

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if flags & _MS_REC:
            return 0
        return super().bound(source, target, fstype, flags)


# --------------------------------------------------------------------------- #
# The operator's read-only layer over the root.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize(
    ("exception", "why"),
    [
        (_ROOT, f"sealed path '{_ROOT}' would be re-opened by it"),
        (_OWN, f"it lies inside sealed subtree '{_ROOT}'"),
        (f"{_ROOT}/runtime-stale", f"it lies inside sealed subtree '{_ROOT}'"),
        (f"{_HOME}/.kiro", f"sealed path '{_ROOT}' would be re-opened by it"),
    ],
    ids=["the root", "a window", "a path below the root", "a directory holding the root"],
)
def test_an_operator_write_exception_never_reopens_the_sealed_root(
    backend: str, exception: str, why: str
) -> None:
    """``agent.sandbox_readonly_exceptions`` at, inside or holding the scratch root is refused.

    The root is a crew mask, which the layer's exceptions are checked against, so the
    operator can make the home read-only around it but cannot re-open any part of it.
    """
    host = _host(
        operator_readonly_probes=(_layer_probe(_HOME),),
        operator_exception_probes=(_layer_probe(exception),),
    )
    plan = _plan(backend, host, extra_private_dirs=(_OWN,))
    assert plan.operator_readonly == (_HOME,)
    assert plan.operator_writable == ()
    assert [r.message % r.args for r in plan.refusals] == [
        f"SECURITY: ignoring agent.sandbox_readonly_exceptions entry '{exception}': {why}"
    ]
    if backend == BACKEND_NAMESPACE:
        assert plan.seal_windowed_masks == (_ROOT,)


def _layer_probe(path: str) -> sandbox_plan.CarveoutProbe:
    return sandbox_plan.CarveoutProbe(raw=path, lexical=path, canonical=path, is_dir=True)
