"""The always-on floors, enforced above the seam for every backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.computer_use import floors, screencast
from kiro_crew.computer_use.backend import UnsupportedBackend
from kiro_crew.computer_use.index import SnapshotIndex
from kiro_crew.computer_use.service import ComputerUseService
from kiro_crew.computer_use.types import (
    AppRef,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    DriverResult,
    ElementRec,
    Snapshot,
    SnapshotRequest,
)

APP = AppRef(name="Calculator", pid=42, bundle_id="calc", window_id=7, window_title="Calculator")
JPEG = b"\xff\xd8fake-jpeg\xff\xd9"


def _snap(**kw) -> Snapshot:  # type: ignore[no-untyped-def]
    elements = kw.pop("elements", (ElementRec(index=0, role="button", title="7"),))
    return Snapshot(
        app=kw.pop("app", APP),
        elements=elements,
        captured_at=1.0,
        image_jpeg=kw.pop("image_jpeg", JPEG),
        image_width=10,
        image_height=10,
        **kw,
    )


class _Stub(UnsupportedBackend):
    """A backend with no native confinement that returns a staged snapshot."""

    def __init__(self, snap: Snapshot, bounds=(0.0, 0.0, 100.0, 100.0)) -> None:  # type: ignore[no-untyped-def]
        super().__init__("stub", "")
        self.snap = snap
        self.bounds = bounds
        self.calls: list[str] = []

    def snapshot(self, app, req):  # type: ignore[no-untyped-def]
        return DriverResult(ok=True, snapshot=self.snap)

    def window_bounds(self, app):  # type: ignore[no-untyped-def]
        return self.bounds

    def click(self, app, rec, req):  # type: ignore[no-untyped-def]
        self.calls.append("click")
        return DriverResult(ok=True, text="clicked")

    def drag(self, app, req):  # type: ignore[no-untyped-def]
        self.calls.append("drag")
        return DriverResult(ok=True, text="dragged")

    def type_text(self, app, rec, text):  # type: ignore[no-untyped-def]
        self.calls.append("type_text")
        return DriverResult(ok=True, text="typed")

    def press_key(self, app, rec, key):  # type: ignore[no-untyped-def]
        self.calls.append("press_key")
        return DriverResult(ok=True, text="pressed")


@pytest.fixture
def relayed(monkeypatch: pytest.MonkeyPatch) -> list[Snapshot]:
    seen: list[Snapshot] = []
    monkeypatch.setattr(screencast, "emit_snapshot_frame", lambda s: seen.append(s) or True)
    return seen


def _service(backend: _Stub) -> ComputerUseService:
    return ComputerUseService(backend=backend, idx=SnapshotIndex())


class TestCaptureFloor:
    @pytest.mark.parametrize(
        "kw, reason",
        [
            ({"has_secure": True}, floors.WITHHELD_SECURE),
            (
                {"elements": (ElementRec(index=0, role="password text", secure=True),)},
                floors.WITHHELD_SECURE,
            ),
            ({"truncated": True}, floors.WITHHELD_INCOMPLETE),
            ({"depth_truncated": True}, floors.WITHHELD_INCOMPLETE),
            ({"app": AppRef(name="x", pid=1, window_id=0)}, floors.WITHHELD_NO_WINDOW),
        ],
    )
    def test_withheld_reasons(self, kw, reason) -> None:  # type: ignore[no-untyped-def]
        assert floors.screenshot_withheld_reason(_snap(**kw)) == reason
        hardened = floors.harden_snapshot(_snap(**kw))
        assert hardened.image_jpeg == b"" and hardened.image_path == ""

    def test_a_backend_cannot_hide_a_secure_element_behind_has_secure_false(self) -> None:
        secret = ElementRec(index=0, role="password text", value="hunter2", secure=True)
        hardened = floors.harden_snapshot(_snap(elements=(secret,), has_secure=False))
        assert hardened.has_secure
        assert hardened.elements[0].value == ""
        assert hardened.image_jpeg == b""

    def test_a_withheld_frame_already_on_disk_is_removed(self, tmp_path: Path) -> None:
        spooled = tmp_path / "shot.jpeg"
        spooled.write_bytes(JPEG)
        hardened = floors.harden_snapshot(_snap(truncated=True, image_path=str(spooled)))
        assert hardened.image_path == "" and not spooled.exists()

    def test_service_never_spools_or_relays_a_secure_window(self, relayed) -> None:  # type: ignore[no-untyped-def]
        secret = ElementRec(index=0, role="password text", secure=True)
        svc = _service(_Stub(_snap(elements=(secret,))))
        out = svc.snapshot(APP, SnapshotRequest(want_image=True), session_key="s")
        assert out.image_path == "" and out.image_jpeg == b""
        assert relayed == []

    def test_service_spools_and_relays_a_permitted_frame_once(self, relayed) -> None:  # type: ignore[no-untyped-def]
        svc = _service(_Stub(_snap()))
        out = svc.snapshot(APP, SnapshotRequest(want_image=True), session_key="s")
        try:
            assert out.image_path and Path(out.image_path).read_bytes() == JPEG
            assert len(relayed) == 1 and relayed[0].image_path == out.image_path
        finally:
            Path(out.image_path).unlink(missing_ok=True)

    def test_a_raising_relay_never_fails_the_observation(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        def boom(snap):  # type: ignore[no-untyped-def]
            raise RuntimeError("relay broke its contract")

        monkeypatch.setattr(screencast, "emit_snapshot_frame", boom)
        svc = _service(_Stub(_snap()))
        out = svc.snapshot(APP, SnapshotRequest(want_image=True), session_key="s")
        try:
            assert out.image_path
        finally:
            Path(out.image_path).unlink(missing_ok=True)


class TestPointerConfinement:
    def test_inside_point_reaches_the_backend(self) -> None:
        stub = _Stub(_snap())
        _service(stub).click(APP, None, ClickRequest(method="app_post", point=(10.0, 10.0)))
        assert stub.calls == ["click"]

    def test_outside_point_is_refused_before_the_backend(self) -> None:
        stub = _Stub(_snap())
        with pytest.raises(ComputerUseError, match="outside"):
            _service(stub).click(APP, None, ClickRequest(method="app_post", point=(500.0, 10.0)))
        assert stub.calls == []

    def test_drag_end_outside_is_refused(self) -> None:
        stub = _Stub(_snap())
        with pytest.raises(ComputerUseError, match="outside"):
            _service(stub).drag(APP, DragRequest(start=(1.0, 1.0), end=(150.0, 1.0)))
        assert stub.calls == []

    def test_unknown_bounds_fail_closed(self) -> None:
        stub = _Stub(_snap(), bounds=None)
        with pytest.raises(ComputerUseError, match="unknown"):
            _service(stub).click(APP, None, ClickRequest(method="app_post", point=(1.0, 1.0)))
        assert stub.calls == []

    def test_element_click_needs_no_bounds(self) -> None:
        stub = _Stub(_snap(), bounds=None)
        rec = ElementRec(index=0, role="button")
        _service(stub).click(APP, rec, ClickRequest(method="accessibility"))
        assert stub.calls == ["click"]

    def test_native_confinement_is_left_to_the_native_driver(self) -> None:
        stub = _Stub(_snap(), bounds=None)
        stub.confines_points_natively = True
        _service(stub).click(APP, None, ClickRequest(method="app_post", point=(999.0, 999.0)))
        assert stub.calls == ["click"]

    @pytest.mark.parametrize(
        "x, y, inside", [(0, 0, True), (99, 99, True), (100, 50, False), (-1, 5, False)]
    )
    def test_bounds_edges(self, x, y, inside) -> None:  # type: ignore[no-untyped-def]
        assert floors.point_in_bounds((0.0, 0.0, 100.0, 100.0), (x, y)) is inside


class TestSelfWindowAndLaunch:
    def test_denied_title_represents_its_process(self) -> None:
        plain = AppRef(name="Firefox", pid=9, window_id=1, window_title="News")
        own = AppRef(name="Firefox", pid=9, window_id=2, window_title="Kiro Crew")
        assert floors.representative_window([plain, own]) is own
        assert floors.representative_window([plain]) is plain
        assert floors.representative_window([]) is None

    @pytest.mark.parametrize(
        "query",
        [
            "/usr/bin/xterm",
            "..\\evil",
            "C:\\Windows\\cmd.exe",
            "https://x",
            "sh -c 'id'; x",
            "-e",
            "~/bin/x",
            "a|b",
        ],
    )
    def test_launch_query_must_be_a_name(self, query: str) -> None:
        assert floors.check_launch_query(query)

    @pytest.mark.parametrize(
        "query", ["Calculator", "gnome-calculator", "Snipping Tool", "Visual Studio Code"]
    )
    def test_plain_names_pass(self, query: str) -> None:
        assert floors.check_launch_query(query) is None
