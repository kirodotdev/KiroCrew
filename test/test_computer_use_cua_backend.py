"""``CuaBackend`` against an in-process fake supervisor, and every floor through it."""

from __future__ import annotations

import io
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.computer_use import screencast
from kiro_crew.computer_use.driver_runtime import session as session_mod
from kiro_crew.computer_use.driver_runtime.backend import CuaBackend, is_secure_element
from kiro_crew.computer_use.driver_runtime.pin import OS_LINUX, OS_MACOS, OS_WINDOWS
from kiro_crew.computer_use.driver_runtime.supervisor import OutcomeUnknown
from kiro_crew.computer_use.index import SnapshotIndex
from kiro_crew.computer_use.service import ComputerUseService
from kiro_crew.computer_use.types import (
    AppRef,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    ElementRec,
    SnapshotRequest,
)

PID, WID = 4242, 77
BOUNDS = {"x": 100, "y": 50, "width": 400, "height": 300}


def _png(width: int = 400, height: int = 300) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, format="PNG")
    return out.getvalue()


def _elements(*extra: dict) -> list[dict]:
    base = [
        {
            "element_token": "s00000001:0",
            "role": "button",
            "label": "7",
            "actions": ["click"],
            "depth": 1,
            "frame": {"x": 10, "y": 10, "w": 40, "h": 30},
        },
        {
            "element_token": "s00000001:1",
            "role": "text box",
            "label": "",
            "value": "42",
            "actions": ["clipboard.copy", "clipboard.cut", "clipboard.paste"],
            "depth": 1,
        },
    ]
    return base + list(extra)


class FakeSupervisor:
    """Answers allowlisted calls from a script; records every call."""

    def __init__(self, tools: "frozenset[str] | None" = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.generation = 1
        self.tools = (
            tools
            if tools is not None
            else frozenset(
                {
                    "list_windows",
                    "get_window_state",
                    "click",
                    "drag",
                    "type_text",
                    "press_key",
                    "hotkey",
                    "set_value",
                    "scroll",
                    "check_permissions",
                    "health_report",
                }
            )
        )
        self.windows: list[dict] = [
            {
                "pid": PID,
                "window_id": WID,
                "app_name": "gnome-calculator",
                "title": "Calculator",
                "bounds": BOUNDS,
            },
        ]
        self.elements = _elements()
        self.state_extra: dict[str, Any] = {}
        self.png = _png()
        self.action_reply: dict[str, Any] = {
            "content": [{"type": "text", "text": "Clicked element [0]."}],
            "structuredContent": {"effect": "confirmed", "summary": "Clicked element [0]."},
        }
        self.raise_on: "dict[str, Exception]" = {}
        self.snap_counter = 1

    def ensure(self) -> None:
        return None

    def available_tools(self) -> frozenset[str]:
        return self.tools

    def call(
        self, tool: str, args: Mapping[str, Any], *, timeout: float, idempotent: bool = False
    ) -> dict:
        self.calls.append((tool, dict(args)))
        if tool in self.raise_on:
            raise self.raise_on[tool]
        if tool == "list_windows":
            return {"structuredContent": {"windows": self.windows}}
        if tool == "get_window_state":
            self.snap_counter += 1
            sid = f"s{self.snap_counter:08x}"
            out = args.get("screenshot_out_file")
            if out and args.get("include_screenshot"):
                Path(out).write_bytes(self.png)
            els = [dict(e, element_token=f"{sid}:{i}") for i, e in enumerate(self.elements)]
            sc = {
                "pid": PID,
                "window_id": WID,
                "app_name": "gnome-calculator",
                "window_title": "Calculator",
                "window_bounds": BOUNDS,
                "elements_complete": True,
                "truncated": False,
                "screenshot_width": 400,
                "snapshot_id": sid,
            }
            if args.get("include_accessibility_tree") is not False:
                sc["elements"] = els
            sc.update(self.state_extra)
            return {"content": [{"type": "text", "text": "state"}], "structuredContent": sc}
        if tool == "check_permissions":
            return {"structuredContent": {"accessibility": True, "screen_recording": False}}
        return self.action_reply


def _backend(
    os_id: str = OS_LINUX, sup: "FakeSupervisor | None" = None, **kw: Any
) -> "tuple[CuaBackend, FakeSupervisor]":
    fake = sup or FakeSupervisor()
    kw.setdefault("session", lambda: X11_SESSION)
    backend = CuaBackend(supervisor=fake, home=kw.pop("home", "/nonexistent-home"), os_id=os_id, **kw)  # type: ignore[arg-type]
    return backend, fake


X11_SESSION = session_mod.detect_linux(
    {"DISPLAY": ":1", "DBUS_SESSION_BUS_ADDRESS": "x"}, uinput=True
)
APP = AppRef(name="gnome-calculator", pid=PID, window_id=WID, window_title="Calculator")
REQ = SnapshotRequest(want_image=False)
#: The records the fake window's walk renders, as a session holding that walk has them.
BUTTON = ElementRec(index=0, role="button", title="7", depth=1)
FIELD = ElementRec(index=1, role="text box", depth=1)


def _read(backend: CuaBackend, req: SnapshotRequest = REQ):  # type: ignore[no-untyped-def]
    result = backend.snapshot(APP, req)
    assert result.ok, result.text
    assert result.snapshot is not None
    return result.snapshot


class TestDiscovery:
    def test_list_apps_one_entry_per_process(self) -> None:
        backend, fake = _backend()
        fake.windows.append(
            {
                "pid": PID,
                "window_id": 78,
                "app_name": "gnome-calculator",
                "title": "Prefs",
                "bounds": BOUNDS,
            }
        )
        fake.windows.append({"pid": 9, "window_id": 1, "app_name": "", "title": "nameless"})
        result = backend.list_apps()
        assert result.ok and [a.pid for a in result.apps] == [PID]
        assert fake.calls[0] == ("list_windows", {"on_screen_only": True})

    def test_self_window_title_represents_the_process(self) -> None:
        backend, fake = _backend()
        fake.windows = [
            {"pid": 7, "window_id": 1, "app_name": "firefox", "title": "News", "bounds": BOUNDS},
            {
                "pid": 7,
                "window_id": 2,
                "app_name": "firefox",
                "title": "Kiro Crew",
                "bounds": BOUNDS,
            },
        ]
        result = backend.resolve_app("firefox")
        assert result.ok and result.app is not None and result.app.window_title == "Kiro Crew"

    def test_unknown_app_is_refused(self) -> None:
        backend, _ = _backend()
        assert not backend.resolve_app("nope").ok


class TestSnapshotMapping:
    def test_elements_tokens_and_bounds(self) -> None:
        backend, fake = _backend()
        snap = _read(backend)
        assert [e.title for e in snap.elements] == ["7", ""]
        assert snap.elements[1].value == "42"
        assert snap.window_bounds == (100.0, 50.0, 400.0, 300.0)
        assert backend.window_bounds(APP) == (100.0, 50.0, 400.0, 300.0)
        tool, args = fake.calls[-1]
        assert tool == "get_window_state" and args["tree_format"] == "elements"
        assert args["include_screenshot"] is False

    def test_linux_password_field_is_secure_and_its_value_never_travels(self) -> None:
        backend, fake = _backend()
        fake.elements = _elements(
            {"element_token": "x", "role": "password text", "value": "hunter2"}
        )
        snap = _read(backend)
        assert snap.has_secure and snap.elements[2].secure and snap.elements[2].value == ""

    def test_windows_edit_is_treated_as_secure_because_unproven(self) -> None:
        assert is_secure_element(OS_WINDOWS, {"role": "Edit"})
        assert not is_secure_element(OS_LINUX, {"role": "text box", "actions": ["clipboard.copy"]})
        # A GTK 4 password entry: plain "text box", copy and cut disabled.
        assert is_secure_element(OS_LINUX, {"role": "text box", "actions": ["activate"]})
        assert is_secure_element(OS_LINUX, {"role": "entry"})
        assert not is_secure_element(OS_LINUX, {"role": "button", "actions": ["click"]})
        assert is_secure_element(OS_MACOS, {"role": "AXTextField", "subrole": "AXSecureTextField"})
        assert not is_secure_element(OS_MACOS, {"role": "AXTextField"})

    def test_incomplete_walk_is_flagged_truncated(self) -> None:
        backend, fake = _backend()
        fake.state_extra = {"elements_complete": False}
        assert _read(backend).truncated

    def test_screenshot_is_converted_to_jpeg_and_the_png_removed(self, tmp_path: Path) -> None:
        backend, fake = _backend(home=tmp_path)
        snap = _read(backend, SnapshotRequest(want_image=True, image_max_px=200))
        assert snap.image_jpeg[:2] == b"\xff\xd8"
        assert max(snap.image_width, snap.image_height) == 200
        out = fake.calls[-1][1]["screenshot_out_file"]
        assert not Path(out).exists()

    def test_secure_window_is_never_converted(self, tmp_path: Path) -> None:
        backend, fake = _backend(home=tmp_path)
        fake.elements = _elements({"role": "password text"})
        snap = _read(backend, SnapshotRequest(want_image=True))
        assert snap.image_jpeg == b""


class TestActions:
    def test_element_click_uses_the_latest_walks_token(self) -> None:
        backend, fake = _backend()
        _read(backend)
        _read(backend)
        rec = BUTTON
        result = backend.click(APP, rec, ClickRequest(method="accessibility"))
        assert result.ok
        tool, args = fake.calls[-1]
        assert tool == "click" and args["element_token"] == "s00000003:0"

    def test_a_reindex_by_another_walk_refuses_instead_of_acting_on_another_element(
        self,
    ) -> None:
        # Session A holds the walk where [0] is the "7" button. Another session
        # walks the same window after a relayout in which [0] is now "Delete".
        backend, fake = _backend()
        held = _read(backend).elements[0]
        assert held.title == "7"
        fake.elements = [dict(fake.elements[0], label="Delete"), *fake.elements[1:]]
        _read(backend)
        result = backend.click(APP, held, ClickRequest(method="accessibility"))
        assert not result.ok and "computer_get_state" in result.text
        assert fake.calls[-1][0] != "click"

    def test_token_from_another_generation_is_refused(self) -> None:
        backend, fake = _backend()
        _read(backend)
        fake.generation += 1
        result = backend.click(APP, BUTTON, ClickRequest(method="accessibility"))
        assert not result.ok and "computer_get_state" in result.text
        assert fake.calls[-1][0] != "click"

    def test_coordinate_click_is_grounded_in_a_capture_and_window_local(
        self, tmp_path: Path
    ) -> None:
        backend, fake = _backend(home=tmp_path)
        result = backend.click(APP, None, ClickRequest(method="app_post", point=(150.0, 80.0)))
        assert result.ok
        (t1, a1), (t2, a2) = fake.calls[-2:]
        assert t1 == "get_window_state" and a1["include_accessibility_tree"] is False
        assert t2 == "click" and (a2["x"], a2["y"]) == (50.0, 30.0)
        assert a2["delivery_mode"] == "background"

    def test_global_is_the_only_foreground_path(self, tmp_path: Path) -> None:
        backend, fake = _backend(home=tmp_path)
        backend.click(APP, None, ClickRequest(method="global", point=(150.0, 80.0)))
        assert fake.calls[-1][1]["delivery_mode"] == "foreground"

    def test_sky_click_refuses_by_name(self) -> None:
        backend, fake = _backend()
        result = backend.click(APP, None, ClickRequest(method="sky_click", point=(1.0, 1.0)))
        assert not result.ok and "sky_click" in result.text and fake.calls == []

    def test_background_unavailable_is_a_typed_refusal_naming_the_alternative(
        self, tmp_path: Path
    ) -> None:
        backend, fake = _backend(home=tmp_path)
        fake.action_reply = {
            "isError": True,
            "content": [{"type": "text", "text": "x"}],
            "structuredContent": {"code": "background_unavailable", "detail": "needs uinput"},
        }
        result = backend.drag(APP, DragRequest(start=(110.0, 60.0), end=(200.0, 60.0)))
        assert not result.ok and "background" in result.text and '"global"' in result.text

    def test_a_curved_drag_is_refused_before_any_input(self, tmp_path: Path) -> None:
        # The upstream drag takes endpoints and steps only; sending a curved
        # request would silently draw a straight stroke instead.
        backend, fake = _backend(home=tmp_path)
        result = backend.drag(
            APP, DragRequest(start=(110.0, 60.0), end=(200.0, 60.0), steps=48, path="curved")
        )
        assert not result.ok and "straight" in result.text
        assert all(tool != "drag" for tool, _ in fake.calls)

    def test_a_point_outside_the_window_as_it_is_now_is_refused(self, tmp_path: Path) -> None:
        # The floor checked the point against the bounds of the last read; the
        # window has shrunk since. The gesture is re-checked against the bounds
        # it is mapped through, so it cannot land in another application.
        backend, fake = _backend(home=tmp_path)
        _read(backend)
        fake.state_extra = {"window_bounds": {"x": 100, "y": 50, "width": 100, "height": 100}}
        click = backend.click(APP, None, ClickRequest(method="global", point=(450.0, 80.0)))
        assert not click.ok and "outside" in click.text
        drag = backend.drag(
            APP, DragRequest(start=(150.0, 80.0), end=(450.0, 80.0), method="global")
        )
        assert not drag.ok and "outside" in drag.text
        assert all(tool not in ("click", "drag") for tool, _ in fake.calls)

    def test_unconfirmed_effect_is_reported_as_unverified(self) -> None:
        backend, fake = _backend()
        _read(backend)
        fake.action_reply = {
            "structuredContent": {"effect": "unverifiable", "summary": "Clicked element [0]."}
        }
        result = backend.click(APP, BUTTON, ClickRequest(method="accessibility"))
        assert result.ok and "could not confirm" in result.text

    def test_no_effect_is_not_done(self) -> None:
        backend, fake = _backend()
        _read(backend)
        fake.action_reply = {"structuredContent": {"effect": "none"}}
        result = backend.set_value(APP, FIELD, "1")
        assert not result.ok and "no effect" in result.text

    def test_press_key_maps_the_spec(self) -> None:
        backend, fake = _backend()
        _read(backend)
        assert backend.press_key(APP, FIELD, "cmd+a").ok
        args = fake.calls[-1][1]
        assert args["key"] == "a" and args["modifiers"] == ["ctrl"] and args["element_token"]

    def test_unsupported_key_refuses_before_the_driver(self) -> None:
        backend, fake = _backend()
        _read(backend)
        n = len(fake.calls)
        assert not backend.press_key(APP, FIELD, "volumeup").ok
        assert len(fake.calls) == n

    def test_unknown_outcome_drops_every_token(self) -> None:
        backend, fake = _backend()
        _read(backend)
        fake.raise_on["click"] = OutcomeUnknown("the desktop driver exited; Re-read the window")
        result = backend.click(APP, BUTTON, ClickRequest(method="accessibility"))
        assert not result.ok and "Re-read" in result.text
        del fake.raise_on["click"]
        again = backend.click(APP, BUTTON, ClickRequest(method="accessibility"))
        assert not again.ok and "computer_get_state" in again.text

    def test_missing_required_tool_refuses(self) -> None:
        backend, fake = _backend(
            sup=FakeSupervisor(tools=frozenset({"list_windows", "get_window_state"}))
        )
        _read(backend)
        result = backend.set_value(APP, FIELD, "1")
        assert not result.ok and "does not provide" in result.text

    def test_typed_text_is_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        backend, fake = _backend()
        _read(backend)
        fake.raise_on["type_text"] = RuntimeError("boom")
        with caplog.at_level(logging.DEBUG):
            backend.type_text(APP, FIELD, "hunter2-secret")
        assert "hunter2-secret" not in caplog.text


class TestSessionGate:
    def test_an_unseeable_session_refuses_instead_of_reporting_an_empty_desktop(self) -> None:
        wayland = session_mod.detect_linux(
            {"WAYLAND_DISPLAY": "w", "DBUS_SESSION_BUS_ADDRESS": "x"}, uinput=True
        )
        backend, fake = _backend(session=lambda: wayland)
        result = backend.list_apps()
        assert not result.ok and "X server" in result.text and fake.calls == []


class TestLaunch:
    def test_path_query_refused_before_any_catalog(self) -> None:
        resolved: list[str] = []
        backend, fake = _backend(
            launcher={"resolve": lambda q: resolved.append(q), "spawn": lambda t: None}
        )
        result = backend.launch_app("/usr/bin/xterm")
        assert not result.ok and resolved == [] and fake.calls == []

    def test_launch_uses_our_catalog_and_never_upstream_launch(self) -> None:
        spawned: list[str] = []
        backend, fake = _backend(
            launcher={"resolve": lambda q: ("/usr/bin/zenity", "Zenity"), "spawn": spawned.append},
            launch_timeout=0.0,
        )
        fake.windows = []
        result = backend.launch_app("zenity")
        assert result.ok and spawned == ["/usr/bin/zenity"]
        assert all(tool != "launch_app" for tool, _ in fake.calls)

    def test_permit_runs_on_the_resolved_identity(self) -> None:
        backend, _ = _backend(
            launcher={"resolve": lambda q: ("/usr/bin/zenity", "Zenity"), "spawn": lambda t: None}
        )
        result = backend.launch_app("zen", permit=lambda who: f"denied {who.display}")
        assert not result.ok and result.text == "denied Zenity"


class TestFloorsThroughTheService:
    """Every lifted floor, with the upstream driver as the backend."""

    def _svc(self, backend: CuaBackend) -> ComputerUseService:
        return ComputerUseService(backend=backend, idx=SnapshotIndex())

    def test_secure_window_is_never_spooled_or_mirrored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []
        monkeypatch.setattr(screencast, "emit_snapshot_frame", lambda s: seen.append(s) or True)
        backend, fake = _backend(home=tmp_path)
        fake.elements = _elements({"role": "password text", "value": "pw"})
        snap = self._svc(backend).snapshot(APP, SnapshotRequest(want_image=True), session_key="s")
        assert snap.image_path == "" and seen == []
        assert all(not e.value for e in snap.elements if e.secure)

    def test_permitted_frame_is_spooled_and_mirrored_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []
        monkeypatch.setattr(screencast, "emit_snapshot_frame", lambda s: seen.append(s) or True)
        backend, _ = _backend(home=tmp_path)
        snap = self._svc(backend).snapshot(APP, SnapshotRequest(want_image=True), session_key="s")
        try:
            assert snap.image_path and len(seen) == 1
        finally:
            Path(snap.image_path).unlink(missing_ok=True)

    def test_truncated_walk_withholds_pixels(self, tmp_path: Path) -> None:
        backend, fake = _backend(home=tmp_path)
        fake.state_extra = {"truncated": True}
        snap = self._svc(backend).snapshot(APP, SnapshotRequest(want_image=True), session_key="s")
        assert snap.image_path == ""

    def test_point_outside_the_window_never_reaches_the_driver(self) -> None:
        backend, fake = _backend()
        svc = self._svc(backend)
        svc.snapshot(APP, REQ, session_key="s")
        n = len(fake.calls)
        with pytest.raises(ComputerUseError, match="outside"):
            svc.click(APP, None, ClickRequest(method="app_post", point=(5.0, 5.0)))
        assert len(fake.calls) == n

    def test_point_with_unknown_bounds_fails_closed(self) -> None:
        backend, fake = _backend()
        with pytest.raises(ComputerUseError, match="unknown"):
            self._svc(backend).click(
                APP, None, ClickRequest(method="app_post", point=(150.0, 80.0))
            )
        assert fake.calls == []

    def test_keys_need_an_addressed_element(self) -> None:
        backend, fake = _backend()
        with pytest.raises(ComputerUseError, match="element_index"):
            self._svc(backend).type_text(APP, None, "x")
        assert fake.calls == []


class TestSession:
    def test_x11_with_uinput_claims_background(self) -> None:
        info = session_mod.detect_linux(
            {"DISPLAY": ":1", "DBUS_SESSION_BUS_ADDRESS": "unix:x"}, uinput=True
        )
        assert info.kind == session_mod.KIND_X11 and info.supported
        assert info.capabilities["background_input"]

    def test_x11_without_uinput_does_not_claim_raw_background_input(self) -> None:
        info = session_mod.detect_linux(
            {"DISPLAY": ":1", "DBUS_SESSION_BUS_ADDRESS": "unix:x"}, uinput=False
        )
        assert not info.capabilities["background_input"]
        assert any("uinput" in limit for limit in info.limitations)

    @pytest.mark.parametrize(
        "env, compositor",
        [
            ({"XDG_CURRENT_DESKTOP": "GNOME"}, "gnome"),
            ({"XDG_CURRENT_DESKTOP": "KDE"}, "kde"),
            ({"SWAYSOCK": "/run/s"}, "sway"),
            ({"HYPRLAND_INSTANCE_SIGNATURE": "x"}, "hyprland"),
            ({"NIRI_SOCKET": "/run/n"}, "niri"),
            ({}, "other"),
        ],
    )
    def test_wayland_through_xwayland_names_its_limits(self, env, compositor) -> None:  # type: ignore[no-untyped-def]
        base = {
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
            "DBUS_SESSION_BUS_ADDRESS": "unix:x",
        }
        info = session_mod.detect_linux({**base, **env}, uinput=False)
        assert info.kind == session_mod.KIND_WAYLAND and info.compositor == compositor
        assert info.supported and not info.capabilities["background_input"]
        assert any("XWayland" in limit for limit in info.limitations)

    def test_wayland_without_xwayland_refuses(self) -> None:
        info = session_mod.detect_linux(
            {"WAYLAND_DISPLAY": "wayland-0", "DBUS_SESSION_BUS_ADDRESS": "x"}, uinput=True
        )
        assert not info.supported and not any(info.capabilities.values())

    def test_no_display_and_no_bus(self) -> None:
        assert not session_mod.detect_linux({}, uinput=True).supported
        info = session_mod.detect_linux({"DISPLAY": ":1"}, uinput=True)
        assert info.atspi == session_mod.ATSPI_MISSING and not info.supported

    def test_windows_session0_is_refused(self) -> None:
        info = session_mod.detect_windows(0)
        assert info.kind == session_mod.KIND_SESSION0 and not info.supported
        assert session_mod.detect_windows(1).kind == session_mod.KIND_WINDOWS_INTERACTIVE


class TestWindowsPasswordProof:
    """The Windows tree carries no ``IsPassword``; an Edit is secure unless proven otherwise."""

    def _snap(self, proof: bool, *extra: dict):  # type: ignore[no-untyped-def]
        fake = FakeSupervisor()
        fake.elements = [{"role": "Edit", "label": "Text Editor", "value": "hello"}, *extra]
        seen: list[AppRef] = []

        def password_free(app: AppRef) -> bool:
            seen.append(app)
            return proof

        backend, _ = _backend(OS_WINDOWS, sup=fake, password_free=password_free)
        return _read(backend), seen

    def test_unproven_edit_is_secure(self) -> None:
        snap, seen = self._snap(False)
        assert snap.elements[0].secure and snap.elements[0].value == "" and seen

    def test_edit_in_a_window_proven_password_free_is_plain(self) -> None:
        snap, _ = self._snap(True)
        assert not snap.elements[0].secure and snap.elements[0].value == "hello"

    def test_a_tree_marked_password_stays_secure_even_with_proof(self) -> None:
        snap, _ = self._snap(True, {"role": "Edit", "is_password": True})
        assert snap.elements[1].secure

    def test_no_edit_means_no_cross_check(self) -> None:
        fake = FakeSupervisor()
        fake.elements = [{"role": "Button", "label": "OK"}]
        calls: list[AppRef] = []
        backend, _ = _backend(OS_WINDOWS, sup=fake, password_free=lambda a: calls.append(a) or True)
        _read(backend)
        assert calls == []


def test_a_named_fields_content_is_rendered_beside_its_label() -> None:
    from kiro_crew.computer_use import render

    named = ElementRec(index=0, role="edit", title="Text Editor", value="hello")
    secure = ElementRec(index=1, role="edit", title="Password", value="pw", secure=True)
    same = ElementRec(index=2, role="text box", title="42", value="42")
    assert '"Text Editor" = "hello"' in render._render_record(named, text_limit=100)
    assert "pw" not in render._render_record(secure, text_limit=100)
    assert render._render_record(same, text_limit=100).count('"42"') == 1


def test_status_keeps_the_settings_setup_flow_reachable(tmp_path: Path) -> None:
    """Not installed / no usable session are setup states, so the panel stays usable."""
    backend, _ = _backend(home=tmp_path)
    status = backend.status()
    assert status.supported and "Settings -> Computer Use" in status.reason
    unknown, _ = _backend(os_id="plan9", home=tmp_path)
    assert not unknown.status().supported
