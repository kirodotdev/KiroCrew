"""The shared UTF-8 subprocess decode mapping must actually pin UTF-8.

A text-mode subprocess call without ``encoding=`` decodes with the locale code
page -- mojibake on Windows. ``UTF8_TEXT`` is the
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
        # encoding pins UTF-8; errors=replace tolerates an undecodable byte.
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


class TestUtf8Stdout:
    """``utf8_stdout`` is the bytes-mode counterpart of ``UTF8_TEXT``: same
    UTF-8-with-replacement policy, WITHOUT the universal-newline translation
    text mode hard-enables. A ``\\r`` in a child's output is content there
    (git prints paths byte-for-byte), so the decode must preserve it."""

    def test_carriage_return_survives_the_decode(self):
        from kiro_crew.subprocess_utf8 import utf8_stdout

        assert utf8_stdout(b"wt\rcr/.git\n") == "wt\rcr/.git\n"

    def test_crlf_is_not_collapsed(self):
        from kiro_crew.subprocess_utf8 import utf8_stdout

        assert utf8_stdout(b"path\r\n") == "path\r\n"

    def test_str_passes_through_for_test_stand_ins(self):
        from kiro_crew.subprocess_utf8 import utf8_stdout

        assert utf8_stdout("already decoded\r\n") == "already decoded\r\n"

    def test_none_is_the_empty_answer(self):
        from kiro_crew.subprocess_utf8 import utf8_stdout

        assert utf8_stdout(None) == ""

    def test_malformed_bytes_degrade_to_replacement(self):
        from kiro_crew.subprocess_utf8 import utf8_stdout

        assert utf8_stdout(b"a\xffb") == "a\ufffdb"

    def test_real_child_output_keeps_its_cr(self):
        """End-to-end: a bytes-mode capture decoded by utf8_stdout hands the
        caller the child's ``\\r`` intact, where ``UTF8_TEXT`` (text mode)
        would have rewritten it to ``\\n``. The child writes through
        ``sys.stdout.buffer`` so its OWN text-mode stdout cannot translate
        the ``\\n`` to ``\\r\\n`` on Windows before the capture ever sees it --
        this test pins the parent-side decode, not the child's platform."""
        from kiro_crew.subprocess_utf8 import utf8_stdout

        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'a\\rb\\n')",
            ],
            capture_output=True,
            env=_CHILD_UTF8_ENV,
        )
        assert done.returncode == 0
        assert utf8_stdout(done.stdout) == "a\rb\n"


class TestUtf8PathStdout:
    """``utf8_path_stdout`` decodes a child's PATH answer so it round-trips.

    ``"replace"`` rewrites a non-UTF-8 byte to U+FFFD, which ``os.fsencode``
    cannot restore -- an ``os.lstat`` on the decoded string then inspects a
    different path than the child printed. ``surrogateescape`` (PEP 383) is
    the round-trip decode the ``os`` layer itself uses.
    """

    def test_non_utf8_byte_becomes_a_lone_surrogate(self):
        from kiro_crew.subprocess_utf8 import utf8_path_stdout

        assert utf8_path_stdout(b"/tmp/repo-\xff/.git\n") == "/tmp/repo-\udcff/.git\n"

    @pytest.mark.skipif(os.name == "nt", reason="fsencode uses surrogatepass on Windows")
    def test_round_trips_through_fsencode_on_posix(self):
        """The exact property the classifier's ``lstat`` depends on: fsencode
        of the decoded path is byte-identical to what git printed."""
        from kiro_crew.subprocess_utf8 import utf8_path_stdout

        raw = b"/tmp/repo-\xff/.git"
        assert os.fsencode(utf8_path_stdout(raw)) == raw

    def test_carriage_return_survives_the_decode(self):
        from kiro_crew.subprocess_utf8 import utf8_path_stdout

        assert utf8_path_stdout(b"wt\rcr/.git\n") == "wt\rcr/.git\n"

    def test_str_passes_through_for_test_stand_ins(self):
        from kiro_crew.subprocess_utf8 import utf8_path_stdout

        assert utf8_path_stdout("already decoded\n") == "already decoded\n"

    def test_none_is_the_empty_answer(self):
        from kiro_crew.subprocess_utf8 import utf8_path_stdout

        assert utf8_path_stdout(None) == ""
