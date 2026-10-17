# Fixtures for semgrep/mock-pid-allocatable.yaml, exercised by `semgrep --test`
# in the SAST job. `ruleid:` asserts the NEXT line MUST match; `ok:` asserts it
# must NOT. The negatives encode the precision contract, and the pairs either
# side of 2**32 pin the threshold itself: move it and this file goes red.

from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import MagicMock as _Double

_UNALLOCATABLE_PID = 99_999_999_999


def _attribute_assignment_carries_an_allocatable_pid() -> None:
    proc = MagicMock()
    # ruleid: kirocrew.mock-pid-allocatable
    proc.pid = 4242
    # ruleid: kirocrew.mock-pid-allocatable
    proc.pid = 12345


def _constructor_keyword_carries_an_allocatable_pid() -> None:
    # ruleid: kirocrew.mock-pid-allocatable
    MagicMock(pid=4321)
    # ruleid: kirocrew.mock-pid-allocatable
    AsyncMock(pid=100, returncode=0)
    # ruleid: kirocrew.mock-pid-allocatable
    mock.MagicMock(pid=99999)
    # ruleid: kirocrew.mock-pid-allocatable
    SimpleNamespace(pid=54321, returncode=None)


def _reserved_pids_are_not_a_safe_spelling() -> None:
    # kill_pid passes its argument straight to os.kill on POSIX, where 0
    # addresses the caller's own process group.
    fake = MagicMock()
    # ruleid: kirocrew.mock-pid-allocatable
    fake.pid = 0
    # ruleid: kirocrew.mock-pid-allocatable
    fake.pid = 1


def _the_threshold_itself() -> None:
    proc = MagicMock()
    # The largest pid Linux will allocate with pid_max at its own ceiling.
    # ruleid: kirocrew.mock-pid-allocatable
    proc.pid = 4194304
    # One below the floor: still inside the range a Windows DWORD pid occupies.
    # ruleid: kirocrew.mock-pid-allocatable
    proc.pid = 4294967295
    # The floor itself, and the first value above every platform's range.
    # ok: kirocrew.mock-pid-allocatable
    proc.pid = 4294967296
    # ok: kirocrew.mock-pid-allocatable
    proc.pid = 99_999_999_999


def _the_convention_spelling_is_clean() -> None:
    proc = MagicMock()
    # ok: kirocrew.mock-pid-allocatable
    proc.pid = _UNALLOCATABLE_PID
    # ok: kirocrew.mock-pid-allocatable
    MagicMock(pid=_UNALLOCATABLE_PID)
    # ok: kirocrew.mock-pid-allocatable
    AsyncMock(pid=_UNALLOCATABLE_PID, returncode=0)


def _non_literals_and_non_bindings_are_clean(other, signal: int) -> None:
    import os

    proc = MagicMock()
    # A pid the OS really issued, copied rather than fabricated.
    # ok: kirocrew.mock-pid-allocatable
    proc.pid = other.pid
    # A read, not a binding.
    # ok: kirocrew.mock-pid-allocatable
    os.kill(other.pid, signal)
    # No pid at all.
    # ok: kirocrew.mock-pid-allocatable
    MagicMock(returncode=0)
    # Not an int, so the helpers refuse it outright on the type check.
    # ok: kirocrew.mock-pid-allocatable
    proc.pid = True


def _a_plain_call_is_not_a_test_double(make_record) -> None:
    # The rule's shape is a fabricated double, not any keyword named pid: a
    # record builder taking a pid the OS issued is ordinary test data.
    # ok: kirocrew.mock-pid-allocatable
    make_record(pid=4242)


def _a_literal_laundered_through_a_variable() -> None:
    # Constant propagation is intraprocedural and on by default, so binding the
    # literal to a name first does not hide it. That is the same hazard, and the
    # rule sees through it.
    proc = MagicMock()
    pid = 4242
    # ruleid: kirocrew.mock-pid-allocatable
    proc.pid = pid


def _a_pid_the_os_issued_stays_clean(spawn) -> None:
    proc = MagicMock()
    live = spawn().pid
    # Nothing to propagate: the value is not a literal anywhere in this frame.
    # ok: kirocrew.mock-pid-allocatable
    proc.pid = live


def _folded_arithmetic_is_still_a_literal() -> None:
    proc = MagicMock()
    # Folded to 4206929, which clears Linux pid_max but is still inside the
    # 32-bit range a Windows pid occupies, so it is not a safe spelling.
    # ruleid: kirocrew.mock-pid-allocatable
    proc.pid = 2**22 + 12345
    # ok: kirocrew.mock-pid-allocatable
    proc.pid = 2**32 + 12345


def _a_negative_pid_is_the_worst_case() -> None:
    proc = MagicMock()
    # os.kill(-1, sig) signals every process this uid owns, and a negative pid
    # is a process-group address rather than a process.
    # ruleid: kirocrew.mock-pid-allocatable
    proc.pid = -1


def _an_aliased_import_does_not_hide_it() -> None:
    # Only the qualified alternative reaches this one. Semgrep resolves the
    # local alias back to the module path, so the name the rule tests is
    # MagicMock even though the source says _Double.
    # ruleid: kirocrew.mock-pid-allocatable
    _Double(pid=4242)


def _a_constructor_with_no_module_to_resolve() -> None:
    # The mirror case: a name with nothing importable behind it, so resolution
    # yields no module path and only the unqualified alternative reaches it.
    # Both alternatives are therefore load-bearing, in opposite directions.
    class NonCallableMock:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    # ruleid: kirocrew.mock-pid-allocatable
    NonCallableMock(pid=4242)
