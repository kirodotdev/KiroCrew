"""The gateway must stop Electron's Crashpad handler from following it into children."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import crashpad_inherit
from kiro_crew.crashpad_inherit import (
    CRASHPAD_MASKS,
    EXC_MASK_CRASH,
    EXC_MASK_RESOURCE,
    MACH_PORT_NULL,
    detach_inherited_crash_handler,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT


class _FakeLibc:
    """Records the Mach call instead of making it."""

    def __init__(self, kern_return: int = 0) -> None:
        self.calls: list[tuple] = []
        self._kr = kern_return
        self.mach_task_self = _Fn(lambda: 0x103)
        self.task_set_exception_ports = _Fn(self._set)

    def _set(self, task, mask, port, behavior, flavor):
        self.calls.append((task, mask, port, behavior, flavor))
        return self._kr


class _Fn:
    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *a):
        return self._fn(*a)


def test_noop_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[tuple] = []

    def _load(*a, **k):
        loaded.append(a)
        return _FakeLibc()

    monkeypatch.setattr(ctypes, "CDLL", _load)
    assert detach_inherited_crash_handler(platform="linux") is False
    assert detach_inherited_crash_handler(platform="win32") is False
    assert loaded == []


def test_clears_exactly_the_crashpad_masks_to_the_null_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = _FakeLibc()
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: libc)
    assert detach_inherited_crash_handler(platform="darwin") is True
    assert len(libc.calls) == 1
    task, mask, port, behavior, flavor = libc.calls[0]
    assert task == 0x103
    assert mask == CRASHPAD_MASKS == EXC_MASK_CRASH | EXC_MASK_RESOURCE
    # Only the two masks Crashpad registers; a wider mask would also wipe any
    # port a debugger or the runtime itself installed.
    assert mask & ~(EXC_MASK_CRASH | EXC_MASK_RESOURCE) == 0
    assert port == MACH_PORT_NULL
    assert behavior == crashpad_inherit.EXCEPTION_DEFAULT
    assert flavor == crashpad_inherit.THREAD_STATE_NONE


def test_reports_failure_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: _FakeLibc(kern_return=4))
    assert detach_inherited_crash_handler(platform="darwin") is False

    def _boom(*a, **k):
        raise OSError("no libSystem")

    monkeypatch.setattr(ctypes, "CDLL", _boom)
    assert detach_inherited_crash_handler(platform="darwin") is False


# Runs in a throwaway child process, never in the pytest worker: the detach
# edits process-global Mach exception ports, and a worker running under a
# debugger or its own Crashpad must keep whatever handler it had. The child
# detaches, reads its own ports, then spawns a grandchild that reads ITS ports
# -- the grandchild is the shape the fix exists for (kiro-cli, MCP servers).
_CHILD_SCRIPT = """\
import ctypes, json, subprocess, sys
from kiro_crew.crashpad_inherit import CRASHPAD_MASKS, detach_inherited_crash_handler


def read_crashpad_ports():
    libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    libc.mach_task_self.restype = ctypes.c_uint32
    masks = (ctypes.c_uint32 * 32)()
    ports = (ctypes.c_uint32 * 32)()
    behaviors = (ctypes.c_int32 * 32)()
    flavors = (ctypes.c_int32 * 32)()
    count = ctypes.c_uint32(32)
    kr = libc.task_get_exception_ports(
        libc.mach_task_self(), CRASHPAD_MASKS, masks, ctypes.byref(count), ports, behaviors, flavors
    )
    return {"kr": kr, "entries": [[masks[i], ports[i]] for i in range(count.value)]}


if sys.argv[1] == "grandchild":
    print(json.dumps(read_crashpad_ports()))
else:
    detached = detach_inherited_crash_handler()
    own = read_crashpad_ports()
    grandchild = subprocess.run(
        [sys.executable, sys.argv[0], "grandchild"], capture_output=True, text=True, timeout=60, check=True
    )
    print(json.dumps({"detached": detached, "own": own, "grandchild": json.loads(grandchild.stdout)}))
"""


def _assert_crashpad_ports_null(report: dict) -> None:
    assert report["kr"] == 0
    for mask, port in report["entries"]:
        if mask & CRASHPAD_MASKS:
            assert port == MACH_PORT_NULL


@pytest.mark.skipif(sys.platform != "darwin", reason="reads a real task's Mach exception ports")
def test_child_task_and_its_children_read_null_ports_afterwards(tmp_path: Path) -> None:
    script = tmp_path / "detach_probe.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    src_root = str(Path(kiro_crew.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src_root, env.get("PYTHONPATH")) if p)
    proc = subprocess.run(
        [sys.executable, str(script), "detach"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        **UTF8_TEXT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["detached"] is True
    _assert_crashpad_ports_null(report["own"])
    _assert_crashpad_ports_null(report["grandchild"])
