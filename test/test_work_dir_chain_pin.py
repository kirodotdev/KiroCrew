"""The caller-named work dir's directory chain is pinned across the agent spawn.

A project directory reaches the spawn as a canonical STRING, validated by the
surface that named it (a slot's project, a folder's project, a cron job's
``project_dir``). On Windows a pathname is re-resolved by ``CreateProcess`` and
by the child, and a component swapped for a junction in between aims that
resolution at a share -- an outbound authentication carrying the gateway's
credentials. There is no handle-based current directory, so the spawn pins the
chain instead: every component opened without following and held without
``FILE_SHARE_DELETE`` until the process ends.

The platform gate is a module seam (``_PIN_WORK_DIR_CHAIN``) so the composition
runs here on any host; the helper itself is exercised against the real
filesystem in its POSIX flavour (``O_NOFOLLOW``), which is the same walk.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import client as client_module
from kiro_crew.acp import runtime as runtime_module
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime


def _alive(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


class TestPinDirectoryChain:
    def test_the_windows_pin_names_a_reparse_point_on_its_own_exception(self, monkeypatch):
        # The Windows flavour reads the reparse bit off the HANDLE it opened,
        # closes it and raises. The verdict must ride on the exception type: a
        # caller that re-derived it from the (now unpinned) name after the
        # close could find the junction gone and lose a security denial.
        from types import SimpleNamespace

        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        opened: list[str] = []
        closed: list[int] = []
        attrs = {"v": 0}
        monkeypatch.setattr(
            platform_compat, "_win_open_without_following", lambda p: (opened.append(str(p)), 77)[1]
        )
        monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_file_attributes=attrs["v"]))
        monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))

        attrs["v"] = (
            platform_compat._WIN_FILE_ATTRIBUTE_DIRECTORY
            | platform_compat._WIN_FILE_ATTRIBUTE_REPARSE_POINT
        )
        with pytest.raises(platform_compat.ReparsePointRefused) as info:
            platform_compat.pin_directory(r"C:\repo\junction")
        assert isinstance(info.value, NotADirectoryError), "still the refusal callers know"
        assert info.value.filename == r"C:\repo\junction"
        assert closed == [77], "the handle is closed before the refusal propagates"

        attrs["v"] = 0  # a plain file: the base type, not the reparse verdict
        with pytest.raises(NotADirectoryError) as info2:
            platform_compat.pin_directory(r"C:\repo\file.txt")
        assert not isinstance(info2.value, platform_compat.ReparsePointRefused)

        attrs["v"] = platform_compat._WIN_FILE_ATTRIBUTE_DIRECTORY
        assert platform_compat.pin_directory(r"C:\repo") == 77
        assert opened == [r"C:\repo\junction", r"C:\repo\file.txt", r"C:\repo"]

    def test_pins_one_handle_per_component_rootmost_first(self, tmp_path: Path):
        leaf = tmp_path / "a" / "b" / "c"
        leaf.mkdir(parents=True)
        fds = platform_compat.pin_directory_chain(str(leaf))
        try:
            drive, rest = os.path.splitdrive(str(leaf))
            parts = [p for p in rest.split(os.sep) if p]
            assert len(fds) == len(parts)
            # Each handle is a real directory; the first is the root-most
            # component and the last is the leaf. Compared by identity where the
            # platform reports one (Windows lists a zero inode for directories).
            for fd in fds:
                assert _alive(fd)
            rootmost = Path(drive + os.sep + parts[0])
            if os.name != "nt":
                assert os.fstat(fds[-1]).st_ino == leaf.stat().st_ino
                assert os.fstat(fds[0]).st_ino == rootmost.stat().st_ino
            else:
                assert rootmost.is_dir()
        finally:
            platform_compat.release_directory_chain(fds)
        assert not any(_alive(fd) for fd in fds)

    def test_a_symlink_component_is_refused_and_earlier_pins_released(
        self, tmp_path: Path, monkeypatch
    ):
        real = tmp_path / "real"
        real.mkdir()
        (real / "repo").mkdir()
        link = tmp_path / "link"
        os.symlink(real, link, target_is_directory=True)
        opened: list[int] = []
        real_pin = platform_compat.pin_directory

        def _spy(path):
            fd = real_pin(path)
            opened.append(fd)
            return fd

        monkeypatch.setattr(platform_compat, "pin_directory", _spy)
        with pytest.raises(OSError):  # ELOOP or ENOTDIR, both refusals
            platform_compat.pin_directory_chain(str(link / "repo"))
        # Everything opened before the link was walked was released again.
        assert opened, "the ancestors were opened before the link was reached"
        assert not any(_alive(fd) for fd in opened)

    def test_a_missing_component_is_refused(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            platform_compat.pin_directory_chain(str(tmp_path / "absent" / "deeper"))

    def test_create_missing_makes_the_tail_under_the_held_ancestors_and_pins_it(
        self, tmp_path: Path, monkeypatch
    ):
        # The one by-name write the walk allows: each missing component is
        # created only after every ancestor is pinned, then pinned itself.
        held_at_mkdir: list[int] = []
        real_mkdir = os.mkdir

        def _spy_mkdir(path, *a, **k):
            held_at_mkdir.append(len(fds_so_far))
            return real_mkdir(path, *a, **k)

        fds_so_far: list[int] = []
        real_pin = platform_compat.pin_directory

        def _spy_pin(p):
            fd = real_pin(p)
            fds_so_far.append(fd)
            return fd

        monkeypatch.setattr(os, "mkdir", _spy_mkdir)
        monkeypatch.setattr(platform_compat, "pin_directory", _spy_pin)
        target = tmp_path / "absent" / "deeper"
        depth_of_tmp = len([p for p in tmp_path.parts[1:]])
        fds, bound = platform_compat.pin_directory_chain_bound(str(target), create_missing=True)
        try:
            assert target.is_dir()
            if not platform_compat._BIND_VOLUME_IDENTITY:
                assert bound == str(target)
            else:
                # Real Windows: the walk opened under the volume identity.
                assert bound.startswith("\\\\?\\Volume{") and bound.endswith("absent\\deeper")
            # ``absent`` was created with every prefix of tmp_path pinned; ``deeper``
            # with ``absent`` pinned too. Each created component is then pinned.
            assert held_at_mkdir == [depth_of_tmp, depth_of_tmp + 1]
            assert len(fds) == depth_of_tmp + 2
        finally:
            platform_compat.release_directory_chain(fds)

    def test_create_missing_still_refuses_a_link_at_the_name(self, tmp_path: Path):
        # Only a MISSING name is created; whatever already sits there is judged by
        # the pin, so a link swapped in ahead of the walk is refused, never followed.
        outside = tmp_path / "outside"
        outside.mkdir()
        link = tmp_path / "link"
        os.symlink(outside, link, target_is_directory=True)
        with pytest.raises(OSError):
            platform_compat.pin_directory_chain_bound(str(link / "deeper"), create_missing=True)
        assert not (outside / "deeper").exists(), "nothing may be created through a link"

    def test_without_create_missing_nothing_is_created(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            platform_compat.pin_directory_chain_bound(str(tmp_path / "absent"))
        assert not (tmp_path / "absent").exists()

    def test_on_windows_the_chain_opens_under_the_volumes_own_identity(self, monkeypatch):
        # The letter is a name the mount manager can rebind to a share between
        # two opens; the volume GUID mount-point name is the local volume's own
        # identity. Every prefix is spelled under THAT, never under the letter.
        monkeypatch.setattr(platform_compat, "_BIND_VOLUME_IDENTITY", True)
        asked: list[str] = []
        guid = "\\\\?\\Volume{0f0f0f0f-0000-0000-0000-000000000001}\\"
        monkeypatch.setattr(
            platform_compat, "local_volume_root", lambda drive: (asked.append(drive), guid)[1]
        )
        opened: list[str] = []
        monkeypatch.setattr(
            platform_compat, "pin_directory", lambda p: (opened.append(p), 1000 + len(opened))[1]
        )
        fds, bound = platform_compat.pin_directory_chain_bound("C:\\Users\\u\\repo")
        assert asked == ["C:"]
        root = guid.rstrip("\\")
        assert opened == [
            root + os.sep + "Users",
            root + os.sep + "Users" + os.sep + "u",
            root + os.sep + "Users" + os.sep + "u" + os.sep + "repo",
        ]
        assert fds == [1001, 1002, 1003]
        assert not any(o.startswith("C:") for o in opened)
        # The spelling handed back is the one the leaf was opened under, so a
        # caller naming the directory to the kernel again names the volume.
        assert bound == opened[-1]
        assert platform_compat.pin_directory_chain("C:\\Users\\u\\repo") == [1004, 1005, 1006]

    def test_a_letter_with_no_local_identity_is_refused_before_anything_opens(self, monkeypatch):
        # A mapped network drive has no volume GUID mount point; that absence is
        # the classification, and it is judged before the first open.
        monkeypatch.setattr(platform_compat, "_BIND_VOLUME_IDENTITY", True)
        monkeypatch.setattr(platform_compat, "local_volume_root", lambda drive: None)
        monkeypatch.setattr(
            platform_compat, "pin_directory", lambda p: pytest.fail(f"opened {p!r}")
        )
        with pytest.raises(platform_compat.NotALocalVolume):
            platform_compat.pin_directory_chain("Z:\\repo")

    @pytest.mark.skipif(os.name == "nt", reason="every absolute spelling here carries a letter")
    def test_a_drive_less_path_is_walked_by_name_even_when_binding(
        self, tmp_path: Path, monkeypatch
    ):
        # Nothing to bind without a letter (a POSIX host driving the branch, or a
        # rooted spelling): the walk proceeds under the spelling itself.
        monkeypatch.setattr(platform_compat, "_BIND_VOLUME_IDENTITY", True)
        monkeypatch.setattr(
            platform_compat, "local_volume_root", lambda drive: pytest.fail("no letter to bind")
        )
        leaf = tmp_path / "a"
        leaf.mkdir()
        fds = platform_compat.pin_directory_chain(str(leaf))
        platform_compat.release_directory_chain(fds)
        assert fds

    def test_off_the_rule_the_spelling_handed_back_is_the_one_given(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(platform_compat, "_BIND_VOLUME_IDENTITY", False)
        leaf = tmp_path / "a" / "b"
        leaf.mkdir(parents=True)
        spelled = str(tmp_path) + os.sep + "a" + os.sep + "." + os.sep + "b"
        fds, bound = platform_compat.pin_directory_chain_bound(spelled)
        platform_compat.release_directory_chain(fds)
        assert bound == spelled

    @pytest.mark.skipif(os.name != "nt", reason="the volume identity is a Windows kernel name")
    def test_on_windows_the_bound_spelling_is_a_cwd_the_kernel_accepts(self, tmp_path: Path):
        # The child's current directory is opened by the kernel at creation; the
        # bound spelling must be one it accepts and one that resolves through
        # the volume rather than the letter. Ask a child where it is.
        import subprocess
        import sys

        leaf = tmp_path / "repo"
        leaf.mkdir()
        fds, bound = platform_compat.pin_directory_chain_bound(str(leaf))
        try:
            assert bound.startswith("\\\\?\\Volume{")
            out = subprocess.run(
                [sys.executable, "-c", "import os; print(os.getcwd())"],
                cwd=bound,
                capture_output=True,
                encoding="utf-8",
                check=True,
            ).stdout.strip()
        finally:
            platform_compat.release_directory_chain(fds)
        assert os.path.samefile(out, str(leaf))

    def test_release_tolerates_an_already_closed_handle(self, tmp_path: Path):
        fds = platform_compat.pin_directory_chain(str(tmp_path))
        os.close(fds[-1])
        platform_compat.release_directory_chain(fds)  # no raise

    def test_a_root_only_path_pins_the_root_itself(self, monkeypatch):
        # ``/`` (``C:\``) has no component for the walk to open, and an empty
        # chain has no leaf for the child to enter: a work dir the validators
        # accept would then be refused at spawn. The root is pinned as itself.
        monkeypatch.setattr(platform_compat, "_BIND_VOLUME_IDENTITY", False)
        root = os.path.abspath(os.sep)
        fds, bound = platform_compat.pin_directory_chain_bound(root)
        try:
            assert len(fds) == 1
            assert os.path.samefile(os.fspath(bound), root)
            dup = platform_compat.duplicate_leaf_descriptor(fds)
            os.close(dup)
        finally:
            platform_compat.release_directory_chain(fds)

    def test_a_root_only_bound_spelling_keeps_its_trailing_separator(self, monkeypatch):
        # ``\\?\Volume{GUID}`` without the separator names the volume DEVICE,
        # not its root directory; a child created under it would not start.
        monkeypatch.setattr(platform_compat, "_BIND_VOLUME_IDENTITY", True)
        monkeypatch.setattr(
            platform_compat,
            "local_volume_root",
            lambda drive: "\\\\?\\Volume{0f0f0f0f-0000-0000-0000-000000000001}\\",
        )
        opened: list[str] = []
        monkeypatch.setattr(platform_compat, "pin_directory", lambda p: (opened.append(p), 7)[1])
        fds, bound = platform_compat.pin_directory_chain_bound("C:\\")
        assert fds == [7]
        assert bound == "\\\\?\\Volume{0f0f0f0f-0000-0000-0000-000000000001}" + os.sep
        assert opened == [bound]


@pytest.mark.parametrize(
    "module, make",
    [
        (runtime_module, lambda wd: AcpRuntime(work_dir=wd)),
        (client_module, lambda wd: AcpClient(work_dir=wd)),
    ],
    ids=["runtime", "client"],
)
class TestSpawnOwnersPinTheNamedWorkDir:
    @pytest.fixture(autouse=True)
    def _fake_leaf_duplicate(self, monkeypatch):
        # These tests fake the chain with made-up numbers; a real ``os.dup`` of
        # one would fail EBADF. The duplicate is the leaf + 5000 so an assertion
        # can tell the bound descriptor from the chain it came from.
        monkeypatch.setattr(
            platform_compat, "duplicate_leaf_descriptor", lambda fds: fds[-1] + 5000
        )

    @pytest.mark.asyncio
    async def test_a_named_work_dir_is_pinned_and_held_until_discard(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        pinned: list[str] = []
        asked: list[dict] = []
        released: list[list[int]] = []
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: (pinned.append(p), asked.append(kw), ([1001, 1002], p))[2],
        )
        monkeypatch.setattr(
            platform_compat, "release_directory_chain", lambda fds: released.append(list(fds))
        )
        owner = make(tmp_path)
        await owner._pin_work_dir_chain()
        assert pinned == [str(tmp_path)]
        # A named work dir that does not exist yet is created UNDER the pin
        # (missing tail made below held ancestors), never by a by-name mkdir
        # ahead of it.
        assert asked == [{"create_missing": True}]
        assert owner._work_dir_chain == [1001, 1002]
        if platform_compat.IS_POSIX:
            # The leaf's duplicate is what the child enters and what the ACP
            # cwd is verified against; it is owned apart from the chain.
            assert owner._bound_workspace_fd == 6002
        else:
            assert owner._bound_workspace_fd is None
        # Held: nothing released yet. The pins end with the process.
        assert released == []
        await owner._discard_bound_workspace()
        assert released == [[1001, 1002]]
        assert owner._work_dir_chain == []
        assert owner._bound_workspace_fd is None

    @pytest.mark.asyncio
    async def test_the_gate_is_on_by_default_on_every_platform(self, module, make):
        # The finding this closes is POSIX: a validated directory renamed to a
        # link aimed outside an agent-authored job's allowed root between the
        # validation and the spawn was followed by the create. The gate is a
        # test seam, not a platform read.
        assert module._PIN_WORK_DIR_CHAIN is True

    def test_the_voice_runtime_check_is_asked_about_the_pinned_descriptor(self, module, make):
        # Structural: the macOS overlap check in the spawn body receives the
        # descriptor the pin produced (and the spelling the child is created
        # under), never ``self._work_dir`` by name -- re-opening the name there
        # would be the by-name resolution the pin exists to remove.
        import inspect

        src = inspect.getsource(module)
        calls = [
            ln.strip()
            for ln in src.splitlines()
            if "bind_voice_safe_agent_workspace_async(" in ln and "import" not in ln
        ]
        assert calls, "the spawn body binds the workspace"
        body = src[src.index("bind_voice_safe_agent_workspace_async(") :]
        call = body[: body.index(")")]
        assert "descriptor=self._bound_workspace_fd" in call
        assert "self._work_dir" not in call.replace("self._spawn_work_dir", "")

    @pytest.mark.skipif(os.name == "nt", reason="the descriptor-entered cwd is POSIX")
    @pytest.mark.asyncio
    async def test_on_posix_the_child_enters_the_pinned_inode_not_the_name(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        # The finding itself, against the real filesystem: a project directory
        # validated moments ago is renamed and a link planted at its name,
        # aimed outside. The pinned leaf still IS the validated directory (the
        # inode), the ACP session cwd check refuses the retargeted name rather
        # than handing it to the peer, and nothing here resolved the link.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        monkeypatch.setattr(
            platform_compat, "duplicate_leaf_descriptor", lambda fds: os.dup(fds[-1])
        )
        home = tmp_path / "data-home" / "workspace"
        home.mkdir(parents=True)
        monkeypatch.setattr(module, "default_work_dir", lambda *a, **k: home)
        allowed = tmp_path / "allowed"
        repo = allowed / "repo"
        repo.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        owner = make(repo)
        await owner._pin_work_dir_chain()
        bound = owner._bound_workspace_fd
        assert isinstance(bound, int)
        assert bound not in owner._work_dir_chain, "the child's descriptor is owned apart"
        validated = os.stat(repo)
        # The swap the finding names, after validation and pin, before the spawn.
        moved = allowed / "repo.moved"
        os.rename(repo, moved)
        os.symlink(outside, repo, target_is_directory=True)
        entered = os.fstat(bound)
        assert (entered.st_dev, entered.st_ino) == (validated.st_dev, validated.st_ino)
        from kiro_crew.pinned_fs import fd_real_path

        assert os.path.samefile(fd_real_path(bound), moved)
        # The peer is not handed the retargeted name.
        with pytest.raises(Exception, match="no longer names it"):
            await owner._session_work_dir()
        await owner._discard_bound_workspace()
        assert owner._bound_workspace_fd is None
        assert not _alive(bound)

    @pytest.mark.skipif(os.name == "nt", reason="the descriptor-entered cwd is POSIX")
    @pytest.mark.asyncio
    async def test_on_posix_a_leaf_that_cannot_be_duplicated_fails_the_spawn_and_releases_the_chain(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        released: list[list[int]] = []
        monkeypatch.setattr(
            platform_compat, "pin_directory_chain_bound", lambda p, **kw: ([1001], p)
        )
        monkeypatch.setattr(
            platform_compat, "release_directory_chain", lambda fds: released.append(list(fds))
        )

        def _refuse(fds):
            raise OSError(24, "too many open files")

        monkeypatch.setattr(platform_compat, "duplicate_leaf_descriptor", _refuse)
        owner = make(tmp_path)
        with pytest.raises(RuntimeError, match="could not be pinned"):
            await owner._pin_work_dir_chain()
        assert released == [[1001]], "a failed duplicate must not leak the pins"
        assert owner._bound_workspace_fd is None

    @pytest.mark.asyncio
    async def test_the_default_work_dir_is_not_pinned(self, module, make, monkeypatch):
        # The runtime's own default tree is not operator-named.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: pytest.fail("default work dir must not be pinned"),
        )
        owner = make(None)
        await owner._pin_work_dir_chain()
        assert owner._work_dir_chain == []

    @pytest.mark.asyncio
    async def test_the_pools_explicit_default_cwd_is_not_pinned(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        # The session pool hands the default cwd over explicitly (the realpath
        # of the default work dir). It is the runtime's own tree: pinning it
        # would hold the data home's directories from the gateway for as long
        # as any pooled agent lives, which a shutdown or pod teardown must not
        # meet. Compared canonical-to-canonical on the gateway's OWN default.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        home = tmp_path / "data-home" / "workspace"
        home.mkdir(parents=True)
        monkeypatch.setattr(module, "default_work_dir", lambda *a, **k: home)
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: pytest.fail("the pool default must not be pinned"),
        )
        owner = make(Path(os.path.realpath(home)))
        await owner._pin_work_dir_chain()
        assert owner._work_dir_chain == []

    @pytest.mark.asyncio
    async def test_the_default_exemption_never_resolves_a_name_on_the_windows_rule(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        # The exemption compares the gateway's own default against the named
        # work dir INSIDE the guard against a re-resolved pathname; on the
        # Windows rule that compare is string work, so a by-name resolution
        # of the agent-writable default never happens here.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        monkeypatch.setattr(platform_compat, "_COMPARE_KEY_RESOLVES", False)
        home = tmp_path / "data-home" / "workspace"
        home.mkdir(parents=True)
        monkeypatch.setattr(module, "default_work_dir", lambda *a, **k: home)
        resolved: list[str] = []
        real_realpath = os.path.realpath
        monkeypatch.setattr(
            os.path, "realpath", lambda p, *a, **k: (resolved.append(str(p)), real_realpath(p))[1]
        )
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: pytest.fail("the default must not be pinned"),
        )
        owner = make(home)
        await owner._pin_work_dir_chain()
        assert owner._work_dir_chain == []
        assert str(home) not in resolved

    @pytest.mark.asyncio
    async def test_a_project_is_still_pinned_when_a_default_exists(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        home = tmp_path / "data-home" / "workspace"
        home.mkdir(parents=True)
        monkeypatch.setattr(module, "default_work_dir", lambda *a, **k: home)
        project = tmp_path / "repo"
        project.mkdir()
        pinned: list[str] = []
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: (pinned.append(p), ([7], p))[1],
        )
        owner = make(project)
        await owner._pin_work_dir_chain()
        assert pinned == [str(project)]
        assert owner._work_dir_chain == [7]

    @pytest.mark.asyncio
    async def test_off_the_gate_nothing_is_pinned(self, module, make, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", False)
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: pytest.fail("pinning is a Windows concern"),
        )
        owner = make(tmp_path)
        await owner._pin_work_dir_chain()
        assert owner._work_dir_chain == []

    @pytest.mark.asyncio
    async def test_a_swapped_component_fails_the_spawn_by_name(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        # The string the surface validated does not name what it validated.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)

        def _refuse(p, **kw):
            raise NotADirectoryError(20, "not a real directory", p)

        monkeypatch.setattr(platform_compat, "pin_directory_chain_bound", _refuse)
        owner = make(tmp_path)
        with pytest.raises(RuntimeError, match="could not be pinned") as info:
            await owner._pin_work_dir_chain()
        assert repr(str(tmp_path)) in str(info.value)
        assert "symlink or junction" in str(info.value)

    @pytest.mark.asyncio
    async def test_the_process_and_the_session_cwd_are_the_volume_identity_on_windows(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        # The chain opens under the volume identity, and the child is CREATED
        # under that same spelling: the letter the validated spelling starts
        # from is a name the mount manager can rebind (a same-user operation)
        # and the kernel's open of the cwd at CreateProcess resolves it, so
        # only the identity spelling carries the pins' verdict to that open.
        # The peer, which resolves its session cwd for itself, is handed the
        # same spelling. That the harnesses start under it is verified on a
        # real Windows runner by the ``windows-volume-cwd-probe`` CI job.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        # A Windows scenario: there the child is created by name (no
        # descriptor-entered cwd), so the POSIX hand-off is off here.
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        identity = "\\\\?\\Volume{0f0f0f0f-0000-0000-0000-000000000001}\\repo"
        monkeypatch.setattr(
            platform_compat, "pin_directory_chain_bound", lambda p, **kw: ([1001], identity)
        )
        monkeypatch.setattr(platform_compat, "release_directory_chain", lambda fds: None)
        owner = make(tmp_path)
        await owner._pin_work_dir_chain()
        assert owner._work_dir_chain == [1001], "the volume-bound pins are held"
        assert owner._spawn_work_dir == identity, "the child is created under the identity"
        assert str(await owner._session_work_dir()) == identity
        await owner._discard_bound_workspace()
        # With the process gone the spawn spelling returns to the validated one.
        assert owner._spawn_work_dir == str(tmp_path)

    @pytest.mark.asyncio
    async def test_the_gateways_own_per_key_work_dir_is_not_pinned(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        # A cwd-less cold start names the key's OWN directory under the gateway
        # default (``_session_work_dir(key)``): the gateway's tree, not an
        # operator- or agent-named one. Pinning it would put the walk on every
        # ordinary session start and hold the data home from the gateway.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        home = tmp_path / "data-home" / "workspace"
        per_key = home / "chat-abc123"
        per_key.mkdir(parents=True)
        monkeypatch.setattr(module, "default_work_dir", lambda *a, **k: home)
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: pytest.fail("the gateway's own per-key dir must not be pinned"),
        )
        owner = make(per_key)
        await owner._pin_work_dir_chain()
        assert owner._work_dir_chain == []
        assert owner._bound_workspace_fd is None

    @pytest.mark.asyncio
    async def test_a_named_dir_deeper_under_the_default_is_still_pinned(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        # Only a DIRECT child of the default is the gateway's own root; a
        # directory named deeper in that tree is somebody's choice and is
        # pinned like any other.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        home = tmp_path / "data-home" / "workspace"
        deeper = home / "chat-abc123" / "checkout"
        deeper.mkdir(parents=True)
        monkeypatch.setattr(module, "default_work_dir", lambda *a, **k: home)
        pinned: list[str] = []
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: (pinned.append(p), ([7], p))[1],
        )
        owner = make(deeper)
        await owner._pin_work_dir_chain()
        assert pinned == [str(deeper)]
        assert owner._work_dir_chain == [7]


class TestThePinPrecedesEveryByNameAccess:
    """The chain is pinned BEFORE the spawn touches the work dir by name.

    The mkdir, the voice-runtime check and the skill projection each resolve
    the pathname; a component swapped for a junction aimed at a share is
    followed by whichever runs first, so a pin placed only before the create
    would leave those earlier accesses open.
    """

    @pytest.fixture(autouse=True)
    def _fake_leaf_duplicate(self, monkeypatch):
        monkeypatch.setattr(
            platform_compat, "duplicate_leaf_descriptor", lambda fds: fds[-1] + 5000
        )

    @pytest.mark.parametrize(
        "module, make, body",
        [
            (runtime_module, lambda wd: AcpRuntime(work_dir=wd), "_spawn_admitted"),
            (client_module, lambda wd: AcpClient(work_dir=wd), "_spawn"),
        ],
        ids=["runtime", "client"],
    )
    @pytest.mark.asyncio
    async def test_the_pin_lands_before_the_spawn_body_and_is_released_when_it_fails(
        self, module, make, body, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        order: list[str] = []
        released: list[list[int]] = []
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: (order.append("pin"), ([7001], p))[1],
        )
        monkeypatch.setattr(
            platform_compat, "release_directory_chain", lambda fds: released.append(list(fds))
        )
        owner = make(tmp_path / "repo")

        async def _body():
            order.append("body")
            assert owner._work_dir_chain == [7001], "the chain must be held while the body runs"
            raise RuntimeError("body failed")

        monkeypatch.setattr(owner, body, _body)
        entry = owner._pinned_spawn_admitted if module is runtime_module else owner._pinned_spawn
        with pytest.raises(RuntimeError, match="body failed"):
            await entry()
        assert order == ["pin", "body"]
        assert released == [[7001]], "a failed body must not leak the pins"
        assert owner._work_dir_chain == []

    @pytest.mark.parametrize(
        "module, make, body, entry",
        [
            (
                runtime_module,
                lambda wd: AcpRuntime(work_dir=wd),
                "_spawn_admitted",
                "_pinned_spawn_admitted",
            ),
            (client_module, lambda wd: AcpClient(work_dir=wd), "_spawn", "_pinned_spawn"),
        ],
        ids=["runtime", "client"],
    )
    def test_no_by_name_access_of_the_work_dir_precedes_the_pin(self, module, make, body, entry):
        # Structural: every by-name touch lives in the pinned body; the entry
        # method itself reaches the work dir only through the pin and the
        # discard. A new pre-pin ``self._work_dir`` use in the entry fails here.
        import inspect

        src = inspect.getsource(getattr(type(make(Path("/x"))), entry))
        code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
        offenders = [
            ln for ln in code_lines if "_work_dir" in ln and "_pin_work_dir_chain" not in ln
        ]
        assert offenders == [], offenders
        assert "_pin_work_dir_chain()" in src
        assert f"{body}()" in src
        assert src.index("_pin_work_dir_chain()") < src.index(f"{body}()")

    @pytest.mark.asyncio
    async def test_the_client_does_not_mkdir_a_named_work_dir_before_the_spawn_pins_it(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(client_module, "_PIN_WORK_DIR_CHAIN", True)
        touched: list[str] = []
        real_mkdir = Path.mkdir

        def _spy(self, *a, **k):
            touched.append(str(self))
            return real_mkdir(self, *a, **k)

        monkeypatch.setattr(Path, "mkdir", _spy)
        client = AcpClient(work_dir=tmp_path / "repo")

        async def _stop():
            raise RuntimeError("stop before spawn")

        monkeypatch.setattr(client, "_pinned_spawn", _stop)
        with pytest.raises(RuntimeError, match="stop before spawn"):
            await client.ensure_ready()
        assert str(tmp_path / "repo") not in touched, touched
        assert client._work_dir_ready is True

    @pytest.mark.asyncio
    async def test_off_the_gate_the_client_still_creates_the_work_dir_ahead_of_the_spawn(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(client_module, "_PIN_WORK_DIR_CHAIN", False)
        client = AcpClient(work_dir=tmp_path / "repo")

        async def _stop():
            raise RuntimeError("stop before spawn")

        monkeypatch.setattr(client, "_pinned_spawn", _stop)
        with pytest.raises(RuntimeError, match="stop before spawn"):
            await client.ensure_ready()
        assert (tmp_path / "repo").is_dir()


class TestSpawnComposition:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="the composition is driven through the POSIX spawn seam (a shell-script "
        "stand-in for kiro-cli and asyncio.create_subprocess_exec); the Windows "
        "owner path is pinned by the per-owner tests above",
    )
    @pytest.mark.asyncio
    async def test_the_chain_is_pinned_before_the_process_is_created_and_released_on_failure(
        self, tmp_path: Path, monkeypatch
    ):
        # Through the real ``_pinned_spawn`` -> ``_spawn``: the pin lands BEFORE the workspace
        # preparation (the first by-name access) and the create call, the child
        # is created ENTERING the pinned leaf by descriptor (no by-name ``cwd``
        # reaches the exec), and the failure path releases chain and descriptor,
        # so a refused spawn leaks no handle.
        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n")
        fake.chmod(0o755)
        order: list[str] = []
        released: list[list[int]] = []
        monkeypatch.setattr(client_module, "_PIN_WORK_DIR_CHAIN", True)
        real_pin = platform_compat.pin_directory_chain_bound
        real_release = platform_compat.release_directory_chain
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p, **kw: (order.append("pin"), real_pin(p, **kw))[1],
        )
        monkeypatch.setattr(
            platform_compat,
            "release_directory_chain",
            lambda fds: (released.append(list(fds)), real_release(fds))[0],
        )
        seen: dict = {}

        async def _exec(*a, **k):
            order.append("spawn")
            seen.update(k)
            seen["bound_alive"] = _alive(client._bound_workspace_fd)
            seen["bound"] = client._bound_workspace_fd
            raise RuntimeError("spawn failed")

        real_prepare = AcpClient._prepare_spawn_workspace

        def _prepare(self):
            order.append("workspace")
            return real_prepare(self)

        with (
            patch.object(client_module, "_resolve_kiro_bin", return_value=str(fake)),
            patch.object(
                client_module,
                "wrap_argv",
                side_effect=lambda argv, mode, **kwargs: (list(argv), None),
            ),
            patch.object(client_module, "assert_voice_runtime_outside_agent_workspace"),
            patch.object(client_module, "cgroup_scope_argv", side_effect=lambda argv: list(argv)),
            patch.object(AcpClient, "_prepare_spawn_workspace", _prepare),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_exec)),
        ):
            client = AcpClient(work_dir=tmp_path / "workspace")
            with pytest.raises(RuntimeError, match="spawn failed"):
                await client._pinned_spawn()

        assert order == ["pin", "workspace", "spawn"]
        assert len(released) == 1 and released[0], "the chain was pinned and released once"
        chain = released[0]
        assert client._work_dir_chain == []
        # The child entered the LEAF by descriptor: the exec carried exactly the
        # bound descriptor in pass_fds and no by-name cwd (the shim fchdirs it).
        assert isinstance(seen["bound"], int) and seen["bound_alive"]
        assert seen["bound"] not in chain, "the child's descriptor is owned apart from the chain"
        assert seen["pass_fds"] == (seen["bound"],)
        assert "cwd" not in seen
        assert client._bound_workspace_fd is None, "the failure path released the descriptor"
        assert not _alive(seen["bound"])
        assert all(not _alive(fd) for fd in chain)


class TestCompareKey:
    """The form two directory spellings are compared in, per platform rule."""

    def test_resolves_by_name_off_windows(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(platform_compat, "_COMPARE_KEY_RESOLVES", True)
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        os.symlink(real, link, target_is_directory=True)
        assert platform_compat.compare_key(str(link)) == platform_compat.compare_key(str(real))

    def test_never_touches_the_filesystem_on_the_windows_rule(self, tmp_path: Path, monkeypatch):
        # A caller-named path is already canonical when it reaches a compare;
        # resolving it by name would open a swapped component. String work only.
        monkeypatch.setattr(platform_compat, "_COMPARE_KEY_RESOLVES", False)
        monkeypatch.setattr(
            os.path, "realpath", lambda p, *a, **k: pytest.fail(f"realpath reached for {p!r}")
        )
        spelled = str(tmp_path / "Repo" / ".." / "repo")
        assert platform_compat.compare_key(spelled) == os.path.normcase(os.path.normpath(spelled))

    def test_a_link_spelling_is_a_different_root_on_the_windows_rule(
        self, tmp_path: Path, monkeypatch
    ):
        # Refusing is the safe answer for a compare that gates where an agent
        # runs: the canonical spelling is what every surface stores.
        monkeypatch.setattr(platform_compat, "_COMPARE_KEY_RESOLVES", False)
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        os.symlink(real, link, target_is_directory=True)
        assert platform_compat.compare_key(str(link)) != platform_compat.compare_key(str(real))
