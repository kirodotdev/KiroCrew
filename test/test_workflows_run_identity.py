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
        _write_run_high_water(root / ".run-id.json", 8, root.parent)
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


@pytest.fixture
def linked_default_home(tmp_path, monkeypatch, request):
    """The default data home reached THROUGH a link, as an operator's layout has it.

    ``home``: ``$HOME`` itself is a link (``/home/u -> /local/home/u`` on a cloud
    desktop). ``crew``: the data home ``~/.kiro/crew`` is a link onto another disk.
    Both sit at or above the workflows directory, so a link there is the
    operator's deployment choice, the same two layouts ``atomic_write``'s
    parent-link guard documents as supported. ``KIROCREW_HOME`` is dropped
    because its override branch resolves the path and never had this bug; the
    memoised default home is reset with it so the test reads its own home.

    Returns ``(lexical_root, real_root)`` -- the workflows directory as named
    through the link, and where it really lives.
    """
    from conftest import make_dir_link
    from kiro_crew.config import paths as config_paths

    real_home = tmp_path / "home-real"
    real_home.mkdir()
    if request.param == "home":
        home = tmp_path / "home-link"
        make_dir_link(home, real_home)
        real_crew = real_home / ".kiro" / "crew"
    else:
        home = real_home
        (home / ".kiro").mkdir()
        real_crew = tmp_path / "another-disk"
        real_crew.mkdir()
        make_dir_link(home / ".kiro" / "crew", real_crew)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(config_paths, "_resolved_home", None)
    monkeypatch.setattr(config_paths, "_config_dir_memo", None)
    return home / ".kiro" / "crew" / "workflows", real_crew / "workflows"


@pytest.mark.parametrize("linked_default_home", ["home", "crew"], indirect=True)
def test_workflow_allocator_allocates_under_a_linked_data_home(linked_default_home):
    lexical, real = linked_default_home
    assert _allocator_root() == lexical
    assert lexical.resolve() != lexical

    assert allocate_run_id() == "wf_000001"
    assert allocate_run_id() == "wf_000002"
    assert (real / ".run-id.lock").read_bytes() == b"1"
    assert (real / ".run-id.json").read_text(encoding="utf-8") == '{"version": 1, "high_water": 2}'


@pytest.mark.parametrize("linked_default_home", ["home"], indirect=True)
@pytest.mark.parametrize("name", [".run-id.lock", ".run-id.json"])
def test_linked_data_home_still_refuses_a_planted_file_link(linked_default_home, tmp_path, name):
    """The trusted link at ``$HOME`` does not extend trust to the allocator's files."""
    from kiro_crew.workflow_memory import WorkflowMemoryError

    _, real = linked_default_home
    assert allocate_run_id() == "wf_000001"
    path = real / name
    original = path.read_bytes()
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(original)
    try:
        path.symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("real file symlinks require Windows Developer Mode")
        raise
    with pytest.raises(WorkflowMemoryError):
        allocate_run_id()
    assert outside.read_bytes() == original


@pytest.mark.parametrize("linked_default_home", ["home"], indirect=True)
def test_linked_data_home_still_refuses_a_redirected_workflows_dir(linked_default_home, tmp_path):
    from conftest import make_dir_link
    from kiro_crew.workflow_memory import WorkflowMemoryError

    lexical, real = linked_default_home
    real.parent.mkdir(parents=True, exist_ok=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    make_dir_link(real, foreign)
    with pytest.raises(WorkflowMemoryError, match="redirected"):
        allocate_run_id()
    assert not list(foreign.iterdir())


@pytest.mark.asyncio
async def test_admission_errors_name_allocator_failures_and_log_them(caplog):
    import logging

    from kiro_crew.workflow_memory import WorkflowAllocatorError, admission_errors

    @admission_errors
    async def start():
        try:
            raise OSError("disk says no")
        except OSError as exc:
            raise WorkflowAllocatorError("Workflow allocator I/O failed") from exc

    with caplog.at_level(logging.WARNING, logger="kiro_crew.workflow_memory"):
        result = await start()
    assert result["ok"] is False
    assert result["admission_rejected"] is True
    assert result["code"] == "workflow_allocator_unavailable"
    assert "Workflow allocator I/O failed" in result["error"]
    assert result["errors"] == [result["error"]]
    assert "Global fallback" not in result["error"]
    assert "disk says no" not in result["error"]
    logged = [record.getMessage() for record in caplog.records]
    assert any(
        "Workflow allocator I/O failed" in line and "disk says no" in line for line in logged
    )


@pytest.mark.asyncio
async def test_admission_errors_name_the_memory_refusal_reason_and_log_it(caplog):
    import logging

    from kiro_crew.workflow_memory import WorkflowMemoryError, admission_errors

    @admission_errors
    async def start():
        raise WorkflowMemoryError("Workflow caller and delivery memory must match")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.workflow_memory"):
        result = await start()
    message = (
        "Workflow memory is unavailable: Workflow caller and delivery memory must match. "
        "No Global fallback was used."
    )
    assert result == {
        "ok": False,
        "error": message,
        "errors": [message],
        "code": "workflow_memory_unavailable",
        "admission_rejected": True,
    }
    assert any("must match" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["a member cannot use the Global store", "member has no persisted identity"]
)
async def test_admission_errors_name_the_member_binding_reason_and_its_remedy(reason):
    from kiro_crew.execution_context import _unavailable
    from kiro_crew.workflow_memory import admission_errors

    @admission_errors
    async def start():
        raise _unavailable(reason)

    result = await start()
    assert result["ok"] is False
    assert result["admission_rejected"] is True
    assert result["code"] == "workflow_memory_unavailable"
    assert result["errors"] == [result["error"]]
    error = result["error"]
    assert error.startswith(f"Workflow memory is unavailable: {reason}. ")
    assert "No Global fallback was used." in error
    assert "start a chat with no mate picked" in error
    assert "pick a mate that has its own memory" in error
    # The shared wrapper is stripped, so neither half of it appears twice.
    assert error.count("memory is unavailable") == 1
    assert "Global was not used" not in error


@pytest.mark.asyncio
async def test_admission_errors_name_a_wrapped_memory_cause_but_not_an_os_cause():
    from kiro_crew.execution_context import _unavailable
    from kiro_crew.workflow_memory import WorkflowMemoryError, admission_errors

    @admission_errors
    async def wrapper_only():
        try:
            raise _unavailable("member database is unreadable")
        except ValueError as exc:
            raise WorkflowMemoryError(
                "Workflow memory is unavailable; Global was not used"
            ) from exc

    @admission_errors
    async def wrapped_member_binding():
        try:
            raise _unavailable("a member cannot use the Global store")
        except ValueError as exc:
            raise _unavailable("malformed execution context") from exc

    @admission_errors
    async def os_cause():
        try:
            raise OSError("/private/path says no")
        except OSError as exc:
            raise WorkflowMemoryError(
                "Workflow memory is unavailable; Global was not used"
            ) from exc

    named = await wrapper_only()
    assert named["error"] == (
        "Workflow memory is unavailable: member database is unreadable. "
        "No Global fallback was used."
    )
    binding = await wrapped_member_binding()
    assert binding["error"].startswith(
        "Workflow memory is unavailable: malformed execution context "
        "(a member cannot use the Global store). No Global fallback was used. "
    )
    assert "start a chat with no mate picked" in binding["error"]
    hidden = await os_cause()
    assert hidden["error"] == "Workflow memory is unavailable; no Global fallback was used."
    assert "/private/path" not in hidden["error"]


@pytest.mark.asyncio
async def test_admission_errors_keep_a_path_a_memory_store_refusal_formats_in_the_log(
    tmp_path, caplog
):
    import logging

    from kiro_crew.memory_stores import (
        LEGACY_MEMBER_MANIFEST,
        UnknownMemoryStore,
        _legacy_member_manifest_owner,
    )
    from kiro_crew.workflow_memory import WorkflowMemoryError, admission_errors

    # A directory where the manifest file belongs makes the real raise site
    # format an OSError, and the path it names, into the refusal text.
    (tmp_path / LEGACY_MEMBER_MANIFEST).mkdir()
    with pytest.raises(UnknownMemoryStore) as raised:
        _legacy_member_manifest_owner(tmp_path)
    refusal = raised.value
    assert tmp_path.name in str(refusal)

    @admission_errors
    async def direct():
        raise refusal

    @admission_errors
    async def wrapped():
        try:
            raise refusal
        except ValueError as exc:
            raise WorkflowMemoryError(
                "Workflow memory is unavailable; Global was not used"
            ) from exc

    for start in (direct, wrapped):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.workflow_memory"):
            error = (await start())["error"]
        assert error == "Workflow memory is unavailable; no Global fallback was used."
        assert any(tmp_path.name in record.getMessage() for record in caplog.records)


def test_every_named_memory_refusal_reason_is_fixed_text():
    """The admission error names only reasons fixed at their raise site.

    ``memory_refusal_message`` shows an execution-context refusal's reason and a
    ``WorkflowMemoryError``'s text to the caller unredacted, which is safe only
    while every raise site passes fixed text rather than formatting an
    exception or a path into it.
    """
    import ast

    import kiro_crew

    package = Path(kiro_crew.__file__).resolve().parent
    sites = {"_unavailable": [package / "execution_context.py"], "WorkflowMemoryError": []}
    for path in sorted(package.rglob("*.py")):
        if "WorkflowMemoryError(" in path.read_text(encoding="utf-8", errors="replace"):
            sites["WorkflowMemoryError"].append(path)

    formatted: list[str] = []
    checked = 0
    for name, paths in sites.items():
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == name
                ):
                    continue
                checked += 1
                fixed = len(node.args) == 1 and (
                    (isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str))
                    or isinstance(node.args[0], ast.Name)
                )
                if not fixed:
                    formatted.append(f"{path.relative_to(package)}:{node.lineno}")
    assert checked > 20
    assert not formatted, f"memory refusals built from formatted text: {formatted}"
