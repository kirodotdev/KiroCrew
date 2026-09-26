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
    def test_on_windows_a_process_is_created_under_the_volume_identity(self, tmp_path: Path):
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


@pytest.mark.parametrize(
    "module, make",
    [
        (runtime_module, lambda wd: AcpRuntime(work_dir=wd)),
        (client_module, lambda wd: AcpClient(work_dir=wd)),
    ],
    ids=["runtime", "client"],
)
class TestSpawnOwnersPinTheNamedWorkDir:
    @pytest.mark.asyncio
    async def test_a_named_work_dir_is_pinned_and_held_until_discard(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        pinned: list[str] = []
        released: list[list[int]] = []
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p: (pinned.append(p), ([1001, 1002], p))[1],
        )
        monkeypatch.setattr(
            platform_compat, "release_directory_chain", lambda fds: released.append(list(fds))
        )
        owner = make(tmp_path)
        await owner._pin_work_dir_chain()
        assert pinned == [str(tmp_path)]
        assert owner._work_dir_chain == [1001, 1002]
        # Held: nothing released yet. The pins end with the process.
        assert released == []
        await owner._discard_bound_workspace()
        assert released == [[1001, 1002]]
        assert owner._work_dir_chain == []

    @pytest.mark.asyncio
    async def test_the_default_work_dir_is_not_pinned(self, module, make, monkeypatch):
        # The runtime's own default tree is not operator-named.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p: pytest.fail("default work dir must not be pinned"),
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
            lambda p: pytest.fail("the pool default must not be pinned"),
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
            lambda p: pytest.fail("the default must not be pinned"),
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
            platform_compat, "pin_directory_chain_bound", lambda p: (pinned.append(p), ([7], p))[1]
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
            lambda p: pytest.fail("pinning is a Windows concern"),
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

        def _refuse(p):
            raise NotADirectoryError(20, "not a real directory", p)

        monkeypatch.setattr(platform_compat, "pin_directory_chain_bound", _refuse)
        owner = make(tmp_path)
        with pytest.raises(RuntimeError, match="could not be pinned") as info:
            await owner._pin_work_dir_chain()
        assert repr(str(tmp_path)) in str(info.value)
        assert "symlink or junction" in str(info.value)

    @pytest.mark.asyncio
    async def test_the_process_is_created_under_the_bound_spelling(
        self, module, make, tmp_path: Path, monkeypatch
    ):
        # The chain hands back the spelling it opened under (the volume identity
        # on Windows); the process is created THERE, while the peer-facing
        # session cwd remains the validated work dir.
        monkeypatch.setattr(module, "_PIN_WORK_DIR_CHAIN", True)
        identity = "\\\\?\\Volume{0f0f0f0f-0000-0000-0000-000000000001}\\repo"
        monkeypatch.setattr(
            platform_compat, "pin_directory_chain_bound", lambda p: ([1001], identity)
        )
        monkeypatch.setattr(platform_compat, "release_directory_chain", lambda fds: None)
        owner = make(tmp_path)
        await owner._pin_work_dir_chain()
        assert owner._spawn_work_dir == identity
        assert str(await owner._session_work_dir()) == str(tmp_path)
        await owner._discard_bound_workspace()
        assert owner._spawn_work_dir == str(tmp_path)


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
        # Through the real ``_spawn``: the pin lands BEFORE the create call and
        # the failure path releases it, so a refused spawn leaks no handle.
        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n")
        fake.chmod(0o755)
        order: list[str] = []
        released: list[list[int]] = []
        monkeypatch.setattr(client_module, "_PIN_WORK_DIR_CHAIN", True)
        monkeypatch.setattr(
            platform_compat,
            "pin_directory_chain_bound",
            lambda p: (order.append("pin"), ([4242], p))[1],
        )
        monkeypatch.setattr(
            platform_compat, "release_directory_chain", lambda fds: released.append(list(fds))
        )

        async def _exec(*a, **k):
            order.append("spawn")
            raise RuntimeError("spawn failed")

        with (
            patch.object(client_module, "_resolve_kiro_bin", return_value=str(fake)),
            patch.object(
                client_module,
                "wrap_argv",
                side_effect=lambda argv, mode, **kwargs: (list(argv), None),
            ),
            patch.object(client_module, "assert_voice_runtime_outside_agent_workspace"),
            patch.object(client_module, "cgroup_scope_argv", side_effect=lambda argv: list(argv)),
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_exec)),
        ):
            client = AcpClient(work_dir=tmp_path / "workspace")
            with pytest.raises(RuntimeError, match="spawn failed"):
                await client._spawn()

        assert order == ["pin", "spawn"]
        assert released == [[4242]]
        assert client._work_dir_chain == []


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
