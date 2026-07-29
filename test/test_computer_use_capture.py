"""Screenshot capture, persistence and the ring trim (``capture_macos.py``).

Four properties, each with a concrete failure it prevents:

* **The spool lives under ``tempfile.gettempdir()``**, never a hardcoded ``/tmp``
  or a raw ``TMPDIR`` read. Pinned by SOURCE TEXT as well as behaviour — the same
  idiom and the same assertion shape ``test_mcp_playwright_proxy.py`` uses — because
  ``/tmp`` does not exist on Windows and macOS hands a sandboxed process a private
  per-user temp dir a hardcoded path would miss.
* **The directory is ``0o700`` and every file goes through
  ``platform_compat.restrict_to_owner``.** The frames are pixels of the operator's
  own windows; another local account must not be able to list or read them. The
  mode is re-asserted after ``makedirs`` because ``mode=`` is filtered by the umask
  and ``exist_ok=True`` skips it entirely for a directory a laxer earlier build
  created.
* **The spool is ring-trimmed to ``SCREENSHOT_KEEP``.** It is a cache, not an
  archive: a long session must not be able to fill the temp volume.
* **A snapshot containing a secure element produces NO screenshot at all.** THE
  always-on floor. A password field's rendered glyphs are a credential even after
  the tree redacted its value, and there is no reliable way to blank a
  sub-rectangle of an already-encoded JPEG — so suppression is whole-window, and a
  partial redaction that missed would be worse than none.

Runs on any platform: the spool is redirected into ``tmp_path`` and the native
encode is monkeypatched, so no framework loads and no window is captured.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.computer_use import capture_macos, macos_ffi
from kiro_crew.computer_use.types import (
    DEFAULT_SCREENSHOT_JPEG_QUALITY,
    DEFAULT_SCREENSHOT_MAX_PX,
    MAX_SCREENSHOT_MAX_PX,
    MIN_SCREENSHOT_MAX_PX,
    SCREENSHOT_DIR_NAME,
    SCREENSHOT_FILE_PREFIX,
    SCREENSHOT_FILE_SUFFIX,
    SCREENSHOT_KEEP,
    SECURE_SUBROLE,
    AppRef,
    ElementRec,
    Snapshot,
)

_JPEG = b"\xff\xd8\xff\xc0\x00\x11\x08\x03\xf0\x05\x00\x03\x01\x11\x00\xff\xd9"
_WINDOW_ID = 8801
_APP = AppRef(name="Finder", pid=1041, bundle_id="com.apple.finder", window_id=_WINDOW_ID)


@pytest.fixture
def spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the screenshot spool into ``tmp_path``.

    Patched at ``macos_ffi.shots_dir_default`` — the single definition
    ``capture_macos.shots_dir`` delegates to — so both the writer and the trimmer
    see the same redirected path and a test cannot accidentally exercise one
    against the real temp dir.
    """
    target = tmp_path / SCREENSHOT_DIR_NAME
    monkeypatch.setattr(macos_ffi, "shots_dir_default", lambda: str(target))
    return target


@pytest.fixture
def encoded(monkeypatch: pytest.MonkeyPatch):
    """Stub the native capture+encode; records the arguments it was given."""
    calls: list[dict[str, object]] = []

    def _capture(window_id: int, *, max_px: int, quality: float):
        calls.append({"window_id": window_id, "max_px": max_px, "quality": quality})
        return _JPEG, 1280, 1008

    monkeypatch.setattr(macos_ffi, "capture_window_jpeg", _capture)
    return calls


def _snapshot(*, secure: bool = False, window_id: int = _WINDOW_ID) -> Snapshot:
    """A snapshot with (or without) a secure element.

    The secure record deliberately reports the INNOCUOUS ``AXTextField`` role with
    a secure SUBROLE and a populated value — exactly what macOS reports for a real
    password box, and the shape a role-only check misses.
    """
    elements = [ElementRec(index=0, role="AXWindow", title="Documents")]
    if secure:
        elements.append(
            ElementRec(
                index=1,
                role="AXTextField",
                subrole=SECURE_SUBROLE,
                title="Password",
                value="correct-horse-battery-staple",
                secure=True,
            )
        )
    app = (
        _APP
        if window_id == _WINDOW_ID
        else AppRef(name=_APP.name, pid=_APP.pid, bundle_id=_APP.bundle_id, window_id=window_id)
    )
    return Snapshot(
        app=app,
        elements=tuple(elements),
        window_title="Documents",
        captured_at=1234.0,
        has_secure=secure,
    )


def _spooled(directory: Path) -> list[str]:
    """Names of the spooled frames, chronologically (the names are timestamps)."""
    if not directory.exists():
        return []
    return sorted(
        name
        for name in os.listdir(directory)
        if name.startswith(SCREENSHOT_FILE_PREFIX) and name.endswith(SCREENSHOT_FILE_SUFFIX)
    )


# ── the tempdir idiom, pinned by source text ──


def test_source_uses_tempfile_gettempdir_not_a_hardcoded_tmp():
    """The spool path must derive from ``tempfile.gettempdir()``.

    Idiom and assertion shape copied from
    ``test_mcp_playwright_proxy.py::test_source_uses_tempfile_gettempdir_not_hardcoded_slash_tmp``.
    ``os.environ.get("TMPDIR", "/tmp")`` was the original defect there: the
    fallback does not exist on Windows and ``os.makedirs`` crashed on it.

    Asserted against ``macos_ffi`` (which owns the default path) plus the absence
    of any hardcoded literal in ``capture_macos`` itself, since the path could be
    re-derived in either file.
    """
    ffi_src = inspect.getsource(macos_ffi)
    assert "tempfile.gettempdir()" in ffi_src
    assert 'os.environ.get("TMPDIR", "/tmp")' not in ffi_src

    capture_src = inspect.getsource(capture_macos)
    assert 'os.environ.get("TMPDIR"' not in capture_src
    assert '"/tmp"' not in capture_src
    assert "'/tmp'" not in capture_src


def test_default_spool_path_is_under_the_platform_tempdir():
    """Behavioural half: the real (un-redirected) path resolves correctly."""
    import tempfile

    path = capture_macos.shots_dir()
    assert path.startswith(tempfile.gettempdir())
    assert path.endswith(SCREENSHOT_DIR_NAME)


# ── directory mode ──


def test_spool_dir_is_created_owner_only(spool: Path):
    """``0o700``: no other local account may list the operator's window pixels."""
    created = capture_macos.ensure_shots_dir()
    assert created == str(spool)
    assert spool.is_dir()
    if platform_compat.IS_POSIX:
        assert spool.stat().st_mode & 0o777 == 0o700


def test_spool_dir_mode_is_reasserted_for_an_existing_lax_dir(spool: Path):
    """A directory an earlier laxer build created must be tightened.

    ``makedirs(mode=...)`` is filtered by the umask, and ``exist_ok=True`` skips
    the mode ENTIRELY for a directory that already exists — so without the
    explicit re-assert a world-readable spool would survive forever.
    """
    spool.mkdir(parents=True)
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the lax mode is the FIXTURE, not the behaviour under test: this line stages a world-writable dir precisely so the assertion below can prove ensure_shots_dir() tightens it back to 0o700. Removing it would delete the regression test for a world-readable screenshot spool.  # noqa: E501
    os.chmod(spool, 0o777)
    capture_macos.ensure_shots_dir()
    if platform_compat.IS_POSIX:
        assert spool.stat().st_mode & 0o777 == 0o700


def test_spool_dir_calls_chmod_safe_not_raw_chmod(spool: Path, monkeypatch: pytest.MonkeyPatch):
    """The mode goes through ``platform_compat``, never a raw ``os.chmod``.

    ``os.chmod`` has no meaningful effect on Windows; routing through the shim is
    what makes the intent portable (and is the repo's standing rule).
    """
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(
        platform_compat, "chmod_safe", lambda path, mode: seen.append((str(path), mode))
    )
    monkeypatch.setattr(capture_macos.platform_compat, "chmod_safe", platform_compat.chmod_safe)
    capture_macos.ensure_shots_dir()
    assert seen == [(str(spool), 0o700)]


def test_unchmodable_dir_warns_but_still_captures(spool: Path, monkeypatch: pytest.MonkeyPatch):
    """A filesystem that cannot apply modes must not disable the feature.

    Refusing to capture over a mode failure would take out a whole capability on,
    e.g., an exFAT volume; the warning is the operator's signal.
    """

    def _boom(path, mode):
        raise OSError("no modes on this filesystem")

    monkeypatch.setattr(capture_macos.platform_compat, "chmod_safe", _boom)
    assert capture_macos.ensure_shots_dir() == str(spool)
    assert spool.is_dir()


# ── file persistence ──


def test_persist_writes_the_bytes_and_returns_the_path(spool: Path):
    path = capture_macos.persist_jpeg(_JPEG)
    assert path
    assert Path(path).read_bytes() == _JPEG
    assert Path(path).parent == spool
    assert Path(path).name.startswith(SCREENSHOT_FILE_PREFIX)
    assert Path(path).name.endswith(SCREENSHOT_FILE_SUFFIX)


def test_persist_restricts_the_file_to_the_owner(spool: Path, monkeypatch: pytest.MonkeyPatch):
    """``restrict_to_owner`` on the FILE too — defence in depth.

    The directory is already ``0o700``, but if that mode could not be applied (the
    warn-and-continue branch above) the per-file restriction is the only thing
    left standing between the frame and another local account.
    """
    seen: list[str] = []
    monkeypatch.setattr(
        capture_macos.platform_compat, "restrict_to_owner", lambda path: seen.append(str(path))
    )
    path = capture_macos.persist_jpeg(_JPEG)
    assert seen == [path]


def test_persist_continues_when_restrict_to_owner_fails(
    spool: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failed restriction warns and continues — the file is in a 0o700 dir."""

    def _boom(path):
        raise OSError("unsupported")

    monkeypatch.setattr(capture_macos.platform_compat, "restrict_to_owner", _boom)
    path = capture_macos.persist_jpeg(_JPEG)
    assert path and Path(path).exists()


def test_persist_of_empty_bytes_writes_nothing(spool: Path):
    """Nothing to persist must not create a zero-byte frame the model is told about."""
    assert capture_macos.persist_jpeg(b"") == ""
    assert _spooled(spool) == []


def test_persist_returns_empty_when_the_write_fails(spool: Path, monkeypatch: pytest.MonkeyPatch):
    """A spool failure degrades to "no image", never an exception.

    The accessibility tree is the primary channel, so a temp-write problem must
    not fail the whole observation.
    """

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(capture_macos.os, "makedirs", _boom)
    assert capture_macos.persist_jpeg(_JPEG) == ""


def test_frame_names_never_collide_even_within_one_millisecond(
    spool: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two captures must NEVER resolve to the same path.

    Originally this only pinned the millisecond stamp, which was not enough: the
    gateway offloads snapshots to a thread pool, so two captures of DIFFERENT
    applications can land in the same millisecond. With a timestamp-only name both
    writers opened one path, the second overwrote the first, and a caller was handed
    a screenshot of an app it never asked about. Allocation is now atomic
    (``tempfile.mkstemp``), so the clock is frozen here — the hostile case — and the
    names must still differ.
    """
    clock = {"now": 1769472013.411}
    monkeypatch.setattr(capture_macos.time, "time", lambda: clock["now"])
    # Same millisecond, three times over.
    paths = [capture_macos.persist_jpeg(_JPEG) for _ in range(3)]
    assert all(paths), paths
    assert len(set(paths)) == 3, paths
    # Every frame keeps its own bytes — the actual failure being prevented.
    for path in paths:
        assert Path(path).read_bytes() == _JPEG
    # The timestamp is still in the name (the ring trim orders by mtime, and a human
    # reading the spool wants it) and the suffix is unchanged.
    for path in paths:
        name = Path(path).name
        assert name.startswith(f"{SCREENSHOT_FILE_PREFIX}1769472013411-")
        assert name.endswith(SCREENSHOT_FILE_SUFFIX)


def test_a_frame_is_created_owner_only_with_no_world_readable_window(
    spool: Path, monkeypatch: pytest.MonkeyPatch
):
    """``mkstemp`` creates at 0o600, so the file is never briefly world-readable."""
    monkeypatch.setattr(capture_macos.time, "time", lambda: 1769472013.411)
    path = capture_macos.persist_jpeg(_JPEG)
    assert path
    if platform_compat.IS_POSIX:
        assert Path(path).stat().st_mode & 0o777 == 0o600


# ── ring trim ──


def test_ring_trims_to_screenshot_keep(spool: Path):
    """The spool is bounded at ``SCREENSHOT_KEEP`` — it is a cache, not an archive."""
    spool.mkdir(parents=True)
    for i in range(SCREENSHOT_KEEP + 25):
        (spool / f"{SCREENSHOT_FILE_PREFIX}{1000000 + i}{SCREENSHOT_FILE_SUFFIX}").write_bytes(
            _JPEG
        )
    removed = capture_macos.trim_shots_dir()
    assert removed == 25
    assert len(_spooled(spool)) == SCREENSHOT_KEEP


def test_ring_trim_removes_the_oldest_first(spool: Path):
    """Oldest-first, by FILENAME.

    The names carry a millisecond timestamp, so a lexical sort IS chronological —
    and needs no ``stat`` per file, and is not racy against a frame still being
    written (whose mtime would be).
    """
    spool.mkdir(parents=True)
    for stamp in (1000, 2000, 3000, 4000):
        (spool / f"{SCREENSHOT_FILE_PREFIX}{stamp}{SCREENSHOT_FILE_SUFFIX}").write_bytes(_JPEG)
    capture_macos.trim_shots_dir(keep=2)
    assert _spooled(spool) == [
        f"{SCREENSHOT_FILE_PREFIX}3000{SCREENSHOT_FILE_SUFFIX}",
        f"{SCREENSHOT_FILE_PREFIX}4000{SCREENSHOT_FILE_SUFFIX}",
    ]


def test_ring_trim_ignores_unrelated_files(spool: Path):
    """Only our own frames are eligible — the temp dir is shared."""
    spool.mkdir(parents=True)
    (spool / "not-ours.txt").write_text("keep me")
    for stamp in (1000, 2000, 3000):
        (spool / f"{SCREENSHOT_FILE_PREFIX}{stamp}{SCREENSHOT_FILE_SUFFIX}").write_bytes(_JPEG)
    capture_macos.trim_shots_dir(keep=1)
    assert (spool / "not-ours.txt").exists()
    assert len(_spooled(spool)) == 1


def test_persist_trims_after_writing(spool: Path):
    """Trimming happens AFTER the write, so the cap holds whatever happens next.

    Trimming first would leave the cap violated by exactly one file for the
    lifetime of a session that then crashed.
    """
    spool.mkdir(parents=True)
    for i in range(SCREENSHOT_KEEP):
        (spool / f"{SCREENSHOT_FILE_PREFIX}{1000 + i:09d}{SCREENSHOT_FILE_SUFFIX}").write_bytes(
            _JPEG
        )
    path = capture_macos.persist_jpeg(_JPEG)
    names = _spooled(spool)
    assert len(names) == SCREENSHOT_KEEP
    assert Path(path).name in names, "the frame just reported to the model must survive the trim"


def test_ring_trim_of_a_missing_dir_is_a_no_op(spool: Path):
    """No spool yet is a normal state (nothing has been captured)."""
    assert capture_macos.trim_shots_dir() == 0


def test_ring_trim_with_nonpositive_keep_does_nothing(spool: Path):
    """``keep <= 0`` must not be read as "delete everything"."""
    spool.mkdir(parents=True)
    (spool / f"{SCREENSHOT_FILE_PREFIX}1000{SCREENSHOT_FILE_SUFFIX}").write_bytes(_JPEG)
    assert capture_macos.trim_shots_dir(keep=0) == 0
    assert len(_spooled(spool)) == 1


def test_ring_trim_skips_a_file_removed_concurrently(spool: Path, monkeypatch: pytest.MonkeyPatch):
    """A file another process deleted under us is not an error worth failing over."""
    spool.mkdir(parents=True)
    for stamp in (1000, 2000, 3000):
        (spool / f"{SCREENSHOT_FILE_PREFIX}{stamp}{SCREENSHOT_FILE_SUFFIX}").write_bytes(_JPEG)

    real_unlink = capture_macos.os.unlink

    def _racy(path):
        real_unlink(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(capture_macos.os, "unlink", _racy)
    # Must not raise; the count it reports simply excludes the racy one.
    capture_macos.trim_shots_dir(keep=1)


# ── the secure-field floor ──


def test_secure_snapshot_produces_no_screenshot_at_all(spool: Path, encoded):
    """**THE always-on floor.** A secure element suppresses the WHOLE window.

    A password field's rendered glyphs are a credential even though the tree
    redacted the value, and there is no reliable way to blank a sub-rectangle of an
    already-encoded JPEG — a partial redaction that missed would be worse than
    none. Asserted three ways: no path, no bytes, and — the strongest — the native
    capture was NEVER CALLED, so the pixels never existed in this process.
    """
    snap = _snapshot(secure=True)
    result = capture_macos.capture_snapshot_image(snap)
    assert result is snap
    assert result.image_path == ""
    assert result.image_jpeg == b""
    assert encoded == [], "a window with a secure field must not even be captured"
    assert _spooled(spool) == []


def test_non_secure_snapshot_is_captured_and_persisted(spool: Path, encoded):
    """The control case: without a secure element the frame is spooled."""
    result = capture_macos.capture_snapshot_image(_snapshot())
    assert result.image_path
    assert Path(result.image_path).read_bytes() == _JPEG
    assert (result.image_width, result.image_height) == (1280, 1008)
    assert len(encoded) == 1


def test_capture_is_skipped_without_a_window_id(spool: Path, encoded):
    """No window id means nothing addressable to capture."""
    snap = _snapshot(window_id=0)
    assert capture_macos.capture_snapshot_image(snap) is snap
    assert encoded == []


def test_capture_degrades_to_tree_only_when_the_encode_yields_nothing(
    spool: Path, monkeypatch: pytest.MonkeyPatch
):
    """A closed window yields a NULL image; the observation still succeeds."""
    monkeypatch.setattr(macos_ffi, "capture_window_jpeg", lambda wid, **kw: (b"", 0, 0))
    snap = _snapshot()
    result = capture_macos.capture_snapshot_image(snap)
    assert result is snap
    assert result.image_path == ""


def test_capture_never_raises_when_the_native_call_fails(
    spool: Path, monkeypatch: pytest.MonkeyPatch
):
    """An FFI failure degrades the result, it does not fail the tool call."""

    def _boom(window_id, **kwargs):
        raise OSError("CoreGraphics said no")

    monkeypatch.setattr(macos_ffi, "capture_window_jpeg", _boom)
    snap = _snapshot()
    assert capture_macos.capture_snapshot_image(snap) is snap


def test_capture_degrades_when_persistence_fails(
    spool: Path, encoded, monkeypatch: pytest.MonkeyPatch
):
    """Bytes with nowhere to live are dropped, not reported as a phantom path."""
    monkeypatch.setattr(capture_macos, "persist_jpeg", lambda raw: "")
    snap = _snapshot()
    result = capture_macos.capture_snapshot_image(snap)
    assert result is snap
    assert result.image_jpeg == b""


# ── compression parameters ──


def test_defaults_are_computer_uses_own_1280_q55(spool: Path, encoded):
    """1280 / q0.55 — deliberately NOT browse's 1920 / q70.

    The accessibility tree is the primary channel and the image is corroboration,
    so the measured 1280/q55 output (~24KB, ~8.3k tokens, verified fully legible)
    is the right trade against a raw window PNG's ~41k tokens.
    """
    capture_macos.capture_snapshot_image(_snapshot())
    assert encoded[0]["max_px"] == DEFAULT_SCREENSHOT_MAX_PX == 1280
    assert encoded[0]["quality"] == pytest.approx(DEFAULT_SCREENSHOT_JPEG_QUALITY / 100.0)
    assert DEFAULT_SCREENSHOT_JPEG_QUALITY == 55


def test_quality_is_scaled_from_the_config_integer(spool: Path, encoded):
    """The config field is 0-100; ImageIO wants 0.0-1.0."""
    capture_macos.capture_snapshot_image(_snapshot(), quality=70)
    assert encoded[0]["quality"] == pytest.approx(0.70)


def test_out_of_range_parameters_are_clamped(spool: Path, encoded):
    """Clamped here, not just in the MCP schema.

    This function is also reachable from ``config.json``, and a zero or negative
    ``max_px`` handed to ImageIO produces either a degenerate image or an unbounded
    one.
    """
    capture_macos.capture_snapshot_image(_snapshot(), max_px=0, quality=0)
    assert encoded[0]["max_px"] == MIN_SCREENSHOT_MAX_PX
    assert encoded[0]["quality"] == pytest.approx(0.01)

    capture_macos.capture_snapshot_image(_snapshot(), max_px=99999, quality=9999)
    assert encoded[1]["max_px"] == MAX_SCREENSHOT_MAX_PX
    assert encoded[1]["quality"] == pytest.approx(1.0)


def test_capture_targets_the_snapshots_own_window_id(spool: Path, encoded):
    """Per-window capture, never full-screen.

    This is the mitigation for "any window can be in frame": the capture is scoped
    to the one window the model addressed.
    """
    capture_macos.capture_snapshot_image(_snapshot())
    assert encoded[0]["window_id"] == _WINDOW_ID


# ── no subprocess, no image library ──


def test_capture_uses_no_subprocess_and_no_image_library():
    """Neither ``screencapture`` nor Pillow is reachable from the module.

    Both were deliberately deleted from the design: a subprocess node would need a
    ``test_spawn_audit.py::BENIGN_SPAWNS`` entry, and Pillow is declared in neither
    ``setup.cfg`` nor ``pyproject.toml`` so it would be an optional dependency to
    degrade around.

    Checked over the module's IMPORTS and CALLS via the AST rather than over the raw
    text: the module's own docstring names both, to explain why they were rejected,
    and a text scan would punish documenting the decision.
    """
    tree = ast.parse(inspect.getsource(capture_macos))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.alias):
            imported.add(node.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)

    for forbidden in ("subprocess", "PIL", "pillow"):
        assert forbidden not in imported, f"capture_macos must not import {forbidden!r}"
    for forbidden in ("run", "Popen", "system", "spawn", "check_output", "popen"):
        assert forbidden not in called, f"capture_macos must not call {forbidden!r}"
    # Structural half: no handle to either was bound at import time.
    assert not hasattr(capture_macos, "subprocess")
    assert not hasattr(capture_macos, "Image")


# ── the live-view (PiP) relay hook ──
#
# The capture layer's only other consumer. These cases pin the WIRING — that a
# successful capture offers the frame it just encoded to the relay, and that a
# suppressed capture offers nothing at all. The relay's own three suppressions
# (no surface scope / secure window / screenshot-denied ceiling) are covered in
# ``test_computer_use_api.py::TestLiveViewSuppression``.


@pytest.fixture
def relayed(monkeypatch: pytest.MonkeyPatch) -> list[Snapshot]:
    """Capture what ``screencast.emit_snapshot_frame`` was handed, if anything."""
    seen: list[Snapshot] = []

    def _emit(snap: Snapshot) -> bool:
        seen.append(snap)
        return True

    monkeypatch.setattr(capture_macos.screencast, "emit_snapshot_frame", _emit)
    return seen


def test_a_successful_capture_offers_the_encoded_frame_to_the_live_view(
    spool: Path, encoded, relayed: list[Snapshot]
):
    """The relay receives the SAME bytes the model got — never a second capture.

    Asserted on identity of the encoded bytes and on the native capture running
    exactly once: the PiP path must not add a capture of its own, or the panel
    would be able to make the agent screenshot windows it never asked about.
    """
    result = capture_macos.capture_snapshot_image(_snapshot())
    assert len(encoded) == 1, "the live view must not trigger a second capture"
    assert len(relayed) == 1
    assert relayed[0].image_jpeg == result.image_jpeg == _JPEG
    assert relayed[0].image_path == result.image_path


def test_a_secure_window_offers_nothing_to_the_live_view(
    spool: Path, encoded, relayed: list[Snapshot]
):
    """No pixels exist, so there is nothing to mirror — the floor holds upstream."""
    capture_macos.capture_snapshot_image(_snapshot(secure=True))
    assert relayed == []


def test_a_failed_capture_offers_nothing_to_the_live_view(
    spool: Path, relayed: list[Snapshot], monkeypatch: pytest.MonkeyPatch
):
    """A NULL image (closed window) must not produce an empty frame on the wire."""
    monkeypatch.setattr(macos_ffi, "capture_window_jpeg", lambda wid, **kw: (b"", 0, 0))
    capture_macos.capture_snapshot_image(_snapshot())
    assert relayed == []


def test_a_failed_persist_offers_nothing_to_the_live_view(
    spool: Path, encoded, relayed: list[Snapshot], monkeypatch: pytest.MonkeyPatch
):
    """The relay rides the persisted result, so a dropped frame is never mirrored."""
    monkeypatch.setattr(capture_macos, "persist_jpeg", lambda raw: "")
    capture_macos.capture_snapshot_image(_snapshot())
    assert relayed == []


def test_a_relay_failure_never_breaks_the_observation(
    spool: Path, encoded, monkeypatch: pytest.MonkeyPatch
):
    """The mirror is decoration; a raising relay must not fail the tool call.

    ``emit_snapshot_frame`` is itself contracted never to raise, so this pins the
    CALL SITE's own robustness against that inner contract being broken later —
    ``capture_snapshot_image`` promises "never raises" and a decorative panel must
    not be able to break that promise.
    """

    def _boom(snap):
        raise RuntimeError("relay exploded")

    monkeypatch.setattr(capture_macos.screencast, "emit_snapshot_frame", _boom)
    result = capture_macos.capture_snapshot_image(_snapshot())
    # The observation is intact: bytes, dimensions and the spooled path all stand.
    assert result.image_path
    assert result.image_jpeg == _JPEG
    assert len(_spooled(spool)) == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])


class TestFramePayloadRegexIsLinear:
    """The base64 gate must not be a ReDoS (CodeQL ``py/polynomial-redos``, high).

    The pattern is applied to a caller-supplied string of up to
    ``MAX_FRAME_B64_CHARS``, so an unbounded-backtracking shape is a remote CPU
    stall on the loopback ingress. The original `[A-Za-z0-9+/]+={0,2}` retried every
    split point on a long non-matching run.
    """

    def test_a_long_non_matching_run_is_rejected_promptly(self):
        import time

        from kiro_crew.computer_use import screencast

        # A long charset run that CANNOT complete the match — the worst case for the
        # vulnerable shape. Generous bound: the linear form does 400k in ~4ms, so a
        # second means the quadratic behaviour is back.
        hostile = "A" * 200_000 + "!"
        start = time.monotonic()
        assert screencast._B64_RE.fullmatch(hostile) is None
        assert time.monotonic() - start < 1.0

    def test_real_base64_still_matches(self):
        from kiro_crew.computer_use import screencast

        for good in ("QUJD", "QUJDRA==", "QUJDRUY=", "A" * 4000):
            assert screencast._B64_RE.fullmatch(good), good

    def test_malformed_padding_is_rejected(self):
        """Stricter than the old pattern: quad structure is now enforced."""
        from kiro_crew.computer_use import screencast

        for bad in ("QUJDRA=", "A=B", "AB!CD", "====", "QUJD="):
            assert screencast._B64_RE.fullmatch(bad) is None, bad


class TestIncompleteScanSuppressesCapture:
    """An incomplete secure-field scan must not permit pixels (reviewer finding).

    ``has_secure`` covers every node the walk CLASSIFIED, including those past the
    reporting budget — but the walk has hard cutoffs of its own
    (``MAX_TREE_NODES_LIMIT`` nodes, the ``MAX_WALK_SECS`` deadline). When one
    fires, ``has_secure=False`` means "none seen", not "none present", so a password
    field beyond the cutoff would otherwise leave the window capturable.
    """

    def test_a_node_truncated_snapshot_is_not_captured(self, spool: Path, encoded):
        snap = _snapshot()
        truncated = Snapshot(
            app=snap.app,
            elements=snap.elements,
            window_title=snap.window_title,
            captured_at=snap.captured_at,
            truncated=True,
        )
        result = capture_macos.capture_snapshot_image(truncated)
        assert result.image_path == ""
        assert encoded == [], "the window was captured despite an incomplete scan"

    def test_a_depth_truncated_snapshot_is_not_captured(self, spool: Path, encoded):
        snap = _snapshot()
        truncated = Snapshot(
            app=snap.app,
            elements=snap.elements,
            window_title=snap.window_title,
            captured_at=snap.captured_at,
            depth_truncated=True,
        )
        result = capture_macos.capture_snapshot_image(truncated)
        assert result.image_path == ""
        assert encoded == []

    def test_a_complete_scan_is_still_captured(self, spool: Path, encoded):
        """The inverse, so the rule above cannot swallow every screenshot."""
        result = capture_macos.capture_snapshot_image(_snapshot())
        assert result.image_path
        assert encoded, "a complete, non-secure snapshot must still be captured"


def test_attaching_a_screenshot_preserves_EVERY_other_snapshot_field(spool: Path, encoded):
    """Capture must be purely ADDITIVE — a real bug, found by reading the code.

    ``capture_snapshot_image`` rebuilt the frozen ``Snapshot`` by enumerating its
    fields, which made it silently lossy: every field added to ``Snapshot``
    afterwards was dropped whenever a screenshot was attached. Caught with
    ``window_bounds`` / ``selected_text``, and the shape of the loss is what makes it
    nasty — the SAME snapshot without a screenshot carried them fine, so the failure
    appears only on responses that also carry an image, and the missing window origin
    is exactly what a model needs to convert an element frame into a screen point.

    Asserted field-by-field against the whole dataclass rather than by naming the two
    fields, so the NEXT field added is covered without anyone remembering to.
    """
    import dataclasses

    snap = dataclasses.replace(
        _snapshot(),
        window_bounds=(220.0, 118.0, 900.0, 600.0),
        selected_text="quarterly",
        elements=(
            ElementRec(index=0, role="AXWindow", title="Documents", frame=(0.0, 0.0, 900.0, 600.0)),
        ),
    )
    result = capture_macos.capture_snapshot_image(snap)
    # The capture really happened (otherwise this would pass trivially).
    assert result.image_path
    image_fields = {"image_jpeg", "image_path", "image_width", "image_height"}
    for f in dataclasses.fields(Snapshot):
        if f.name in image_fields:
            continue
        assert getattr(result, f.name) == getattr(snap, f.name), f.name
