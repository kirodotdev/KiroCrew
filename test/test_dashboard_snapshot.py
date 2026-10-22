"""Tests for ``dashboard_snapshot`` -- a picture of a crewmate's own Dashboard page.

Four layers, cheapest first.

* :mod:`kiro_crew.dashboard_page_image`: how a picture is read, bounded and taken again
  smaller, with the browser call stubbed.
* The page run against the FAKE DevTools browser: the picture is asked for only when the
  caller asks, taken after the page settles, and missing with a reason whenever the run
  drew nothing worth showing; only a run asked for the record is given it.
* The route, with the caller gate and the run stubbed: only the caller's own page in force
  is drawn, the picture lands in an owner-only file under the data home that no link can
  redirect, and every way the answer is ``no_image``.
* The MCP tool, with the gateway stubbed: it names the file the gateway wrote, and
  "no image" never names a file.
"""

from __future__ import annotations

import base64
import json
import os
import random
import stat
import struct
import sys
import tempfile
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from fake_devtools_browser import FakeBrowser

from kiro_crew import (
    dashboard_frame,
    dashboard_page_image,
    dashboard_snapshot_look,
    dashboard_synth,
    dashboard_verify,
    mcp_panel,
    pinned_fs,
    platform_compat,
)
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.dashboard.handlers import agent_panel_snapshot, member_dashboard_fields
from kiro_crew.dashboard_page_image import PageImage

LINUX_ONLY = pytest.mark.skipif(
    not platform_compat.IS_LINUX, reason="the page check runs on Linux only"
)

#: The picture is written through a pinned directory walk, which Windows cannot make.
PINNED_WALK = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="no pinned directory walk here"
)

SLUG = "fleet-crew"
CREW = "fleet-crew"


def _png(width: int = 4, height: int = 3, *, pad: int = 0) -> bytes:
    """A real PNG of *width* by *height*, with *pad* bytes of an ancillary chunk."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        dashboard_page_image.PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + (chunk(b"tEXt", b"pad\x00" + b"x" * pad) if pad else b"")
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# --------------------------------------------------------------------------- #
# 1. Taking a picture
# --------------------------------------------------------------------------- #


def test_a_png_header_gives_its_size_and_anything_else_gives_none():
    assert dashboard_page_image.png_size(_png(7, 5)) == (7, 5)
    assert dashboard_page_image.png_size(b"GIF89a" + b"\x00" * 30) is None
    assert dashboard_page_image.png_size(_png()[:20]) is None


def _calls(*replies: dict[str, Any]):
    sent: list[dict[str, Any]] = []
    queue = list(replies)

    def call(params):
        sent.append(dict(params))
        return queue.pop(0)

    return call, sent


def test_a_picture_within_the_cap_is_taken_once_at_full_scale():
    call, sent = _calls({"data": _b64(_png(1280, 800))})

    image = dashboard_page_image.capture(call, (1280, 800))

    assert (image.width, image.height, image.reason) == (1280, 800, "")
    assert image.png == _png(1280, 800)
    assert sent == [{"format": "png", "fromSurface": True, "captureBeyondViewport": False}]


def test_a_picture_over_the_cap_is_taken_again_at_half_scale(monkeypatch):
    monkeypatch.setattr(dashboard_page_image, "MAX_IMAGE_BYTES", 2000)
    call, sent = _calls({"data": _b64(_png(40, 25, pad=3000))}, {"data": _b64(_png(20, 12))})

    image = dashboard_page_image.capture(call, (40, 25))

    assert (image.width, image.height) == (20, 12)
    assert sent[1]["clip"] == {"x": 0, "y": 0, "width": 40, "height": 25, "scale": 0.5}


def test_a_picture_still_over_the_cap_at_half_scale_is_not_kept(monkeypatch):
    monkeypatch.setattr(dashboard_page_image, "MAX_IMAGE_BYTES", 2000)
    big = {"data": _b64(_png(40, 25, pad=3000))}
    call, sent = _calls(big, big)

    image = dashboard_page_image.capture(call, (40, 25))

    assert image.png == b"" and "even at half size" in image.reason
    assert len(sent) == 2


def test_a_reply_too_long_to_fit_is_refused_before_it_is_decoded(monkeypatch):
    """The cap is checked on the encoded length first, so a huge reply costs no decode."""
    monkeypatch.setattr(dashboard_page_image, "_MAX_ENCODED_CHARS", 100)
    call, sent = _calls({"data": "A" * 400}, {"data": "A" * 400})

    image = dashboard_page_image.capture(call, (40, 25))

    assert image.png == b"" and len(sent) == 2


@pytest.mark.parametrize(
    ("reply", "words"),
    [
        ({}, "no picture"),
        ({"data": "not base64!"}, "could not be read"),
        ({"data": _b64(b"GIF89a" + b"\x00" * 40)}, "not a PNG"),
        ({"data": _b64(_png(2000, 10))}, "larger than the page view"),
    ],
)
def test_a_reply_that_is_not_a_usable_png_has_a_reason_and_no_bytes(reply, words):
    call, _sent = _calls(reply)

    image = dashboard_page_image.capture(call, (1280, 800))

    assert image.png == b"" and words in image.reason


# --------------------------------------------------------------------------- #
# 2. The page run, against a fake browser
# --------------------------------------------------------------------------- #


@pytest.fixture
def fast(monkeypatch, tmp_path):
    """A short settle, a private temp root, a fixed copy, memory to spare, and the test's
    own interpreter as the launcher."""
    monkeypatch.setattr(dashboard_verify, "_SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(dashboard_verify, "_UNUSABLE", {})
    monkeypatch.setattr(dashboard_verify, "find_launcher_python", lambda: sys.executable)
    monkeypatch.setattr(platform_compat, "host_available_mib", lambda: 1 << 20)
    monkeypatch.setattr(dashboard_synth, "_fresh_rng", lambda: random.Random(1234))
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return temp_root


def _read() -> dict[str, Any]:
    return dashboard_frame.read_payload({"open_prs": 7}, agentic=[], seq=1)


def _run(monkeypatch, fake: FakeBrowser, page: str = "<p>fine</p>", **kw: Any):
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: str(fake.executable))
    return dashboard_verify.verify_page(page, _read(), **kw)


def _shots(fake: FakeBrowser) -> list[dict[str, Any]]:
    return [r for r in fake.records("command") if r["method"] == "Page.captureScreenshot"]


@LINUX_ONLY
def test_a_run_asked_for_a_picture_hands_back_the_png_of_the_settled_page(
    monkeypatch, tmp_path, fast
):
    fake = FakeBrowser(tmp_path, screenshots=[_b64(_png(1280, 800))])

    v = _run(monkeypatch, fake, capture=True)

    assert v.status == dashboard_verify.STATUS_PASSED
    assert v.image == PageImage(_png(1280, 800), 1280, 800)
    assert "image" not in v.wire() and "png" not in json.dumps(v.wire())
    (shot,) = _shots(fake)
    assert shot["params"]["format"] == "png" and shot["params"]["captureBeyondViewport"] is False


@LINUX_ONLY
def test_a_run_not_asked_for_a_picture_takes_none(monkeypatch, tmp_path, fast):
    """The save's own run takes no picture and holds none."""
    fake = FakeBrowser(tmp_path, screenshots=[_b64(_png())])

    v = _run(monkeypatch, fake)

    assert v.status == dashboard_verify.STATUS_PASSED and v.image is None
    assert _shots(fake) == []


@LINUX_ONLY
def test_a_failed_page_keeps_its_errors_and_its_picture(monkeypatch, tmp_path, fast):
    page = "<p>x</p>\n<script>\n  boom();\n</script>"
    fake = FakeBrowser(
        tmp_path,
        screenshots=[_b64(_png())],
        events=[{"type": "exception", "find": "boom()", "description": "ReferenceError: boom"}],
    )

    v = _run(monkeypatch, fake, page, capture=True)

    assert v.status == dashboard_verify.STATUS_FAILED
    assert [e.line for e in v.errors] == [3]
    assert v.image is not None and v.image.png == _png()


@LINUX_ONLY
def test_a_browser_that_cannot_take_the_picture_leaves_the_verdict_alone(
    monkeypatch, tmp_path, fast
):
    fake = FakeBrowser(tmp_path, screenshot_error=True)

    v = _run(monkeypatch, fake, capture=True)

    assert v.status == dashboard_verify.STATUS_PASSED
    assert v.image is not None and v.image.png == b""
    assert v.image.reason == "the browser could not take the picture"


@LINUX_ONLY
def test_a_picture_that_never_comes_back_ends_at_the_runs_limit(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path, screenshot_silent=True)

    v = _run(monkeypatch, fake, capture=True, timeout=2.0)

    assert v.status == dashboard_verify.STATUS_PASSED
    assert v.image is not None and "time limit" in v.image.reason


@LINUX_ONLY
def test_a_page_that_never_loaded_is_not_pictured(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path, load=False, screenshots=[_b64(_png())])

    v = _run(monkeypatch, fake, capture=True, timeout=1.5)

    assert v.status == dashboard_verify.STATUS_TIMED_OUT and v.image is None
    assert _shots(fake) == []


@LINUX_ONLY
def test_a_crashed_page_is_not_pictured(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path, events=[{"type": "crash"}], screenshots=[_b64(_png())])

    v = _run(monkeypatch, fake, capture=True)

    assert v.status == dashboard_verify.STATUS_FAILED and v.image is None


@LINUX_ONLY
def test_a_page_stopped_by_a_library_the_run_cannot_fetch_is_not_pictured(
    monkeypatch, tmp_path, fast
):
    """Its picture would show the page without the library, which is not the page."""
    page = (
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>\n'
        "<script>\n  new Chart();\n</script>"
    )
    fake = FakeBrowser(
        tmp_path,
        screenshots=[_b64(_png())],
        events=[
            {"type": "request", "id": "lib", "url": "https://cdn.jsdelivr.net/npm/chart.js@4"},
            {
                "type": "exception",
                "find": "new Chart",
                "description": "ReferenceError: Chart is not defined",
            },
        ],
    )

    v = _run(monkeypatch, fake, page, capture=True)

    assert v.status == dashboard_verify.STATUS_SKIPPED and v.image is None


@LINUX_ONLY
def test_a_page_that_guards_a_library_it_could_not_load_is_not_pictured(
    monkeypatch, tmp_path, fast
):
    """No error names the library when the page draws inside ``if (window.Chart)``, yet
    the picture would still lack what the tab draws."""
    url = "https://cdn.jsdelivr.net/npm/chart.js@4"
    page = f'<script src="{url}"></script>\n<script>\n  if (window.Chart) new Chart();\n</script>'
    fake = FakeBrowser(
        tmp_path,
        screenshots=[_b64(_png())],
        events=[{"type": "request", "id": "lib", "url": url}],
    )

    v = _run(monkeypatch, fake, page, capture=True, real_record=True)

    assert v.status == dashboard_verify.STATUS_PASSED
    assert v.image is not None and v.image.png == b""
    assert "cdn.jsdelivr.net/npm/chart.js@4" in v.image.reason


def test_the_pipe_takes_the_pictures_larger_reply_only_while_it_is_read():
    """The page's own messages keep the ordinary cap; the picture's reply alone may be
    larger, and the cap is put back whatever the capture did."""
    seen: list[int] = []

    class _Pipe:
        max_message_bytes = dashboard_verify._MAX_MESSAGE_BYTES

        def call(self, method, params, *, session, deadline):
            seen.append(self.max_message_bytes)
            raise dashboard_verify._BrowserGone("gone")

    pipe = _Pipe()
    run = dashboard_verify._Run(pipe, dashboard_verify._compose("<p>x</p>", _read()), 0.0)

    image = run._picture()

    assert seen == [dashboard_page_image.MAX_REPLY_BYTES]
    assert pipe.max_message_bytes == dashboard_verify._MAX_MESSAGE_BYTES
    assert image.reason == "the browser closed before the picture was taken"


def test_the_largest_viewport_png_fits_the_pictures_reply_cap():
    """Four bytes a pixel at the run's viewport, base64-encoded, with room for framing."""
    width, height = dashboard_verify._VIEWPORT["width"], dashboard_verify._VIEWPORT["height"]
    worst = (width * 4 + 1) * height
    assert (worst + 2) // 3 * 4 + 4096 < dashboard_page_image.MAX_REPLY_BYTES


@LINUX_ONLY
def test_a_run_over_the_record_hands_the_page_the_record_itself(monkeypatch, tmp_path, fast):
    """A snapshot of the page in force is drawn over the real read, and says so."""
    fake = FakeBrowser(tmp_path, screenshots=[_b64(_png())])
    read = dashboard_frame.read_payload({"open_prs": 4242}, agentic=[], seq=1)
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: str(fake.executable))

    v = dashboard_verify.verify_page("<p>x</p>", read, capture=True, real_record=True)

    assert v.status == dashboard_verify.STATUS_PASSED and v.data == "record"
    (served,) = fake.records("fulfill")
    assert "4242" in served["document"]


@LINUX_ONLY
def test_a_failure_over_the_record_does_not_call_its_values_invented(monkeypatch, tmp_path, fast):
    page = "<p>x</p>\n<script>\n  boom();\n</script>"
    fake = FakeBrowser(
        tmp_path,
        events=[{"type": "exception", "find": "boom()", "description": "ReferenceError: boom"}],
    )

    v = _run(monkeypatch, fake, page, real_record=True)

    assert v.status == dashboard_verify.STATUS_FAILED and v.data == "record"
    assert dashboard_verify._INVENTED_NOTE not in v.notes


@LINUX_ONLY
def test_the_save_check_still_never_hands_the_page_the_record(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path)
    read = dashboard_frame.read_payload({"open_prs": 4242}, agentic=[], seq=1)
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: str(fake.executable))

    v = dashboard_verify.verify_page("<p>x</p>", read)

    assert v.data == "synthetic"
    (served,) = fake.records("fulfill")
    assert "4242" not in served["document"]


#: The look the glue fixture hands the snapshot, two lines long.
_LOOK = "<style>look</style>\n<script>runtime</script>\n"


@pytest.fixture
def glue(monkeypatch):
    """The glue with the runner, the grant and its audit stubbed; what each was given."""
    from kiro_crew.dashboard.handlers import member_dashboard

    state = SimpleNamespace(asked=[], audited=[], answer=None)

    def _verify(html, read, **kw):
        state.asked.append({"html": html, **kw})
        return state.answer or dashboard_verify.Verification(dashboard_verify.STATUS_PASSED)

    granted = member_dashboard._EgressDecision(True, "policy", "no_restriction")
    monkeypatch.setattr(dashboard_verify, "preflight", lambda: "")
    monkeypatch.setattr(dashboard_verify, "verify_page", _verify)
    monkeypatch.setattr(dashboard_snapshot_look, "look_prefix", lambda: (_LOOK, ""))
    monkeypatch.setattr(member_dashboard, "_egress_decision", lambda *_a: granted)
    monkeypatch.setattr(
        member_dashboard, "_audit_page_script_grant", lambda **kw: state.audited.append(kw)
    )
    return state


_GLUE_PACKAGE = {
    "view": {"html": "<p>x</p>"},
    "model": {"types": {}},
    "theme": {"tokens": {"--accent": "#123456"}, "css": ".card { color: var(--accent); }"},
}


def test_only_the_snapshot_glue_asks_the_runner_for_the_record(glue):
    """The save's check keeps the synthetic copy; the snapshot asks for the record."""
    member_dashboard_fields.verify_package_page(SLUG, CREW, "slot-1", _GLUE_PACKAGE)
    member_dashboard_fields.snapshot_package_page(
        SLUG, CREW, "slot-1", _GLUE_PACKAGE, None, "dashboard-7", 4, None
    )

    assert "real_record" not in glue.asked[0] and "capture" not in glue.asked[0]
    assert glue.asked[1]["real_record"] is True and glue.asked[1]["capture"] is True


def test_the_snapshot_is_audited_as_the_applied_page_and_the_save_as_staged(glue):
    """The log names which applied page ran script over the record."""
    member_dashboard_fields.verify_package_page(SLUG, CREW, "slot-1", _GLUE_PACKAGE)
    member_dashboard_fields.snapshot_package_page(
        SLUG, CREW, "slot-1", _GLUE_PACKAGE, None, "dashboard-7", 4, None
    )

    staged, applied = glue.audited
    assert (staged["package_slug"], staged["version"]) == ("(staged)", 0)
    assert (applied["member"], applied["package_slug"], applied["version"]) == (
        CREW,
        "dashboard-7",
        4,
    )


def test_the_snapshot_draws_the_look_and_theme_first_and_the_save_check_does_not(glue):
    """The tab puts its look and the package theme in front of the page; so does this."""
    theme = member_dashboard_fields._theme_style(_GLUE_PACKAGE)
    assert "--accent" in theme

    member_dashboard_fields.verify_package_page(SLUG, CREW, "slot-1", _GLUE_PACKAGE)
    member_dashboard_fields.snapshot_package_page(
        SLUG, CREW, "slot-1", _GLUE_PACKAGE, None, "dashboard-7", 4, None
    )

    assert glue.asked[0]["html"] == "<p>x</p>"
    assert glue.asked[1]["html"] == _LOOK + theme + "<p>x</p>"


def test_a_snapshot_error_line_is_counted_from_view_html_not_the_theme(glue):
    lines = (_LOOK + member_dashboard_fields._theme_style(_GLUE_PACKAGE)).count("\n")
    glue.answer = dashboard_verify.Verification(
        dashboard_verify.STATUS_FAILED,
        (
            dashboard_verify.PageError("exception", "in the page", lines + 3, 5),
            dashboard_verify.PageError("csp", "in the theme", lines, 2),
            dashboard_verify.PageError("console", "nowhere", None, None),
        ),
    )

    wire, _image = member_dashboard_fields.snapshot_package_page(
        SLUG, CREW, "slot-1", _GLUE_PACKAGE, None, "dashboard-7", 4, None
    )

    assert [(e["line"], e["column"]) for e in wire["errors"]] == [
        (3, 5),
        (None, None),
        (None, None),
    ]


def test_the_snapshot_reads_the_live_sessions_unit_and_the_save_check_the_newest(glue, monkeypatch):
    """A slot can hold a live session and a later, unrelated chain; the tab reads the
    live one, so the picture must too."""
    from kiro_crew.dashboard.handlers import member_dashboard

    units: list[str] = []
    gateway = SimpleNamespace(sessions="live-sessions")

    class _Page:
        def __init__(self, *_a, session_unit: str, **_kw):
            units.append(session_unit)

        def arm(self):
            return {"fields": {}}

        def disarm(self):
            pass

    def _verify(html, read, **kw):
        read()
        return dashboard_verify.Verification(dashboard_verify.STATUS_PASSED)

    monkeypatch.setattr(member_dashboard_fields, "LivePage", _Page)
    monkeypatch.setattr(dashboard_verify, "verify_page", _verify)
    monkeypatch.setattr(member_dashboard, "_newest_unit", lambda _slot: "newest-unit")
    monkeypatch.setattr(
        member_dashboard,
        "_live_or_newest_unit",
        lambda state, slot: "live-unit" if state is gateway and slot == "slot-1" else "wrong",
    )

    member_dashboard_fields.verify_package_page(SLUG, CREW, "slot-1", _GLUE_PACKAGE)
    member_dashboard_fields.snapshot_package_page(
        SLUG, CREW, "slot-1", _GLUE_PACKAGE, None, "dashboard-7", 4, gateway
    )

    assert units == ["newest-unit", "live-unit"]


def test_the_snapshot_draws_in_the_stored_ui_language_and_the_save_check_in_english(
    glue, monkeypatch
):
    """No browser says the reader's language, so the snapshot takes the stored one."""
    seen: list[dict[str, Any]] = []

    class _Page:
        def __init__(self, *_a, **kw):
            seen.append(kw)

        def arm(self):
            return {"fields": {}}

        def disarm(self):
            pass

    def _verify(html, read, **kw):
        read()
        return dashboard_verify.Verification(dashboard_verify.STATUS_PASSED)

    monkeypatch.setattr(member_dashboard_fields, "LivePage", _Page)
    monkeypatch.setattr(dashboard_verify, "verify_page", _verify)
    monkeypatch.setattr(member_dashboard_fields, "_persisted_locale", lambda: "zh-CN")

    member_dashboard_fields.verify_package_page(SLUG, CREW, "slot-1", _GLUE_PACKAGE)
    member_dashboard_fields.snapshot_package_page(
        SLUG, CREW, "slot-1", _GLUE_PACKAGE, None, "dashboard-7", 4, None
    )

    assert seen[0]["locale"] == ""
    assert seen[1]["locale"] == "zh-CN"


@LINUX_ONLY
def test_a_page_with_its_own_error_and_a_missing_library_is_not_pictured(
    monkeypatch, tmp_path, fast
):
    """The page's own error keeps it failed, and the picture would still lack the library."""
    page = (
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>\n'
        "<script>\n  new Chart();\n</script>\n<script>\n  boom();\n</script>"
    )
    fake = FakeBrowser(
        tmp_path,
        screenshots=[_b64(_png())],
        events=[
            {"type": "request", "id": "lib", "url": "https://cdn.jsdelivr.net/npm/chart.js@4"},
            {
                "type": "exception",
                "find": "new Chart",
                "description": "ReferenceError: Chart is not defined",
            },
            {"type": "exception", "find": "boom()", "description": "ReferenceError: boom"},
        ],
    )

    v = _run(monkeypatch, fake, page, capture=True, real_record=True)

    assert v.status == dashboard_verify.STATUS_FAILED
    assert v.image is not None and v.image.png == b""
    assert "'Chart is not defined'" in v.image.reason


def test_a_snapshot_with_no_built_tailwind_runtime_draws_nothing_and_says_why(glue, monkeypatch):
    monkeypatch.setattr(
        dashboard_snapshot_look, "look_prefix", lambda: ("", dashboard_snapshot_look.NO_RUNTIME)
    )

    wire, image = member_dashboard_fields.snapshot_package_page(
        SLUG, CREW, "slot-1", _GLUE_PACKAGE, None, "dashboard-7", 4, None
    )

    assert wire["status"] == "skipped" and wire["reason"] == dashboard_snapshot_look.NO_RUNTIME
    assert image is None and glue.asked == [] and glue.audited == []


def test_the_look_inlines_the_built_runtime_and_the_light_tokens(monkeypatch, tmp_path):
    """The runtime is publish_sync's, escaped in any casing, as a published widget is."""
    from kiro_crew import publish_sync

    runtime = tmp_path / "tailwindcss-browser.js"
    runtime.write_text('var s="</script>",t="</SCRIPT>";tw()')
    monkeypatch.setattr(publish_sync, "_TAILWIND_RUNTIME_FILE", runtime)

    markup, reason = dashboard_snapshot_look.look_prefix()

    assert reason == ""
    assert '<script>var s="<\\/script>",t="<\\/SCRIPT>";tw()</script>' in markup
    assert "--card:#ffffff" in markup and "color-scheme:light" in markup
    assert "--surface:var(--card)" in markup
    # The frame's host stylesheet sets the body's padding and font, as on the tab; the
    # prefix comes after it, so it must not set them again.
    assert "padding" not in markup and "font-family" not in markup


def test_the_look_without_a_built_runtime_names_the_missing_file(monkeypatch, tmp_path):
    from kiro_crew import publish_sync

    monkeypatch.setattr(publish_sync, "_TAILWIND_RUNTIME_FILE", tmp_path / "absent.js")

    assert dashboard_snapshot_look.look_prefix() == ("", dashboard_snapshot_look.NO_RUNTIME)


_WEBSITE = Path(__file__).resolve().parents[1] / "website" / "src"


def _light_block() -> dict[str, str]:
    css = (_WEBSITE / "styles" / "design-tokens.css").read_text(encoding="utf-8")
    start = css.index('[data-theme="light"],[data-theme="amber-light"]{')
    body = css[css.index("{", start) + 1 : css.index("}", start)]
    pairs = (d.split(":", 1) for d in body.replace("\n", "").split(";") if ":" in d)
    return {name.strip(): value.strip() for name, value in pairs}


def test_the_light_tokens_are_the_dashboards_own_light_theme():
    """One source of truth: a palette change in design-tokens.css reddens this."""
    light = _light_block()
    assert {name: light[name] for name in dashboard_snapshot_look.LIGHT_TOKENS} == (
        dashboard_snapshot_look.LIGHT_TOKENS
    )


def test_the_look_names_what_the_tabs_frame_builder_names():
    """The token names, aliases and Tailwind block the tab's frame uses."""
    import re

    ts = (_WEBSITE / "lib" / "widgetSrcdoc.ts").read_text(encoding="utf-8")
    names = re.search(r"THEME_VAR_NAMES = \[(.*?)\] as const", ts, re.S).group(1)
    assert re.findall(r"'(--[\w-]+)'", names) == list(dashboard_snapshot_look.LIGHT_TOKENS)
    aliases = re.search(r"THEME_VAR_ALIASES[^{]*= \{(.*?)\}", ts, re.S).group(1)
    assert dict(re.findall(r"'(--[\w-]+)': '(--[\w-]+)'", aliases)) == (
        dashboard_snapshot_look.ALIASES
    )
    directives = re.search(r"const TAILWIND_V4_DIRECTIVES =\s*'([^']*)'", ts).group(1)
    assert directives == dashboard_snapshot_look.TAILWIND_DIRECTIVES


# --------------------------------------------------------------------------- #
# 3. The route
# --------------------------------------------------------------------------- #


def _package(bound_to: str = f"crewmate:{SLUG}") -> dict[str, Any]:
    return {
        "kind": "dashboard",
        "bound_to": bound_to,
        "model": {"types": {"open_prs": {"type": "number", "source": {"agentic": True}}}},
        "view": {"html": '<p data-dashboard-field="open_prs"></p>'},
        "theme": {"tokens": {}},
    }


@pytest.fixture
def home(monkeypatch, tmp_path):
    """The data home the pictures land in."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(agent_panel_snapshot, "_shot_root", lambda: root)
    return root


@pytest.fixture
def ran(monkeypatch):
    """The run, stubbed: records what it was given and answers with ``ran.answer``."""
    calls: list[SimpleNamespace] = []
    state = SimpleNamespace(
        calls=calls,
        answer=({"status": "passed", "data": "record", "errors": []}, PageImage(_png(), 4, 3)),
    )

    def _snapshot(slug, crew, slot, package, deadline, package_slug, version, gateway):
        calls.append(
            SimpleNamespace(
                slug=slug,
                crew=crew,
                slot=slot,
                package=package,
                package_slug=package_slug,
                version=version,
                state=gateway,
            )
        )
        return state.answer

    monkeypatch.setattr(member_dashboard_fields, "snapshot_package_page", _snapshot)
    return state


def _live(monkeypatch, *, pinned: dict[str, Any] | None, package_slug: str = "dashboard-0"):
    from kiro_crew.dashboard_templates import instance as instance_store

    # A template page in force: a record that released packages, so the older rule is off.
    record = SimpleNamespace(
        package_slug=package_slug if pinned is not None else "",
        package_released=pinned is None,
        structure_locked=False,
    )
    monkeypatch.setattr(instance_store, "read", lambda _slug: record)
    monkeypatch.setattr(
        instance_store,
        "pinned_package",
        lambda _r: (
            None
            if pinned is None
            else instance_store.PinnedPackage(package_slug, 5, json.dumps(pinned))
        ),
    )


@asynccontextmanager
async def _client(monkeypatch, *, resolved=(SLUG, CREW, "slot-1")):
    async def _resolve(_request, operation):
        assert operation == "dashboard_snapshot"
        if resolved is None:
            return None, web.json_response(
                {"error": "not a dashboard thread", "code": "no_dashboard_slot"}, status=400
            )
        return resolved, None

    monkeypatch.setattr(routes, "_resolve_dashboard_caller", _resolve)
    app = web.Application()
    app["state"] = SimpleNamespace(broadcast_ws=lambda *_a, **_k: None)
    routes.register_agent_panel_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def _snap(client: TestClient, query: str = "") -> tuple[int, dict[str, Any]]:
    resp = await client.get("/api/agent-panel/dashboard/snapshot" + query)
    return resp.status, await resp.json()


def _shots_of(home) -> list[str]:
    folder = home / agent_panel_snapshot.SHOT_ROOT / SLUG
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@PINNED_WALK
@pytest.mark.asyncio
async def test_the_page_in_force_is_drawn_and_lands_in_an_owner_only_file(monkeypatch, ran, home):
    pinned = _package()
    pinned["view"] = {"html": "<p>the page in force</p>"}
    _live(monkeypatch, pinned=pinned)

    async with _client(monkeypatch) as client:
        status, body = await _snap(client)

    assert status == 200, body
    snapshot = body["snapshot"]
    assert snapshot["status"] == "image" and snapshot["data"] == "record"
    assert (snapshot["width"], snapshot["height"], snapshot["package_version"]) == (4, 3, 5)
    assert snapshot["bytes"] == len(_png())
    # The PNG is never in the body: only its path is.
    assert "png_base64" not in snapshot and _b64(_png()) not in json.dumps(body)
    path = snapshot["path"]
    folder = home / agent_panel_snapshot.SHOT_ROOT / SLUG
    assert os.path.dirname(path) == str(folder)
    with open(path, "rb") as fh:
        assert fh.read() == _png()
    if platform_compat.IS_POSIX:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(folder.parent).st_mode) == 0o700
    (call,) = ran.calls
    assert (call.slug, call.crew, call.slot) == (SLUG, CREW, "slot-1")
    assert call.package["view"]["html"] == "<p>the page in force</p>"
    assert (call.package_slug, call.version) == ("dashboard-0", 5)
    assert call.state is client.app["state"]


@pytest.mark.asyncio
async def test_a_template_page_in_force_has_no_image(monkeypatch, ran, home):
    _live(monkeypatch, pinned=None)

    async with _client(monkeypatch) as client:
        status, body = await _snap(client)

    assert status == 200
    assert body["snapshot"] == {"status": "no_image", "reason": agent_panel_snapshot.NOT_COMPOSED}
    assert ran.calls == [] and _shots_of(home) == []


def _older_record(monkeypatch, *, cut_version: int, package: dict[str, Any] | None = None):
    """An unlocked record from before pins: no package key, nothing released."""
    from kiro_crew import artifacts, dashboard_agentic
    from kiro_crew.dashboard.handlers import member_dashboard
    from kiro_crew.dashboard_templates import instance as instance_store

    record = SimpleNamespace(package_slug="", package_released=False, structure_locked=False)
    monkeypatch.setattr(instance_store, "read", lambda _slug: record)
    monkeypatch.setattr(instance_store, "pinned_package", lambda _r: None)
    monkeypatch.setattr(dashboard_agentic, "bound_package", lambda s: f"bound-{s}")
    monkeypatch.setattr(dashboard_agentic, "has_person_cut_version", lambda _s: cut_version > 0)
    art = SimpleNamespace(
        kind="dashboard",
        source_path="",
        content=json.dumps(package or _package()),
        version=cut_version + 1,
        created_at="2026-10-23T00:00:00Z",
    )
    store = SimpleNamespace(get=lambda _slug, **_kw: art)
    monkeypatch.setattr(artifacts, "get_default_store", lambda: store)
    monkeypatch.setattr(member_dashboard, "applied_package_version", lambda _a: cut_version)


@PINNED_WALK
@pytest.mark.asyncio
async def test_an_older_unpinned_record_draws_the_bound_package_the_tab_shows(
    monkeypatch, ran, home
):
    """A record from before pins keeps the tab's older rule: the bound package's newest
    version a person cut."""
    _older_record(monkeypatch, cut_version=3)

    async with _client(monkeypatch) as client:
        _status, body = await _snap(client)

    assert body["snapshot"]["status"] == "image", body
    (call,) = ran.calls
    assert (call.package_slug, call.version) == (f"bound-{CREW}", 3)


@pytest.mark.asyncio
async def test_an_older_record_with_no_cut_version_has_no_image(monkeypatch, ran, home):
    """A bound draft nobody applied is not the page in force, on the tab or here."""
    _older_record(monkeypatch, cut_version=0)

    async with _client(monkeypatch) as client:
        _status, body = await _snap(client)

    assert body["snapshot"] == {"status": "no_image", "reason": agent_panel_snapshot.NOT_COMPOSED}
    assert ran.calls == []


@pytest.mark.asyncio
async def test_a_page_bound_to_another_crewmate_is_not_drawn(monkeypatch, ran, home):
    _live(monkeypatch, pinned=_package(bound_to="crewmate:other-crew"))

    async with _client(monkeypatch) as client:
        _status, body = await _snap(client)

    assert body["snapshot"]["status"] == "no_image"
    assert "not bound to you" in body["snapshot"]["reason"]
    assert ran.calls == [] and _shots_of(home) == []


@pytest.mark.asyncio
async def test_a_root_session_has_no_image_and_runs_nothing(monkeypatch, ran, home):
    async with _client(monkeypatch, resolved=("session:abc", "", "s")) as client:
        _status, body = await _snap(client)

    assert body["snapshot"]["status"] == "no_image" and ran.calls == []


@pytest.mark.asyncio
async def test_a_caller_the_gate_refuses_is_refused_whole(monkeypatch, ran, home):
    async with _client(monkeypatch, resolved=None) as client:
        status, body = await _snap(client)

    assert status == 400 and body["code"] == "no_dashboard_slot" and ran.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["?page=saved", "?slug=other-crew"])
async def test_an_argument_is_refused(monkeypatch, ran, home, query):
    """Only the page in force is drawn, so nothing can ask for a saved page or another one."""
    _live(monkeypatch, pinned=_package())

    async with _client(monkeypatch) as client:
        status, body = await _snap(client, query)

    assert status == 400 and body["code"] == "validation_error" and ran.calls == []


@pytest.mark.asyncio
async def test_a_busy_check_answers_no_image_in_words_for_a_snapshot(monkeypatch, ran, home):
    """A skipped run is never answered with a picture, and its reason says to ask again."""
    _live(monkeypatch, pinned=_package())
    ran.answer = ({"status": "skipped", "reason": dashboard_verify._BUSY, "errors": []}, None)

    async with _client(monkeypatch) as client:
        _status, body = await _snap(client)

    assert body["snapshot"] == {"status": "no_image", "reason": agent_panel_snapshot.BUSY}
    assert body["verification"]["status"] == "skipped" and _shots_of(home) == []


@pytest.mark.asyncio
async def test_a_short_of_memory_skip_is_no_image_with_the_runs_reason(monkeypatch, ran, home):
    _live(monkeypatch, pinned=_package())
    ran.answer = (
        {"status": "skipped", "reason": "the machine is short of memory", "errors": []},
        None,
    )

    async with _client(monkeypatch) as client:
        _status, body = await _snap(client)

    assert body["snapshot"]["reason"] == "the machine is short of memory"


@pytest.mark.asyncio
async def test_a_run_whose_picture_failed_says_why_and_writes_nothing(monkeypatch, ran, home):
    _live(monkeypatch, pinned=_package())
    ran.answer = (
        {"status": "passed", "data": "record", "errors": []},
        PageImage(reason="the browser could not take the picture"),
    )

    async with _client(monkeypatch) as client:
        _status, body = await _snap(client)

    assert body["snapshot"]["reason"] == "the browser could not take the picture"
    assert "path" not in body["snapshot"] and _shots_of(home) == []


@PINNED_WALK
@pytest.mark.asyncio
async def test_each_crewmate_keeps_only_its_newest_pictures(monkeypatch, ran, home):
    _live(monkeypatch, pinned=_package())
    stamps = iter(range(1_000, 1_000 + agent_panel_snapshot.SHOT_KEEP + 3))
    monkeypatch.setattr(agent_panel_snapshot, "_now_ms", lambda: next(stamps))
    other = home / agent_panel_snapshot.SHOT_ROOT / "other-crew"
    other.mkdir(parents=True)
    (other / "page-1-aaaa.png").write_bytes(_png())

    async with _client(monkeypatch) as client:
        for _ in range(agent_panel_snapshot.SHOT_KEEP + 3):
            _status, body = await _snap(client)

    names = _shots_of(home)
    assert len(names) == agent_panel_snapshot.SHOT_KEEP
    assert os.path.basename(body["snapshot"]["path"]) == names[-1]
    # Another crewmate's directory is not this crewmate's to trim.
    assert [p.name for p in other.iterdir()] == ["page-1-aaaa.png"]


@PINNED_WALK
@pytest.mark.asyncio
async def test_a_picture_named_older_than_the_rest_is_still_the_one_returned(
    monkeypatch, ran, home
):
    """A clock that stepped back names the new picture oldest; the trim keeps it."""
    _live(monkeypatch, pinned=_package())
    folder = home / agent_panel_snapshot.SHOT_ROOT / SLUG
    folder.mkdir(parents=True)
    for stamp in range(2_000, 2_000 + agent_panel_snapshot.SHOT_KEEP):
        (folder / f"page-{stamp}-aaaa.png").write_bytes(_png())
    monkeypatch.setattr(agent_panel_snapshot, "_now_ms", lambda: 1_000)

    async with _client(monkeypatch) as client:
        _status, body = await _snap(client)

    path = body["snapshot"]["path"]
    assert os.path.exists(path) and os.path.basename(path).startswith("page-1000-")
    assert len(_shots_of(home)) == agent_panel_snapshot.SHOT_KEEP
    assert "page-2000-aaaa.png" not in _shots_of(home)


@LINUX_ONLY
@pytest.mark.asyncio
@pytest.mark.parametrize("planted", ["root", "crewmate"])
async def test_a_link_planted_in_the_picture_path_is_not_followed(
    monkeypatch, ran, home, tmp_path, planted
):
    """An agent can write in the data home; a link there must not steer the write."""
    _live(monkeypatch, pinned=_package())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if planted == "root":
        os.symlink(elsewhere, home / agent_panel_snapshot.SHOT_ROOT)
    else:
        (home / agent_panel_snapshot.SHOT_ROOT).mkdir()
        os.symlink(elsewhere, home / agent_panel_snapshot.SHOT_ROOT / SLUG)

    async with _client(monkeypatch) as client:
        _status, body = await _snap(client)

    assert body["snapshot"]["status"] == "no_image"
    assert body["snapshot"]["reason"] == "the picture could not be written to a file"
    assert list(elsewhere.iterdir()) == []


@pytest.mark.asyncio
async def test_a_host_with_no_pinned_walk_answers_no_image_and_writes_nothing(
    monkeypatch, ran, home
):
    """Windows cannot pin the directory chain, so the picture is never written by name."""
    _live(monkeypatch, pinned=_package())
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)

    async with _client(monkeypatch) as client:
        _status, body = await _snap(client)

    assert body["snapshot"]["status"] == "no_image"
    assert body["snapshot"]["reason"] == "the picture could not be written to a file"
    assert _shots_of(home) == []


def test_the_picture_directory_is_readable_by_the_crewmate_and_served_by_nothing(tmp_path):
    """The crewmate's own file-read tool reaches it; the file explorer, the one route that
    lists the data home, refuses it."""
    from kiro_crew.apps.builtins.file_explorer import server as explorer
    from kiro_crew.security import is_sensitive_path

    crew_home = Path.home() / ".kiro" / "crew"
    picture = crew_home / agent_panel_snapshot.SHOT_ROOT / SLUG / "page-1-abcd.png"

    assert not is_sensitive_path(str(picture))
    assert explorer._is_sensitive(picture)
    assert agent_panel_snapshot.SHOT_ROOT not in explorer._KIROCREW_SAFE_SUBDIRS


# --------------------------------------------------------------------------- #
# 4. The MCP tool
# --------------------------------------------------------------------------- #


def _gateway(monkeypatch, body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    asked: list[tuple[str, dict[str, Any]]] = []

    def _get(path, **kw):
        asked.append((path, kw))
        return body

    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk-1", ""))
    monkeypatch.setattr(mcp_panel, "_get", _get)
    return asked


def test_the_tool_names_the_file_the_gateway_wrote(monkeypatch, tmp_path):
    path = str(tmp_path / "dashboard-snapshots" / "fleet-crew" / "page-1-abcd.png")
    asked = _gateway(
        monkeypatch,
        {
            "snapshot": {
                "status": "image",
                "path": path,
                "package_version": 5,
                "width": 1280,
                "height": 800,
                "bytes": 300 * 1024,
                "data": "record",
            },
            "verification": {"status": "passed", "data": "record", "errors": []},
        },
    )

    out = mcp_panel._call_tool_inner("dashboard_snapshot", {})

    assert out.startswith(f"Snapshot of your page in force (version 5): {path}\n")
    assert "1280x800 png, 300 KiB" in out and "real data" in out
    assert "default light look" in out and "theme pack" in out
    assert "verification: passed -- the page ran once over your crewmate's data" in out
    assert "invented" not in out and "not real" not in out
    ((route, kw),) = asked
    assert route == "/api/agent-panel/dashboard/snapshot" and kw["session_key"] == "sk-1"


def test_no_image_names_no_file_and_says_an_old_one_is_not_current(monkeypatch):
    _gateway(
        monkeypatch,
        {
            "snapshot": {"status": "no_image", "reason": "busy: try later"},
            "verification": {"status": "skipped", "reason": "busy: try later"},
        },
    )

    out = mcp_panel._call_tool_inner("dashboard_snapshot", {})

    assert out.startswith("No image of your page: busy: try later.")
    assert "not this page as it is now" in out and ".png" not in out


def test_an_image_answer_without_a_path_is_no_image(monkeypatch):
    _gateway(monkeypatch, {"snapshot": {"status": "image"}, "verification": None})

    assert mcp_panel._call_tool_inner("dashboard_snapshot", {}).startswith("No image")


def test_the_tool_refuses_a_caller_whose_identity_is_not_strict(monkeypatch):
    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("", "Error: no identity"))

    def _boom(*_a: Any, **_k: Any):  # pragma: no cover - must not run
        raise AssertionError("a caller with no strict identity reached the gateway")

    monkeypatch.setattr(mcp_panel, "_get", _boom)

    assert mcp_panel._call_tool_inner("dashboard_snapshot", {}) == "Error: no identity"


def test_the_tool_takes_no_arguments():
    from kiro_crew.validation import MCP_PANEL_SCHEMAS, ValidationError, validate_tool_args

    schema = MCP_PANEL_SCHEMAS["dashboard_snapshot"]
    assert validate_tool_args({}, schema) == {}
    for bad in ({"page": "saved"}, {"slug": "other-crew"}):
        with pytest.raises(ValidationError):
            validate_tool_args(bad, schema)
    tool = next(t for t in mcp_panel._list_tools() if t["name"] == "dashboard_snapshot")
    assert tool["inputSchema"]["properties"] == {}
