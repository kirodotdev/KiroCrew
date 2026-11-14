"""``external_editor`` -- CLI resolution, socket liveness, and path resolution.

The dashboard handler that calls this module is tested for policy (which status,
which audit line); the resolution mechanics are pinned here, where they live.
Three properties carry the module and each has a reason to exist:

* ``connect()`` is the liveness test, because an IPC socket FILE outlives the
  window that made it -- a host with weeks of uptime carries dozens that answer
  nothing, and picking one by its presence on disk opens into nowhere;
* ``AF_UNIX`` is absent on a platform without unix sockets, so its absence has to
  answer False rather than raise -- the probe runs on every branding read;
* a target is absolutized lexically before anything screens it, so the string
  checked for sensitivity is the string the editor receives.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import external_editor

# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sock_dir() -> Path:
    """A directory short enough to bind an ``AF_UNIX`` socket in.

    ``sun_path`` is capped near 104 bytes, which ``tmp_path`` exceeds under xdist,
    so the socket tests take the suite's short root (see ``tmpdir_helpers``)
    instead. Removed at teardown, because nothing else owns a short-rooted
    directory.
    """
    base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "edsock-", dir=short_tmp_base()))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _touch(path: Path, *, mtime: float, body: str = "") -> Path:
    """A file at *path* with a pinned mtime, parents created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _listener(path: Path) -> socket.socket:
    """A bound, listening AF_UNIX socket at *path*. Caller closes it."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


def _cli(root: Path, commit: str, *, mtime: float, version: str = "1.2.3") -> Path:
    """A launcher stub under a server-bin *root*, shaped like the real one."""
    return _touch(
        root / commit / "bin" / "remote-cli" / "kiro",
        mtime=mtime,
        body=f'#!/usr/bin/env sh\nVERSION="{version}"\nCOMMIT="{commit}"\n',
    )


# ── Socket liveness ──────────────────────────────────────────────────────────


class TestAccepts:
    def test_a_listening_socket_accepts(self, sock_dir: Path) -> None:
        sock_path = sock_dir / "vscode-ipc-live.sock"
        listener = _listener(sock_path)
        try:
            assert external_editor._accepts(str(sock_path)) is True
        finally:
            listener.close()

    def test_an_abandoned_socket_file_does_not_accept(self, sock_dir: Path) -> None:
        """The case the probe exists for: the file survives its window."""
        sock_path = sock_dir / "vscode-ipc-dead.sock"
        listener = _listener(sock_path)
        listener.close()
        assert sock_path.exists()
        assert external_editor._accepts(str(sock_path)) is False

    def test_a_missing_path_does_not_accept(self, tmp_path: Path) -> None:
        assert external_editor._accepts(str(tmp_path / "absent.sock")) is False

    def test_a_platform_without_af_unix_answers_false(self, sock_dir: Path) -> None:
        """Answering rather than raising -- the branding read probes on every load.

        The module's own ``socket`` binding is replaced with one that carries no
        ``AF_UNIX``, which is the genuine shape of the platform this covers,
        instead of mutating the stdlib module for other tests in this worker.
        """
        sock_path = sock_dir / "vscode-ipc-live.sock"
        listener = _listener(sock_path)
        try:
            no_af_unix = SimpleNamespace(SOCK_STREAM=socket.SOCK_STREAM)
            with patch.object(external_editor, "socket", no_af_unix):
                assert external_editor._accepts(str(sock_path)) is False
        finally:
            listener.close()


class TestLiveSocket:
    def test_prefers_the_newest_socket_that_answers(self, sock_dir: Path) -> None:
        older = sock_dir / "vscode-ipc-older.sock"
        newer = sock_dir / "vscode-ipc-newer.sock"
        live_older = _listener(older)
        live_newer = _listener(newer)
        os.utime(older, (1_000.0, 1_000.0))
        os.utime(newer, (2_000.0, 2_000.0))
        try:
            with patch.object(external_editor, "_socket_dirs", return_value=[str(sock_dir)]):
                assert external_editor.live_socket() == str(newer)
        finally:
            live_older.close()
            live_newer.close()

    def test_skips_a_newer_dead_socket_for_an_older_live_one(self, sock_dir: Path) -> None:
        """Ordering is a preference, not the answer -- liveness decides."""
        dead = sock_dir / "vscode-ipc-dead.sock"
        live = sock_dir / "vscode-ipc-live.sock"
        _listener(dead).close()
        listener = _listener(live)
        os.utime(live, (1_000.0, 1_000.0))
        os.utime(dead, (2_000.0, 2_000.0))
        try:
            with patch.object(external_editor, "_socket_dirs", return_value=[str(sock_dir)]):
                assert external_editor.live_socket() == str(live)
        finally:
            listener.close()

    def test_no_socket_answers_none(self, sock_dir: Path) -> None:
        _listener(sock_dir / "vscode-ipc-dead.sock").close()
        with patch.object(external_editor, "_socket_dirs", return_value=[str(sock_dir)]):
            assert external_editor.live_socket() is None


# ── Remote CLI resolution ────────────────────────────────────────────────────


class TestRemoteCli:
    def test_picks_the_newest_unpacked_build(self, tmp_path: Path) -> None:
        """By mtime: the commit hash in the directory name carries no ordering."""
        root = tmp_path / ".kiro-server" / "bin"
        _cli(root, "aaaaaaaa", mtime=1_000.0)
        newest = _cli(root, "00000000", mtime=2_000.0)
        with patch.object(Path, "home", return_value=tmp_path):
            assert external_editor.remote_cli() == newest

    def test_a_host_the_editor_never_connected_to_answers_none(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            assert external_editor.remote_cli() is None


class TestCliVersion:
    def test_reads_the_version_out_of_the_launcher_stub(self, tmp_path: Path) -> None:
        cli = _cli(tmp_path, "abc", mtime=1_000.0, version="9.9.9")
        assert external_editor._cli_version(cli) == "9.9.9"

    def test_a_stub_with_no_version_line_answers_none(self, tmp_path: Path) -> None:
        cli = _touch(tmp_path / "kiro", mtime=1_000.0, body="#!/bin/sh\necho hi\n")
        assert external_editor._cli_version(cli) is None

    def test_an_unreadable_stub_answers_none(self, tmp_path: Path) -> None:
        assert external_editor._cli_version(tmp_path / "absent") is None


# ── editor_status ────────────────────────────────────────────────────────────


class TestEditorStatus:
    def test_no_remote_cli_reports_no_server(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            status = external_editor.editor_status()
        assert status["available"] is False
        assert status["reason"] == external_editor.REASON_NO_SERVER
        assert status["version"] is None

    def test_a_cli_with_no_attached_window_reports_no_window(
        self, tmp_path: Path, sock_dir: Path
    ) -> None:
        """Abandoned socket files on disk count as zero connections, not as one."""
        root = tmp_path / ".kiro-server" / "bin"
        _cli(root, "abc", mtime=1_000.0, version="4.5.6")
        _listener(sock_dir / "vscode-ipc-dead.sock").close()
        with (
            patch.object(Path, "home", return_value=tmp_path),
            patch.object(external_editor, "_socket_dirs", return_value=[str(sock_dir)]),
        ):
            status = external_editor.editor_status()
        assert status["available"] is False
        assert status["reason"] == external_editor.REASON_NO_WINDOW
        assert status["version"] == "4.5.6"
        assert status["connections"] == 0

    def test_a_live_window_reports_ok_and_counts_live_endpoints(
        self, tmp_path: Path, sock_dir: Path
    ) -> None:
        root = tmp_path / ".kiro-server" / "bin"
        _cli(root, "abc", mtime=1_000.0, version="4.5.6")
        live_one = _listener(sock_dir / "vscode-ipc-one.sock")
        live_two = _listener(sock_dir / "vscode-ipc-two.sock")
        _listener(sock_dir / "vscode-ipc-dead.sock").close()
        try:
            with (
                patch.object(Path, "home", return_value=tmp_path),
                patch.object(external_editor, "_socket_dirs", return_value=[str(sock_dir)]),
            ):
                status = external_editor.editor_status()
        finally:
            live_one.close()
            live_two.close()
        assert status["available"] is True
        assert status["reason"] == external_editor.REASON_OK
        assert status["version"] == "4.5.6"
        assert status["connections"] == 2


# ── open_path ────────────────────────────────────────────────────────────────


class TestOpenPathAutoDetected:
    def test_no_remote_cli_is_unavailable(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            with pytest.raises(external_editor.EditorUnavailable):
                external_editor.open_path("/w/a.py")

    def test_no_attached_window_is_unavailable(self, tmp_path: Path) -> None:
        root = tmp_path / ".kiro-server" / "bin"
        _cli(root, "abc", mtime=1_000.0)
        with (
            patch.object(Path, "home", return_value=tmp_path),
            patch.object(external_editor, "live_socket", return_value=None),
        ):
            with pytest.raises(external_editor.EditorUnavailable):
                external_editor.open_path("/w/a.py")

    def test_opens_a_new_window_through_the_live_socket(self, tmp_path: Path) -> None:
        root = tmp_path / ".kiro-server" / "bin"
        cli = _cli(root, "abc", mtime=1_000.0)
        with (
            patch.object(Path, "home", return_value=tmp_path),
            patch.object(external_editor, "live_socket", return_value="/tmp/vscode-ipc-zzq.sock"),
            patch.object(external_editor, "_run") as run,
        ):
            served = external_editor.open_path("/w/a.py")
        assert served == "/tmp/vscode-ipc-zzq.sock"
        assert run.call_args.args[0] == [str(cli), "-n", "/w/a.py"]
        assert run.call_args.kwargs["env"]["VSCODE_IPC_HOOK_CLI"] == ("/tmp/vscode-ipc-zzq.sock")

    def test_reuse_replaces_the_folder_in_the_most_recent_window(self, tmp_path: Path) -> None:
        root = tmp_path / ".kiro-server" / "bin"
        cli = _cli(root, "abc", mtime=1_000.0)
        with (
            patch.object(Path, "home", return_value=tmp_path),
            patch.object(external_editor, "live_socket", return_value="/tmp/vscode-ipc-zzq.sock"),
            patch.object(external_editor, "_run") as run,
        ):
            external_editor.open_path("/w/a.py", reuse=True)
        assert run.call_args.args[0] == [str(cli), "-r", "/w/a.py"]


# ── _run ─────────────────────────────────────────────────────────────────────


class TestRun:
    def test_spawns_the_fixed_argv_with_no_shell(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["zzqedit"], 0, "", "")
            external_editor._run(["zzqedit", "/w/a.py"], env=None)
        assert run.call_args.args[0] == ["zzqedit", "/w/a.py"]
        assert "shell" not in run.call_args.kwargs

    def test_a_nonzero_exit_reports_the_programs_own_message(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["zzqedit"], 1, "", "zzq refused the open"
            )
            with pytest.raises(external_editor.EditorOpenFailed) as err:
                external_editor._run(["zzqedit"], env=None)
        assert "zzq refused the open" in str(err.value)

    def test_a_silent_nonzero_exit_reports_the_code(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["zzqedit"], 3, "", "")
            with pytest.raises(external_editor.EditorOpenFailed) as err:
                external_editor._run(["zzqedit"], env=None)
        assert "3" in str(err.value)

    def test_a_program_that_cannot_start_names_no_path(self) -> None:
        """The clicking user cannot act on a launcher location on the gateway host."""
        with patch("subprocess.run", side_effect=OSError("ENOENT")):
            with pytest.raises(external_editor.EditorOpenFailed) as err:
                external_editor._run(["/opt/zzq/secret/kiro"], env=None)
        assert "/opt/zzq/secret" not in str(err.value)

    def test_a_wedged_socket_times_out_rather_than_hanging(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(["zzqedit"], external_editor._OPEN_TIMEOUT),
        ):
            with pytest.raises(external_editor.EditorOpenFailed) as err:
                external_editor._run(["zzqedit"], env=None)
        assert "acknowledge" in str(err.value)


class TestResolveTarget:
    """A transcript path is text, so the three shapes it can take each get an answer.

    The refusal arm carries the weight: the editor CLI resolves a relative
    argument against the gateway process's working directory, which is a
    directory the text never named, so a relative path with no project directory
    to anchor it opens whichever file happens to sit there.
    """

    def test_an_absolute_path_is_returned_normalized(self) -> None:
        assert external_editor.resolve_target("/srv/app/./main.py") == "/srv/app/main.py"

    def test_a_tilde_expands_against_the_running_users_home(self) -> None:
        assert external_editor.resolve_target("~/notes.md") == os.path.join(
            os.path.expanduser("~"), "notes.md"
        )

    def test_a_relative_path_joins_the_project_directory(self, tmp_path) -> None:
        got = external_editor.resolve_target("website/src/main.tsx", base=str(tmp_path))
        assert got == os.path.join(str(tmp_path), "website/src/main.tsx")

    def test_a_relative_path_with_no_base_is_refused(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KIROCREW_PROJECT_DIR", None)
            assert external_editor.resolve_target("website/src/main.tsx") is None

    def test_the_project_directory_comes_from_the_environment(self, tmp_path) -> None:
        with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            assert external_editor.resolve_target("main.py") == os.path.join(
                str(tmp_path), "main.py"
            )

    def test_a_stale_project_directory_counts_as_absent(self, tmp_path) -> None:
        """A base that is not a directory resolves nothing that exists."""
        with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(tmp_path / "gone")}):
            assert external_editor.resolve_target("main.py") is None

    def test_an_unknown_tilde_user_is_refused_rather_than_joined(self, tmp_path) -> None:
        """expanduser returns `~zzquser/x` unchanged, which must not read as relative."""
        with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(tmp_path)}):
            assert external_editor.resolve_target("~zzquser-not-a-user/x") is None

    def test_an_empty_path_is_refused(self) -> None:
        assert external_editor.resolve_target("   ") is None
