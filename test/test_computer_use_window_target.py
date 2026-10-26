"""Window targeting: ``computer_list_windows`` and ``window_id``.

``apps_macos.list_apps`` collapses an app to its frontmost titled window, so a small
helper window with a generic title (a 66x20 window titled "Window") can win the
collapse. These tools let the model list an app's windows and name the one it means.

The cases pinned below:

* an auxiliary window in front of the main one: both are listed, and naming the main
  one's id reads the main one;
* window order or focus changes: actions that name the window stay bound to it;
* the selected window closes, or its id is recycled by another process: refused;
* a small, legitimate dialog stays selectable;
* a prohibited window anywhere in a macOS process still refuses the whole process.

Zero native calls: the dispatcher cases run on the shipped fake backend, and the
platform cases monkeypatch ``macos_ffi`` / ``windows_ffi``.
"""

from __future__ import annotations

import dataclasses
import types
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from mcp_computer_helpers import fake_backend_fixture  # noqa: F401
from mcp_computer_helpers import home_fixture  # noqa: F401
from mcp_computer_helpers import keystone_fixture  # noqa: F401
from mcp_computer_helpers import _dispatch, _enable

from kiro_crew.computer_use import apps_macos, apps_windows
from kiro_crew.computer_use import index as cu_index
from kiro_crew.computer_use import macos_ffi, policy, snapshot_macos, tools
from kiro_crew.computer_use import windows_ffi as win_ffi
from kiro_crew.computer_use.types import (
    ERROR_PREFIX,
    MAX_WINDOW_ID,
    SNAPSHOT_TTL_SECS,
    TOOL_CLICK,
    TOOL_GET_STATE,
    TOOL_LIST_WINDOWS,
    AppRef,
    ComputerUseError,
    PolicyConfig,
    WindowRef,
)
from kiro_crew.platform.governance_parts.tool_calls import (
    CU_CLASS_OBSERVE,
    computer_use_action_classes,
)
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.fake_computer_use import (
    FAKE_FILES_APP,
    FakeComputerUseBackend,
    FakeNode,
)

# The issue's shape: a tiny helper window listed BEFORE the real document window.
_HELPER_ID = 9101
_MAIN_ID = FAKE_FILES_APP.window_id


def _press_tree(title: str) -> FakeNode:
    """A one-button window whose title says which window a walk reached."""
    return FakeNode(
        role="AXWindow",
        title=title,
        frame=(0.0, 0.0, 900.0, 600.0),
        children=(FakeNode(role="AXButton", title=f"{title} button", actions=("AXPress",)),),
    )


def _stage_two_windows(fake: FakeComputerUseBackend) -> AppRef:
    """Put a helper window IN FRONT of Fake Files' main window; return the helper."""
    helper = replace(FAKE_FILES_APP, window_id=_HELPER_ID, window_title="Window")
    # The helper is the app's DEFAULT window (the one ``resolve_app`` returns), which
    # is exactly the situation the report describes.
    fake.apps = (helper,) + tuple(a for a in fake.apps if a.key != FAKE_FILES_APP.key)
    fake.extra_windows = {FAKE_FILES_APP.key: (FAKE_FILES_APP,)}
    fake.window_trees = {
        helper.window_key: _press_tree("Helper"),
        FAKE_FILES_APP.window_key: _press_tree("Main document"),
    }
    return helper


def test_the_multi_window_note_redacts_a_credential_in_the_app_label():
    """The note joins the reply after the tree's redaction pass, so a credential in
    the app's identity must be redacted in the note itself."""
    secret = "AKIAIOSFODNN7EXAMPLE"

    class _TwoWindows:
        def list_windows(self, app):
            return (WindowRef(window_id=1, title="One"), WindowRef(window_id=2, title="Two"))

    walked = replace(FAKE_FILES_APP, bundle_id=f"dev.kirocrew.key={secret}")
    note = tools._multi_window_note(_TwoWindows(), walked, PolicyConfig())  # type: ignore[arg-type]
    assert note, "two windows must produce the note"
    assert secret not in note
    assert f"window_id {walked.window_id}" in note


# ── dispatcher: discovery and selection ──


def test_list_windows_shows_every_window_with_its_id(keystone: Path, fake_backend):
    _enable(keystone)
    _stage_two_windows(fake_backend)
    out = _dispatch(TOOL_LIST_WINDOWS, app=FAKE_FILES_APP.name)
    assert not out.startswith(ERROR_PREFIX), out
    assert f"window_id {_HELPER_ID}" in out
    assert f"window_id {_MAIN_ID}" in out
    # The helper is listed first: front to back, the order the window server reports.
    assert out.index(f"window_id {_HELPER_ID}") < out.index(f"window_id {_MAIN_ID}")


def test_naming_the_main_window_reads_the_main_window(keystone: Path, fake_backend):
    _enable(keystone)
    _stage_two_windows(fake_backend)
    default = _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name)
    assert "Helper button" in default
    chosen = _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=_MAIN_ID)
    assert "Main document button" in chosen
    assert "Helper button" not in chosen
    assert f"Window id: {_MAIN_ID}." in chosen


def test_a_multi_window_snapshot_says_so(keystone: Path, fake_backend):
    """Without the note a model has no reason to look for the second window."""
    _enable(keystone)
    _stage_two_windows(fake_backend)
    out = _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name)
    assert "has 2 windows" in out
    assert TOOL_LIST_WINDOWS in out


def test_a_single_window_snapshot_carries_no_multi_window_note(keystone: Path, fake_backend):
    _enable(keystone)
    out = _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name)
    assert "windows; this is window_id" not in out
    assert f"Window id: {_MAIN_ID}." in out


def test_an_unknown_window_id_is_refused_before_any_walk(keystone: Path, fake_backend):
    _enable(keystone)
    _stage_two_windows(fake_backend)
    out = _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=4242)
    assert out.startswith(ERROR_PREFIX)
    assert "window_id 4242" in out and TOOL_LIST_WINDOWS in out
    assert not any(name == "snapshot" for name, _ in fake_backend.calls)


# ── dispatcher: actions stay bound to the chosen window ──


def test_an_action_naming_the_window_acts_on_that_window(keystone: Path, fake_backend):
    _enable(keystone)
    _stage_two_windows(fake_backend)
    _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=_MAIN_ID)
    out = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, window_id=_MAIN_ID, element_index=1)
    assert not out.startswith(ERROR_PREFIX), out
    # The refreshed tree is the main window's, not the helper's.
    assert "Main document button" in out and "Helper button" not in out


def test_a_focus_change_does_not_move_a_bound_action(keystone: Path, fake_backend):
    """The user brings the main window forward between the snapshot and the click:
    the app's default window changes, the action that names the helper stays put."""
    _enable(keystone)
    helper = _stage_two_windows(fake_backend)
    _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=_HELPER_ID)
    # Refocus: the main window becomes the app's default window, the helper an extra.
    fake_backend.apps = (FAKE_FILES_APP,) + tuple(
        a for a in fake_backend.apps if a.key != FAKE_FILES_APP.key
    )
    fake_backend.extra_windows = {FAKE_FILES_APP.key: (helper,)}
    out = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, window_id=_HELPER_ID, element_index=1)
    assert not out.startswith(ERROR_PREFIX), out
    assert "Helper button" in out and "Main document button" not in out


def test_an_action_without_window_id_names_the_window_its_indices_belong_to(
    keystone: Path, fake_backend
):
    """Indices from the main window must not be applied to the default (helper)
    window. The refusal says which window_id to pass instead of the bare
    "call computer_get_state first", which would lead to a snapshot of the helper."""
    _enable(keystone)
    _stage_two_windows(fake_backend)
    _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=_MAIN_ID)
    calls_before = len(fake_backend.calls)
    out = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=1)
    assert out.startswith(ERROR_PREFIX)
    assert f"window_id={_MAIN_ID}" in out
    assert not any(name == "click" for name, _ in fake_backend.calls[calls_before:])


def test_a_closed_window_refuses_the_action(keystone: Path, fake_backend):
    _enable(keystone)
    _stage_two_windows(fake_backend)
    _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=_MAIN_ID)
    fake_backend.extra_windows = {}  # the main window closed
    out = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, window_id=_MAIN_ID, element_index=1)
    assert out.startswith(ERROR_PREFIX)
    assert f"window_id {_MAIN_ID}" in out
    assert not any(name == "click" for name, _ in fake_backend.calls)


# ── dispatcher: policy and schema ──


def test_list_windows_of_a_denied_app_is_refused(keystone: Path, fake_backend):
    """The target policy runs on the resolved app before its windows are listed."""
    _enable(keystone, extra_denied_apps=[FAKE_FILES_APP.bundle_id])
    _stage_two_windows(fake_backend)
    out = _dispatch(TOOL_LIST_WINDOWS, app=FAKE_FILES_APP.name)
    assert out.startswith(ERROR_PREFIX)
    assert not any(name == "list_windows" for name, _ in fake_backend.calls)


def test_a_window_id_in_a_denied_app_gets_the_policy_refusal_whether_or_not_it_exists(
    keystone: Path, fake_backend
):
    """The target policy answers before the window id is checked. A "no window N"
    refusal from a denied app would tell the caller which ids exist in it."""
    _enable(keystone, extra_denied_apps=[FAKE_FILES_APP.bundle_id])
    _stage_two_windows(fake_backend)
    live = _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=_MAIN_ID)
    missing = _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=4242)
    assert live.startswith(ERROR_PREFIX) and missing.startswith(ERROR_PREFIX)
    assert "window_id 4242" not in missing
    assert live == missing
    assert not any(name == "snapshot" for name, _ in fake_backend.calls)


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


def test_the_multi_window_note_does_not_count_a_window_the_operator_denies(
    keystone: Path, fake_backend
):
    """The note counts the same windows ``computer_list_windows`` shows. Counting a
    denied sibling would disclose that it exists and point the model at a listing
    that does not show it."""
    _enable(keystone, extra_denied_apps=[FAKE_FILES_APP.window_title])
    _stage_two_windows(fake_backend)
    out = _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name)
    assert not out.startswith(ERROR_PREFIX), out
    assert "Helper button" in out
    assert "has 2 windows" not in out


def test_an_expired_snapshot_is_reported_as_old_not_redirected_to_a_sibling(
    keystone: Path, fake_backend, monkeypatch: pytest.MonkeyPatch
):
    """The default window's snapshot expires while a sibling's is still fresh. An
    action on the default window must get the "state is Ns old" refusal; pointing it
    at the sibling would apply the default window's indices to another window."""
    _enable(keystone)
    _stage_two_windows(fake_backend)
    clock = ManualClock(monotonic_start=1000.0)
    clock.install(monkeypatch, cu_index)
    real_snapshot = fake_backend.snapshot

    def snapshot_on_the_test_clock(app, req):
        result = real_snapshot(app, req)
        snap = dataclasses.replace(result.snapshot, captured_at=clock.monotonic())
        return dataclasses.replace(result, snapshot=snap)

    monkeypatch.setattr(fake_backend, "snapshot", snapshot_on_the_test_clock)
    _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name)
    clock.advance(60)
    _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=_MAIN_ID)
    clock.advance(SNAPSHOT_TTL_SECS - 55)
    out = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=1)
    assert out.startswith(ERROR_PREFIX)
    assert "old" in out
    assert "window_id=" not in out
    assert not any(name == "click" for name, _ in fake_backend.calls)


@pytest.mark.parametrize("bad", [-1, MAX_WINDOW_ID + 1, True, "12"])
def test_an_out_of_range_window_id_is_refused_by_the_schema(keystone: Path, fake_backend, bad):
    _enable(keystone)
    out = _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, window_id=bad)
    assert out.startswith(ERROR_PREFIX)
    assert fake_backend.calls == []


def test_list_windows_requires_an_app(keystone: Path, fake_backend):
    _enable(keystone)
    assert _dispatch(TOOL_LIST_WINDOWS).startswith(ERROR_PREFIX)


def test_list_windows_is_classified_as_an_observation():
    """So it is auto-approved like ``computer_list_apps`` instead of falling to the
    fail-closed ``mutate`` default an unregistered action gets."""
    assert computer_use_action_classes(TOOL_LIST_WINDOWS) == (CU_CLASS_OBSERVE,)


# ── macOS: window enumeration and selection ──


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


def test_macos_selects_the_main_window_by_id(mac_windows):
    chosen = apps_macos.resolve_window(_NOTES, 72)
    assert chosen.window_id == 72 and chosen.window_title == "All on My Mac"
    assert chosen.pinned_window is True
    assert chosen.pid == 500 and chosen.bundle_id == "com.apple.Notes"


def test_macos_keeps_a_small_dialog_selectable(mac_windows):
    assert apps_macos.resolve_window(_NOTES, 71).window_id == 71


@pytest.mark.parametrize("window_id", [99, 80])
def test_macos_refuses_a_window_that_is_gone_or_another_process(mac_windows, window_id):
    """80 is a live window, but of pid 501: a recycled id must not cross processes."""
    with pytest.raises(ComputerUseError, match=TOOL_LIST_WINDOWS):
        apps_macos.resolve_window(_NOTES, window_id)


def test_macos_a_denied_sibling_refuses_the_whole_process(monkeypatch):
    """Input is per pid on macOS, so selecting an innocuous window of a browser that
    also hosts Kiro Crew's dashboard must still be refused."""
    windows = [
        _mac_win(1, 637, "Inbox"),
        _mac_win(2, 637, "Kiro Crew - Settings"),
    ]
    monkeypatch.setattr(macos_ffi, "window_list", lambda: list(windows))
    chrome = AppRef(name="Google Chrome", pid=637, bundle_id="com.google.Chrome", window_id=1)
    picked = apps_macos.resolve_window(chrome, 1)
    assert policy.check_app(picked, PolicyConfig()) is not None


def test_a_real_click_on_a_pinned_window_is_refused_where_a_sibling_covers_it(monkeypatch):
    """A global click or drag carries no window: on a pinned window it must land on
    that window, not on a sibling of the same process lying over the point."""
    windows = [
        _mac_win(73, 500, "Sibling", bounds=(100.0, 100.0, 300.0, 300.0)),
        _mac_win(72, 500, "All on My Mac", bounds=(0.0, 25.0, 1200.0, 800.0)),
    ]
    monkeypatch.setattr(macos_ffi, "window_list", lambda: list(windows))
    over_sibling, clear = (150.0, 150.0), (900.0, 500.0)
    assert apps_macos.pid_owns_point(500, *over_sibling, window_id=72) is False
    assert apps_macos.pid_owns_point(500, *clear, window_id=72) is True
    # Without a pinned window the app-level rule still holds: any window of the pid.
    assert apps_macos.pid_owns_point(500, *over_sibling) is True


def test_an_app_post_point_on_a_pinned_window_must_not_be_covered_by_a_sibling(monkeypatch):
    """An app-scoped click goes to the app's own front window at the point, so on a
    pinned window a sibling of the same process covering the point refuses it.
    Another application's window over the point does not matter on this path."""
    windows = [
        _mac_win(66, 501, "Other app", bounds=(0.0, 0.0, 2000.0, 2000.0)),
        _mac_win(73, 500, "Sibling", bounds=(100.0, 100.0, 300.0, 300.0)),
        _mac_win(72, 500, "All on My Mac", bounds=(0.0, 25.0, 1200.0, 800.0)),
    ]
    monkeypatch.setattr(macos_ffi, "window_list", lambda: list(windows))
    assert apps_macos.pinned_window_tops_point(500, 72, 150.0, 150.0) is False
    assert apps_macos.pinned_window_tops_point(500, 72, 900.0, 500.0) is True
    assert apps_macos.pinned_window_tops_point(500, 72, 1900.0, 1900.0) is False


def test_the_driver_refuses_an_app_post_click_off_the_pinned_window(monkeypatch):
    from kiro_crew.computer_use import macos_driver
    from kiro_crew.computer_use.types import CLICK_METHOD_APP_POST, ClickRequest

    posted: list = []
    monkeypatch.setattr(macos_ffi, "post_mouse_click", lambda *a, **k: posted.append(a))
    monkeypatch.setattr(apps_macos, "pinned_window_tops_point", lambda pid, wid, x, y: False)
    pinned = replace(FAKE_FILES_APP, window_id=72, pinned_window=True)
    req = ClickRequest(method=CLICK_METHOD_APP_POST, point=(150.0, 150.0))
    result = macos_driver.MacOSBackend().click(pinned, None, req)
    assert result.ok is False and "window_id 72" in result.text
    assert posted == []
    # The unpinned default window keeps the app's own routing.
    unpinned = replace(FAKE_FILES_APP, window_id=72)
    assert macos_driver.MacOSBackend().click(unpinned, None, req).ok is True
    assert len(posted) == 1


def test_a_pinned_scroll_does_not_fall_back_to_a_process_wide_wheel_event(monkeypatch):
    """The wheel fallback carries no window, so the app may scroll a sibling while
    the reply refreshes the pinned window. Refused when pinned; kept when not."""
    from kiro_crew.computer_use import macos_driver
    from kiro_crew.computer_use.types import SCROLL_DOWN, ElementRec

    @contextmanager
    def _element(app, rec):
        yield object()

    wheels: list = []
    monkeypatch.setattr(macos_driver, "_addressed", _element)
    monkeypatch.setattr(macos_ffi, "ax_perform", lambda el, action: macos_ffi.AX_ERR_SUCCESS - 1)
    monkeypatch.setattr(macos_driver, "_scroll_by_page_area", lambda *a, **k: None)
    monkeypatch.setattr(macos_ffi, "ax_frame", lambda el: (10.0, 20.0, 300.0, 200.0))
    monkeypatch.setattr(macos_ffi, "post_scroll", lambda *a, **k: wheels.append(a))
    rec = ElementRec(index=3, role="AXScrollArea")
    pinned = replace(FAKE_FILES_APP, window_id=72, pinned_window=True)
    result = macos_driver.MacOSBackend().scroll(pinned, rec, SCROLL_DOWN, 1)
    assert result.ok is False and "window_id 72" in result.text
    assert wheels == []
    unpinned = replace(FAKE_FILES_APP, window_id=72)
    assert macos_driver.MacOSBackend().scroll(unpinned, rec, SCROLL_DOWN, 1).ok is True
    assert len(wheels) == 1


@pytest.mark.parametrize("verb", ["type_text", "press_key"])
def test_keystrokes_to_a_pinned_background_window_are_refused(monkeypatch, verb):
    """Keystrokes are posted to the process and land in its front window, so a
    pinned background window would forward them to a sibling the target policy
    never judged (possibly one an operator pattern denies). Refused before any
    focus change or keystroke; delivered when the pinned window is in front."""
    from kiro_crew.computer_use import apps_macos, macos_driver

    sent: list = []
    focused: list = []
    monkeypatch.setattr(macos_ffi, "post_text", lambda *a: sent.append(a))
    monkeypatch.setattr(macos_ffi, "post_key", lambda *a: sent.append(a))
    monkeypatch.setattr(macos_ffi, "ax_set_true", lambda *a: focused.append(a))
    front = {"id": 11}
    monkeypatch.setattr(
        apps_macos, "pinned_window_is_front", lambda pid, window_id: window_id == front["id"]
    )
    arg = "hello" if verb == "type_text" else "return"
    pinned = replace(FAKE_FILES_APP, window_id=72, pinned_window=True)
    result = getattr(macos_driver.MacOSBackend(), verb)(pinned, None, arg)
    assert result.ok is False and "window_id 72" in result.text
    assert sent == [] and focused == []
    front["id"] = 72
    assert getattr(macos_driver.MacOSBackend(), verb)(pinned, None, arg).ok is True
    assert len(sent) == 1


def test_the_front_window_check_reads_the_first_normal_layer_window_of_the_pid(monkeypatch):
    from kiro_crew.computer_use import apps_macos

    class _Info:
        def __init__(self, pid, window_id, layer):
            self.pid, self.window_id, self.layer = pid, window_id, layer

    normal = macos_ffi.CG_WINDOW_LAYER_NORMAL
    windows = [
        _Info(9, 1, normal),
        _Info(5, 30, normal + 3),
        _Info(5, 11, normal),
        _Info(5, 72, normal),
    ]
    monkeypatch.setattr(macos_ffi, "window_list", lambda: windows)
    assert apps_macos.pinned_window_is_front(5, 11) is True
    assert apps_macos.pinned_window_is_front(5, 72) is False

    def _boom():
        raise OSError("window server")

    monkeypatch.setattr(macos_ffi, "window_list", _boom)
    assert apps_macos.pinned_window_is_front(5, 11) is False


class _AxWin:
    def __init__(self, window_id: int, title: str = "", main: bool = False) -> None:
        self.window_id, self.title, self.main = window_id, title, main


@pytest.fixture
def ax(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(macos_ffi, "ax_window_id", lambda w: w.window_id)
    monkeypatch.setattr(macos_ffi, "ax_str", lambda w, attr: w.title)
    monkeypatch.setattr(macos_ffi, "ax_bool", lambda w, attr, default=False: w.main)


def test_a_pinned_window_missing_from_the_ax_tree_is_not_substituted(ax):
    windows = [_AxWin(71, "Window", main=False), _AxWin(73, "Other", main=True)]
    assert snapshot_macos._select_window(windows, 72, "All on My Mac", pinned=True) is None
    # The unpinned default still falls back, as before.
    assert snapshot_macos._select_window(windows, 72, "", pinned=False) is windows[1]


def _cg_windows(monkeypatch: pytest.MonkeyPatch, *infos: "tuple[int, str]", pid: int = 5):
    rows = [
        types.SimpleNamespace(
            pid=pid, window_id=wid, title=title, layer=macos_ffi.CG_WINDOW_LAYER_NORMAL
        )
        for wid, title in infos
    ]
    monkeypatch.setattr(macos_ffi, "window_list", lambda: rows)


def test_a_pinned_window_falls_back_to_its_title_only_without_window_ids(ax, monkeypatch):
    """When the private id symbol answers 0 for every window, the title prefix is the
    only correlation left; AXMain and the first window are still never used."""
    _cg_windows(monkeypatch, (71, "Other"), (72, "All on My Mac"))
    windows = [_AxWin(0, "Other", main=True), _AxWin(0, "All on My Mac – 6 notes")]
    chosen = snapshot_macos._select_window(windows, 72, "All on My Mac", pinned=True, pid=5)
    assert chosen is windows[1]
    assert snapshot_macos._select_window(windows, 72, "Nothing", pinned=True, pid=5) is None


def test_a_pinned_title_fallback_does_not_reach_a_sibling_after_the_window_closes(ax, monkeypatch):
    """Window 72 "Document A" closes between the window-list read and the AX walk.
    The only AX title left that starts with "Document A" is a sibling the target
    policy never checked, so the fallback must refuse instead of walking it."""
    _cg_windows(monkeypatch, (73, "Document A private"))
    windows = [_AxWin(0, "Document A private")]
    assert snapshot_macos._select_window(windows, 72, "Document A", pinned=True, pid=5) is None


def test_a_pinned_title_fallback_refuses_a_prefix_two_live_windows_share(ax, monkeypatch):
    """Both windows are live, but the AX tree only exposes one. The CoreGraphics list
    shows the prefix is not unique, so the AX match cannot be trusted to be 72."""
    _cg_windows(monkeypatch, (72, "Document A"), (73, "Document A private"))
    windows = [_AxWin(0, "Document A private")]
    assert snapshot_macos._select_window(windows, 72, "Document A", pinned=True, pid=5) is None


def test_a_pinned_title_fallback_refuses_when_the_pinned_window_was_retitled(ax, monkeypatch):
    """Window 72 is still live but now reads "Document B", so the one window left
    with the old prefix is sibling 73. The sole match has to be 72 itself."""
    _cg_windows(monkeypatch, (72, "Document B"), (73, "Document A private"))
    windows = [_AxWin(0, "Document B"), _AxWin(0, "Document A private")]
    assert snapshot_macos._select_window(windows, 72, "Document A", pinned=True, pid=5) is None


def test_a_pinned_window_past_the_default_inspection_cap_is_found(ax, monkeypatch):
    """``computer_list_windows`` lists every window, so a pinned id beyond the first
    ``MAX_WINDOWS_TO_INSPECT`` AX windows must still be selected; the unpinned default
    keeps its bounded scan."""
    count = snapshot_macos.MAX_WINDOWS_TO_INSPECT + 1
    windows = [_AxWin(1000 + i, f"Document {i}") for i in range(count)]
    released: list = []

    def retained(elem, name, *, limit=0):
        return list(windows if limit <= 0 else windows[:limit])

    monkeypatch.setattr(macos_ffi, "ax_retained_elements", retained)
    monkeypatch.setattr(macos_ffi, "same_element", lambda a, b: a is b)
    monkeypatch.setattr(macos_ffi, "release_all", lambda refs: released.extend(refs))
    target = windows[-1]
    chosen, err = snapshot_macos._frontmost_window(
        object(), window_id=target.window_id, window_title="", pinned=True
    )
    assert chosen is target and err == macos_ffi.AX_ERR_SUCCESS
    assert target not in released and len(released) == count - 1
    released.clear()
    unpinned, _ = snapshot_macos._frontmost_window(
        object(), window_id=target.window_id, window_title="", pinned=False
    )
    assert unpinned is not target


def test_a_pinned_title_shared_by_two_windows_selects_neither(ax):
    """Without window ids, two windows with the same title cannot be told apart, so
    a pinned selection is refused rather than handed the first one."""
    windows = [_AxWin(0, "Untitled document 1"), _AxWin(0, "Untitled document 1")]
    assert snapshot_macos._select_window(windows, 72, "Untitled document", pinned=True) is None
    assert (
        snapshot_macos._select_window(windows, 72, "Untitled document", pinned=False) is windows[0]
    )


class _AxElem:
    def __init__(self, owner: object) -> None:
        self.owner = owner


@pytest.fixture
def ax_focus(monkeypatch: pytest.MonkeyPatch):
    @contextmanager
    def owned(elem, attr):
        yield (elem.owner, macos_ffi.AX_ERR_SUCCESS if elem.owner is not None else -25205)

    monkeypatch.setattr(macos_ffi, "ax_owned_attr", owned)
    monkeypatch.setattr(macos_ffi, "same_element", lambda a, b: a is b)
    monkeypatch.setattr(snapshot_macos, "_selected_text", lambda focused, text_limit: "secret")


def test_a_pinned_snapshot_reports_no_selection_from_a_sibling_window(ax_focus):
    """The app's focused element sits in a sibling (possibly one the operator denies):
    a pinned snapshot of another window must not relay that sibling's selection."""
    chosen, sibling = object(), object()
    assert (
        snapshot_macos._window_selection(_AxElem(sibling), chosen, pinned=True, text_limit=50) == ""
    )
    assert snapshot_macos._window_selection(_AxElem(None), chosen, pinned=True, text_limit=50) == ""
    assert (
        snapshot_macos._window_selection(_AxElem(chosen), chosen, pinned=True, text_limit=50)
        == "secret"
    )
    # The unpinned default window keeps reporting the app's selection.
    assert (
        snapshot_macos._window_selection(_AxElem(sibling), chosen, pinned=False, text_limit=50)
        == "secret"
    )


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
    return infos


def test_windows_lists_the_windows_of_the_same_process_only(win_windows):
    notepad = apps_windows.resolve_app("notepad")
    assert [w.window_id for w in apps_windows.list_windows(notepad)] == [0x10, 0x20]


def test_windows_selects_a_window_and_refuses_another_processes(win_windows):
    notepad = apps_windows.resolve_app("notepad")
    assert apps_windows.resolve_window(notepad, 0x20).window_title == "b.txt - Notepad"
    with pytest.raises(ComputerUseError):
        apps_windows.resolve_window(notepad, 0x30)


def test_windows_a_selector_cannot_cross_apps_behind_one_broker(win_windows):
    """Settings and HP Audio Control share a pid; neither is a window of the other."""
    settings = apps_windows.resolve_app("Settings")
    assert [w.window_id for w in apps_windows.list_windows(settings)] == [0x40]
    with pytest.raises(ComputerUseError):
        apps_windows.resolve_window(settings, 0x50)
