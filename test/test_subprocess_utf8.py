"""The shared UTF-8 subprocess decode mapping must actually pin UTF-8.

Follow-up to #3219/#3669: a text-mode subprocess call without ``encoding=``
decodes with the locale code page -- mojibake on Windows. ``UTF8_TEXT`` is the
one shared definition of "this child's output is UTF-8"; these tests pin that
the definition is complete (text mode on, UTF-8, replacement errors), that it
survives a real subprocess round-trip, and that it cannot be mutated by a
caller.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from kiro_crew.subprocess_utf8 import UTF8_TEXT

# A child printing this exercises multi-byte UTF-8; under cp1252 these bytes
# decode to mojibake, so a passing equality check proves UTF-8 decoding.
SNOWMAN_LINE = "\u2603 caf\u00e9 \u3053\u3093"

# The child re-encodes its stdout with its own locale unless told otherwise;
# pinning the CHILD to UTF-8 keeps the test about OUR decode side. The rest of
# os.environ is inherited: a bare single-key env drops SystemRoot, which is a
# documented way to break a CPython child on Windows.
_CHILD_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


class TestUtf8TextMapping:
    def test_carries_the_complete_decode_pin(self):
        # text=True stays present so kwargs spies that check it keep seeing it;
        # encoding is the actual fix; errors=replace matches the #3669 shape.
        assert dict(UTF8_TEXT) == {
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }

    def test_is_read_only(self):
        # One module mutating the shared mapping would silently change the
        # decode policy of every call site in the process.
        with pytest.raises(TypeError):
            UTF8_TEXT["encoding"] = "ascii"  # type: ignore[index]

    def test_splats_into_subprocess_run(self):
        result = subprocess.run(
            [sys.executable, "-c", f"print({SNOWMAN_LINE!r})"],
            capture_output=True,
            env=_CHILD_UTF8_ENV,
            **UTF8_TEXT,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == SNOWMAN_LINE

    def test_replaces_malformed_bytes_instead_of_raising(self):
        # A child emitting bytes that are NOT valid UTF-8 must degrade to
        # U+FFFD in place, not throw the whole output away.
        argv = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe end\\n')",
        ]
        result = subprocess.run(argv, capture_output=True, **UTF8_TEXT)
        assert result.stdout == "ok \ufffd\ufffd end\n"
