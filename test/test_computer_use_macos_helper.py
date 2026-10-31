"""Behavioural tests for the macOS :class:`MacOSHelper` and its permission glue.

agent-desktop reads the accessibility tree and performs every element action; what
is left on the macOS side is :class:`~kiro_crew.computer_use.macos_driver.MacOSHelper`
(coordinate input, keystrokes, launch, window inventory) and the advisory
permission probe (:mod:`~kiro_crew.computer_use.permissions`). Everything here drives
their REAL bodies against the fake-framework harness from ``test_computer_use_ffi.py``,
so no native library is loaded and every case runs on the Linux and Windows CI shards.

What is asserted, and why each one earns a test:

* **Permission probing is ADVISORY and never raises.** ``doctor`` reported both
  grants "missing" on a machine where a full-fidelity capture succeeded (macOS
  attributes TCC to the responsible parent), so this must never become a gate.
* **The responsible-process walk names the OUTERMOST identifiable ancestor**, which
  is the process macOS grants TCC to.
* **The helper converts every failure into a DriverResult, never an exception.** It
  runs inside the gateway; an escaping exception from a ctypes boundary is how a
  tool error becomes a 500 (or worse, a crash).
* **The three coordinate click paths, and which of them touches the operator's
  cursor.** ``app_post`` and ``sky_click`` are app-scoped and leave the physical
  mouse alone; ``global`` deliberately warps it. A regression that routed an
  ``app_post`` click through the global tap would hijack the operator's mouse on a
  call that promised not to.
* **A real-pointer click is confined to the authorized app's own window**, because a
  global event carries no pid and lands on whatever owns the pixel.
"""

from __future__ import annotations

import ctypes
import os
import sys

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
from test_computer_use_ffi import _H_SOURCE, _Fakes, _in_dll

from kiro_crew import platform_compat
from kiro_crew.computer_use import macos_driver, macos_ffi, permissions, policy
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_GLOBAL,
    MOUSE_BUTTON_MIDDLE,
    MOUSE_BUTTON_RIGHT,
    PERMISSION_UNSUPPORTED,
    AppRef,
    ClickRequest,
    DragRequest,
)

# This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
# so the shared macOS handle caches are put back after each one (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


_APP = AppRef(name="Fixture", pid=4242, bundle_id="dev.kirocrew.fixture", window_id=77)


@pytest.fixture(name="fakes")
def fakes_fixture(monkeypatch: pytest.MonkeyPatch):
    """Install the fake frameworks and force a re-bind (see test_computer_use_ffi).

    Three steps, all required: ``IS_MACOS = True`` so ``_frameworks()`` will load;
    ``ctypes.CDLL`` / ``find_library`` / ``c_void_p.in_dll`` routed at the fakes; and
    ``reset_frameworks()`` before AND after, because the bound handles are cached in a
    module global.
    """
    fake = _Fakes()
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


class TestPermissions:
    def test_probe_is_advisory_and_never_raises(self, fakes: _Fakes):
        """Never a gate: both grants read "missing" on a machine that captured fine.

        macOS attributes TCC to the RESPONSIBLE PARENT process, so a probe of our
        own bundle can report missing while the operation succeeds.
        """
        probe = permissions.probe()
        assert probe.accessibility
        assert probe.screen_recording

    def test_probe_reports_unsupported_off_macos(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(permissions.platform_compat, "IS_MACOS", False, raising=False)
        probe = permissions.probe()
        assert probe.accessibility == PERMISSION_UNSUPPORTED
        assert probe.screen_recording == PERMISSION_UNSUPPORTED

    def test_probe_dict_is_json_safe(self, fakes: _Fakes):
        """The dashboard serialises this straight into a JSON response."""
        import json

        json.dumps(permissions.probe_dict())

    def test_settings_urls_are_empty_off_macos(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(permissions.platform_compat, "IS_MACOS", False, raising=False)
        assert permissions.settings_urls() == {}

    def test_settings_urls_are_deep_links_on_macos(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(permissions.platform_compat, "IS_MACOS", True, raising=False)
        urls = permissions.settings_urls()
        assert urls
        assert all(url.startswith("x-apple.systempreferences:") for url in urls.values())

    def test_never_requests_screen_capture_access(self):
        """``CGRequestScreenCaptureAccess`` pops a system dialog from a background
        process, which would be a UX ambush from a headless gateway probe. Asserted
        structurally over the AST so a docstring mentioning it cannot pass."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(permissions))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "CGRequestScreenCaptureAccess" not in called


class TestResponsibleProcessWalk:
    """The ancestor walk, driven over a FAKE process tree rather than the host's.

    Driving the real ancestry makes the coverage of these branches a property of the
    machine, not of the code: a container whose chain is deeper than the 8-level
    bound never executes the termination ``break`` at all, so the same source line is
    covered on one runner and uncovered on another. Injecting the tree makes each
    branch a statement about the walk instead.
    """

    @staticmethod
    def _tree(monkeypatch: pytest.MonkeyPatch, *, chain: list[str]):
        """Install a fake ancestry whose first pid is this process's REAL pid.

        Keying on the real pid keeps ``os.getpid`` untouched: swapping it on the
        stdlib module is process-wide, and coverage names its data file and xdist
        identifies its worker from that same call. *chain* lists the bundle display
        name per level outward, ``""`` for a level with no bundle.
        """
        from kiro_crew.computer_use import apps_macos

        pids = [os.getpid()] + [90 - 10 * i for i in range(len(chain) - 1)]
        names = dict(zip(pids, chain))
        parents = dict(zip(pids, pids[1:] + [1]))
        monkeypatch.setattr(
            apps_macos,
            "resolve_identity",
            lambda pid: apps_macos.AppIdentity(display_name=names.get(pid, "")),
            raising=False,
        )
        monkeypatch.setattr(
            permissions.platform_compat,
            "get_ppid",
            lambda pid: parents.get(pid, 0),
            raising=False,
        )

    def test_reports_the_outermost_named_ancestor_not_the_nearest(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """macOS grants TCC to the process it considers responsible, which is the
        OUTERMOST identifiable ancestor -- so a nearer name must be overwritten by a
        further one, not kept."""
        self._tree(monkeypatch, chain=["python3", "kiro-cli", "Kiro Crew"])
        assert permissions.responsible_process_name() == "Kiro Crew"

    def test_the_walk_terminates_at_init(self, monkeypatch: pytest.MonkeyPatch):
        """Reaching pid 1 ends the walk. Without this the loop would keep asking the
        OS about pid 1's parent, which on Linux answers 0 forever."""
        self._tree(monkeypatch, chain=["Kiro Crew"])
        assert permissions.responsible_process_name() == "Kiro Crew"

    def test_a_self_referential_parent_does_not_spin(self, monkeypatch: pytest.MonkeyPatch):
        """An unusual or hostile tree can report itself as its own parent."""
        from kiro_crew.computer_use import apps_macos

        monkeypatch.setattr(
            apps_macos,
            "resolve_identity",
            lambda pid: apps_macos.AppIdentity(display_name="Kiro Crew"),
            raising=False,
        )
        monkeypatch.setattr(permissions.platform_compat, "get_ppid", lambda pid: pid, raising=False)
        assert permissions.responsible_process_name() == "Kiro Crew"

    def test_a_failing_ppid_lookup_keeps_the_name_found_so_far(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Diagnostic-only: a raising ``get_ppid`` must stop the walk, not the page."""
        from kiro_crew.computer_use import apps_macos

        monkeypatch.setattr(
            apps_macos,
            "resolve_identity",
            lambda pid: apps_macos.AppIdentity(display_name="Kiro Crew"),
            raising=False,
        )

        def _boom(pid: int) -> int:
            raise OSError("no such process")

        monkeypatch.setattr(permissions.platform_compat, "get_ppid", _boom, raising=False)
        assert permissions.responsible_process_name() == "Kiro Crew"

    def test_an_unidentifiable_chain_falls_back_to_the_executable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A bare venv launch resolves no bundle anywhere; naming the interpreter is
        still more useful to an operator than a generic phrase."""
        self._tree(monkeypatch, chain=[""])
        assert permissions.responsible_process_name() == os.path.basename(sys.executable)


class TestMacOSHelperErrorDiscipline:
    def test_helper_reports_platform_id_macos(self):
        assert macos_driver.MacOSHelper().platform_id == "macos"

    def test_guarded_converts_an_unexpected_error_into_a_result(self):
        """The helper runs in the gateway; an escaping ctypes error is a 500.

        Every failure must come back as a ``DriverResult`` carrying model-facing
        prose instead.
        """

        def _boom():
            raise RuntimeError("ctypes went sideways")

        result = macos_driver._guarded("probe", _boom)
        assert result.ok is False
        assert "probe" in result.text or result.text


class TestMacOSHelperKeyboardAndLifecycle:
    """The helper's keyboard, launch-inventory and lifecycle methods.

    These pin the behaviours that would otherwise only be caught by driving a real
    desktop: that ``press_key`` refuses an unknown key loudly rather than sending a
    different keystroke, that ``type_text`` posts unicode, and that every path returns
    a ``DriverResult`` instead of raising.
    """

    def test_press_key_refuses_an_unknown_key_loudly(self, fakes: _Fakes):
        """Silently dropping an unrecognised modifier would send a DIFFERENT
        keystroke into a live application, so the refusal is the correct behaviour."""
        result = macos_driver.MacOSHelper().press_key(_APP, "cmd+notakey")
        assert result.ok is False
        assert result.text

    def test_press_key_posts_a_parsed_combination(self, fakes: _Fakes):
        result = macos_driver.MacOSHelper().press_key(_APP, "cmd+s")
        assert result.ok is True
        posted = [a for (n, a) in fakes.cg.calls if n == "CGEventPostToPid"]
        assert posted, "no key event reached the target pid"

    def test_type_text_posts_unicode_rather_than_keycodes(self, fakes: _Fakes):
        result = macos_driver.MacOSHelper().type_text(_APP, "héllo")
        assert result.ok is True
        assert "5" in result.text  # character count, not keycode count

    def test_list_apps_and_resolve_app_round_trip(self, fakes: _Fakes):
        helper = macos_driver.MacOSHelper()
        listed = helper.list_apps()
        assert listed.ok in (True, False)  # depends on the fake window list
        assert isinstance(helper.resolve_app("Fixture").ok, bool)

    def test_close_is_idempotent(self, fakes: _Fakes):
        helper = macos_driver.MacOSHelper()
        helper.close()
        helper.close()  # must not raise

    def test_probe_permissions_delegates(self, fakes: _Fakes):
        probe = macos_driver.MacOSHelper().probe_permissions()
        assert probe.accessibility


class TestMacOSPointerPaths:
    """The coordinate click paths, and which of them touches the operator's cursor.

    The distinction this class exists to pin: ``app_post`` is app-scoped
    (``CGEventPostToPid``) and leaves the physical mouse exactly where the user left
    it, while ``global`` deliberately warps it (``CGWarpMouseCursorPosition`` + the
    global ``CGEventPost`` tap). A regression that quietly routed an ``app_post`` click
    through the global tap would hijack the operator's mouse on a call that promised
    not to, so the assertions are on WHICH delivery symbol was reached, not merely on
    ``ok``.
    """

    @pytest.fixture(autouse=True)
    def _owns_the_point(self, monkeypatch):
        """Satisfy the real-pointer confinement precondition for this class.

        The ``global`` path refuses a point that is not inside the resolved app's own
        window (that is what stops an allowed app plus coordinates over a denied one
        from clicking the denied one). These cases are about the DELIVERY mechanics —
        warp-before-post, which post symbol is reached — so ownership is granted
        here; the refusal itself is pinned in ``TestPointOwnershipConfinement``.
        """
        from kiro_crew.computer_use import apps_macos

        monkeypatch.setattr(apps_macos, "pid_owns_point", lambda pid, x, y: True)

    def _events(self, fakes: _Fakes) -> list[tuple]:
        return [a for (n, a) in fakes.cg.calls if n == "CGEventCreateMouseEvent"]

    def test_app_post_click_never_warps_the_cursor(self, fakes: _Fakes):
        result = macos_driver.MacOSHelper().click(
            _APP, ClickRequest(method=CLICK_METHOD_APP_POST, point=(120.0, 340.0))
        )
        assert result.ok is True
        events = self._events(fakes)
        assert events, "no mouse event was created"
        # The CGPoint reached the constructor as a STRUCT with the caller's values.
        assert all(args[2] == (120.0, 340.0) for args in events)
        assert [a for (n, a) in fakes.cg.calls if n == "CGEventPostToPid"]
        assert not [a for (n, a) in fakes.cg.calls if n == "CGEventPost"]
        assert not [a for (n, a) in fakes.cg.calls if n == "CGWarpMouseCursorPosition"]

    def test_app_post_click_posts_a_down_up_pair(self, fakes: _Fakes):
        macos_driver.MacOSHelper().click(
            _APP, ClickRequest(method=CLICK_METHOD_APP_POST, point=(1.0, 2.0))
        )
        types = [args[1] for args in self._events(fakes)]
        assert types == [
            macos_ffi.K_CG_EVENT_LEFT_MOUSE_DOWN,
            macos_ffi.K_CG_EVENT_LEFT_MOUSE_UP,
        ]

    def test_every_mouse_event_gets_an_explicit_zero_flag_mask(self, fakes: _Fakes):
        """Same hazard as ``post_key``: a synthesized event inherits LIVE modifiers.

        The private event source is only half the fix — the explicit
        ``CGEventSetFlags(event, 0)`` is what clears the state, so skipping it
        "because there are no modifiers" would deliver a cmd-click whenever the user
        happened to be holding a key.
        """
        macos_driver.MacOSHelper().click(
            _APP, ClickRequest(method=CLICK_METHOD_APP_POST, point=(5.0, 6.0))
        )
        flags = [args for (n, args) in fakes.cg.calls if n == "CGEventSetFlags"]
        assert len(flags) == len(self._events(fakes))
        assert all(args[1] == 0 for args in flags)

    def test_mouse_events_use_the_private_event_source(self, fakes: _Fakes):
        macos_driver.MacOSHelper().click(
            _APP, ClickRequest(method=CLICK_METHOD_APP_POST, point=(5.0, 6.0))
        )
        created = [a for (n, a) in fakes.cg.calls if n == "CGEventSourceCreate"]
        assert created and all(
            args[0] == macos_ffi.K_CG_EVENT_SOURCE_STATE_PRIVATE for args in created
        )

    @pytest.mark.parametrize(
        "button,expected_down",
        [
            (MOUSE_BUTTON_RIGHT, macos_ffi.K_CG_EVENT_RIGHT_MOUSE_DOWN),
            (MOUSE_BUTTON_MIDDLE, macos_ffi.K_CG_EVENT_OTHER_MOUSE_DOWN),
        ],
    )
    def test_a_non_left_button_uses_its_own_event_type(
        self, fakes: _Fakes, button: str, expected_down: int
    ):
        """A right-click posted as ``LeftMouseDown`` with ``button=1`` is delivered
        as a LEFT click, so the per-button event TYPE is what makes it real."""
        macos_driver.MacOSHelper().click(
            _APP,
            ClickRequest(method=CLICK_METHOD_APP_POST, point=(9.0, 9.0), button=button),
        )
        assert self._events(fakes)[0][1] == expected_down

    def test_a_double_click_sets_the_click_state_field(self, fakes: _Fakes):
        """A double click is a pair whose ``kCGMouseEventClickState`` is 2, NOT two
        independent pairs — AppKit reads ``NSEvent.clickCount`` from that field."""
        macos_driver.MacOSHelper().click(
            _APP, ClickRequest(method=CLICK_METHOD_APP_POST, point=(4.0, 4.0), count=2)
        )
        states = [
            args[2]
            for (n, args) in fakes.cg.calls
            if n == "CGEventSetIntegerValueField"
            and args[1] == macos_ffi.K_CG_MOUSE_EVENT_CLICK_STATE
        ]
        assert states == [1, 1, 2, 2]

    def test_global_click_warps_the_cursor_before_posting(self, fakes: _Fakes):
        """The warp must come FIRST: a global event is delivered to whatever sits
        under the cursor, so posting first would click wherever the user left it."""
        result = macos_driver.MacOSHelper().click(
            _APP, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(700.0, 500.0))
        )
        assert result.ok is True
        names = [n for (n, _a) in fakes.cg.calls]
        assert names.index("CGWarpMouseCursorPosition") < names.index("CGEventPost")
        warps = [a for (n, a) in fakes.cg.calls if n == "CGWarpMouseCursorPosition"]
        assert warps[0][0] == (700.0, 500.0)
        # The global tap, NOT the per-pid delivery: this path is simulating a real
        # physical mouse, which has no pid form.
        assert [a for (n, a) in fakes.cg.calls if n == "CGEventPost"]

    def test_a_refused_warp_still_completes_the_click(self, fakes: _Fakes):
        """Degrading beats refusing: a coherent click at the unchanged cursor is
        recoverable and visible in the refreshed tree, while failing the whole action
        on an advisory return code is not."""
        fakes.cg.warp_result = 1
        result = macos_driver.MacOSHelper().click(
            _APP, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(1.0, 1.0))
        )
        assert result.ok is True

    def test_a_pointless_click_method_is_refused(self, fakes: _Fakes):
        result = macos_driver.MacOSHelper().click(
            _APP, ClickRequest(method="teleport", point=(1.0, 1.0))
        )
        assert result.ok is False
        assert "teleport" in result.text

    def test_a_coordinate_method_without_a_point_is_refused(self, fakes: _Fakes):
        result = macos_driver.MacOSHelper().click(_APP, ClickRequest(method=CLICK_METHOD_APP_POST))
        assert result.ok is False
        assert not self._events(fakes)

    def test_drag_posts_down_then_dragged_then_up(self, fakes: _Fakes):
        """The intermediate ``MouseDragged`` events are not padding: a bare down/up
        pair is not a drag to most apps, because the gesture is recognized from the
        motion between the endpoints (TextEdit selected nothing without them)."""
        result = macos_driver.MacOSHelper().drag(
            _APP,
            DragRequest(start=(10.0, 10.0), end=(110.0, 10.0), method=CLICK_METHOD_APP_POST),
        )
        assert result.ok is True
        types = [args[1] for args in self._events(fakes)]
        assert types[0] == macos_ffi.K_CG_EVENT_LEFT_MOUSE_DOWN
        assert types[-1] == macos_ffi.K_CG_EVENT_LEFT_MOUSE_UP
        assert types[1:-1] == [macos_ffi.K_CG_EVENT_LEFT_MOUSE_DRAGGED] * (macos_ffi.DRAG_STEPS - 1)
        # Endpoints exact, intermediates strictly between them.
        points = [args[2] for args in self._events(fakes)]
        assert points[0] == (10.0, 10.0)
        assert points[-1] == (110.0, 10.0)
        assert all(10.0 < px < 110.0 for px, _py in points[1:-1])

    def test_app_scoped_drag_never_warps_the_cursor(self, fakes: _Fakes):
        macos_driver.MacOSHelper().drag(
            _APP,
            DragRequest(start=(1.0, 1.0), end=(2.0, 2.0), method=CLICK_METHOD_APP_POST),
        )
        assert not [a for (n, a) in fakes.cg.calls if n == "CGWarpMouseCursorPosition"]
        assert not [a for (n, a) in fakes.cg.calls if n == "CGEventPost"]

    def test_global_drag_warps_before_every_event(self, fakes: _Fakes):
        """Warping once would leave the OS delivering the intermediate events to
        whatever sat under the START point, which is not a drag."""
        macos_driver.MacOSHelper().drag(
            _APP,
            DragRequest(start=(0.0, 0.0), end=(50.0, 50.0), method=CLICK_METHOD_GLOBAL),
        )
        warps = [a for (n, a) in fakes.cg.calls if n == "CGWarpMouseCursorPosition"]
        assert len(warps) == len(self._events(fakes)) == macos_ffi.DRAG_STEPS + 1

    def test_drag_refuses_the_accessibility_method(self, fakes: _Fakes):
        """No AX action expresses a sweep between two points, so the request cannot
        be honoured and must not be silently performed as something else."""
        result = macos_driver.MacOSHelper().drag(
            _APP,
            DragRequest(start=(1.0, 1.0), end=(2.0, 2.0), method=CLICK_METHOD_ACCESSIBILITY),
        )
        assert result.ok is False
        assert not self._events(fakes)

    def test_an_unknown_mouse_button_raises_a_typed_refusal(self, fakes: _Fakes):
        """Never defaulted to left: a substituted button performs a DIFFERENT
        gesture, and a right-click that becomes a left-click activates something
        instead of opening a context menu."""
        result = macos_driver.MacOSHelper().click(
            _APP,
            ClickRequest(method=CLICK_METHOD_APP_POST, point=(1.0, 1.0), button="pinky"),
        )
        assert result.ok is False
        assert not self._events(fakes)

    def test_every_mouse_event_is_released(self, fakes: _Fakes):
        """A leaked CGEvent per click is a real RSS bug over a long session."""
        before = len(fakes.heap.live)
        macos_driver.MacOSHelper().click(
            _APP, ClickRequest(method=CLICK_METHOD_APP_POST, point=(3.0, 3.0), count=3)
        )
        macos_driver.MacOSHelper().drag(
            _APP,
            DragRequest(start=(1.0, 1.0), end=(9.0, 9.0), method=CLICK_METHOD_APP_POST),
        )
        # The cached event SOURCE legitimately survives (it is process-wide); every
        # EVENT must not.
        leaked = [h for h in fakes.heap.live[before:] if h not in (_H_SOURCE,)]
        assert leaked == [], f"{len(leaked)} mouse event(s) leaked"


class TestCFStringValueGuard:
    """``cf_string_value`` type-guards its input — the chokepoint that keeps an
    UNCATCHABLE ObjC abort out of the process.

    ``CFStringGetLength`` on a non-CFString raises ``NSInvalidArgumentException`` ->
    ``libc++abi: terminating``, exit 134, which ctypes cannot translate and
    ``_guarded`` could not catch. App-controlled data (a window title, an action
    name) reaches this helper, so the guard lives inside it where no caller can skip
    it.
    """

    def test_cf_string_value_returns_empty_for_a_non_string(self, fakes: _Fakes):
        macos_ffi._frameworks()
        assert macos_ffi.cf_string_value(fakes.new_number(7)) == ""
        assert macos_ffi.cf_string_value(fakes.new_string("real")) == "real"


class TestDenylistCaseFolding:
    """Case-insensitive matching, now over the ONE retained entry.

    The terminal / credential-manager / system-settings / auth-prompt entries are
    gone, so this exercises ``kirocrew_self`` — the rule that keeps the keystone
    (and therefore the primary enable) out of the agent's reach.
    """

    def test_matching_is_case_insensitive_on_both_sides(self):
        for name, bundle in (
            ("KIRO CREW", "DEV.KIRO.CREW"),
            ("kiro crew", "dev.kiro.crew"),
            ("Kiro Crew", "Dev.Kiro.Crew"),
        ):
            assert policy.denied_rule_for(AppRef(name=name, pid=1, bundle_id=bundle)) is not None

    def test_an_unrelated_app_is_not_matched(self):
        assert (
            policy.denied_rule_for(AppRef(name="Preview", pid=1, bundle_id="com.apple.Preview"))
            is None
        )


class TestPointOwnershipConfinement:
    """A real-pointer click must be confined to the authorized app's own window.

    A global event carries no pid, so the app identity is the only boundary: naming
    an allowed app while passing coordinates over a denied one would otherwise pass
    the policy check on app A and click app B.
    """

    def test_a_real_pointer_click_outside_the_authorized_window_is_refused(
        self, fakes: _Fakes, monkeypatch
    ):
        from kiro_crew.computer_use import apps_macos

        monkeypatch.setattr(apps_macos, "pid_owns_point", lambda pid, x, y: False)
        result = macos_driver.MacOSHelper().click(
            _APP,
            ClickRequest(point=(10.0, 10.0), method="global", button="left", count=1),
        )
        assert result.ok is False
        assert "not inside" in result.text
        assert "app_post" in result.text  # names the recovery

    def test_point_ownership_takes_the_TOPMOST_window(self, fakes: _Fakes, monkeypatch):
        """ "Any window of mine contains it" would permit a click on an overlapper."""
        from kiro_crew.computer_use import apps_macos

        mine, theirs = 4242, 9999
        front = macos_ffi.WindowInfo(
            window_id=1,
            pid=theirs,
            owner_name="Denied",
            title="on top",
            layer=0,
            bounds=(0.0, 0.0, 100.0, 100.0),
        )
        behind = macos_ffi.WindowInfo(
            window_id=2,
            pid=mine,
            owner_name="Fixture",
            title="behind",
            layer=0,
            bounds=(0.0, 0.0, 100.0, 100.0),
        )
        # Window-list order IS front-to-back z-order.
        monkeypatch.setattr(macos_ffi, "window_list", lambda: [front, behind])
        # The point is inside BOTH rects; the front window belongs to someone else.
        assert apps_macos.pid_owns_point(mine, 50.0, 50.0) is False
        assert apps_macos.pid_owns_point(theirs, 50.0, 50.0) is True

    def test_point_ownership_fails_closed_when_bounds_are_unreadable(
        self, fakes: _Fakes, monkeypatch
    ):
        from kiro_crew.computer_use import apps_macos

        unreadable = macos_ffi.WindowInfo(
            window_id=1,
            pid=4242,
            owner_name="Fixture",
            title="no bounds",
            layer=0,
            bounds=None,
        )
        monkeypatch.setattr(macos_ffi, "window_list", lambda: [unreadable])
        assert apps_macos.pid_owns_point(4242, 10.0, 10.0) is False


class TestOverlayWindowsBlockPointerClicks:
    """A non-normal window over the target must REFUSE, not be skipped.

    If ``pid_owns_point`` skipped non-layer-0 entries, a notification banner or an
    open menu sitting on top of the authorized app would be ignored and the app
    UNDERNEATH would grant permission — for a click the overlay actually swallows.

    ``list_apps`` still skips those layers, for the unrelated reason that they are
    not addressable TARGETS. The two functions answer different questions.
    """

    def test_a_notification_over_the_app_refuses(self, fakes: _Fakes, monkeypatch):
        from kiro_crew.computer_use import apps_macos

        banner = macos_ffi.WindowInfo(
            window_id=1,
            pid=8888,
            owner_name="Notification Center",
            title="",
            layer=25,
            bounds=(0.0, 0.0, 100.0, 100.0),
        )
        mine = macos_ffi.WindowInfo(
            window_id=2,
            pid=4242,
            owner_name="Fixture",
            title="doc",
            layer=0,
            bounds=(0.0, 0.0, 100.0, 100.0),
        )
        monkeypatch.setattr(macos_ffi, "window_list", lambda: [banner, mine])
        assert apps_macos.pid_owns_point(4242, 50.0, 50.0) is False

    def test_the_same_app_still_owns_an_uncovered_point(self, fakes: _Fakes, monkeypatch):
        """The inverse, so the rule cannot refuse everything."""
        from kiro_crew.computer_use import apps_macos

        banner = macos_ffi.WindowInfo(
            window_id=1,
            pid=8888,
            owner_name="Notification Center",
            title="",
            layer=25,
            bounds=(0.0, 0.0, 40.0, 40.0),
        )
        mine = macos_ffi.WindowInfo(
            window_id=2,
            pid=4242,
            owner_name="Fixture",
            title="doc",
            layer=0,
            bounds=(0.0, 0.0, 200.0, 200.0),
        )
        monkeypatch.setattr(macos_ffi, "window_list", lambda: [banner, mine])
        # Outside the banner, inside my window.
        assert apps_macos.pid_owns_point(4242, 120.0, 120.0) is True

    def test_an_unplaceable_window_refuses_rather_than_looking_past_it(
        self, fakes: _Fakes, monkeypatch
    ):
        """A window with no readable bounds could be covering the point."""
        from kiro_crew.computer_use import apps_macos

        unknown = macos_ffi.WindowInfo(
            window_id=1,
            pid=8888,
            owner_name="Mystery",
            title="",
            layer=0,
            bounds=None,
        )
        mine = macos_ffi.WindowInfo(
            window_id=2,
            pid=4242,
            owner_name="Fixture",
            title="doc",
            layer=0,
            bounds=(0.0, 0.0, 200.0, 200.0),
        )
        monkeypatch.setattr(macos_ffi, "window_list", lambda: [unknown, mine])
        assert apps_macos.pid_owns_point(4242, 50.0, 50.0) is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
