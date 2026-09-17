"""Workflow ID high-water allocation is atomic and grants no scope."""

import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_workflows_private_execution import SCRIPT, finished
from test_workflows_private_execution import world as _world

from kiro_crew.workflow_memory import WorkflowScope, allocate_run_id, read_binding
from kiro_crew.workflows.service import WorkflowService

world = _world


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["alice", "bob", "global"])
async def test_simultaneous_services_never_share_run_identity(world, second):
    services = [
        WorkflowService(sessions=world.sessions, context_builder=world.builder, persist=False)
        for _ in range(2)
    ]
    first, other = await asyncio.gather(
        services[0].start(SCRIPT, session_key="dashboard:alice"),
        services[1].start(SCRIPT, session_key=f"dashboard:{second}"),
    )
    assert "run_id" in first, first
    assert "run_id" in other, other
    assert first["run_id"] != other["run_id"]
    runs = await asyncio.gather(finished(services[0], first), finished(services[1], other))
    scopes = await asyncio.gather(
        *(WorkflowScope.restore(run.run_id, record=run.to_store_json()) for run in runs)
    )
    assert scopes[0].store == world.stores["alice"]
    assert scopes[1].store == world.stores.get(second, "")
    for run, scope in zip(runs, scopes):
        assert run.result == [[scope.store or "V1"] * 2] + [scope.store or "V1"] * 3


def test_thread_allocations_are_unique():
    import threading

    barrier = threading.Barrier(6, timeout=5)

    def claim(_):
        barrier.wait()
        return allocate_run_id()

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert set(pool.map(claim, range(6))) == {f"wf_{n:06d}" for n in range(1, 7)}
    assert read_binding("wf_000001") is None
    assert allocate_run_id() == "wf_000007"


@pytest.mark.parametrize("delay_creator", [False, True])
def test_processes_reserve_distinct_ids_without_publishing_authority(tmp_path, delay_creator):
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = (
        "import sys, os, time\n"
        "from contextlib import contextmanager\n"
        "from pathlib import Path\n"
        "from kiro_crew import platform_compat as p\n"
        "from kiro_crew.workflow_memory import allocate_run_id\n"
        "real_lock = p.file_lock\n"
        "@contextmanager\n"
        "def delayed(fd, **kw):\n"
        " if sys.argv[1] == 'delay':\n"
        "  Path('created').touch()\n"
        "  deadline = time.monotonic() + 15\n"
        "  while not Path('allocated').exists():\n"
        "   if time.monotonic() > deadline: raise TimeoutError('other allocator stalled')\n"
        "   time.sleep(.01)\n"
        " with real_lock(fd, **kw): yield\n"
        "p.file_lock = delayed\n"
        "sys.stdin.readline()\n"
        "if sys.argv[1] == 'follower':\n"
        " deadline = time.monotonic() + 15\n"
        " while not Path('created').exists():\n"
        "  if time.monotonic() > deadline: raise TimeoutError('creator stalled')\n"
        "  time.sleep(.01)\n"
        "key = allocate_run_id()\n"
        "Path('allocated').touch()\n"
        "print(key, flush=True)\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(source_root))
    children = []
    try:
        for index in range(2):
            role = ("delay" if index == 0 else "follower") if delay_creator else "normal"
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, role],
                    cwd=tmp_path,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
            )
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
            child.stdin.close()
            child.stdin = None
        ids = []
        for child in children:
            out, error = child.communicate(timeout=20)
            assert child.returncode == 0, error
            ids.append(out.strip())
        assert set(ids) == {"wf_000001", "wf_000002"}
        assert all(read_binding(key) is None for key in ids)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("unc", [False, True])
def test_windows_resolved_prefix_is_only_a_spelling(unc):
    from pathlib import PureWindowsPath

    from kiro_crew.platform_compat import strip_extended_length_prefix

    root = r"\\server\share\Crew" if unc else r"C:\Users\Runner\Crew"
    plain = PureWindowsPath(root) / "workflows" / ".reserved" / "digest"
    extended = PureWindowsPath(("\\\\?\\UNC\\" + str(plain)[2:]) if unc else "\\\\?\\" + str(plain))
    assert extended != plain
    assert strip_extended_length_prefix(extended) == plain
    assert strip_extended_length_prefix(extended.parent / "foreign") != plain


@pytest.mark.parametrize("component", ["lock", "counter"])
def test_reservation_accepts_resolve_prefix_during_creation(monkeypatch, component):
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        resolved = real_resolve(path, *args, **kwargs)
        matched = {
            "lock": path.name == ".run-id.lock",
            "counter": path.name == ".run-id.json",
        }[component]
        if matched:
            # Only the spelling returned by the OS is synthetic. Allocation,
            # collision detection and protected binding reads remain real.
            return Path("\\\\?\\" + str(resolved))
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve)
    assert allocate_run_id() == "wf_000001"
    assert allocate_run_id() == "wf_000002"
    assert read_binding("wf_000001") is None


def test_workflow_allocator_refuses_real_directory_redirect(tmp_path):
    from conftest import make_dir_link
    from kiro_crew.workflow_memory import WorkflowMemoryError

    path = _allocator_root()
    path.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "foreign"
    outside.mkdir()
    make_dir_link(path, outside)
    with pytest.raises(WorkflowMemoryError, match="redirected"):
        allocate_run_id()
    assert not list(outside.iterdir())


def _allocator_root():
    from kiro_crew.workflows.store import default_workflows_dir

    return default_workflows_dir()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "{",
        "[]",
        '{"version":2,"high_water":1}',
        '{"version":true,"high_water":1}',
        '{"version":1,"high_water":true}',
        '{"version":1,"high_water":1.0}',
        '{"version":1,"high_water":-1}',
        '{"version":1,"high_water":18446744073709551616}',
    ],
)
def test_enabled_counter_never_reinitializes(payload):
    from kiro_crew.workflow_memory import WorkflowMemoryError

    allocate_run_id()
    counter = _allocator_root() / ".run-id.json"
    if payload is None:
        counter.unlink()
    else:
        counter.write_text(payload, encoding="utf-8")
    with pytest.raises(WorkflowMemoryError):
        allocate_run_id(900)
    assert (counter.read_text() if counter.exists() else None) == payload


@pytest.mark.parametrize("existing_counter", [False, True])
def test_crash_before_enablement_can_finish_initialization(existing_counter):
    from kiro_crew.workflow_memory import _write_run_high_water

    root = _allocator_root()
    root.mkdir(parents=True)
    (root / ".run-id.lock").touch()
    if existing_counter:
        _write_run_high_water(root / ".run-id.json", 8)
    assert allocate_run_id() == ("wf_000009" if existing_counter else "wf_000001")
    assert (root / ".run-id.lock").read_bytes() == b"1"


def test_six_digit_boundary_and_uint64_exhaustion():
    from kiro_crew.workflow_memory import _RUN_ID_MAX, WorkflowMemoryError

    assert allocate_run_id(999998) == "wf_999999"
    assert allocate_run_id() == "wf_1000000"
    assert allocate_run_id(_RUN_ID_MAX - 1) == f"wf_{_RUN_ID_MAX}"
    with pytest.raises(WorkflowMemoryError, match="exhausted"):
        allocate_run_id()


@pytest.mark.parametrize("floor", [True, -1, 1.0, "3", 1 << 64])
def test_recovery_floor_is_strict_uint64(floor):
    from kiro_crew.workflow_memory import WorkflowMemoryError

    with pytest.raises(WorkflowMemoryError, match="floor"):
        allocate_run_id(floor)


@pytest.mark.parametrize("phase", ["write", "file_sync", "dir_sync", "lock"])
def test_allocator_io_failure_never_returns_an_id(monkeypatch, phase):
    import kiro_crew.workflow_memory as wm

    assert allocate_run_id() == "wf_000001"

    def fail(*args, **kwargs):
        raise OSError("injected I/O failure")

    with monkeypatch.context() as patch:
        if phase == "write":
            patch.setattr(wm, "atomic_write", fail)
        elif phase == "file_sync":
            patch.setattr(wm.os, "fsync", fail)
        elif phase == "dir_sync":
            patch.setattr(wm, "fsync_dir", fail)
        else:
            patch.setattr(wm.platform_compat, "file_lock", fail)
        with pytest.raises(wm.WorkflowMemoryError, match="I/O"):
            allocate_run_id()
    assert int(allocate_run_id()[3:]) > 1


def test_replace_then_sync_failure_burns_the_visible_high_water(monkeypatch):
    import kiro_crew.workflow_memory as wm

    assert allocate_run_id() == "wf_000001"
    real_write = wm.atomic_write

    def replaced(*args, **kwargs):
        real_write(*args, **kwargs)
        raise OSError("crash after replace")

    with monkeypatch.context() as patch:
        patch.setattr(wm, "atomic_write", replaced)
        with pytest.raises(wm.WorkflowMemoryError):
            allocate_run_id()
    assert allocate_run_id() == "wf_000003"


@pytest.mark.parametrize("name", [".run-id.lock", ".run-id.json"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_allocator_rejects_redirected_or_nonregular_files(tmp_path, name, kind):
    from kiro_crew.workflow_memory import WorkflowMemoryError

    assert allocate_run_id() == "wf_000001"
    path = _allocator_root() / name
    original = path.read_bytes()
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(original)
    if kind == "symlink":
        try:
            path.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("real file symlinks require Windows Developer Mode")
            raise
    elif kind == "hardlink":
        os.link(outside, path)
    else:
        path.mkdir()
    with pytest.raises(WorkflowMemoryError):
        allocate_run_id()
    assert outside.read_bytes() == original


def test_lock_inode_stays_fixed_and_new_allocations_create_no_directories():
    assert allocate_run_id() == "wf_000001"
    root = _allocator_root()
    lock = root / ".run-id.lock"
    inode = lock.stat()
    for _ in range(10):
        allocate_run_id()
    assert os.path.samestat(inode, lock.stat())
    assert {entry.name for entry in root.iterdir()} == {".run-id.lock", ".run-id.json"}


@pytest.mark.parametrize(
    "current_sid,owner_sid,local,accepted",
    [
        ("current", "current", True, True),
        ("current", "current", False, True),
        ("current", "S-1-5-32-544", True, True),
        ("current", "S-1-5-32-544", False, False),
        ("current", "S-1-5-32-544", None, False),
        ("current", "foreign", True, False),
        ("current", "foreign", False, False),
        ("current", "S-1-5-18", True, False),
        ("current", None, True, False),
        (None, "current", True, False),
        (None, "S-1-5-32-544", True, False),
        ("", "S-1-5-32-544", True, False),
    ],
)
def test_allocator_windows_owner_is_checked_not_synthesized_uid(
    monkeypatch, current_sid, owner_sid, local, accepted
):
    from types import SimpleNamespace

    import kiro_crew.workflow_memory as wm

    security = SimpleNamespace(owner_sid=owner_sid, volume_is_local=local)
    monkeypatch.setattr(wm.platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(wm.platform_compat, "current_user_sid", lambda: current_sid)
    monkeypatch.setattr(wm.windows_acl, "describe", lambda path: security)
    if accepted:
        wm._allocator_owner(Path("unused"), SimpleNamespace(st_uid=0))
    else:
        with pytest.raises(wm.WorkflowMemoryError, match="owner"):
            wm._allocator_owner(Path("unused"), SimpleNamespace(st_uid=0))


def test_allocator_windows_unreadable_owner_still_refuses(monkeypatch):
    from types import SimpleNamespace

    import kiro_crew.workflow_memory as wm

    def unreadable(path):
        raise wm.windows_acl.AclUnavailable("descriptor unavailable")

    monkeypatch.setattr(wm.platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(wm.platform_compat, "current_user_sid", lambda: "current")
    monkeypatch.setattr(wm.windows_acl, "describe", unreadable)
    with pytest.raises(wm.WorkflowMemoryError, match="owner is unavailable"):
        wm._allocator_owner(Path("unused"), SimpleNamespace(st_uid=0))


@pytest.mark.parametrize("owner,accepted", [(41, True), (0, False), (42, False)])
def test_allocator_posix_owner_remains_exact(monkeypatch, owner, accepted):
    from types import SimpleNamespace

    import kiro_crew.workflow_memory as wm

    monkeypatch.setattr(wm.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(wm.platform_compat, "local_user_id", lambda: 41)
    if accepted:
        wm._allocator_owner(Path("unused"), SimpleNamespace(st_uid=owner))
    else:
        with pytest.raises(wm.WorkflowMemoryError, match="owner"):
            wm._allocator_owner(Path("unused"), SimpleNamespace(st_uid=owner))


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows security descriptors")
class TestNativeWindowsAllocator:
    @pytest.fixture(autouse=True)
    def _windows_restrict_to_owner_stub(self):
        """Override the suite's no-op DACL fixture only for this native test."""
        yield

    def test_keeps_owner_and_locks_down_every_object(self):
        from kiro_crew import platform_compat, windows_acl

        root = _allocator_root()
        root.mkdir(parents=True)
        owner_before = windows_acl.describe(root).owner_sid
        sid = platform_compat.current_user_sid()
        assert sid
        assert owner_before in {sid, "S-1-5-32-544"}
        assert allocate_run_id() == "wf_000001"
        assert allocate_run_id() == "wf_000002"
        for path in (root.parent, root, root / ".run-id.lock", root / ".run-id.json"):
            assert windows_acl.describe(path).owner_sid == owner_before
            assert windows_acl.owner_only_dacl_matches(
                path, inherit=path.is_dir(), sids=("S-1-3-4", sid)
            )
        assert read_binding("wf_000001") is None


@pytest.mark.parametrize("component", ["directory", "lock", "counter"])
def test_allocator_lockdown_failure_does_not_issue_or_advance(monkeypatch, component):
    import kiro_crew.workflow_memory as wm

    assert allocate_run_id() == "wf_000001"
    root = _allocator_root()
    counter = root / ".run-id.json"
    before = counter.read_bytes()
    target = {
        "directory": root,
        "lock": root / ".run-id.lock",
        "counter": counter,
    }[component]
    helper = "restrict_dir_to_owner" if component == "directory" else "restrict_to_owner"
    real_restrict = getattr(wm.platform_compat, helper)

    def refuse(path):
        if Path(path) == target:
            raise OSError("injected lockdown failure")
        return real_restrict(path)

    with monkeypatch.context() as patch:
        patch.setattr(wm.platform_compat, helper, refuse)
        with pytest.raises(wm.WorkflowMemoryError, match="I/O failed"):
            allocate_run_id()
    assert counter.read_bytes() == before
    assert allocate_run_id() == "wf_000002"


@pytest.mark.parametrize("after_witness", [False, True])
def test_initial_sync_interruption_is_recoverable_without_reuse(monkeypatch, after_witness):
    import kiro_crew.workflow_memory as wm

    real_sync = wm.os.fsync
    real_write = wm.os.write
    published = False

    def write(fd, data):
        nonlocal published
        count = real_write(fd, data)
        if data == b"1":
            published = True
        return count

    def fail(fd):
        if published == after_witness:
            raise OSError("initial sync interrupted")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(wm.os, "write", write)
        patch.setattr(wm.os, "fsync", fail)
        with pytest.raises(wm.WorkflowMemoryError, match="I/O"):
            allocate_run_id()
    assert allocate_run_id() == "wf_000001"
    assert allocate_run_id() == "wf_000002"


def test_invalid_witness_never_resets_counter():
    from kiro_crew.workflow_memory import WorkflowMemoryError

    allocate_run_id()
    (_allocator_root() / ".run-id.lock").write_bytes(b"2")
    with pytest.raises(WorkflowMemoryError, match="witness"):
        allocate_run_id()
