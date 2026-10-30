"""Origin verification of a persisted registry clone.

A persisted clone's origin is verified before any pull, the manifest fed to admission
describes the repo that gets cloned, a mismatched origin is moved aside before the
fresh clone (logging the mismatch) and restored if that clone fails, an unreadable
origin never enters the move-aside path, a stale clone that cannot be moved
aside aborts the install, and the origin read probes the clone's ``.git`` off the
event loop thread.

``test_external_registry.py`` holds the index cache, fetch and lookup tests and names
every sibling; shared fixtures live in ``external_registry_helpers.py``.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from external_registry_helpers import _catalog_absent  # noqa: F401
from external_registry_helpers import _explicit_registry_execution_admission  # noqa: F401


def _real_argv(captured_argv):
    """Strip the spawn shim's own prologue from a captured argv.

    ``create_subprocess_limited`` runs commands through the post-exec shim
    (``python -I -S -c <shim> --rlimits=… -- <real argv>``), so a captured
    spawn does not start with the command itself. Return the argv after the
    ``--`` separator, with argv[0] reduced to its basename (git resolves to an
    absolute path, and to ``git.EXE`` on Windows).
    """
    argv = list(captured_argv)
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    if argv:
        argv[0] = os.path.basename(argv[0]).lower().removesuffix(".exe")
    return argv


def _git_subcommand_index(argv: list[str]) -> int:
    """Index of the git subcommand token: skip ``git`` and every leading ``-c KEY`` pair.

    The hooks/fsmonitor neutralizer splices a variable number of ``-c KEY=VALUE`` pairs
    in after ``git``, so the subcommand sits at a variable position. Matching
    ``clone``/``pull`` as a bare substring of the argv would also accept the token in an
    option value or a URL, so the asserts read the subcommand slot instead.
    """
    i = 1
    while i < len(argv) and argv[i] == "-c":
        i += 2
    return i


class TestStaleCloneOriginVerification:
    """A persisted clone's origin must be verified before any pull.

    The credential posture is decided from git_url, but `git pull origin`
    talks to the CLONE's origin. A stale clone (e.g. a registry replaced
    with the same app name) must be discarded, never pulled credentialed.
    """

    @staticmethod
    def _make_fake_exec(captured, origin_url):
        class _FakeProc:
            returncode = 0

            def __init__(self, out=b""):
                self._out = out

            async def communicate(self):
                return (self._out, None)

        async def _fake_exec(*args, **kwargs):
            captured.setdefault("calls", []).append(list(args))
            # The origin read is sandbox-routed + shim-wrapped now, so match on
            # the real argv rather than the raw prefix.
            if _real_argv(args)[:4] == ["git", "remote", "get-url", "origin"]:
                return _FakeProc(origin_url.encode() + b"\n")
            return _FakeProc()

        return _fake_exec

    @pytest.mark.asyncio
    async def test_mismatched_origin_discards_clone_and_reclones(self, tmp_path):
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app"
        (dest / ".git").mkdir(parents=True)  # looks like an existing clone

        captured: dict = {}
        vetted_url = "ssh://git.example.com/team/MyRegistry"
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard", **_k: (a, None),
            ),
            # cgroup_scope_argv wraps the argv on Linux (identity on macOS) —
            # pin it so the captured commands are platform-independent.
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch.object(
                _asyncio,
                "create_subprocess_exec",
                new=self._make_fake_exec(captured, "ssh://git.example.com/team/OldSibling"),
            ),
        ):
            err = await reg._git_clone_or_pull(vetted_url, "main", dest, [], index_originated=False)

        assert err is None
        argvs = [_real_argv(c) for c in captured["calls"]]
        # Stale clone was removed (rmtree) and a FRESH clone from the vetted
        # URL ran — never a pull against the mismatched origin.
        assert not dest.exists()
        # The hooks/fsmonitor neutralizer is spliced in after ``git``, so the
        # subcommand sits past a variable number of ``-c`` pairs — read the
        # subcommand slot rather than matching ``clone`` as a bare substring, and
        # keep the pin on argv[0] == "git".
        assert any(
            a[0] == "git" and a[_git_subcommand_index(a)] == "clone" and vetted_url in a
            for a in argvs
        )
        assert not any(
            a[0] == "git" and a[_git_subcommand_index(a)] == "pull" for a in argvs
        ), argvs

    @pytest.mark.asyncio
    async def test_matching_origin_pulls_in_place(self, tmp_path):
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app"
        (dest / ".git").mkdir(parents=True)

        captured: dict = {}
        vetted_url = "ssh://git.example.com/team/MyRegistry"
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard", **_k: (a, None),
            ),
            # cgroup_scope_argv wraps the argv on Linux (identity on macOS) —
            # pin it so the captured commands are platform-independent.
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch.object(
                _asyncio,
                "create_subprocess_exec",
                new=self._make_fake_exec(captured, vetted_url),
            ),
        ):
            err = await reg._git_clone_or_pull(vetted_url, "main", dest, [], index_originated=False)

        assert err is None
        assert dest.exists()
        argvs = [_real_argv(c) for c in captured["calls"]]
        # The in-place update is a single ``git pull --ff-only`` against the vetted
        # URL, with the hooks/fsmonitor neutralizer spliced in after ``git`` -- read
        # the subcommand slot past the variable ``-c`` pairs.
        subcommands = [a[_git_subcommand_index(a)] for a in argvs if a and a[0] == "git"]
        assert "pull" in subcommands, argvs
        assert "clone" not in subcommands, argvs
        assert any(
            a[0] == "git" and a[_git_subcommand_index(a)] == "pull" and "--ff-only" in a
            for a in argvs
        ), argvs


class TestManifestOriginGate:
    """The manifest fed to admission must describe the repo that gets cloned.

    ``_fetch_app_manifest`` shortcuts to the persisted clone's ``app.json``,
    but that clone is keyed on app NAME only. When a registry is replaced, a
    checkout of a different repo can sit under the same name — and
    ``_git_clone_or_pull`` then discards it and re-clones from the new URL.
    Reusing the stale ``app.json`` would admit repo A's manifest and run repo
    B's code, so the shortcut only applies when the clone's origin matches.
    """

    @staticmethod
    def _fake_spawn(captured, origin_url, origin_rc=0):
        """One fake for both spawns this path makes.

        The origin read and the manifest clone both go through
        ``create_subprocess_limited``, so the fake discriminates on the argv:
        an origin read answers with *origin_url* (or fails with *origin_rc*),
        anything else is recorded as a clone attempt.
        """

        class _Proc:
            def __init__(self, out=b"", rc=0):
                self._out = out
                self.returncode = rc

            async def communicate(self):
                return (self._out, b"")

        async def _spawn(*args, **kwargs):
            argv = list(args)
            if argv[:4] == ["git", "remote", "get-url", "origin"]:
                captured.setdefault("origin_reads", []).append(argv)
                if origin_rc != 0:
                    return _Proc(b"", origin_rc)
                return _Proc(origin_url.encode() + b"\n")
            captured.setdefault("clones", []).append(argv)
            return _Proc()

        return _spawn

    def _seed_stale_clone(self, tmp_path, marker):
        clone_dir = tmp_path / "app-sources" / "myapp"
        (clone_dir / ".git").mkdir(parents=True)
        (clone_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (clone_dir / "app.json").write_text(
            json.dumps({"name": "myapp", "version": marker}), encoding="utf-8"
        )
        return clone_dir

    @staticmethod
    def _patches(clone_dir, spawn):
        """Common patch set: identity sandbox wrappers + the single spawn fake.

        wrap_argv / cgroup_scope_argv are pinned to identity so the argv the
        fake sees is the real command, platform-independently.
        """
        return (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=clone_dir),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard", **_k: (a, None),
            ),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch("kiro_crew.apps.registry.create_subprocess_limited", new=spawn),
        )

    @pytest.mark.asyncio
    async def test_mismatched_origin_does_not_reuse_persisted_manifest(self, tmp_path):
        """A stale clone's app.json must never be handed to the admission gate."""
        from kiro_crew.apps import registry as reg

        clone_dir = self._seed_stale_clone(tmp_path, "STALE")
        vetted_url = "ssh://git.example.com/team/NewRepo"
        captured: dict = {}
        # clone left behind by the REPLACED registry
        spawn = self._fake_spawn(captured, "ssh://git.example.com/team/OldRepo")

        p1, p2, p3, p4, p5 = self._patches(clone_dir, spawn)
        with p1, p2, p3, p4, p5:
            manifest = await reg._fetch_app_manifest(
                "team/NewRepo", "main", app_name="myapp", git_url=vetted_url
            )

        # The stale manifest was NOT reused...
        assert manifest is None or manifest.get("version") != "STALE"
        # ...and a fresh clone of the vetted URL was attempted instead.
        assert any(vetted_url in c for c in captured.get("clones", []))

    @pytest.mark.asyncio
    async def test_matching_origin_reuses_persisted_manifest(self, tmp_path):
        """The fast path still works when the clone really is that repo."""
        from kiro_crew.apps import registry as reg

        clone_dir = self._seed_stale_clone(tmp_path, "CURRENT")
        vetted_url = "ssh://git.example.com/team/NewRepo"
        captured: dict = {}
        spawn = self._fake_spawn(captured, vetted_url)

        p1, p2, p3, p4, p5 = self._patches(clone_dir, spawn)
        with p1, p2, p3, p4, p5:
            manifest = await reg._fetch_app_manifest(
                "team/NewRepo", "main", app_name="myapp", git_url=vetted_url
            )

        assert manifest is not None and manifest["version"] == "CURRENT"
        # No network clone needed.
        assert not captured.get("clones")

    @pytest.mark.asyncio
    async def test_unreadable_origin_fails_closed(self, tmp_path):
        """An origin that cannot be read is treated as a mismatch, not a match."""
        from kiro_crew.apps import registry as reg

        clone_dir = self._seed_stale_clone(tmp_path, "STALE")
        vetted_url = "ssh://git.example.com/team/NewRepo"
        captured: dict = {}
        spawn = self._fake_spawn(captured, "", origin_rc=128)

        p1, p2, p3, p4, p5 = self._patches(clone_dir, spawn)
        with p1, p2, p3, p4, p5:
            manifest = await reg._fetch_app_manifest(
                "team/NewRepo", "main", app_name="myapp", git_url=vetted_url
            )

        assert manifest is None or manifest.get("version") != "STALE"


class TestStaleCloneDeletionFailsClosed:
    """A stale clone that cannot be moved aside must abort, never be pulled.

    The move-aside rename can fail (a locked ``.git/index.lock`` on Windows, a
    permission error). If the install continued, the surviving clone would be
    pulled from its OWN unverified origin under the credential posture decided
    for the vetted URL, and built under an admission decision made for a
    different repository.
    """

    @pytest.mark.asyncio
    async def test_surviving_stale_clone_aborts_the_install(self, tmp_path):
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app"
        (dest / ".git").mkdir(parents=True)

        captured: dict = {}

        class _Proc:
            returncode = 0

            def __init__(self, out=b""):
                self._out = out

            async def communicate(self):
                return (self._out, b"")

        async def _spawn(*args, **kwargs):
            argv = list(args)
            captured.setdefault("calls", []).append(argv)
            if argv[:4] == ["git", "remote", "get-url", "origin"]:
                return _Proc(b"ssh://git.example.com/team/OldRepo\n")
            return _Proc()

        original_rename = Path.rename

        def _failing_rename(self_path, target):
            if ".stale-" in str(target):
                raise OSError("Permission denied: locked files")
            return original_rename(self_path, target)

        log_lines: list = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard", **_k: (a, None),
            ),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch("kiro_crew.apps.registry.create_subprocess_limited", new=_spawn),
            # rename fails (Windows lock / permissions)
            patch.object(Path, "rename", _failing_rename),
        ):
            err = await reg._git_clone_or_pull(
                "ssh://git.example.com/team/NewRepo", "main", dest, log_lines
            )

        # Fails closed with an error dict...
        assert err is not None and not err["ok"]
        assert err["code"] == "stale_clone_not_removed"
        # ...and never pulls or clones over the surviving stale checkout.
        assert not any(c[:2] == ["git", "pull"] for c in captured["calls"])
        assert not any(c[:2] == ["git", "clone"] for c in captured["calls"])


class TestOriginMismatchDeleteOrder:
    """Regression tests for the delete-order fix: origin-mismatch must move
    aside the old checkout BEFORE cloning, and delete AFTER success.  On clone
    failure/timeout, the old checkout is RESTORED so local changes survive.
    """

    @pytest.mark.asyncio
    async def test_failed_reclone_preserves_old_checkout(self, tmp_path):
        """Origin mismatch + FAILED fresh clone → old checkout still present
        at dest, error dict returned."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        # Marker proving old content survived.
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        class _FailProc:
            returncode = 128  # simulate git clone failure

            async def communicate(self):
                return (b"fatal: remote not found", None)

        async def _fake_create_subprocess(*args, **kwargs):
            # Simulate: dest was moved aside, clone attempted, git fails.
            dest.mkdir(parents=True, exist_ok=True)
            return _FailProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        # Must return error.
        assert err is not None
        assert err["ok"] is False
        assert err["error"] == "git clone failed"
        # The old checkout content MUST still be at dest (restored).
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs visible.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_successful_reclone_replaces_checkout(self, tmp_path):
        """Origin mismatch + successful fresh clone → dest contains the new
        clone, moved-aside path deferred to pending_cleanup."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "old-file.txt").write_text("old", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            # Simulate successful clone: create .git in dest.
            dest.mkdir(parents=True, exist_ok=True)
            new_git = dest / ".git"
            new_git.mkdir(parents=True, exist_ok=True)
            (new_git / "config").write_text(
                f'[remote "origin"]\n\turl = {new_url}\n',
                encoding="utf-8",
            )
            (dest / "new-file.txt").write_text("new", encoding="utf-8")
            return _SuccessProc()

        log_lines: list[str] = []
        pending_cleanup: list[Path] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
                pending_cleanup=pending_cleanup,
            )

        # Success.
        assert err is None
        # New clone content present.
        assert (dest / "new-file.txt").exists()
        assert (dest / "new-file.txt").read_text() == "new"
        # Old file gone (was in the moved-aside dir).
        assert not (dest / "old-file.txt").exists()
        # moved-aside dir deferred to pending_cleanup (not deleted yet).
        assert len(pending_cleanup) == 1
        assert pending_cleanup[0].exists()
        assert ".stale-" in pending_cleanup[0].name

    @pytest.mark.asyncio
    async def test_move_aside_failure_returns_stale_clone_not_removed(self, tmp_path):
        """Move-aside failure (mock rename to raise) → stale_clone_not_removed
        error, mismatched clone never pulled/built."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "intact.txt").write_text("do not touch", encoding="utf-8")

        original_rename = Path.rename

        def _failing_rename(self_path, target):
            if ".stale-" in str(target):
                raise OSError("Permission denied: locked files")
            return original_rename(self_path, target)

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        # Mock create_subprocess_limited to simulate `git remote get-url origin`
        # returning the stale_url (used by _clone_origin_url before the rename).
        class _OriginProc:
            returncode = 0

            async def communicate(self):
                return (stale_url.encode() + b"\n", None)

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=AsyncMock(return_value=_OriginProc()),
            ),
            patch.object(Path, "rename", _failing_rename),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        # Must return stale_clone_not_removed error.
        assert err is not None
        assert err["ok"] is False
        assert err["code"] == "stale_clone_not_removed"
        # The original clone is UNTOUCHED — no data loss.
        assert (dest / "intact.txt").exists()
        assert (dest / "intact.txt").read_text() == "do not touch"

    @pytest.mark.asyncio
    async def test_timeout_reclone_preserves_old_checkout(self, tmp_path):
        """Origin mismatch + clone TIMEOUT → old checkout still present at
        dest (same preservation guarantee as the failure path)."""
        import asyncio as _asyncio

        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        class _HangingProc:
            returncode = None
            pid = 99999

            async def communicate(self):
                raise _asyncio.TimeoutError()

            def kill(self):
                pass

            async def wait(self):
                self.returncode = -9

        async def _fake_create_subprocess(*args, **kwargs):
            # Simulate: dest created (partial clone) then timeout.
            dest.mkdir(parents=True, exist_ok=True)
            return _HangingProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch("kiro_crew.apps.registry._kill_process_group", new=AsyncMock()),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        # Must return timeout error.
        assert err is not None
        assert err["ok"] is False
        assert "timed out" in err["error"]
        # The old checkout content MUST be restored at dest.
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_spawn_exception_restores_old_checkout(self, tmp_path):
        """Origin mismatch + create_subprocess_limited RAISES (spawn failure)
        → old checkout restored at dest, no stale-* leftover.

        Regression: prior to the try/finally guard, a spawn failure after
        move-aside would strand the old checkout under .stale-* permanently.
        """
        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        # Marker proving old content survived.
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        # First call: _clone_origin_url reads the stale origin.
        # Second call: the fresh-clone create_subprocess_limited raises.
        call_count = 0

        class _OriginProc:
            returncode = 0

            async def communicate(self):
                return (stale_url.encode() + b"\n", None)

        async def _subprocess_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # _clone_origin_url: return stale_url
                return _OriginProc()
            # Fresh-clone spawn: simulate OSError (e.g. exec not found)
            raise OSError("No such file or directory: 'git'")

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_subprocess_side_effect,
            ),
        ):
            with pytest.raises(OSError, match="No such file"):
                await reg._git_clone_or_pull(
                    new_url,
                    "main",
                    dest,
                    log_lines,
                    index_originated=False,
                )

        # The old checkout content MUST be restored at dest despite the exception.
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_cancellation_restores_old_checkout(self, tmp_path):
        """Origin mismatch + CancelledError during clone → old checkout
        restored at dest.

        Regression: CancelledError must also trigger the try/finally restore
        path (it propagates through the outer try without hitting the timeout
        or returncode handlers).
        """
        import asyncio as _asyncio

        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        call_count = 0

        class _OriginProc:
            returncode = 0

            async def communicate(self):
                return (stale_url.encode() + b"\n", None)

        async def _subprocess_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _OriginProc()
            # Simulate cancellation during the clone spawn.
            raise _asyncio.CancelledError()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_subprocess_side_effect,
            ),
        ):
            with pytest.raises(_asyncio.CancelledError):
                await reg._git_clone_or_pull(
                    new_url,
                    "main",
                    dest,
                    log_lines,
                    index_originated=False,
                )

        # The old checkout content MUST be restored at dest.
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_cancellation_during_communicate_kills_process(self, tmp_path):
        """Origin mismatch + CancelledError during proc.communicate() → process
        is killed, partial dest removed, old checkout restored.

        Regression test for GPT 5.6 finding: cancellation after the clone
        process starts must not leave the process running or the old checkout
        stranded.
        """
        import asyncio as _asyncio

        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        call_count = 0
        killed = False

        class _OriginProc:
            returncode = 0

            async def communicate(self):
                return (stale_url.encode() + b"\n", None)

        class _CloneProc:
            """Simulates a running clone process that gets cancelled."""

            pid = 99999
            returncode = None

            async def communicate(self):
                # Simulate the outer task being cancelled during this await.
                raise _asyncio.CancelledError()

            async def wait(self):
                self.returncode = -9
                return -9

            def kill(self):
                nonlocal killed
                killed = True

        async def _subprocess_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                # Call 1: _clone_origin_url (reads the stale origin)
                return _OriginProc()
            # Call 2: the actual fresh-clone spawn
            return _CloneProc()

        async def _fake_kill_process_group(proc):
            nonlocal killed
            killed = True

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_subprocess_side_effect,
            ),
            patch(
                "kiro_crew.apps.registry._kill_process_group",
                side_effect=_fake_kill_process_group,
            ),
        ):
            with pytest.raises(_asyncio.CancelledError):
                await reg._git_clone_or_pull(
                    new_url,
                    "main",
                    dest,
                    log_lines,
                    index_originated=False,
                )

        # The process MUST have been killed.
        assert killed, "Clone process was not killed on cancellation"
        # The old checkout content MUST be restored at dest.
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0


class TestUnreadableOriginAbort:
    """Regression: unreadable origin must NOT enter destructive move-aside path.

    A checkout with a corrupt .git/config or missing remote enters the
    move-aside → re-clone → delete path, permanently losing local edits even
    though the checkout might be the right repo.
    """

    @pytest.mark.asyncio
    async def test_unreadable_origin_returns_error_without_destroying(self, tmp_path):
        """Checkout with unreadable origin → error, dest untouched."""
        import kiro_crew.apps.registry as reg

        git_url = "https://example.com/app.git"
        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        # Corrupt config — _clone_origin_url will return None.
        (git_dir / "config").write_text("garbage", encoding="utf-8")
        (dest / "local-edits.txt").write_text("precious work", encoding="utf-8")

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=None),
            ),
        ):
            err = await reg._git_clone_or_pull(
                git_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        # Must return the unreadable_clone_origin error (slug in `code`,
        # human sentence in `error` — the install banner renders `error`).
        assert err is not None
        assert err["ok"] is False
        assert err["code"] == "unreadable_clone_origin"
        # Dest is UNTOUCHED — no rename, no rmtree, no re-clone.
        assert (dest / "local-edits.txt").exists()
        assert (dest / "local-edits.txt").read_text() == "precious work"
        # No stale-* dirs created.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_readable_different_origin_still_reclones(self, tmp_path):
        """Readable but different origin → move-aside/re-clone path (not blocked)."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old.example.com/app.git"
        new_url = "https://new.example.com/app.git"
        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            return _SuccessProc()

        log_lines: list[str] = []
        pending_cleanup: list[Path] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
                pending_cleanup=pending_cleanup,
            )

        # Success — re-clone worked.
        assert err is None
        # moved-aside path is deferred.
        assert len(pending_cleanup) == 1


class TestOriginMismatchLogsBeforeMoveAside:
    """Regression (First Principles round-6 Watch): the origin-mismatch
    move-aside path must announce WHY it is replacing the checkout -- naming
    the mismatched origin -- for parity with the branch-mismatch path. A
    branch rebuild had dropped that log line, leaving the origin-mismatch
    re-clone silent while the branch-mismatch path still logged its reason.
    """

    @pytest.mark.asyncio
    async def test_origin_mismatch_logs_the_mismatched_origin(self, tmp_path):
        import kiro_crew.apps.registry as reg

        existing_origin = "https://example.com/old-owner/app.git"
        requested_url = "https://example.com/new-owner/app.git"
        dest = tmp_path / "app-sources" / "app"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {existing_origin}\n',
            encoding="utf-8",
        )
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            # The re-clone recreates the checkout at the requested origin.
            dest.mkdir(parents=True, exist_ok=True)
            new_git = dest / ".git"
            new_git.mkdir(parents=True, exist_ok=True)
            (new_git / "config").write_text(
                f'[remote "origin"]\n\turl = {requested_url}\n',
                encoding="utf-8",
            )
            return _SuccessProc()

        log_lines: list[str] = []
        pending_cleanup: list[Path] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=existing_origin),
            ),
        ):
            err = await reg._git_clone_or_pull(
                requested_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
                pending_cleanup=pending_cleanup,
            )

        assert err is None
        # The stale clone was moved aside for re-clone.
        assert len(pending_cleanup) == 1
        assert ".stale-" in pending_cleanup[0].name
        # The reason is logged, naming the mismatched origin, for parity with
        # the branch-mismatch path. Assert on log_lines, never raw git argv.
        origin_log = [
            line
            for line in log_lines
            if "Existing clone origin" in line and "does not match" in line
        ]
        assert origin_log, f"expected an origin-mismatch log line, got {log_lines!r}"
        assert existing_origin in origin_log[0]
        assert "moving aside stale clone for re-clone" in origin_log[0]


class TestCloneOriginProbeOffLoop:
    """The ``.git`` probe in ``_clone_origin_url`` runs off the event loop thread.

    The checkout sits in the data home's apps directory, and a stat on a mount that
    stopped answering blocks for as long as the kernel does: on the loop that stalls
    every session, so the probe hops to a worker thread before the git spawn.
    """

    @staticmethod
    def _track_git_probe(dest: Path, threads: set[int]):
        real_is_dir = Path.is_dir

        def tracked_is_dir(self, *a, **kw):
            if self == dest / ".git":
                threads.add(threading.get_ident())
            return real_is_dir(self, *a, **kw)

        return patch.object(Path, "is_dir", tracked_is_dir)

    @pytest.mark.asyncio
    async def test_missing_git_dir_returns_none_without_spawning(self, tmp_path):
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app"
        dest.mkdir()  # a directory, but not a clone
        loop_thread = threading.get_ident()
        probe_threads: set[int] = set()
        spawn = AsyncMock()
        with (
            self._track_git_probe(dest, probe_threads),
            patch("kiro_crew.apps.registry.create_subprocess_limited", new=spawn),
        ):
            assert await reg._clone_origin_url(dest) is None

        spawn.assert_not_called()
        assert probe_threads, "the .git probe never ran"
        assert loop_thread not in probe_threads, "the .git probe ran on the event loop thread"

    @pytest.mark.asyncio
    async def test_existing_clone_reads_origin_after_off_loop_probe(self, tmp_path):
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app"
        (dest / ".git").mkdir(parents=True)
        origin = "ssh://git.example.com/team/MyRegistry"
        loop_thread = threading.get_ident()
        probe_threads: set[int] = set()
        spawned: list[list[str]] = []

        class _OriginProc:
            returncode = 0

            async def communicate(self):
                return (origin.encode() + b"\n", None)

        async def _fake_spawn(*argv, **kwargs):
            spawned.append(list(argv))
            assert kwargs["cwd"] == str(dest)
            return _OriginProc()

        with (
            self._track_git_probe(dest, probe_threads),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard", **_k: (a, None),
            ),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch("kiro_crew.apps.registry.create_subprocess_limited", side_effect=_fake_spawn),
        ):
            assert await reg._clone_origin_url(dest) == origin

        assert [_real_argv(a) for a in spawned] == [["git", "remote", "get-url", "origin"]]
        assert probe_threads, "the .git probe never ran"
        assert loop_thread not in probe_threads, "the .git probe ran on the event loop thread"
