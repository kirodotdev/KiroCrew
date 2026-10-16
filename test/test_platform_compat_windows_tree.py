"""Windows process trees: telling the owned tree from recycled PIDs, and ending it.

A stranger holding a recycled PID must not make the owned tree unkillable, a
handle read before its exit FILETIME is published is retried rather than
misread, a drain member that refuses ``TerminateProcess`` while exiting reads as
exited, and the descendant probes keep a failure's native error without
sensitive text. Most of these run on every platform through faked ``kernel32``
calls; the exit-FILETIME race and the protected-process premise need the real
kernel and run on the Windows shard. The Win32 struct layouts the probes share
are pinned to module scope. A topic of the ``test_platform_compat`` family; see
``test_platform_compat.py``.
"""

from __future__ import annotations

import os
import threading
import types

import pytest

from kiro_crew import platform_compat as pc

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_platform_compat family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestWindowsRecycledDescendantAgeDisproof:
    """A stranger holding a recycled PID must not make the owned tree unkillable.

    Toolhelp reports numeric parent PIDs. When a genuine intermediate exits, its
    PID can be reused by an unrelated, far older process whose stale parent field
    still names a PID inside this tree, so the numeric walk pulls that stranger
    in as a candidate. A termination handle on a stranger is refused, and an
    unopenable surviving candidate is a fatal incomplete tree -- so the provider
    returns without killing anything it actually owns.

    A process that already existed before the root cannot descend from it. The
    creation instant is readable through a query-only handle, which is granted
    where a termination handle is refused, so that disproof is available exactly
    when it is needed. Every other shape stays fail-closed.
    """

    ROOT_PID = 100
    ROOT_HANDLE = 8001
    ROOT_CREATED = 1_000

    def _install(self, monkeypatch, *, first_map, fresh_map, opens, identities, query_handles):
        """Drive the Windows branch on any host; return the observed call log."""

        scans = 0
        closed: list[int] = []
        query_opens: list[int] = []
        query_closed: list[int] = []

        def snapshot():
            nonlocal scans
            scans += 1
            return dict(first_map) if scans == 1 else dict(fresh_map)

        def open_termination(child_pid, **_kwargs):
            return opens.get(child_pid)

        def open_query(child_pid):
            query_opens.append(child_pid)
            return query_handles.get(child_pid)

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
        monkeypatch.setattr(pc, "_open_process_termination_handle", open_termination)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            lambda handle, **_kwargs: identities.get(handle),
        )
        monkeypatch.setattr(pc, "_open_process_query_handle", open_query)
        monkeypatch.setattr(pc, "_close_process_handle", query_closed.append)
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        monkeypatch.setattr(pc, "pid_exists", lambda _pid: False)
        return closed, query_opens, query_closed

    def test_older_unopenable_stranger_does_not_block_the_owned_tree(self, monkeypatch):
        closed, query_opens, query_closed = self._install(
            monkeypatch,
            first_map={101: self.ROOT_PID, 102: 101},
            fresh_map={101: self.ROOT_PID, 102: 101},
            opens={101: 9001},
            identities={
                self.ROOT_HANDLE: (self.ROOT_PID, self.ROOT_CREATED, None),
                9001: (101, 1_100, None),
                7102: (102, 500, None),
            },
            query_handles={102: 7102},
        )

        handles = pc.descendant_termination_handles(self.ROOT_PID, {}, self.ROOT_HANDLE)

        assert handles == {101: 9001}
        assert closed == []
        assert query_opens == [102]
        assert query_closed == [7102]

    def test_a_disproven_stranger_takes_its_own_numeric_subtree_with_it(self, monkeypatch):
        closed, _query_opens, _query_closed = self._install(
            monkeypatch,
            first_map={101: self.ROOT_PID, 102: 101, 103: 102},
            fresh_map={101: self.ROOT_PID, 102: 101, 103: 102},
            opens={101: 9001, 103: 9003},
            identities={
                self.ROOT_HANDLE: (self.ROOT_PID, self.ROOT_CREATED, None),
                9001: (101, 1_100, None),
                9003: (103, 1_200, None),
                7102: (102, 500, None),
            },
            query_handles={102: 7102},
        )

        handles = pc.descendant_termination_handles(self.ROOT_PID, {}, self.ROOT_HANDLE)

        # 103's only claimed route to the root runs through a process that
        # predates the root, so its own recent creation proves nothing.
        assert handles == {101: 9001}
        assert closed == [9003]

    @pytest.mark.parametrize(
        "created, query_handle",
        [
            pytest.param(1_000, 7102, id="equal_to_root"),
            pytest.param(1_050, 7102, id="later_than_root"),
            pytest.param(None, 7102, id="identity_unreadable"),
            pytest.param(None, None, id="query_handle_refused"),
        ],
    )
    def test_an_undisprovable_unopenable_candidate_stays_fail_closed(
        self, monkeypatch, created, query_handle
    ):
        identities = {
            self.ROOT_HANDLE: (self.ROOT_PID, self.ROOT_CREATED, None),
            9001: (101, 1_100, None),
        }
        if created is not None:
            identities[7102] = (102, created, None)
        closed, _query_opens, _query_closed = self._install(
            monkeypatch,
            first_map={101: self.ROOT_PID, 102: 101},
            fresh_map={101: self.ROOT_PID, 102: 101},
            opens={101: 9001},
            identities=identities,
            query_handles={102: query_handle} if query_handle else {},
        )

        with pytest.raises(OSError, match="Windows descendant handles unavailable"):
            pc.descendant_termination_handles(self.ROOT_PID, {}, self.ROOT_HANDLE)

        assert closed == [9001]

    def test_a_pinned_retained_identity_under_a_stranger_stays_fail_closed(self, monkeypatch):
        closed, _query_opens, _query_closed = self._install(
            monkeypatch,
            first_map={101: self.ROOT_PID, 102: 101, 103: 102},
            fresh_map={101: self.ROOT_PID, 102: 101, 103: 102},
            opens={101: 9001},
            identities={
                self.ROOT_HANDLE: (self.ROOT_PID, self.ROOT_CREATED, None),
                9001: (101, 1_100, None),
                9003: (103, 1_200, None),
                7102: (102, 500, None),
            },
            query_handles={102: 7102},
        )

        # Dropping 103 would discard authority an earlier scan already proved,
        # so the contradiction is reported rather than resolved by guessing.
        with pytest.raises(OSError, match="Windows descendant handles unavailable"):
            pc.descendant_termination_handles(self.ROOT_PID, {103: 9003}, self.ROOT_HANDLE)

        assert closed == [9001]

    def test_a_vanished_unopenable_candidate_is_not_queried_for_its_age(self, monkeypatch):
        closed, query_opens, _query_closed = self._install(
            monkeypatch,
            first_map={101: self.ROOT_PID, 102: 101},
            fresh_map={101: self.ROOT_PID},
            opens={101: 9001},
            identities={
                self.ROOT_HANDLE: (self.ROOT_PID, self.ROOT_CREATED, None),
                9001: (101, 1_100, None),
            },
            query_handles={},
        )

        handles = pc.descendant_termination_handles(self.ROOT_PID, {}, self.ROOT_HANDLE)

        # Fresh absence already accounts for this candidate; an exited process
        # has no readable creation instant to disprove anything with.
        assert handles == {101: 9001}
        assert closed == []
        assert query_opens == []

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Win32 handle-rights premise")
    def test_a_protected_process_refuses_termination_but_answers_its_creation(self):
        """Prove on a real kernel the premise every faked case above assumes.

        The disproof only ever runs for a candidate whose termination handle was
        refused, so it is worth nothing unless a creation instant is still
        readable for such a process. Were that false, the disproof would never
        fire on a real host and the recycled-PID abort would survive with every
        faked test above still green.

        The System process is the stable instance of that shape: terminating it
        is denied to every caller, while ``PROCESS_QUERY_LIMITED_INFORMATION``
        exists precisely so an unprivileged reader can still identify it. A
        positive instant here also proves the read validated its own handle,
        since an identity naming another PID is reported as unknown.
        """

        system_pid = 4
        granted = pc._open_process_termination_handle(system_pid)
        if granted is not None:
            pc.close_process_handle(granted)
        assert granted is None, "the System process granted a termination handle"

        instant = pc._windows_process_query_creation(system_pid)
        assert isinstance(instant, int) and instant > 0, (
            "a query-only handle could not read the System process creation instant, "
            "so the creation-order disproof cannot fire on this host"
        )


@pytest.mark.skipif(
    not pc.IS_WINDOWS,
    reason="exercises the real Windows ctypes identity path (ctypes.WinDLL, "
    "wintypes.FILETIME); the logic is Windows-native and runs on the Windows shard",
)
class TestWindowsHandleIdentityExitFiletimeRace:
    """GetExitCodeProcess reports the exit before the exit FILETIME is published.

    A handle read inside that window looks exited-with-exit_time==0. Treating it
    as "no identity" made ``descendant_termination_handles`` raise on a healthy
    tree, which surfaced as a ~1-in-3 false "Install Kiro CLI" on Windows.
    """

    # The pid every faked handle below reports.
    FAKE_PID = 4242

    @classmethod
    def _kernel32(cls, exit_filetimes):
        """Fake kernel32 replaying *exit_filetimes* from successive time reads.

        A ``0`` entry is the exited-but-unpublished window; a non-zero entry is a
        published exit FILETIME. The process always reports as exited.
        """

        reads = iter(exit_filetimes)

        class _Fn:
            """Stands in for a ctypes function pointer (assignable argtypes)."""

            argtypes: list = []
            restype = None

            def __init__(self, impl):
                self._impl = impl

            def __call__(self, *args):
                return self._impl(*args)

        def _get_process_times(_handle, creation, exit_, _kernel, _user):
            creation._obj.dwHighDateTime = 0
            creation._obj.dwLowDateTime = 100
            exit_._obj.dwHighDateTime = 0
            exit_._obj.dwLowDateTime = next(reads, 0)
            return 1

        def _get_exit_code(_handle, code):
            code._obj.value = 0  # any value but STILL_ACTIVE (259)
            return 1

        return types.SimpleNamespace(
            GetProcessId=_Fn(lambda _handle: cls.FAKE_PID),
            GetProcessTimes=_Fn(_get_process_times),
            GetExitCodeProcess=_Fn(_get_exit_code),
            # Liveness is decided by a zero-timeout wait on the process object,
            # because exit code 259 collides with STILL_ACTIVE. This fake's
            # process has exited, so its object is signalled: WAIT_OBJECT_0.
            WaitForSingleObject=_Fn(lambda _handle, _millis: 0x00000000),
        )

    def test_identity_retries_until_exit_filetime_is_published(self, monkeypatch):
        # First two reads land inside the unpublished window; the third has the
        # real exit time. The identity must be returned, not refused.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        fake = self._kernel32([0, 0, 0, 777])
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: fake)
        # flake-ok: no-ops the stdlib sleep the exit-FILETIME retry reads, for one synchronous call
        monkeypatch.setattr(pc.time, "sleep", lambda _s: None)

        identity = pc._windows_process_handle_identity(5)

        assert identity is not None
        pid, creation, exit_time = identity
        assert (pid, creation, exit_time) == (4242, 100, 777)

    def test_identity_gives_up_when_exit_filetime_never_publishes(self, monkeypatch):
        # A handle whose exit time never appears must still be refused, so the
        # PID-recycling guard the caller depends on is not weakened into a
        # blanket "assume it is fine".
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        fake = self._kernel32([0] * 500)
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: fake)
        # flake-ok: no-ops the stdlib sleep the exit-FILETIME retry reads, for one synchronous call
        monkeypatch.setattr(pc.time, "sleep", lambda _s: None)
        monkeypatch.setattr(pc, "_WINDOWS_EXIT_FILETIME_TIMEOUT_SECS", 0.01)

        assert pc._windows_process_handle_identity(5) is None

    def test_descendant_scan_does_not_raise_for_a_root_inside_the_window(
        self,
        monkeypatch,
    ):
        # The defect's actual blast radius: an exited root whose FILETIME has not
        # published yet must not make the scan raise "root handle identity
        # mismatch" at its caller, which is what failed the whole kiro-cli probe.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        fake = self._kernel32([0, 0, 555])
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: fake)
        # flake-ok: no-ops the stdlib sleep the exit-FILETIME retry reads, for one synchronous call
        monkeypatch.setattr(pc.time, "sleep", lambda _s: None)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: {})

        # 4242 is the pid the fake handle reports, so the root identity matches.
        assert pc.descendant_termination_handles(4242, {}, 8001) == {}

    def test_start_time_read_answers_without_sleeping(self, monkeypatch):
        # get_process_start_id documents itself as non-blocking and safe to call
        # from the event loop, and callers take it at its word from coroutines. A
        # pid whose exit FILETIME never publishes must therefore answer from the
        # creation half immediately: any sleep on this path stalls every other
        # task on the loop for the poll's whole bound.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        fake = self._kernel32([0] * 500)
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: fake)
        monkeypatch.setattr(pc, "_open_process_query_handle", lambda _pid: 5)
        monkeypatch.setattr(pc, "_close_process_handle", lambda _handle: None)

        slept: list[float] = []
        # flake-ok: records the retry's sleeps instead of sleeping, for one synchronous call
        monkeypatch.setattr(pc.time, "sleep", lambda secs: slept.append(secs))

        assert pc.process_start_time(self.FAKE_PID) == "100"
        assert slept == []

    def test_exit_bound_caller_still_waits_for_the_published_filetime(
        self,
        monkeypatch,
    ):
        # The creation-only mode is opt-in: a caller that must certify an exit
        # keeps the default, so the drain's exit bound stays a real published
        # FILETIME rather than the first unpublished read.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        fake = self._kernel32([0, 0, 888])
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: fake)
        # flake-ok: no-ops the stdlib sleep the exit-FILETIME retry reads, for one synchronous call
        monkeypatch.setattr(pc.time, "sleep", lambda _s: None)

        assert pc._windows_process_handle_identity(5) == (self.FAKE_PID, 100, 888)


class _RefusedProcessKernel32:
    """kernel32 for ONE process object whose terminate is refused, on a virtual clock.

    ``signals_at_ms`` is the instant after the refusal at which the object signals
    (``None``: never). ``WaitForSingleObject`` answers as the kernel does for that
    object: signalled when the instant falls inside the wait, otherwise a timeout once
    the whole wait has elapsed. So what a caller sees depends on how long it asks to
    wait, never on how fast this host runs. ``exit_code_after_refusal`` is what
    ``GetExitCodeProcess`` answers once the terminate has been refused; the read
    before it always answers STILL_ACTIVE. Every call after the refusal overwrites
    the last error, as ctypes' saved copy is overwritten by every call.
    """

    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    WAIT_FAILED = 0xFFFFFFFF

    class _Export:
        """A ctypes function pointer stand-in: ``argtypes``/``restype`` are assignable."""

        argtypes: list = []
        restype = None

        def __init__(self, impl):
            self._impl = impl

        def __call__(self, *args):
            return self._impl(*args)

    def __init__(
        self,
        *,
        signals_at_ms=None,
        exit_code_after_refusal=259,
        refusal=5,
        reread_ok=True,
        wait_fails=False,
    ):
        self.now_ms = 0
        self.signals_at_ms = signals_at_ms
        self.exit_code_after_refusal = exit_code_after_refusal
        self.refusal = refusal
        self.reread_ok = reread_ok
        self.wait_fails = wait_fails
        self.refused = False
        self.last_error = 0
        self.exit_code_reads = 0
        self.waits: list[int] = []
        self.GetExitCodeProcess = self._Export(self._get_exit_code)
        self.TerminateProcess = self._Export(self._terminate)
        self.WaitForSingleObject = self._Export(self._wait)

    def _get_exit_code(self, _handle, out):
        self.exit_code_reads += 1
        if not self.refused:
            out._obj.value = 259
            return 1
        self.last_error = 0
        out._obj.value = self.exit_code_after_refusal
        return 1 if self.reread_ok else 0

    def _terminate(self, _handle, _code):
        self.refused = True
        self.last_error = self.refusal
        return 0

    def _wait(self, _handle, millis):
        self.waits.append(int(millis))
        self.last_error = 0
        if self.wait_fails:
            return self.WAIT_FAILED
        if self.signals_at_ms is not None and self.signals_at_ms <= self.now_ms + millis:
            self.now_ms = max(self.now_ms, self.signals_at_ms)
            return self.WAIT_OBJECT_0
        self.now_ms += millis
        return self.WAIT_TIMEOUT


class TestTerminateRefusedOnAnExitingMember:
    """A drain member whose own exit has begun refuses ``TerminateProcess``.

    The kernel answers a terminate aimed at a process already in its exit path with
    ERROR_ACCESS_DENIED, the code a genuine refusal carries. That exit publishes the
    exit code, then runs the process down, and only then signals the object, and the
    refusal starts at the rundown. A console host leaving with its last client lands
    there between the drain's liveness read and its terminate, so a zero-time look
    at the object can still find it unsignalled.
    """

    @staticmethod
    def _install(monkeypatch, kernel32):
        """Route only this test thread's kernel32 and last error to *kernel32*."""
        import ctypes

        owner = threading.get_ident()
        real_windll = getattr(ctypes, "WinDLL", None)
        real_last_error = pc._windows_last_error

        def windll(name, **kwargs):
            if threading.get_ident() == owner:
                return kernel32
            if real_windll is None:
                raise AttributeError("WinDLL")
            return real_windll(name, **kwargs)

        facade = types.SimpleNamespace(**{**vars(ctypes), "WinDLL": windll})
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "ctypes", facade)
        monkeypatch.setattr(
            pc,
            "_windows_last_error",
            lambda: kernel32.last_error if threading.get_ident() == owner else real_last_error(),
        )

    def test_a_published_exit_code_reads_as_exited_without_waiting(self, monkeypatch):
        # The shard's interleaving: refused, exit code already published, object
        # not yet signalled. The exit code settles it, so nothing waits.
        kernel32 = _RefusedProcessKernel32(exit_code_after_refusal=1)
        self._install(monkeypatch, kernel32)

        assert pc.terminate_process_handle(9) is False
        assert kernel32.waits == []

    def test_a_member_whose_exit_code_is_259_reads_as_exited_once_it_signals(self, monkeypatch):
        # Exit code 259 is indistinguishable from STILL_ACTIVE, so the signal
        # decides: the object signals 50 ms after the refusal, inside the wait.
        kernel32 = _RefusedProcessKernel32(signals_at_ms=50)
        self._install(monkeypatch, kernel32)

        assert pc.terminate_process_handle(9) is False
        assert kernel32.waits == [pc._WINDOWS_TERMINATE_REFUSAL_WAIT_MS]

    def test_a_refusal_of_a_live_process_still_raises_after_one_bounded_wait(self, monkeypatch):
        kernel32 = _RefusedProcessKernel32()
        self._install(monkeypatch, kernel32)

        with pytest.raises(OSError, match="TerminateProcess failed") as raised:
            pc.terminate_process_handle(9)

        # The terminate's own error, not the last error the re-read and wait left.
        assert raised.value.errno == 5
        assert kernel32.waits == [pc._WINDOWS_TERMINATE_REFUSAL_WAIT_MS]
        assert kernel32.now_ms == pc._WINDOWS_TERMINATE_REFUSAL_WAIT_MS
        # Bounded well inside one drain pass, so a refusal never spends the pass.
        assert pc._WINDOWS_TERMINATE_REFUSAL_WAIT_MS < pc._WINDOWS_TREE_REAP_TIMEOUT_SECS * 1000

    def test_on_the_event_loop_a_refusal_is_never_waited_out(self, monkeypatch):
        import asyncio

        kernel32 = _RefusedProcessKernel32(signals_at_ms=50)
        self._install(monkeypatch, kernel32)

        async def terminate_on_the_loop():
            return pc.terminate_process_handle(9)

        with pytest.raises(OSError, match="TerminateProcess failed"):
            asyncio.run(terminate_on_the_loop())
        assert kernel32.waits == [0]

    def test_on_the_event_loop_an_already_signalled_refusal_reads_as_exited(self, monkeypatch):
        # The zero-time look still answers: an object already signalled is an exit.
        import asyncio

        kernel32 = _RefusedProcessKernel32(signals_at_ms=0)
        self._install(monkeypatch, kernel32)

        async def terminate_on_the_loop():
            return pc.terminate_process_handle(9)

        assert asyncio.run(terminate_on_the_loop()) is False
        assert kernel32.waits == [0]

    def test_any_other_refusal_raises_at_once(self, monkeypatch):
        kernel32 = _RefusedProcessKernel32(signals_at_ms=0, refusal=6)  # ERROR_INVALID_HANDLE
        self._install(monkeypatch, kernel32)

        with pytest.raises(OSError, match="TerminateProcess failed") as raised:
            pc.terminate_process_handle(9)

        assert raised.value.errno == 6
        assert kernel32.exit_code_reads == 1
        assert kernel32.waits == []

    def test_an_unreadable_exit_code_falls_back_to_the_signal(self, monkeypatch):
        kernel32 = _RefusedProcessKernel32(
            signals_at_ms=0, exit_code_after_refusal=1, reread_ok=False
        )
        self._install(monkeypatch, kernel32)

        assert pc.terminate_process_handle(9) is False
        assert kernel32.waits == [pc._WINDOWS_TERMINATE_REFUSAL_WAIT_MS]

    def test_an_object_that_cannot_be_waited_on_still_raises(self, monkeypatch):
        kernel32 = _RefusedProcessKernel32(wait_fails=True)
        self._install(monkeypatch, kernel32)

        with pytest.raises(OSError, match="TerminateProcess failed") as raised:
            pc.terminate_process_handle(9)

        assert raised.value.errno == 5


class TestCtypesStructsAreModuleScoped:
    """``ctypes.POINTER(T)`` memoises T -> POINTER(T) forever.

    ctypes keeps that memo in a module-level dict with no eviction, so a
    Structure subclass declared inside a function body pins a fresh pair of type
    objects on EVERY call. The leak is ctypes', not Win32's, so this covers the
    Mach layouts too. The Windows metrics/enumeration helpers are polled
    (the dashboard's system-metrics endpoint, the RSS-recycle watchdog, the
    tree-kill parent-map walk, the MCP pipe's per-connection peer check), which
    turned that into unbounded growth in a long-running gateway -- measured at
    ~8 KiB per ``proc_rss_bytes`` call, never reclaimed.

    Asserting on the source keeps this enforceable from the POSIX fleet, where
    the Windows branches never execute.
    """

    #: Helpers whose ctypes struct layouts must come from module scope.
    _CTYPES_STRUCT_USERS = (
        "get_ppid",
        "_windows_process_parent_map",
        "_win_process_image_name",
        "_process_token_sid_unguarded",
        "proc_rss_bytes",
        "proc_peak_rss_bytes",
        "_windows_memory_counters",
        "_macos_current_rss_bytes",
        "proc_rss_bytes_for_pid",
        "system_memory",
        "apply_job_limits",
        "resume_process_main_thread",
        # Mach, not Win32: same memo, same unbounded growth. This one is polled by
        # the sub-agent auto-sizer and by the xdist worker budget.
        "macos_vm_statistics",
    )

    def test_the_shared_layouts_are_defined_once_at_module_scope(self) -> None:
        import ctypes

        for name in (
            "_ProcessEntry32",
            "_ProcessMemoryCounters",
            "_MemoryStatusEx",
            "_SidAndAttributes",
            "_TokenUser",
            "_IoCounters",
            "_JobObjectBasicLimitInformation",
            "_JobObjectExtendedLimitInformation",
            "_ThreadEntry32",
            "_VMStatistics64",
            "_MachTimeValue",
            "_MachTaskBasicInfo",
        ):
            assert issubclass(getattr(pc, name), ctypes.Structure), name

    @pytest.mark.parametrize("func_name", _CTYPES_STRUCT_USERS)
    def test_no_helper_declares_a_structure_in_its_body(self, func_name: str) -> None:
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(pc, func_name))))
        local_structs = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(base, ast.Attribute) and base.attr in ("Structure", "Union")
                for base in node.bases
            )
        ]
        assert not local_structs, (
            f"{func_name} declares {local_structs} in its body; each call would pin a new "
            "type in ctypes' pointer-type memo. Hoist the layout to module scope."
        )

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Win32 metrics paths")
    def test_repeated_metrics_calls_add_no_pointer_memo_entries(self) -> None:
        """The behavioural half: polling must not grow ctypes' memo at all."""
        import ctypes

        memo = ctypes._pointer_type_cache  # type: ignore[attr-defined]
        pid = os.getpid()
        probes = (
            pc.proc_rss_bytes,
            pc.proc_peak_rss_bytes,
            lambda: pc.proc_rss_bytes_for_pid(pid),
            pc.system_memory,
            lambda: pc.get_ppid(pid),
            lambda: pc.process_owner_sid(pid),
        )
        for probe in probes:
            probe()  # a first call may legitimately populate the memo once
        before = len(memo)
        for _ in range(25):
            for probe in probes:
                probe()
        assert len(memo) == before


class TestWindowsDescendantFailureDiagnostics:
    @staticmethod
    def kernel(monkeypatch, open_result=0, exception=None):
        class Call:
            def __init__(self, fn):
                self.fn = fn

            def __call__(self, *args):
                return self.fn(*args)

        calls = []

        def open_process(access, inherit, pid):
            calls.append((access, inherit, pid))
            if exception is not None:
                raise exception
            return open_result

        kernel = types.SimpleNamespace(OpenProcess=Call(open_process))
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
        return calls

    @pytest.mark.parametrize("error", [5, 87])
    def test_opener_captures_immediate_native_error(self, monkeypatch, error):
        calls = self.kernel(monkeypatch)
        monkeypatch.setattr(pc, "_windows_last_error", lambda: error)
        failure = []
        assert pc._open_process_termination_handle(101, failure=failure) is None
        assert failure == [f"winerror={error}"]
        assert calls == [(0x101001, False, 101)]

    def test_opener_records_exception_type_not_sensitive_text(self, monkeypatch):
        self.kernel(monkeypatch, exception=RuntimeError("sensitive-secret"))
        failure = []
        assert pc._open_process_termination_handle(101, failure=failure) is None
        assert failure == ["exception=RuntimeError"]

    def test_success_does_not_collect_diagnostics(self, monkeypatch):
        self.kernel(monkeypatch, open_result=9001)
        monkeypatch.setattr(pc, "_windows_last_error", lambda: pytest.fail("not a failure"))
        failure = []
        assert pc._open_process_termination_handle(101, failure=failure) == 9001
        assert failure == []

    @pytest.mark.parametrize("identity", [(101, 20, None), (101, 20, 30), None])
    def test_query_handle_is_query_only_and_always_closed(self, monkeypatch, identity):
        calls = self.kernel(monkeypatch, open_result=9002)
        closed = []
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: identity)
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        assert pc._windows_process_query_diagnostic(101) == f"identity={identity}"
        assert calls == [(0x1000, False, 101)]
        assert closed == [9002]

    def test_query_exception_is_sanitized_and_handle_closed(self, monkeypatch):
        self.kernel(monkeypatch, open_result=9002)
        closed = []

        def unreadable(_handle):
            raise RuntimeError("sensitive-secret")

        monkeypatch.setattr(pc, "_windows_process_handle_identity", unreadable)
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        assert pc._windows_process_query_diagnostic(101) == "exception=RuntimeError"
        assert closed == [9002]

    def test_query_denial_is_unknown_not_death(self, monkeypatch):
        self.kernel(monkeypatch)
        monkeypatch.setattr(pc, "_windows_last_error", lambda: 5)
        assert pc._windows_process_query_diagnostic(101) == "winerror=5"

    @pytest.mark.parametrize(
        "query",
        ["identity=(101, 20, 30)", "identity=(101, 20, None)", "winerror=5", "identity=None"],
    )
    def test_still_present_candidate_always_refuses_with_bounded_evidence(self, monkeypatch, query):
        scans = 0
        queried = []
        closed = []

        def snapshot():
            nonlocal scans
            scans += 1
            return {101: 102, 102: 100, 999: 1}

        def open_handle(_pid, *, failure=None):
            failure.append("winerror=5")
            return None

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
        monkeypatch.setattr(pc, "_open_process_termination_handle", open_handle)
        monkeypatch.setattr(
            pc, "_windows_process_handle_identity", {8001: (100, 10, None), 9001: (102, 15, 40)}.get
        )
        monkeypatch.setattr(
            pc, "_windows_process_query_diagnostic", lambda pid: queried.append(pid) or query
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        with pytest.raises(OSError, match="Windows descendant handles unavailable") as exc:
            pc.descendant_termination_handles(100, {102: 9001}, 8001)
        text = str(exc.value)
        assert "open=winerror=5" in text
        assert "first_chain=[101, 102, 100]" in text
        assert "fresh_chain=[101, 102, 100]" in text
        assert "100: (100, 10, None)" in text
        assert "102: (102, 15, 40)" in text
        assert query in text
        assert "999" not in text
        assert queried == [101]
        assert closed == []
        assert scans == 2

    def test_diagnostic_failure_cannot_replace_refusal(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: {101: 100})
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _p, **_k: None)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (100, 10, None))

        def broken(*_a, **_k):
            raise RuntimeError("sensitive-secret")

        monkeypatch.setattr(pc, "_windows_descendant_failure_details", broken)
        with pytest.raises(
            OSError, match="Windows descendant handles unavailable: \\[101\\]"
        ) as exc:
            pc.descendant_termination_handles(100, {}, 8001)
        assert "sensitive-secret" not in str(exc.value)

    def test_error_survives_later_snapshot_and_query_calls(self, monkeypatch):
        calls = self.kernel(monkeypatch)
        error = [5]
        scans = [0]
        monkeypatch.setattr(pc, "_windows_last_error", lambda: error[0])

        def snapshot():
            scans[0] += 1
            error[0] = 5 if scans[0] == 1 else 87
            return {101: 100}

        monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (100, 10, None))
        with pytest.raises(OSError) as exc:
            pc.descendant_termination_handles(100, {}, 8001)
        assert "open=winerror=5" in str(exc.value)
        assert "query_unvalidated=winerror=87" in str(exc.value)
        # Termination open, then the creation-order read that tries to disprove
        # this candidate's ancestry, then the diagnostic's own unvalidated look.
        assert calls == [
            (0x101001, False, 101),
            (0x1000, False, 101),
            (0x1000, False, 101),
        ]

    def test_diagnostics_bound_candidates_and_ancestry(self, monkeypatch):
        queried = []
        monkeypatch.setattr(
            pc,
            "_windows_process_query_diagnostic",
            lambda pid: queried.append(pid) or "identity=None",
        )
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (100, 10, None))
        parents = {pid: pid + 1 for pid in range(101, 140)}
        details = pc._windows_descendant_failure_details(
            {101, 110, 120, 130}, parents, parents, {100: 8001}, (100, 10, None), {}
        )
        assert queried == [101, 110, 120]
        assert "total=4" in details
        assert "first_chain=[101, 102, 103, 104, 105, 106, 107, 108]" in details
        assert "109" not in details
        assert "130" not in details
        assert len(details) < 1500

    @pytest.mark.parametrize("vanished", [True, False])
    def test_success_never_queries_or_logs_diagnostics(self, monkeypatch, caplog, vanished):
        scans = [0]
        closed = []

        def snapshot():
            scans[0] += 1
            return {} if vanished and scans[0] > 1 else {101: 100}

        def forbidden(*_a, **_k):
            pytest.fail("successful discovery must not run diagnostics")

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
        monkeypatch.setattr(
            pc, "_open_process_termination_handle", lambda _p, **_k: None if vanished else 9001
        )
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            {8001: (100, 10, None), 9001: (101, 20, None)}.get,
        )
        monkeypatch.setattr(pc, "_windows_descendant_failure_details", forbidden)
        monkeypatch.setattr(pc, "_windows_process_query_diagnostic", forbidden)
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        assert pc.descendant_termination_handles(100, {}, 8001) == ({} if vanished else {101: 9001})
        assert closed == []
        assert caplog.records == []

    def test_failure_evidence_sink_cannot_change_opener_result(self, monkeypatch):
        self.kernel(monkeypatch)
        monkeypatch.setattr(pc, "_windows_last_error", lambda: 5)

        class BrokenList(list):
            def append(self, _item):
                raise RuntimeError("sensitive-secret")

        assert pc._open_process_termination_handle(101, failure=BrokenList()) is None
