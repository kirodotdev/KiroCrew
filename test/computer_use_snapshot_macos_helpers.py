"""Shared fixture and staging helpers for the macOS snapshot suites.

``test_computer_use_snapshot_macos.py`` and its topic siblings
``test_computer_use_snapshot_macos_element_actions.py`` and
``test_computer_use_snapshot_macos_tree_content.py`` import what they use from here by
name: the ``fakes`` fixture, which installs the fake frameworks from
``test_computer_use_ffi.py`` as if on macOS with the Electron opt-in wait and poll zeroed;
the ``_APP`` target; ``_req``, which builds a ``SnapshotRequest``; and ``_stage_window``,
``_stage_element`` and ``_frame``, which stage a window, its elements and their
``AXValue``-boxed frames. Because ``fakes`` replaces ``ctypes.CDLL`` and ``find_library``,
``test_darwin_libc_handle_ratchet.py`` requires this module to carry the
``fresh_darwin_libc_handles`` ``pytestmark`` as well. This module is never collected, so
the reset runs through the same mark in each test file. It is a plain module, not a
conftest.
"""

from __future__ import annotations

import ctypes

# Reuse the harness rather than forking a second set of fakes: a divergent copy
# would drift from the real symbol table and quietly stop proving anything.
import pytest

# The fake-framework harness is shared with ``test_computer_use_ffi`` rather than
# forked (a divergent copy would drift from the real symbol table and quietly stop
# proving anything). Imported by MODULE NAME, not as ``test.test_computer_use_ffi``:
# ``test/`` has no ``__init__.py``, so it is not a package — the dotted form happens
# to resolve when the whole suite is collected from the repo root and fails with
# ``ModuleNotFoundError`` under CI's sharded collection, which is exactly how it
# broke on the 3.10 shard. pytest inserts each test file's directory on
# ``sys.path`` (rootdir-relative ``consider_namespace_packages``-free import mode),
# so the bare module name is the form that always resolves.
from test_computer_use_ffi import _H_ELEMENT, _Fakes, _in_dll

from kiro_crew import platform_compat
from kiro_crew.computer_use import macos_ffi, permissions
from kiro_crew.computer_use.types import AppRef, SnapshotRequest

# This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
# so the shared macOS handle caches are put back after each one (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


_APP = AppRef(name="Fixture", pid=4242, bundle_id="dev.kirocrew.fixture", window_id=77)


@pytest.fixture(name="fakes")
def fakes_fixture(monkeypatch: pytest.MonkeyPatch):
    """Install the fake frameworks and force a re-bind (see test_computer_use_ffi)."""
    fake = _Fakes()
    # The fake trees are deliberately smaller than ELECTRON_STUB_NODE_THRESHOLD, so
    # nearly every test took the opt-in retry path and slept its full 2s budget --
    # ~2s x 119 tests, the slowest file in the suite. Nothing asserts on the poll
    # interval, only on the retry behaviour, which still runs.
    monkeypatch.setattr(macos_ffi, "ELECTRON_OPT_IN_WAIT_SECS", 0.0)
    monkeypatch.setattr(macos_ffi, "ELECTRON_OPT_IN_POLL_SECS", 0.0)
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(macos_ffi.platform_compat, "IS_MACOS", True, raising=False)
    monkeypatch.setattr(permissions.platform_compat, "IS_MACOS", True, raising=False)
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: f"/fake/{name}")
    monkeypatch.setattr(ctypes, "CDLL", lambda path, *a, **k: fake.by_name(path))
    monkeypatch.setattr(
        ctypes.c_void_p,
        "in_dll",
        staticmethod(lambda lib, name: _in_dll(lib, name)),
        raising=False,
    )
    macos_ffi.reset_frameworks()
    yield fake
    macos_ffi.reset_frameworks()


def _frame(fake: _Fakes, handle: int, x: float, y: float, w: float, h: float) -> None:
    """Give *handle* an ``AXPosition``/``AXSize`` pair, as real AX does.

    Both are ``AXValue`` BOXES, not plain numbers — staging them any other way
    would sidestep the type check ``macos_ffi._unbox_ax_value`` exists for and the
    test would pass against a path production never takes.
    """
    fake.ax.attrs[(handle, "AXPosition")] = fake.ax.ax_value(macos_ffi.K_AX_VALUE_CG_POINT, x, y)
    fake.ax.attrs[(handle, "AXSize")] = fake.ax.ax_value(macos_ffi.K_AX_VALUE_CG_SIZE, w, h)


def _stage_window(
    fake: _Fakes,
    *,
    children: list[dict] | None = None,
    bounds: tuple[float, float, float, float] | None = None,
) -> int:
    """Stage one app element owning one window, with an optional child list.

    Returns the window handle. Each child dict may carry ``role``, ``subrole``,
    ``title``, ``value``, ``actions``, ``frame``, ``selected``, ``expanded``,
    ``settable``, ``rows`` and ``visible_children``.

    *bounds* gives the WINDOW its own rect, which is what turns element frames on:
    without it ``_local_frame`` has no origin to subtract and every record's
    ``frame`` is ``None`` — the deliberate degradation, and the default here so
    existing cases keep asserting exactly what they asserted before.
    """
    window = fake.new_element()
    # The app element handle the fake's AXUIElementCreateApplication returns.
    app_handle = _H_ELEMENT
    fake.ax.attrs[(app_handle, "AXWindows")] = fake.new_array([window])
    fake.ax.attrs[(window, "AXRole")] = fake.new_string("AXWindow")
    fake.ax.attrs[(window, "AXTitle")] = fake.new_string("Fixture Window")
    if bounds is not None:
        _frame(fake, window, *bounds)

    kids: list[int] = []
    for spec in children or []:
        child = _stage_element(fake, spec)
        kids.append(child)
    if kids:
        fake.ax.attrs[(window, "AXChildren")] = fake.new_array(kids)
    return window


def _stage_element(fake: _Fakes, spec: dict) -> int:
    """Stage one element from a spec dict and return its handle.

    Split out of ``_stage_window`` so a test can build a nested shape (a row inside
    a pressable wrapper, a table whose rows live in ``AXRows``) without hand-rolling
    the attribute staging and getting the AXValue boxing subtly wrong.
    """
    handle = spec.get("handle") or fake.new_element()
    fake.ax.attrs[(handle, "AXRole")] = fake.new_string(spec.get("role", "AXGroup"))
    if spec.get("subrole"):
        fake.ax.attrs[(handle, "AXSubrole")] = fake.new_string(spec["subrole"])
    if spec.get("title"):
        fake.ax.attrs[(handle, "AXTitle")] = fake.new_string(spec["title"])
    if spec.get("value"):
        fake.ax.attrs[(handle, "AXValue")] = fake.new_string(spec["value"])
    if spec.get("actions") is not None:
        fake.ax.actions[handle] = fake.new_array([fake.new_string(a) for a in spec["actions"]])
    if spec.get("frame") is not None:
        _frame(fake, handle, *spec["frame"])
    if spec.get("selected") is not None:
        fake.ax.attrs[(handle, "AXSelected")] = fake.new_bool(spec["selected"])
    if spec.get("expanded") is not None:
        fake.ax.attrs[(handle, "AXExpanded")] = fake.new_bool(spec["expanded"])
    if spec.get("settable"):
        fake.ax.settable[(handle, "AXValue")] = True
    for attr, key in (("AXChildren", "children"), ("AXRows", "rows")):
        if spec.get(key):
            nested = [_stage_element(fake, s) for s in spec[key]]
            fake.ax.attrs[(handle, attr)] = fake.new_array(nested)
    if spec.get("visible_children"):
        # Staged as HANDLES, not specs, so a test can put the SAME row in both
        # AXRows and AXVisibleChildren — which is the shape the identity dedup
        # exists for, and which nested specs could not express.
        fake.ax.attrs[(handle, "AXVisibleChildren")] = fake.new_array(
            list(spec["visible_children"])
        )
    return handle


def _req(**kw) -> SnapshotRequest:
    base = dict(max_nodes=200, max_depth=32, text_limit=500, want_image=False)
    base.update(kw)
    return SnapshotRequest(**base)
