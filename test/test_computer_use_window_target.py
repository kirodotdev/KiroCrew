"""Window listing: ``computer_list_windows``.

``apps_macos.list_apps`` collapses an app to its frontmost titled window, so a small
helper window with a generic title (a 66x20 window titled "Window") can win the
collapse and hide the app's other windows from the model. This tool lists them.

The cases pinned below:

* an auxiliary window in front of the main one: both are listed, front to back;
* the target policy refuses a denied app before listing, and hides a denied window;
* the tool is approved per call, never auto-approved like the two observation tools;
* each platform lists only the windows of the resolved application.

Zero native calls: the dispatcher cases run on the shipped fake backend, and the
platform cases monkeypatch ``macos_ffi`` / ``windows_ffi``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from mcp_computer_helpers import fake_backend_fixture  # noqa: F401
from mcp_computer_helpers import home_fixture  # noqa: F401
from mcp_computer_helpers import keystone_fixture  # noqa: F401
from mcp_computer_helpers import _dispatch, _enable

from kiro_crew.computer_use import apps_macos, apps_windows, macos_ffi, render
from kiro_crew.computer_use import windows_ffi as win_ffi
from kiro_crew.computer_use.types import (
    ERROR_PREFIX,
    MUTATING_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_CLICK,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
    TOOL_LIST_WINDOWS,
    AppRef,
)
from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, _cu_read_only_auto_approve
from kiro_crew.platform.governance_parts.tool_calls import (
    CU_CLASS_MUTATE,
    CU_CLASS_OBSERVE,
    computer_use_action_classes,
)
from kiro_crew.testing.fake_computer_use import FAKE_FILES_APP, FakeComputerUseBackend

# A tiny helper window listed BEFORE the real document window.
_HELPER_ID = 9101
_MAIN_ID = FAKE_FILES_APP.window_id


def _stage_two_windows(fake: FakeComputerUseBackend) -> AppRef:
    """Put a helper window IN FRONT of Fake Files' main window; return the helper."""
    helper = replace(FAKE_FILES_APP, window_id=_HELPER_ID, window_title="Window")
    # The helper is the app's DEFAULT window (the one ``resolve_app`` returns), so the
    # document window is reachable only through the listing.
    fake.apps = (helper,) + tuple(a for a in fake.apps if a.key != FAKE_FILES_APP.key)
    fake.extra_windows = {FAKE_FILES_APP.key: (FAKE_FILES_APP,)}
    return helper


# ── dispatcher ──


def test_list_windows_shows_every_window_with_its_id(keystone: Path, fake_backend):
    _enable(keystone)
    _stage_two_windows(fake_backend)
    out = _dispatch(TOOL_LIST_WINDOWS, app=FAKE_FILES_APP.name)
    assert not out.startswith(ERROR_PREFIX), out
    assert f"window_id {_HELPER_ID}" in out
    assert f"window_id {_MAIN_ID}" in out
    # The helper is listed first: front to back, the order the window server reports.
    assert out.index(f"window_id {_HELPER_ID}") < out.index(f"window_id {_MAIN_ID}")


def test_list_windows_of_a_denied_app_is_refused(keystone: Path, fake_backend):
    """The target policy runs on the resolved app before its windows are listed."""
    _enable(keystone, extra_denied_apps=[FAKE_FILES_APP.bundle_id])
    _stage_two_windows(fake_backend)
    out = _dispatch(TOOL_LIST_WINDOWS, app=FAKE_FILES_APP.name)
    assert out.startswith(ERROR_PREFIX)
    assert not any(name == "list_windows" for name, _ in fake_backend.calls)


def test_list_windows_hides_a_window_whose_title_the_operator_denies(keystone: Path, fake_backend):
    """``extra_denied_apps`` matches window titles, so a sibling whose title it denies
    stays out of the listing exactly as ``computer_list_apps`` keeps it out, even
    though the resolved (default) window passes the policy."""
    _enable(keystone, extra_denied_apps=[FAKE_FILES_APP.window_title])
    _stage_two_windows(fake_backend)
    out = _dispatch(TOOL_LIST_WINDOWS, app=FAKE_FILES_APP.name)
    assert not out.startswith(ERROR_PREFIX), out
    assert f"window_id {_HELPER_ID}" in out
    assert f"window_id {_MAIN_ID}" not in out
    assert FAKE_FILES_APP.window_title not in out


def test_list_windows_hides_a_kiro_crew_window_of_the_app(keystone: Path, fake_backend):
    """The built-in floor applies per window too: a sibling showing Kiro Crew's own
    dashboard is left out of the listing with no operator pattern configured."""
    _enable(keystone)
    _stage_two_windows(fake_backend)
    own = replace(FAKE_FILES_APP, window_id=9102, window_title="Kiro Crew dashboard")
    fake_backend.extra_windows = {FAKE_FILES_APP.key: (FAKE_FILES_APP, own)}
    out = _dispatch(TOOL_LIST_WINDOWS, app=FAKE_FILES_APP.name)
    assert not out.startswith(ERROR_PREFIX), out
    assert f"window_id {_MAIN_ID}" in out
    assert "window_id 9102" not in out
    assert "Kiro Crew" not in out


def test_list_windows_of_an_app_showing_kiro_crew_is_refused(keystone: Path, fake_backend):
    """When the resolved window is Kiro Crew's own, the target policy refuses before
    any window of that process is listed."""
    _enable(keystone)
    own = replace(FAKE_FILES_APP, window_title="Kiro Crew")
    fake_backend.apps = (own,) + tuple(a for a in fake_backend.apps if a.key != own.key)
    out = _dispatch(TOOL_LIST_WINDOWS, app=FAKE_FILES_APP.name)
    assert out.startswith(ERROR_PREFIX)
    assert not any(name == "list_windows" for name, _ in fake_backend.calls)


def test_list_windows_of_an_unknown_app_is_refused(keystone: Path, fake_backend):
    _enable(keystone)
    out = _dispatch(TOOL_LIST_WINDOWS, app="No Such App")
    assert out.startswith(ERROR_PREFIX)
    assert not any(name == "list_windows" for name, _ in fake_backend.calls)


def test_list_windows_requires_an_app(keystone: Path, fake_backend):
    _enable(keystone)
    assert _dispatch(TOOL_LIST_WINDOWS).startswith(ERROR_PREFIX)


def test_an_app_with_no_listed_window_renders_an_empty_listing():
    """The window can close between resolving the app and reading its window list."""
    out = render.render_windows(FAKE_FILES_APP, ())
    assert out.startswith("0 on-screen window(s)")
    assert "window_id" not in out


# ── approval ──


def test_list_windows_is_not_an_observation():
    """``observe`` is the class the gate may approve without asking; listing an app's
    windows is approved per call instead, so it carries ``mutate`` like the tools that
    always ask, and the tool-set partition puts it with them."""
    assert computer_use_action_classes(TOOL_LIST_WINDOWS) == (CU_CLASS_MUTATE,)
    assert CU_CLASS_OBSERVE not in computer_use_action_classes(TOOL_LIST_WINDOWS)
    assert TOOL_LIST_WINDOWS in MUTATING_TOOLS
    assert TOOL_LIST_WINDOWS not in READ_ONLY_TOOLS


@pytest.mark.parametrize(
    ("tool", "auto_approved"),
    [
        (TOOL_LIST_APPS, True),
        (TOOL_GET_STATE, True),
        (TOOL_LIST_WINDOWS, False),
        (TOOL_CLICK, False),
    ],
)
def test_read_only_auto_approve_helper_passes_only_the_observation_tools(
    monkeypatch: pytest.MonkeyPatch, tool: str, auto_approved: bool
):
    """With Computer Use enabled, ``_cu_read_only_auto_approve`` passes the two
    observation tools and refuses ``computer_list_windows``, the same answer it gives
    a click."""
    from kiro_crew.computer_use import enable_state

    monkeypatch.setattr(enable_state, "is_enabled", lambda: True)
    assert _cu_read_only_auto_approve(f"mcp__kirocrew-computer__{tool}") is auto_approved


@pytest.mark.parametrize("kind", ["", "other"])
def test_the_gate_asks_for_list_windows_as_it_asks_for_a_click(kind: str):
    """At the PreToolUse gate the listing gets the same decision as a click for a
    call that carries no read-only kind: no auto-approval."""
    manager = HookManager()
    listing = manager.on_tool_call(f"mcp__kirocrew-computer__{TOOL_LIST_WINDOWS}", tool_kind=kind)
    click = manager.on_tool_call(f"mcp__kirocrew-computer__{TOOL_CLICK}", tool_kind=kind)
    assert listing.action == click.action
    assert listing.action != TOOL_AUTO_APPROVE


# ── macOS: window enumeration ──


def _mac_win(window_id: int, pid: int, title: str, bounds=(0.0, 0.0, 800.0, 600.0)):
    return macos_ffi.WindowInfo(
        window_id=window_id, pid=pid, owner_name="Notes", title=title, layer=0, bounds=bounds
    )


@pytest.fixture
def mac_windows(monkeypatch: pytest.MonkeyPatch):
    """A Notes process with a 66x20 helper in front of its main window."""
    windows = [
        _mac_win(71, 500, "Window", bounds=(10.0, 10.0, 66.0, 20.0)),
        _mac_win(72, 500, "All on My Mac", bounds=(0.0, 25.0, 1200.0, 800.0)),
        _mac_win(80, 501, "Other app"),
        macos_ffi.WindowInfo(
            window_id=90, pid=500, owner_name="Notes", title="menu", layer=25, bounds=None
        ),
    ]
    monkeypatch.setattr(macos_ffi, "window_list", lambda: list(windows))
    monkeypatch.setattr(apps_macos, "resolve_identity", lambda pid: apps_macos.AppIdentity())
    return windows


_NOTES = AppRef(name="Notes", pid=500, bundle_id="com.apple.Notes", window_id=71)


def test_macos_lists_every_layer_zero_window_of_the_process(mac_windows):
    listed = apps_macos.list_windows(_NOTES)
    assert [w.window_id for w in listed] == [71, 72]
    assert listed[0].bounds == (10.0, 10.0, 66.0, 20.0)


# ── Windows: the windows of one application ──


@contextmanager
def _null_scope():
    yield


def _w(hwnd: int, pid: int, title: str, exe: str = "notepad.exe") -> win_ffi.WindowInfo:
    return win_ffi.WindowInfo(
        hwnd=hwnd,
        root_hwnd=hwnd,
        pid=pid,
        title=title,
        class_name="Notepad",
        exe_name=exe,
        bounds=(0.0, 0.0, 100.0, 100.0),
    )


@pytest.fixture
def win_windows(monkeypatch: pytest.MonkeyPatch):
    infos = [
        _w(0x10, 42, "a.txt - Notepad"),
        _w(0x20, 42, "b.txt - Notepad"),
        _w(0x30, 43, "c.txt - Notepad"),
        # Two packaged apps behind one broker pid: each is its own application.
        _w(0x40, 7, "Settings", exe="ApplicationFrameHost.exe"),
        _w(0x50, 7, "HP Audio Control", exe="ApplicationFrameHost.exe"),
    ]
    monkeypatch.setattr(win_ffi, "window_list", lambda: list(infos))
    monkeypatch.setattr(win_ffi, "dpi_awareness_scope", _null_scope)
    monkeypatch.setattr(win_ffi, "window_bounds", lambda h: (1.0, 2.0, 3.0, 4.0))
    monkeypatch.setattr(win_ffi, "window_is_live", lambda h: True)
    return infos


def test_windows_lists_the_windows_of_the_same_process_only(win_windows):
    notepad = apps_windows.resolve_app("notepad")
    listed = apps_windows.list_windows(notepad)
    assert [w.window_id for w in listed] == [0x10, 0x20]
    assert listed[0].bounds == (1.0, 2.0, 3.0, 4.0)


def test_windows_lists_one_window_per_app_behind_one_broker(win_windows):
    """Settings and HP Audio Control share a pid; neither is a window of the other."""
    settings = apps_windows.resolve_app("Settings")
    assert [w.window_id for w in apps_windows.list_windows(settings)] == [0x40]


def test_windows_a_listed_title_reaches_that_window(win_windows):
    """On Windows ``app`` matches a title fragment, so a title from the listing
    resolves to that window rather than the app's first one."""
    assert apps_windows.resolve_app("b.txt - Notepad").window_id == 0x20
