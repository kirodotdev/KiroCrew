"""Launcher-owned records retire without granting a historical sweep authority.

The subprocess runs the generated parent and both real pipe handshakes. Only
privileged namespace/map operations and the post-handshake child payload are
substituted; signals target only the generated test parent. No host mounts or
credentials are involved.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew import platform_compat, sandbox

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux launcher lifecycle")


def _records(pids):
    return [p for p in pids.iterdir() if p.name != ".reclaim.lock"]


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    monkeypatch.setattr(sandbox.Path, "home", lambda: home)
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: False)
    monkeypatch.setattr(sandbox, "_private_memory_view_setup", lambda *args: "")
    return home


def _run_launcher(home, *, fault="", code=0, private=False, on_contended=None):
    source = sandbox._build_launcher_script(private_memory=private)
    # Keep the generated parent and child handshake verbatim. Kernel mounting
    # is covered by the sandbox suite, not by these publication lifecycle tests.
    child_start = source.index("        # Private mount propagation", source.index("def main():"))
    footer = source.index('if __name__ == "__main__":')
    source = (
        source[:child_start]
        + '        if FAULT == "blocked-wait":\n'
        + "            os.close(hold_w)\n"
        + "            os.read(hold_r, 1)\n"
        + "        sys.exit(CHILD_CODE)\n\n"
        + source[footer:]
    )
    harness = textwrap.dedent("""
        import builtins
        import io
        from pathlib import Path

        original_open = builtins.open
        original_write = os.write
        original_wait = os.waitpid
        original_replace = os.replace
        original_dump = json.dump
        original_mkstemp = tempfile.mkstemp
        original_unlink = os.unlink
        original_read = os.read
        hold_r, hold_w = os.pipe()
        import signal
        original_flock = fcntl.flock
        contended = []
        def contending_flock(fd, operation):
            try:
                return original_flock(fd, operation)
            except BlockingIOError:
                # Announce the first contended attempt once; the test acts then.
                if FAULT.startswith("contended") and not contended:
                    contended.append(1)
                    print("contending", flush=True)
                raise
        fcntl.flock = contending_flock
        def read(fd, size):
            if FAULT == "blocked-read" and os.getpid() == int(os.environ["KIROCREW_HOST_PID"]):
                print("blocked", flush=True)
            return original_read(fd, size)
        os.read = read
        def interrupt(stage):
            if FAULT in {"sigterm-" + stage, "sigint-" + stage}:
                signum = signal.SIGINT if FAULT.startswith("sigint-") else signal.SIGTERM
                signal.raise_signal(signum)
                signal.raise_signal(signum)
        def mkstemp(*args, **kwargs):
            result = original_mkstemp(*args, **kwargs)
            interrupt("staging")
            return result
        tempfile.mkstemp = mkstemp
        def assert_record_lock_held():
            lock_path = Path(HOME) / "member-memory-bindings" / "pids" / ".reclaim.lock"
            fd = os.open(lock_path, os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                raise AssertionError("generated mutation ran without the common lock")
            finally:
                os.close(fd)
        def unlink(*args, **kwargs):
            assert_record_lock_held()
            interrupt("cleanup")
            return original_unlink(*args, **kwargs)
        os.unlink = unlink
        if FAULT.startswith("target-"):
            pids = Path(HOME) / "member-memory-bindings" / "pids"
            pids.mkdir(parents=True, exist_ok=True)
            target = pids / (str(os.getpid()) + ".namespace.json")
            marker = Path(HOME) / "foreign"
            marker.write_text("foreign")
            if FAULT == "target-symlink":
                target.symlink_to(marker)
            elif FAULT == "target-directory":
                target.mkdir()
            elif FAULT == "target-hardlink":
                os.link(marker, target)
        class Kernel:
            calls = 0
            def unshare(self, flags):
                self.calls += 1
                if FAULT == "blocked-read":
                    os.close(hold_w)
                    os.read(hold_r, 1)
                    sys.exit(0)
                return 1 if FAULT == "setup" and self.calls == 2 else 0
        _libc = Kernel()
        def map_open(path, *args, **kwargs):
            if str(path).startswith("/proc/") and str(path).endswith(
                ("/setgroups", "/uid_map", "/gid_map")
            ):
                if FAULT == "maps":
                    raise PermissionError("synthetic map failure")
                return io.StringIO()
            return original_open(path, *args, **kwargs)
        builtins.open = map_open
        def dump(row, handle, *args, **kwargs):
            if FAULT == "serialization":
                handle.write("partial")
                raise OSError("synthetic serialization failure")
            return original_dump(row, handle, *args, **kwargs)
        json.dump = dump
        def replace(src, dst, *args, **kwargs):
            assert_record_lock_held()
            if FAULT == "publication":
                raise PermissionError("synthetic publication failure")
            interrupt("publication")
            result = original_replace(src, dst, *args, **kwargs)
            interrupt("published")
            return result
        os.replace = replace
        def write(fd, data):
            if FAULT == "brokenpipe" and data == b"n":
                # Only fail the parent's release, not the child's readiness.
                if os.getpid() == int(os.environ["KIROCREW_HOST_PID"]):
                    raise BrokenPipeError("synthetic release failure")
            return original_write(fd, data)
        os.write = write
        def wait(pid, options):
            record = Path(HOME) / "member-memory-bindings" / "pids" / (
                str(os.getpid()) + ".namespace.json"
            )
            if not record.exists():
                # Degraded launch: publication was skipped (un-tightenable
                # directory), so there is no record to inspect or retire. The
                # wait itself must still complete and preserve the child code.
                return original_wait(pid, options)
            row = json.loads(record.read_text())
            # Age is not retirement authority, even for a long-running V1.
            os.utime(record, (1, 1))
            print(json.dumps(row), flush=True)
            interrupt("wait")
            result = original_wait(pid, options)
            assert record.is_file(), "retired before wait returned"
            if FAULT == "replacement":
                other = record.with_suffix(".new")
                other.write_text("replacement")
                original_replace(other, record)
            if FAULT == "wait":
                raise OSError("synthetic wait failure after reaping")
            return result
        os.waitpid = wait
        """)
    source = source.replace(
        'if __name__ == "__main__":',
        f"FAULT = {fault!r}\nCHILD_CODE = {code!r}\nHOME = {str(home)!r}\n"
        + harness
        + '\nif __name__ == "__main__":',
    )
    script = home.parent / "launcher.py"
    script.write_text(source, encoding="utf-8")
    if fault.startswith("contended"):
        import select

        with subprocess.Popen(
            [sys.executable, "-I", "-S", str(script), "synthetic-child"],
            cwd=home,
            env={"HOME": str(home), "TMPDIR": str(home.parent), "KIROCREW_HOME": str(home)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ) as process:
            try:
                assert select.select([process.stdout], [], [], 10)[0], "publisher never contended"
                first = process.stdout.readline().strip()
                assert first == "contending", first
                on_contended(process)
                stdout, stderr = process.communicate(timeout=10)
                return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
    if fault.startswith("blocked-"):
        import select
        import signal

        with subprocess.Popen(
            [sys.executable, "-I", "-S", str(script), "synthetic-child"],
            cwd=home,
            env={"HOME": str(home), "TMPDIR": str(home.parent), "KIROCREW_HOME": str(home)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ) as process:
            try:
                assert select.select([process.stdout], [], [], 5)[0], "no wait handshake"
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
    return subprocess.run(
        [sys.executable, "-I", "-S", str(script), "synthetic-child"],
        cwd=home,
        env={"HOME": str(home), "TMPDIR": str(home.parent), "KIROCREW_HOME": str(home)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize(
    "fault,code",
    [
        ("", 0),
        ("", 7),
        ("maps", 0),
        ("setup", 0),
        ("brokenpipe", 0),
        ("serialization", 0),
        ("publication", 0),
        ("wait", 0),
    ],
)
def test_generated_launcher_retires_owned_names(launcher, private, fault, code):
    pids = launcher / "member-memory-bindings" / "pids"
    pids.mkdir(parents=True, mode=0o700)
    foreign = {"123.namespace.json": "legacy", "tmpforeign.tmp": "inflight", "123.json": "binding"}
    for name, content in foreign.items():
        (pids / name).write_text(content)
    result = _run_launcher(launcher, fault=fault, code=code, private=private)
    assert result.returncode == (1 if fault else code), result.stderr
    assert {p.name: p.read_text() for p in _records(pids)} == foreign
    if not fault or fault == "wait":
        row = json.loads(result.stdout)
        assert row["private_memory"] is private
        assert row["process_start"]
        assert len(row["namespaces"]) == 2


def test_generated_launcher_preserves_replacement(launcher):
    result = _run_launcher(launcher, fault="replacement")
    assert result.returncode == 0, result.stderr
    pids = launcher / "member-memory-bindings" / "pids"
    assert [p.read_text() for p in _records(pids)] == ["replacement"]


def _helper_sources() -> str:
    """Run verbatim generated helpers in a subprocess, not in-process AST exec."""
    source = sandbox._build_launcher_script()
    tree = ast.parse(source)
    names = {"_namespace_record_directory", "_namespace_record_lock", "_retire_namespace_record"}
    segments: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, node.name
            segments.append(segment)
    assert len(segments) == len(names), "all launcher helpers must be extractable"
    return "\n\n".join(segments)


# Exercise descriptor ownership and injected filesystem failures in an isolated child.
_HELPER_DRIVER = """
import fcntl
import json
import os
import stat
import sys
import time
from types import SimpleNamespace

REAL_UID = {real_uid!r}
RECORD_LOCK_POLL_SECS = 0.02
_real_stat = os.stat

{helpers}


def _apply_mutation(pids, target, mutation):
    if mutation == "symlink":
        target.unlink()
        os.symlink(str(pids / "absent"), str(target))
    elif mutation == "directory":
        target.unlink()
        os.mkdir(str(target))
    elif mutation == "replacement":
        target.unlink()
        target.write_text("new publication")
    elif mutation == "hardlink":
        os.link(str(target), str(pids / "another-name"))


def _patched_stat(name, target_name):
    def inner(path, *args, **kwargs):
        if path == target_name:
            if name == "unknown":
                raise PermissionError("synthetic probe failure")
            info = _real_stat(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=-1)
        return _real_stat(path, *args, **kwargs)
    return inner


def main():
    from pathlib import Path

    op = sys.argv[1]
    if op == "hold_lock":
        directory = _namespace_record_directory()
        lock = _namespace_record_lock(directory)
        try:
            print("locked", flush=True)
            assert sys.stdin.read(1) == "x"
        finally:
            os.close(lock)
            os.close(directory)
        return
    if op == "directory":
        try:
            fd = _namespace_record_directory()
        except BaseException as exc:  # noqa: BLE001 - report type + message
            print(json.dumps({{"error": type(exc).__name__, "message": str(exc)}}))
            return
        os.close(fd)
        print(json.dumps({{"ok": True}}))
        return
    if op == "retire":
        target_name = sys.argv[2]
        mutation = sys.argv[3]
        home = Path(os.environ["KIROCREW_HOME"])
        pids = home / "member-memory-bindings" / "pids"
        target = pids / target_name
        target.write_text("owned")
        directory = _namespace_record_directory()
        owned = os.open(target_name, os.O_RDONLY, dir_fd=directory)
        try:
            _apply_mutation(pids, target, mutation)
            if mutation in ("unknown", "foreign"):
                os.stat = _patched_stat(mutation, target_name)
            try:
                _retire_namespace_record(directory, target_name, owned)
            finally:
                os.stat = _real_stat
        finally:
            os.close(owned)
            os.close(directory)
        print(json.dumps({{"lexists": os.path.lexists(str(target))}}))
        return
    if op == "stays_pinned":
        target_name = sys.argv[2]
        home = Path(os.environ["KIROCREW_HOME"])
        pids = home / "member-memory-bindings" / "pids"
        target = pids / target_name
        target.write_text("owned")
        moved = pids.with_name("moved")
        foreign = home.parent / "foreign"
        foreign.mkdir()
        (foreign / target_name).write_text("foreign")
        directory = _namespace_record_directory()
        owned = os.open(target_name, os.O_RDONLY, dir_fd=directory)
        try:
            # Swap the pinned directory's PATH out from under the retirement:
            # the fd stays pinned to the original dir, so the retire must operate
            # only through that fd and never re-resolve the (now foreign) path.
            pids.rename(moved)
            os.symlink(str(foreign), str(pids))
            _retire_namespace_record(directory, target_name, owned)
        finally:
            os.close(owned)
            os.close(directory)
        print(
            json.dumps(
                {{
                    "moved_exists": os.path.exists(str(moved / target_name)),
                    "foreign": (foreign / target_name).read_text(),
                }}
            )
        )
        return
    raise SystemExit("unknown op")


if __name__ == "__main__":
    main()
"""


def _run_helper(home, op, *args):
    """Run one launcher-helper operation in a subprocess; return CompletedProcess."""
    script = _HELPER_DRIVER.format(
        real_uid=platform_compat.local_user_id(),
        helpers=_helper_sources(),
    )
    path = home.parent / "helper_driver.py"
    path.write_text(script, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", "-S", str(path), op, *args],
        cwd=home,
        env={"HOME": str(home), "TMPDIR": str(home.parent), "KIROCREW_HOME": str(home)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("component", ["member-memory-bindings", "pids"])
def test_linked_protected_directory_is_refused(launcher, component):
    foreign = launcher.parent / "foreign"
    foreign.mkdir()
    marker = foreign / "keep"
    marker.write_text("foreign")
    parent = launcher
    if component == "pids":
        parent = launcher / "member-memory-bindings"
        parent.mkdir(mode=0o700)
    (parent / component).symlink_to(foreign, target_is_directory=True)
    result = _run_helper(launcher, "directory")
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome.get("error"), outcome
    assert list(foreign.iterdir()) == [marker]
    assert marker.read_text() == "foreign"


@pytest.mark.parametrize(
    "mutation", ["symlink", "directory", "replacement", "hardlink", "unknown", "foreign"]
)
def test_retirement_preserves_unowned_or_unknown_entry(launcher, mutation):
    pids = launcher / "member-memory-bindings" / "pids"
    pids.mkdir(parents=True, mode=0o700)
    result = _run_helper(launcher, "retire", "owned.namespace.json", mutation)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["lexists"] is True


def test_retirement_stays_in_pinned_directory(launcher):
    pids = launcher / "member-memory-bindings" / "pids"
    pids.mkdir(parents=True, mode=0o700)
    result = _run_helper(launcher, "stays_pinned", "owned.namespace.json")
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["moved_exists"] is False
    assert outcome["foreign"] == "foreign"


@pytest.mark.parametrize("kind", ["symlink", "directory", "hardlink"])
def test_generated_launcher_refuses_unsafe_target(launcher, kind):
    result = _run_launcher(launcher, fault=f"target-{kind}")
    assert result.returncode == 1
    assert "unsafe namespace record target" in result.stderr
    assert (launcher / "foreign").read_text() == "foreign"
    targets = _records(launcher / "member-memory-bindings" / "pids")
    assert len(targets) == 1
    target = targets[0]
    if kind == "symlink":
        assert target.is_symlink()
    elif kind == "directory":
        assert target.is_dir()
    else:
        assert target.stat().st_nlink == 2


@pytest.mark.parametrize("component", ["home", "member-memory-bindings", "pids"])
def test_writable_protected_directory_is_refused(launcher, component):
    pids = launcher / "member-memory-bindings" / "pids"
    pids.mkdir(parents=True, mode=0o700)
    path = {"home": launcher, "member-memory-bindings": pids.parent, "pids": pids}[component]
    platform_compat.chmod_safe(path, 0o777)
    try:
        result = _run_helper(launcher, "directory")
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout)
        assert outcome.get("error") == "PermissionError", outcome
        assert outcome.get("message") == "unsafe namespace record directory", outcome
    finally:
        platform_compat.chmod_safe(path, 0o700)


@pytest.mark.parametrize("code", [0, 7])
def test_generated_launcher_degrades_on_untightenable_directory(launcher, code):
    """Unavailable authority publication preserves the child's own exit status."""
    pids = launcher / "member-memory-bindings" / "pids"
    pids.mkdir(parents=True, mode=0o700)
    platform_compat.chmod_safe(pids, 0o777)  # group/other writable -> refused
    try:
        result = _run_launcher(launcher, code=code)
    finally:
        platform_compat.chmod_safe(pids, 0o700)
    # Launch proceeded: the child's own exit code is preserved (no abort).
    assert result.returncode == code, result.stderr
    assert "skipping namespace record publication" in result.stderr
    assert "chmod 0700" in result.stderr
    # No record was published for this launch.
    assert [p.name for p in _records(pids)] == []


def test_maintenance_preserves_old_live_v1_and_unknown_backlog(launcher, monkeypatch):
    from kiro_crew import member_memory_auth as auth

    pids = launcher / "member-memory-bindings" / "pids"
    pids.mkdir(parents=True, mode=0o700)
    pid = os.getpid()
    start = platform_compat.get_process_start_id(pid)
    assert start
    namespaces = []
    for kind in ("user", "mnt"):
        info = Path(f"/proc/{pid}/ns/{kind}").stat()
        namespaces.append([info.st_dev, info.st_ino])
    record = pids / f"{pid}.namespace.json"
    record.write_text(
        json.dumps({"process_start": start, "namespaces": namespaces, "private_memory": False})
    )
    os.utime(record, (1, 1))
    auth.publish_member_session_pid(pid, "dashboard:global", home=launcher, memory_store="")
    for index in range(1000):
        (pids / f"tmp-unknown-{index}.tmp").write_text("unknown publisher")
    (pids / "1.namespace.json").write_text("stale or unknown")
    before = {p.name: p.read_bytes() for p in _records(pids)}
    for name in (
        "_cleanup_stale_sandbox_mount_sources",
        "_cleanup_legacy_mount_source_residue",
        "_cleanup_retired_acp_snapshot_dir",
    ):
        monkeypatch.setattr(sandbox, name, lambda *args: 0)
    assert (
        sandbox.cleanup_stale_sandbox_profiles(
            data_home=launcher, legacy_dir=str(launcher / "absent-legacy")
        )
        == 0
    )
    assert {p.name: p.read_bytes() for p in _records(pids)} == before
    assert auth._matches_published_sandbox_namespace(pid, pid, start, launcher)


@pytest.mark.parametrize("stage", ["staging", "publication", "published", "wait", "cleanup"])
@pytest.mark.parametrize("code", [0, 7])
@pytest.mark.parametrize("signal_name,signal_code", [("sigterm", 143), ("sigint", 130)])
def test_generated_launcher_signal_retires_owned_names(
    launcher, stage, code, signal_name, signal_code
):
    result = _run_launcher(launcher, fault=f"{signal_name}-{stage}", code=code)
    pids = launcher / "member-memory-bindings" / "pids"
    assert list(_records(pids)) == []
    assert result.returncode == (code if stage == "cleanup" else signal_code), result.stderr


@pytest.mark.parametrize("stage", ["blocked-read", "blocked-wait"])
def test_generated_launcher_interrupts_blocking_wait(launcher, stage):
    result = _run_launcher(launcher, fault=stage)
    assert result.returncode == 143, result.stderr
    pids = launcher / "member-memory-bindings" / "pids"
    assert not pids.exists() or list(_records(pids)) == []


def test_generated_publisher_skips_when_module_lock_outlives_bound(launcher, monkeypatch):
    import time

    from kiro_crew import member_process_records as records

    # The launcher renders the module bound at build time; keep the test quick.
    monkeypatch.setattr(records, "LOCK_WAIT_SECS", 0.2)
    with (
        records.record_directory(launcher, create=True) as directory,
        records.record_lock(directory),
    ):
        began = time.monotonic()
        result = _run_launcher(launcher)
        waited = time.monotonic() - began
    assert result.returncode == 0, result.stderr
    assert waited < 5, waited
    assert "skipping namespace record publication" in result.stderr
    assert "process record lock held for more than 0.2s" in result.stderr
    assert "unavailable" not in result.stderr
    assert _records(launcher / "member-memory-bindings" / "pids") == []


def test_generated_publisher_racing_held_sweep_lock_lands_once_released(launcher):
    from contextlib import ExitStack

    from kiro_crew import member_process_records as records

    with ExitStack() as held:
        directory = held.enter_context(records.record_directory(launcher, create=True))
        held.enter_context(records.record_lock(directory))
        pids = launcher / "member-memory-bindings" / "pids"

        def release(process):
            # The publisher is blocked on our lock; nothing was published yet.
            assert _records(pids) == []
            held.close()

        result = _run_launcher(launcher, fault="contended", on_contended=release)
    assert result.returncode == 0, result.stderr
    assert "skipping namespace record publication" not in result.stderr
    row = json.loads(result.stdout)
    assert row["process_start"] and len(row["namespaces"]) == 2
    assert _records(pids) == []


@pytest.mark.parametrize("release", [False, True], ids=["held-past-bound", "released"])
def test_generated_launcher_sigterm_during_contention_wait_exits_bounded(
    launcher, monkeypatch, release
):
    import signal
    import time
    from contextlib import ExitStack

    from kiro_crew import member_process_records as records

    monkeypatch.setattr(records, "LOCK_WAIT_SECS", 0.5)
    pids = launcher / "member-memory-bindings" / "pids"
    with ExitStack() as held:
        directory = held.enter_context(records.record_directory(launcher, create=True))
        held.enter_context(records.record_lock(directory))

        def signal_while_waiting(process):
            # The parent is inside its bounded wait: the signal is deferred, never
            # interrupts the acquire, and is honored at the next handshake.
            process.send_signal(signal.SIGTERM)
            if release:
                held.close()

        began = time.monotonic()
        result = _run_launcher(
            launcher, fault="contended-sigterm", on_contended=signal_while_waiting
        )
    assert time.monotonic() - began < 8
    assert result.returncode == 143, result.stderr
    if release:
        # Landed after release, then the deferred signal retired the owned record.
        assert "skipping namespace record publication" not in result.stderr
    else:
        assert "process record lock held for more than 0.5s" in result.stderr
    assert _records(pids) == []


def test_generated_publisher_refuses_unavailable_root_without_waiting(launcher, monkeypatch):
    import time

    from kiro_crew import member_process_records as records

    monkeypatch.setattr(records, "LOCK_WAIT_SECS", 30.0)
    pids = launcher / "member-memory-bindings" / "pids"
    pids.mkdir(parents=True, mode=0o700)
    (pids / ".reclaim.lock").mkdir()
    began = time.monotonic()
    result = _run_launcher(launcher)
    assert time.monotonic() - began < 15
    assert result.returncode == 0, result.stderr
    assert "protected directory/lock unavailable" in result.stderr
    assert "lock held for more than" not in result.stderr


def test_generated_lock_serializes_module_publisher_invalidator_and_reaper(launcher, monkeypatch):
    import select

    from kiro_crew import member_memory_auth as auth
    from kiro_crew import member_process_records as records

    # The module publishers wait a bounded time for the generated lock, then skip.
    monkeypatch.setattr(records, "LOCK_WAIT_SECS", 0.1)
    pid = 424242
    with records.record_directory(launcher, create=True):
        pass
    path = launcher / "member-memory-bindings" / "pids" / f"{pid}.json"
    row = {"version": 2, "session_key": "old", "process_start": "old", "memory_store": ""}
    path.write_text(json.dumps(row))
    script = launcher.parent / "held_lock.py"
    script.write_text(
        _HELPER_DRIVER.format(real_uid=platform_compat.local_user_id(), helpers=_helper_sources())
    )
    with subprocess.Popen(
        [sys.executable, "-I", "-S", str(script), "hold_lock"],
        cwd=launcher,
        env={"KIROCREW_HOME": str(launcher)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as child:
        try:
            assert select.select([child.stdout], [], [], 5)[0], "generated lock not acquired"
            assert child.stdout.readline().strip() == "locked"
            monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "new")
            auth.publish_member_session_pid(pid, "new", home=launcher, memory_store="")
            auth.publish_member_session_pid(pid, "", home=launcher, memory_store="")
            assert records.reclaim_stale_member_bindings(data_home=launcher) == (0, None)
            assert json.loads(path.read_text()) == row
            _, stderr = child.communicate("x", timeout=5)
            assert child.returncode == 0, stderr
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
    assert records.reclaim_stale_member_bindings(data_home=launcher) == (1, None)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_generated_publisher_refuses_unsafe_shared_lock(launcher, kind):
    pids = launcher / "member-memory-bindings" / "pids"
    pids.mkdir(parents=True, mode=0o700)
    lock = pids / ".reclaim.lock"
    marker = launcher / "untouched"
    marker.write_text("untouched")
    if kind == "symlink":
        lock.symlink_to(marker)
    elif kind == "hardlink":
        os.link(marker, lock)
    else:
        os.mkfifo(lock)
    result = _run_launcher(launcher)
    assert result.returncode == 0, result.stderr
    assert "skipping namespace record publication" in result.stderr
    assert _records(pids) == []
    assert marker.read_text() == "untouched"


def test_generated_retirement_respects_module_lock(launcher):
    from kiro_crew import member_process_records as records

    with (
        records.record_directory(launcher, create=True) as directory,
        records.record_lock(directory),
    ):
        result = _run_helper(launcher, "retire", "owned.namespace.json", "none")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["lexists"] is True
    result = _run_helper(launcher, "retire", "owned.namespace.json", "none")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["lexists"] is False
