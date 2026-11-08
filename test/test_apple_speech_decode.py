"""Decode contract of the Apple speech helper build's toolchain spawns.

`xcrun` and `swiftc` write UTF-8, and the compile diagnostics carry the cache
path under HOME. Decoding them with the locale's preferred encoding turns a
non-ASCII HOME into mojibake under a non-UTF-8 locale, so each spawn must pin
the shared `UTF8_TEXT` decode. Every test stubs `subprocess.run` and drives the
function down its failure branch, so no toolchain is needed and they run on
every platform.
"""

from __future__ import annotations

import subprocess

from kiro_crew import apple_speech
from kiro_crew.subprocess_utf8 import UTF8_TEXT


def _decode_spy(calls: list[dict]):
    """A `subprocess.run` stand-in that records the kwargs and reports failure."""

    def run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 1, "", "error: /tmp/caf\u00e9/x.swift")

    return run


def _assert_utf8_decode(calls: list[dict]) -> None:
    assert len(calls) == 1, calls
    assert calls[0].get("encoding") == UTF8_TEXT["encoding"]
    assert calls[0].get("errors") == UTF8_TEXT["errors"]


def test_sdk_lookup_decodes_xcrun_as_utf8(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(apple_speech.subprocess, "run", _decode_spy(calls))
    apple_speech._sdk_path()
    _assert_utf8_decode(calls)


def test_swiftc_lookup_decodes_xcrun_as_utf8(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(apple_speech, "_swiftc_fast", lambda: None)
    monkeypatch.setattr(apple_speech.subprocess, "run", _decode_spy(calls))
    assert apple_speech._swiftc() is None
    _assert_utf8_decode(calls)


def test_compile_decodes_swiftc_diagnostics_as_utf8(monkeypatch, tmp_path):
    calls: list[dict] = []
    monkeypatch.setattr(apple_speech, "_cache_dir", lambda: tmp_path / "apple-speech")
    monkeypatch.setattr(apple_speech, "_swiftc", lambda: "/Library/Developer/swiftc")
    monkeypatch.setattr(apple_speech, "_sdk_path", lambda: "/Library/Developer/sdk")
    monkeypatch.setattr(apple_speech.subprocess, "run", _decode_spy(calls))
    got = apple_speech._build_helper(apple_speech._HELPER_NAME, "AppleTranscribe.swift", build=True)
    assert got is None
    _assert_utf8_decode(calls)
