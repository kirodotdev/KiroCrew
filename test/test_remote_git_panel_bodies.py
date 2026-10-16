"""Guest-side Git body reads for the remote Git panel: bounded blob and
worktree reads, mode and rename headers, CRLF handling, gitlinks, and key
material in over-cap and complete files."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from test_coder_session_launch import PEM_BEGIN_RSA, PEM_END_RSA, _plain_git_repo


@pytest.mark.parametrize("deleted", [False, True])
def test_git_blob_reads_stay_bounded_and_send_no_over_cap_body(tmp_path, monkeypatch, deleted):
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    body = b"a whole line\n" * wt.MAX_READ_BYTES
    path = root / "large.txt"
    path.write_bytes(body)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "large blob")
    if deleted:
        path.unlink()
    real_popen = subprocess.Popen
    reads, killed, spawned = [], [], []
    stop_read = wt._stop_read

    class BoundedPipe:
        def __init__(self, stream):
            self.stream = stream
            self.total = 0

        def read(self, size=-1):
            assert 0 < size <= wt.MAX_READ_BYTES + 1 - self.total
            chunk = self.stream.read(size)
            self.total += len(chunk)
            reads.append(self.total)
            return chunk

        def close(self):
            self.stream.close()

    class BlobProcess(real_popen):
        def __init__(self, argv, **kwargs):
            super().__init__(argv, **kwargs)
            self.is_blob = "cat-file" in argv and "blob" in argv
            if self.is_blob:
                spawned.append((argv, kwargs))
                self.stdout = BoundedPipe(self.stdout)

        def communicate(self, *args, **kwargs):
            assert not self.is_blob, "cat-file must not buffer its whole blob in communicate"
            return super().communicate(*args, **kwargs)

    def kill(proc):
        if proc.is_blob:
            killed.append(proc.returncode)
        stop_read(proc)

    monkeypatch.setattr(wt.subprocess, "Popen", BlobProcess)
    monkeypatch.setattr(wt, "_stop_read", kill)
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "large.txt"})
    assert result["git"] is True and result["truncated"] is True
    assert reads and max(reads) == wt.MAX_READ_BYTES + 1
    assert killed == [None]
    assert "old" not in result and "new" not in result
    assert result["old_mode"] == "100644"
    assert result["new_mode"] == (None if deleted else "100644")
    assert len(spawned) == 1
    argv, kwargs = spawned[0]
    assert "--literal-pathspecs" in argv and "core.fsmonitor=" in argv
    assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
    assert kwargs["start_new_session"] is True and kwargs["cwd"] == str(root)


@pytest.mark.parametrize("op", ["git_log", "git_status"])
def test_a_huge_history_or_status_read_is_streamed_to_the_cap(tmp_path, monkeypatch, op):
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    huge = "s" * (2 * wt.MAX_READ_BYTES)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    git(root, "add", ".")
    # One commit subject, or one untracked file list, larger than the read cap.
    (tmp_path / "message").write_text(huge, encoding="utf-8")
    git(root, "commit", "-q", "-F", str(tmp_path / "message"))
    for i in range(2 * wt.MAX_READ_BYTES // 200):
        (root / f"untracked-{i:05d}-{'n' * 180}").write_text("", encoding="utf-8")
    real_popen = subprocess.Popen
    buffered = []

    class Streamed(real_popen):
        def communicate(self, *args, **kwargs):
            buffered.append(self.args)
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(wt.subprocess, "Popen", Streamed)
    result = wt.browse(wt.Workspace(str(root)), {"op": op})
    assert result["git"] is True and result["truncated"] is True, result
    assert 0 < len(result["output"].encode()) <= wt.MAX_READ_BYTES
    # Only the small config probe may buffer; the final read is streamed and capped.
    assert not any(("log" if op == "git_log" else "status") in argv for argv in buffered)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest workspaces")
def test_a_guest_with_timeout_but_no_git_reports_git_missing(tmp_path, monkeypatch):
    import shutil

    from kiro_crew.remote_session import workspace_tools as wt

    timer = shutil.which("timeout")
    if timer is None:
        pytest.skip("needs coreutils timeout")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "timeout").symlink_to(timer)
    monkeypatch.setenv("PATH", str(bin_dir))
    for op in ("git_status", "git_log", "git_bodies"):
        result = wt.browse(wt.Workspace(str(tmp_path)), {"op": op, "path": "a.txt"})
        assert result["code"] == "git_failed", result
        assert result["error_text"] == "git is not installed", result


def test_git_blob_stream_timeout_stops_before_reaping(tmp_path, monkeypatch):
    import io
    import subprocess
    import threading

    from kiro_crew.remote_session import workspace_tools as wt
    from kiro_crew.testing.ids import UNALLOCATABLE_PID

    events = []
    release = threading.Event()

    class WaitingPipe(io.BytesIO):
        def read(self, size=-1):
            assert size == wt.MAX_READ_BYTES + 1
            assert release.wait(30), "group cleanup did not release the pipe reader"
            return b""

    def wait(timeout):
        events.append("reaped")
        process.returncode = -9
        return -9

    process = SimpleNamespace(
        pid=UNALLOCATABLE_PID,
        returncode=None,
        stdout=WaitingPipe(),
        stderr=io.BytesIO(),
        wait=wait,
    )

    def kill(proc):
        assert proc is process and proc.returncode is None
        events.append("stopped")
        release.set()

    def spawn(argv, **kwargs):
        assert kwargs["start_new_session"] is True
        return process

    # Without coreutils timeout the read's own deadline is the only one.
    monkeypatch.setattr(wt.shutil, "which", lambda name: None)
    monkeypatch.setattr(wt.subprocess, "Popen", spawn)
    monkeypatch.setattr(wt, "_stop_read", kill)
    with pytest.raises(subprocess.TimeoutExpired):
        wt._run_grouped(
            ["git", "cat-file", "blob", "a" * 40],
            cwd=str(tmp_path),
            env={},
            timeout=0,
            stderr=subprocess.PIPE,
            stdout_limit=wt.MAX_READ_BYTES + 1,
        )
    assert events == ["stopped", "reaped"]
    assert process.stdout.closed and process.stderr.closed


def _over_cap_lines(prefix, count):
    return b"".join(f"{prefix}-{i:04d} ".encode() + b"x" * 84 + b"\n" for i in range(count))


@pytest.mark.parametrize("change", ["insert", "shrink", "both"])
def test_an_over_cap_tracked_file_sends_no_body_and_no_hunks(tmp_path, change):
    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    old = _over_cap_lines("original", 2000 if change == "insert" else 3000)
    new = {
        "insert": _over_cap_lines("inserted", 50) + old,
        "shrink": old[: old.index(b"original-0010")],
        "both": _over_cap_lines("replaced", 3000),
    }[change]
    assert len(old) + len(new) > wt.MAX_READ_BYTES
    (root / "large.txt").write_bytes(old)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    (root / "large.txt").write_bytes(new)
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "large.txt"})
    assert result["truncated"] is True and result["git"] is True
    assert result["path"] == "large.txt"
    assert (result["old_mode"], result["new_mode"]) == ("100644", "100644")
    assert "old" not in result and "new" not in result
    view = _git_diff_from_bodies(result, "large.txt")
    assert view["truncated"] is True and view["output"] == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_an_over_cap_file_still_shows_its_mode_and_rename_header(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    body = _over_cap_lines("original", 3000).decode()
    root, git = _committed_file(tmp_path, "a.sh", body)
    git(root, "mv", "a.sh", "b.sh")
    (root / "b.sh").write_text(body + "tail\n", encoding="utf-8")
    (root / "b.sh").chmod(0o755)
    result = wt.browse(
        wt.Workspace(str(root)), {"op": "git_bodies", "path": "b.sh", "from": "a.sh"}
    )
    assert result["truncated"] is True and result["old_path"] == "a.sh"
    patch = _gateway_diff(root, "b.sh", "a.sh")
    assert patch == (
        "diff --git a/a.sh b/b.sh\nold mode 100644\nnew mode 100755\n"
        "rename from a.sh\nrename to b.sh\n"
    )
    (root / "added.txt").write_text(body, encoding="utf-8")
    assert _gateway_diff(root, "added.txt") == (
        "diff --git a/added.txt b/added.txt\nnew file mode 100644\n"
    )


def test_a_truncated_reply_carrying_bodies_builds_no_hunks():
    from kiro_crew.coder.session_browse import _git_diff_from_bodies

    old = "".join(f"line {i}\n" for i in range(20))
    reply = {"git": True, "path": "f", "truncated": True, "old": old, "new": old[:40]}
    view = _git_diff_from_bodies(reply, "f")
    assert view == {"git": True, "truncated": True, "output": ""}
    reply.update(old_mode="100644", new_mode=None)
    output = _git_diff_from_bodies(reply, "f")["output"]
    assert output == "diff --git a/f b/f\ndeleted file mode 100644\n"
    assert "line" not in output


def _committed_file(tmp_path, name="tracked.sh", body="echo hi\n"):
    root, git = _plain_git_repo(tmp_path)
    (root / name).write_text(body, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    return root, git


def _gateway_diff(root, path, source=None):
    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.remote_session import workspace_tools as wt

    request = {"op": "git_bodies", "path": path}
    if source is not None:
        request["from"] = source
    result = wt.browse(wt.Workspace(str(root)), request)
    assert result["git"] is True, result
    return _git_diff_from_bodies(result, path)["output"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_a_mode_only_change_shows_git_mode_lines(tmp_path):
    import subprocess

    root, git = _committed_file(tmp_path)
    (root / "tracked.sh").chmod(0o755)
    patch = _gateway_diff(root, "tracked.sh")
    assert patch == "diff --git a/tracked.sh b/tracked.sh\nold mode 100644\nnew mode 100755\n"
    real = subprocess.run(
        ["git", "-C", str(root), "diff", "--no-color", "HEAD", "--", "tracked.sh"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert real.startswith(patch)
    (root / "tracked.sh").write_text("echo bye\n", encoding="utf-8")
    patch = _gateway_diff(root, "tracked.sh")
    assert "old mode 100644\nnew mode 100755\n" in patch
    assert "-echo hi\n+echo bye\n" in patch


def test_a_mode_change_git_ignores_is_not_shown(tmp_path):
    root, git = _committed_file(tmp_path)
    git(root, "config", "core.fileMode", "false")
    (root / "tracked.sh").chmod(0o755)
    assert _gateway_diff(root, "tracked.sh") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_new_and_deleted_files_name_their_mode(tmp_path):
    root, git = _committed_file(tmp_path)
    (root / "added.sh").write_text("x\n", encoding="utf-8")
    (root / "added.sh").chmod(0o755)
    assert "\nnew file mode 100755\n--- /dev/null\n+++ b/added.sh\n" in _gateway_diff(
        root, "added.sh"
    )
    (root / "tracked.sh").unlink()
    assert "\ndeleted file mode 100644\n--- a/tracked.sh\n+++ /dev/null\n" in _gateway_diff(
        root, "tracked.sh"
    )


def test_a_renamed_file_diffs_against_its_source(tmp_path):
    body = "".join(f"line {i}\n" for i in range(20))
    root, git = _committed_file(tmp_path, "a.txt", body)
    git(root, "mv", "a.txt", "b.txt")
    (root / "b.txt").write_text(body.replace("line 7\n", "line seven\n"), encoding="utf-8")
    # Without the source the HEAD side is absent: a whole-file addition.
    assert "--- /dev/null" in _gateway_diff(root, "b.txt")
    patch = _gateway_diff(root, "b.txt", "a.txt")
    assert patch.startswith(
        "diff --git a/a.txt b/b.txt\nrename from a.txt\nrename to b.txt\n"
        "--- a/a.txt\n+++ b/b.txt\n"
    ), patch
    changed = [line for line in patch.splitlines()[5:] if line[:1] in "+-"]
    assert changed == ["-line 7", "+line seven"]
    # A pure rename is still a difference.
    (root / "b.txt").write_text(body, encoding="utf-8")
    assert _gateway_diff(root, "b.txt", "a.txt") == (
        "diff --git a/a.txt b/b.txt\nrename from a.txt\nrename to b.txt\n"
    )


def test_a_rename_source_is_confined_to_the_workspace(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _committed_file(tmp_path, "a.txt", "x\n")
    ws = wt.Workspace(str(root))
    for source in ("../outside.txt", str(tmp_path / "outside.txt")):
        result = wt.browse(ws, {"op": "git_bodies", "path": "a.txt", "from": source})
        assert "outside" in result["error"] and "old" not in result, result
    refused = wt.browse(ws, {"op": "git_bodies", "path": "a", "from": 1})
    assert "from must be a path" in refused["error"]
    # A source HEAD does not hold reads the target's own entry.
    result = wt.browse(ws, {"op": "git_bodies", "path": "a.txt", "from": "gone.txt"})
    assert "old_path" not in result and result["old"] == "x\n"


@pytest.mark.parametrize(
    "attributes,autocrlf,normalise",
    [
        ("* text eol=crlf", "false", True),
        ("* text", "false", True),
        ("* text=auto", "false", True),
        ("* eol=crlf", "false", True),
        ("* eol=lf", "false", True),
        ("", "true", True),
        ("", "input", True),
        ("* -text eol=crlf", "true", False),
        ("", "false", False),
    ],
)
def test_git_body_crlf_normalisation_matches_git(
    tmp_path, monkeypatch, attributes, autocrlf, normalise
):
    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-config"))
    git(root, "config", "core.autocrlf", autocrlf)
    (root / ".gitattributes").write_text(attributes + "\n", encoding="utf-8")
    (root / "f.txt").write_bytes(b"one\ntwo\nthree\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    (root / "f.txt").write_bytes(b"one\r\nchanged\r\nthree\r\n")
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "f.txt"})
    expected = "one\nchanged\nthree\n" if normalise else "one\r\nchanged\r\nthree\r\n"
    assert result["new"] == expected
    assert result["old"] == "one\ntwo\nthree\n" and result["truncated"] is False
    patch = _git_diff_from_bodies(result, "f.txt")["output"]
    if normalise:
        changes = [
            s
            for s in patch.splitlines()
            if s[:1] in ("+", "-") and not s.startswith(("+++", "---"))
        ]
        assert changes == ["-two", "+changed"]


def test_git_auto_text_keeps_crlf_when_a_nul_is_beyond_binary_probe(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    (root / ".gitattributes").write_bytes(b"* text=auto\n")
    body = b"line\r\n" * 2000 + b"\0tail\r\n"
    (root / "f.txt").write_bytes(body)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "f.txt"})
    assert result["old"] == result["new"] == body.decode()
    assert result["truncated"] is False


@pytest.mark.parametrize("attributes,autocrlf", [("* text=auto", "false"), ("", "true")])
def test_auto_crlf_keeps_a_file_committed_with_crlf_as_git_does(
    tmp_path, monkeypatch, attributes, autocrlf
):
    root, git = _plain_git_repo(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-config"))
    # Commit the CRLF blob first, then turn automatic conversion on.
    (root / "f.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "config", "core.autocrlf", autocrlf)
    (root / ".gitattributes").write_text(attributes + "\n", encoding="utf-8")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-q", "-m", "attributes")
    (root / "f.txt").write_bytes(b"one\r\nchanged\r\nthree\r\n")
    patch = _gateway_diff(root, "f.txt")
    lines = patch.split("\n")
    removed = [line for line in lines if line.startswith("-") and line != "--- a/f.txt"]
    added = [line for line in lines if line.startswith("+") and line != "+++ b/f.txt"]
    assert removed == ["-two\r"] and added == ["+changed\r"], patch
    real = git(root, "diff", "--no-color", "--numstat", "HEAD", "--", "f.txt")
    assert real.split()[:2] == ["1", "1"]


@pytest.mark.parametrize("on_disk", [True, False])
def test_a_gitlink_replaced_by_staged_files_is_refused_before_any_diff(
    tmp_path, monkeypatch, on_disk
):
    import shutil
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    (root / "keep.txt").write_text("x\n", encoding="utf-8")
    git(root, "add", "keep.txt")
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
    git(root, "commit", "-q", "-m", "gitlink")
    git(root, "rm", "-q", "--cached", "sub")
    (root / "sub").mkdir()
    for i in range(3):
        (root / "sub" / f"f{i}.txt").write_text("text\n" * 10, encoding="utf-8")
    git(root, "add", "sub")
    if not on_disk:
        shutil.rmtree(root / "sub")
    real_popen = subprocess.Popen
    spawned, buffered = [], []

    class Recording(real_popen):
        def __init__(self, argv, **kwargs):
            spawned.append(list(argv))
            super().__init__(argv, **kwargs)

        def communicate(self, *args, **kwargs):
            buffered.append(self.args)
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(wt.subprocess, "Popen", Recording)
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "sub"})
    assert result["code"] == "git_failed", result
    assert result["error_text"] == "a file path is required, not a directory"
    # The subtree's patch is never built, and no body read is buffered whole.
    assert not any("diff" in argv for argv in spawned), spawned
    assert not any("ls-files" in argv for argv in buffered), buffered


@pytest.mark.parametrize("large", [False, True])
def test_a_gitlink_replaced_by_a_file_uses_the_bounded_worktree_read(tmp_path, monkeypatch, large):
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    (root / "keep.txt").write_text("x\n", encoding="utf-8")
    git(root, "add", "keep.txt")
    commit = "1" * 40
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},sub")
    git(root, "commit", "-q", "-m", "gitlink")
    body = b"replacement\n" * (wt.MAX_READ_BYTES if large else 3)
    (root / "sub").write_bytes(body)
    real_popen = subprocess.Popen
    spawned = []

    class Recording(real_popen):
        def __init__(self, argv, **kwargs):
            spawned.append(list(argv))
            super().__init__(argv, **kwargs)

    monkeypatch.setattr(wt.subprocess, "Popen", Recording)
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "sub"})
    assert result["git"] is True, result
    assert not any("diff" in argv for argv in spawned), spawned
    assert (result["old_mode"], result["new_mode"]) == ("160000", "100644")
    assert result["truncated"] is large
    if large:
        assert "old" not in result and "new" not in result
    else:
        assert result["old"] == f"Subproject commit {commit}\n"
        assert result["new"] == body.decode()


@pytest.mark.parametrize("side", ["new", "old"])
def test_an_over_cap_file_holding_a_pem_sends_no_key_line(tmp_path, side):
    import base64

    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    blob = base64.b64encode(bytes(range(256)) * 4).decode()
    key_lines = [blob[i : i + 32] for i in range(0, len(blob), 32)]
    ordinary = "ordinary-" + "x" * 110 + "\n"
    prefix = ordinary * ((wt.MAX_READ_BYTES // 2 - 256) // len(ordinary))
    pem = "\n".join("    " + line for line in [PEM_BEGIN_RSA, *key_lines, PEM_END_RSA])
    content = prefix + "  key: |\n" + pem + "\n" + ordinary * (wt.MAX_READ_BYTES // len(ordinary))
    path = root / "config.yaml"
    path.write_text(content, encoding="utf-8")
    if side == "old":
        git(root, "add", "config.yaml")
        git(root, "commit", "-q", "-m", "seed")
        path.unlink()
    bodies = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "config.yaml"})
    assert bodies["truncated"] is True
    assert "old" not in bodies and "new" not in bodies
    assert not any(line in json.dumps(bodies) for line in key_lines)
    view = _git_diff_from_bodies(bodies, "config.yaml")
    assert view["truncated"] is True
    assert not any(line in view["output"] for line in key_lines)


@pytest.mark.parametrize("side", ["new", "old"])
def test_an_indented_pem_with_no_footer_in_a_complete_file_shows_no_key_line(
    tmp_path, monkeypatch, side
):
    import base64
    import secrets

    from kiro_crew.coder import session_browse
    from kiro_crew.remote_session import workspace_tools as wt
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG

    root, git = _plain_git_repo(tmp_path)
    blob = base64.b64encode(secrets.token_bytes(768)).decode()
    key_lines = [blob[i : i + 32] for i in range(0, len(blob), 32)]
    content = "config:\n  key: |\n" + "".join(
        "    " + line + "\n" for line in [PEM_BEGIN_RSA, *key_lines]
    )
    path = root / "config.yaml"
    path.write_text(content, encoding="utf-8")
    if side == "old":
        git(root, "add", "config.yaml")
        git(root, "commit", "-q", "-m", "seed")
        path.unlink()
    bodies = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "config.yaml"})
    assert bodies["truncated"] is False and bodies[side] == content
    output = session_browse._git_diff_from_bodies(bodies, "config.yaml")["output"]
    assert "config:" in output and REDACTED_CREDENTIAL_TAG in output
    assert not any(line in output for line in key_lines)
    # The shared redactor alone masks a footerless key only through unindented
    # lines, so without the withholding step this complete file leaks.
    monkeypatch.setattr(session_browse, "_withhold_unclosed_pem", lambda body: body)
    leaked = session_browse._git_diff_from_bodies(bodies, "config.yaml")["output"]
    assert any(line in leaked for line in key_lines)


def test_a_complete_pem_uses_whole_body_redaction(tmp_path):
    import base64

    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.platform import redact_via_context
    from kiro_crew.remote_session import workspace_tools as wt
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG

    root, _git = _plain_git_repo(tmp_path)
    blob = base64.b64encode(bytes(range(256))).decode()
    key_lines = [blob[i : i + 32] for i in range(0, len(blob), 32)]
    pem = "\n".join([PEM_BEGIN_RSA, *key_lines, PEM_END_RSA])
    ordinary = "ordinary-" + "x" * 110 + "\n"
    (root / "f").write_text(pem + "\n" + ordinary * 20, encoding="utf-8")
    bodies = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "f"})
    assert bodies["truncated"] is False and PEM_END_RSA in bodies["new"]
    clean = redact_via_context(bodies["new"])
    assert REDACTED_CREDENTIAL_TAG in clean
    output = _git_diff_from_bodies(bodies, "f")["output"]
    assert not any(line in output for line in key_lines)
    assert "+" + ordinary in output
    assert [line[1:] for line in output.splitlines() if line.startswith("+")][
        1:
    ] == clean.splitlines()
