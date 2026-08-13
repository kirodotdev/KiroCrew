"""Guards the committed macOS ``.icns`` bundles against malformed small slots.

electron-builder's own PNG->icns converter writes the legacy ``icp4`` / ``icp5``
slots (16pt / 32pt @1x) with PNG payloads. macOS decodes those two slots as raw
ARGB, so every small-icon consumer — Spotlight rows, Finder list views, and
third-party app pickers such as Logi Options+ — renders the compressed bytes as
colored static instead of the icon, while 128px-and-up surfaces look correct.

The fix is to ship .icns files produced by Apple's ``iconutil``
(``packaging/make-icns.sh``), which emits ``ic04`` / ``ic05`` for those sizes,
and point ``mac.icon`` at them. This suite is the regression gate: it fails if
someone regenerates the icons with a converter that reintroduces
``icp4``/``icp5``, or points ``mac.icon`` back at a PNG (which hands icns
generation to electron-builder again). Pure byte parsing — no macOS-only tools,
so it runs on Linux CI too.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ELECTRON_DIR = _REPO_ROOT / "website" / "electron"
#: Every .icns the desktop build can ship: the default app and the side-by-side
#: nightly variant (selected via ``-c.mac.icon=`` in packaging/build-desktop.sh).
_ICNS_FILES = ("icon.icns", "icon-nightly.icns")
#: The slots macOS mis-decodes when they carry PNG payloads.
_FORBIDDEN_SLOTS = ("icp4", "icp5")
#: Slots macOS decodes as raw ARGB rather than PNG (16pt and 32pt @1x). Writing
#: a PNG stream into these is exactly the bug this suite guards against.
_ARGB_SLOTS = ("ic04", "ic05")
#: Slots macOS actually consults for the small sizes that were rendering as
#: static, plus the retina variants. Missing any of these means a consumer has
#: to scale a larger rep, which is the blurry-icon failure mode.
_REQUIRED_SLOTS = ("ic04", "ic05", "ic07", "ic11", "ic12")


def _slots(icns: Path) -> dict[str, bytes]:
    """Parse an .icns into {4-char slot name: payload bytes}."""
    data = icns.read_bytes()
    magic, declared_len = struct.unpack(">4sI", data[:8])
    assert magic == b"icns", f"{icns.name}: not an icns file (magic={magic!r})"
    assert declared_len == len(data), (
        f"{icns.name}: truncated — header declares {declared_len} bytes, "
        f"file is {len(data)}"
    )
    out: dict[str, bytes] = {}
    offset = 8
    while offset + 8 <= len(data):
        name, length = struct.unpack(">4sI", data[offset : offset + 8])
        if length <= 8:
            break
        out[name.decode("ascii", "replace")] = data[offset + 8 : offset + length]
        offset += length
    return out


@pytest.mark.parametrize("filename", _ICNS_FILES)
def test_icns_exists_and_parses(filename: str) -> None:
    icns = _ELECTRON_DIR / filename
    assert icns.is_file(), (
        f"{filename} is missing — regenerate it with packaging/make-icns.sh"
    )
    assert _slots(icns), f"{filename}: no icon representations found"


@pytest.mark.parametrize("filename", _ICNS_FILES)
def test_icns_has_no_legacy_png_slots(filename: str) -> None:
    """``icp4``/``icp5`` render as colored static on macOS. Never ship them."""
    present = set(_slots(_ELECTRON_DIR / filename))
    offenders = sorted(present & set(_FORBIDDEN_SLOTS))
    assert not offenders, (
        f"{filename} carries {offenders}, which macOS decodes as raw ARGB and "
        "draws as colored static at 16/32px. Regenerate with Apple's iconutil "
        "via packaging/make-icns.sh instead of letting electron-builder convert "
        "a PNG."
    )


@pytest.mark.parametrize("filename", _ICNS_FILES)
def test_icns_covers_required_sizes(filename: str) -> None:
    present = set(_slots(_ELECTRON_DIR / filename))
    missing = [slot for slot in _REQUIRED_SLOTS if slot not in present]
    assert not missing, (
        f"{filename} is missing {missing} — small-size consumers would have to "
        "downscale a larger representation. Regenerate with "
        "packaging/make-icns.sh."
    )


@pytest.mark.parametrize("filename", _ICNS_FILES)
def test_icns_payload_encoding_matches_slot(filename: str) -> None:
    """The 16/32pt slots are ARGB; the rest are PNG. This is the whole bug.

    macOS decodes ``ic04``/``ic05`` (and the legacy ``icp4``/``icp5``) as raw
    ARGB, and everything from ``ic07`` up as PNG. electron-builder wrote PNG
    bytes into the ARGB-decoded slots, so macOS drew the compressed stream as
    pixels — the colored static. iconutil writes an ``ARGB``-prefixed body
    there instead, which is why the pre-built icns renders correctly.
    """
    for slot, body in _slots(_ELECTRON_DIR / filename).items():
        if slot == "info":  # iconutil's trailing plist, not an image
            continue
        if slot in _ARGB_SLOTS:
            assert body[:4] == b"ARGB", (
                f"{filename}: slot {slot} must carry an ARGB payload — macOS "
                "decodes this slot as raw ARGB, so a PNG body renders as "
                "colored static. Regenerate with packaging/make-icns.sh."
            )
        else:
            assert body[:8] == b"\x89PNG\r\n\x1a\n", (
                f"{filename}: slot {slot} is not a PNG payload — regenerate "
                "with packaging/make-icns.sh"
            )


def test_mac_icon_config_points_at_icns() -> None:
    """``mac.icon`` must name the .icns, or electron-builder converts a PNG."""
    raw = (_ELECTRON_DIR / "package.json").read_text(encoding="utf-8")
    build = json.loads(raw)["build"]
    assert build["mac"]["icon"] == "icon.icns", (
        "website/electron/package.json build.mac.icon must be 'icon.icns'; a "
        ".png hands icns generation back to electron-builder, which writes the "
        "icp4/icp5 slots that render as static."
    )


def test_nightly_icon_override_points_at_icns() -> None:
    """The nightly variant overrides mac.icon on the CLI — keep it an .icns."""
    # encoding is explicit: the script contains non-ASCII (em dashes), which
    # Windows' default cp1252 locale cannot decode.
    script = (_REPO_ROOT / "packaging" / "build-desktop.sh").read_text(
        encoding="utf-8"
    )
    assert "-c.mac.icon=icon-nightly.icns" in script, (
        "packaging/build-desktop.sh must override the nightly icon with "
        "icon-nightly.icns, not a .png"
    )
