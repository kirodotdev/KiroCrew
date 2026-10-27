"""Windows native-profile admission and protected-document publication fence.

Read leases last for the native launcher's lifetime. A writer takes an exclusive
lease; the kernel then refuses new readers until publication/activation finishes.
The file pins remain unchanged. This module also runs inside the isolated stdlib
launcher, so package imports belong inside host-only functions.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import ctypes
import hashlib
import json
import os
import stat
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DOCUMENTS = ("config.json", "config.local.json", "agent_model_state.json")
GATE_LEAF = "native-settings.lock"
STATUS_LEAF = "native-settings-status.json"
STATUS_LOCK_LEAF = "native-settings-status.lock"
_UNSET = object()


class NativeSettingsTransitionBusy(OSError):
    """Another live gateway owns this home's transition."""


class NativeSettingsSuperseded(OSError):
    """A stale transition must not overwrite its successor's status."""


class NativeSettingsRestartRequired(PermissionError):
    def __init__(self, step: str = "save") -> None:
        self.step = step
        super().__init__(
            "Native Windows sessions must finish or be explicitly restarted before these settings can change"
        )

    def response(self) -> dict:
        return {
            "ok": False,
            "status": "restart_required",
            "saved": False,
            "active": False,
            "restart_required": True,
            "code": "native_settings_restart_required",
            "failed_step": self.step,
            "action": "restart_and_apply",
        }


def _home() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home()


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


def prepare_gate(home: Path | None = None) -> Path:
    return _prepare_lock_file(home or _home(), GATE_LEAF)


def _prepare_lock_file(home: Path, name: str) -> Path:
    root = home / "run"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / name
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
        from kiro_crew import platform_compat

        platform_compat.restrict_to_owner(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & 0x400
    ):
        raise OSError("Native settings admission file is aliased")
    return path


@contextlib.contextmanager
def _open(path: Path, *, write: bool) -> Iterator[int]:
    kernel = _kernel()
    handle = kernel.CreateFileW(
        str(path), 0x40000000 if write else 0x80000000, 0 if write else 1, None, 3, 0x00200000, None
    )
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        raise NativeSettingsRestartRequired("admission")
    try:
        yield int(handle)
    finally:
        kernel.CloseHandle(handle)


def status(home: Path | None = None) -> dict:
    try:
        value = json.loads(((home or _home()) / "run" / STATUS_LEAF).read_text("utf-8"))
        if isinstance(value, dict):
            return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        pass
    return {
        "status": "activation_failed",
        "saved": False,
        "active": False,
        "starts_blocked": True,
        "failed_step": "read_activation_state",
        "action": "restart_and_review",
    }


@contextlib.contextmanager
def _status_lock(home: Path):
    from kiro_crew import platform_compat

    path = _prepare_lock_file(home, STATUS_LOCK_LEAF)
    with (
        path.open("a+b") as file,
        platform_compat.file_lock(file.fileno(), exclusive=True, required=True, timeout=2),
    ):
        yield


def transition_owner_alive(record: dict) -> bool:
    """Unknown identity is busy, never authority to retire a foreign actor."""
    from kiro_crew import platform_compat

    pid, start = record.get("owner_pid"), record.get("owner_start")
    if type(pid) is not int or pid <= 1 or not isinstance(start, str) or not start:
        return True
    current = platform_compat.get_process_start_id(pid)
    if current is None:
        return platform_compat.pid_exists(pid)
    return current == start and platform_compat.pid_exists(pid)


def claim_transition(value: dict, home: Path | None = None) -> dict:
    """Cross-process compare-and-set before any session stop or config write."""
    root = home or _home()
    with _status_lock(root):
        previous = status(root)
        if (
            previous.get("status") in {"stopping", "saved"}
            and previous.get("starts_blocked")
            and transition_owner_alive(previous)
        ):
            raise NativeSettingsTransitionBusy("Another Crew gateway is changing native settings")
        result = with_owner_identity(
            {**value, "prior_admission_blocked": bool(previous.get("starts_blocked"))}
        )
        _record_status(result, root)
        return result


def with_owner_identity(value: dict) -> dict:
    from kiro_crew import platform_compat

    return {
        **value,
        "owner_pid": os.getpid(),
        "owner_start": platform_compat.get_process_start_id(os.getpid()),
    }


def record_status(value: dict, home: Path | None = None, *, expected_transition=_UNSET) -> None:
    root = home or _home()
    with _status_lock(root):
        if (
            expected_transition is not _UNSET
            and status(root).get("transition_id") != expected_transition
        ):
            raise NativeSettingsSuperseded("Native settings transition was superseded")
        _record_status(value, root)


def _record_status(value: dict, home: Path) -> None:
    from kiro_crew.atomic_write import atomic_write

    atomic_write(
        home / "run" / STATUS_LEAF, json.dumps(value, sort_keys=True), restrict_to_owner=True
    )


def pins_present(home: Path | None = None) -> bool:
    if os.name != "nt":
        return False
    root = home or _home()
    try:
        with write_transaction(root):
            for name in DOCUMENTS:
                with _delete_preflight(root / name):
                    pass
        return False
    except NativeSettingsRestartRequired:
        return True


@contextlib.contextmanager
def read_gate(path: Path) -> Iterator[None]:
    with _open(path, write=False):
        if status(path.parent.parent).get("starts_blocked") is True:
            raise NativeSettingsRestartRequired("activation")
        yield


def snapshot(home: Path | None = None) -> dict[str, str | None]:
    root = home or _home()
    result: dict[str, str | None] = {}
    for name in DOCUMENTS:
        try:
            result[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        except FileNotFoundError:
            result[name] = None
    return result


def verify_snapshot(path: Path, expected: dict[str, str | None]) -> None:
    if snapshot(path.parent.parent) != expected:
        raise NativeSettingsRestartRequired("stale_start")


@dataclass
class _Permit:
    home: Path
    active: bool = True


_PERMIT: contextvars.ContextVar[_Permit | None] = contextvars.ContextVar(
    "native_settings_permit", default=None
)


@contextlib.contextmanager
def write_transaction(home: Path | None = None) -> Iterator[None]:
    """Only this calling context and its workers inherit the revocable permit."""
    root = home or _home()
    if permitted() and root == _home():
        yield
        return
    permit = _Permit(root)
    with _open(prepare_gate(root), write=True):
        token = _PERMIT.set(permit)
        try:
            yield
        finally:
            permit.active = False
            _PERMIT.reset(token)


async def _settle(task: asyncio.Task):
    """Finish owned handle I/O even if the awaiting task is cancelled."""
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


@contextlib.asynccontextmanager
async def async_write_transaction(home: Path | None = None):
    """Acquire/close the OS lease off-loop; bind its permit in the caller."""
    root = home or _home()
    if permitted() and root == _home():
        yield
        return

    def acquire():
        lease = _open(prepare_gate(root), write=True)
        lease.__enter__()
        return lease

    lease, cancelled = await _settle(asyncio.create_task(asyncio.to_thread(acquire)))
    if cancelled:
        await _settle(asyncio.create_task(asyncio.to_thread(lease.__exit__, None, None, None)))
        raise asyncio.CancelledError
    permit = _Permit(root)
    token = _PERMIT.set(permit)
    try:
        yield
    finally:
        permit.active = False
        _PERMIT.reset(token)
        _, cancelled = await _settle(
            asyncio.create_task(asyncio.to_thread(lease.__exit__, None, None, None))
        )
        if cancelled:
            raise asyncio.CancelledError


def permitted() -> bool:
    permit = _PERMIT.get()
    return permit is not None and permit.active and permit.home == _home()


def _protected(path: Path, home: Path) -> bool:
    # Also recognize a supported config symlink's resolved destination.
    return any(path.resolve() == (home / name).resolve() for name in DOCUMENTS)


@contextlib.contextmanager
def _delete_preflight(path: Path) -> Iterator[None]:
    if not path.exists():
        yield
        return
    kernel = _kernel()
    handle = kernel.CreateFileW(str(path), 0x00010000, 7, None, 3, 0x00200000, None)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        raise NativeSettingsRestartRequired("release_file_pins")
    # Close BEFORE replace. Windows refuses replacing a target with an open
    # DELETE handle even when that handle shares DELETE. The exclusive gate
    # prevents a new native launcher from taking file pins in this window.
    kernel.CloseHandle(handle)
    yield


@contextlib.contextmanager
def guarded_replace(source: Path | str, destination: Path | str) -> Iterator[None]:
    if os.name != "nt" or not ({Path(source).name, Path(destination).name} & set(DOCUMENTS)):
        yield
        return
    home = _home()
    targets = [path for path in (Path(source), Path(destination)) if _protected(path, home)]
    if not targets:
        yield
        return
    with contextlib.ExitStack() as stack:
        if not permitted():
            stack.enter_context(write_transaction(home))
            if status(home).get("starts_blocked"):
                raise NativeSettingsRestartRequired("activation")
        for path in targets:
            stack.enter_context(_delete_preflight(path))
        yield


@contextlib.contextmanager
def guarded_config_write(path: Path) -> Iterator[None]:
    """Cover the loader's supported config-symlink target before it is resolved."""
    if os.name != "nt" or not _protected(path, _home()):
        yield
        return
    with contextlib.ExitStack() as stack:
        if not permitted():
            stack.enter_context(write_transaction())
            if status().get("starts_blocked"):
                raise NativeSettingsRestartRequired("activation")
        stack.enter_context(_delete_preflight(path.resolve()))
        yield
