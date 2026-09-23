"""The memory-bounded PDF extractor: ``kiro_crew.pdf_extract`` and its child.

What is pinned here is the BOUND, not pdfplumber's output: that a page which
inflates past the ceiling fails inside the child and comes back as a reported
``memory`` failure; that the child's own caps cut text and pages and say so; and
that the parent trusts nothing the child writes without checking it against the
caps it asked for.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time

import pytest
from pdf_test_helpers import flate_bomb_pdf, text_pdf

from kiro_crew import pdf_extract, pdf_extract_child, sandbox

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX rlimits")

_FAR = 30.0


def _soon() -> float:
    return time.monotonic() + _FAR


@pytest.fixture(scope="module")
def bomb() -> bytes:
    return flate_bomb_pdf()


class TestBound:
    """The reason the module exists: one page past the ceiling is not fatal."""

    def test_a_plain_pdf_comes_back_as_page_segments(self):
        outcome = pdf_extract.extract_pdf_segments(
            text_pdf("Hello bounded PDF"), max_chars=4000, deadline=_soon()
        )
        assert outcome == pdf_extract.PdfExtraction(
            (("page 1", "Hello bounded PDF"),), False, None, 1
        )
        assert not outcome.resource_failure

    def test_a_flate_bomb_fails_in_the_child_as_a_memory_failure(self, bomb):
        """The allocation happens in the child and is refused THERE.

        A refused ``zlib.decompress`` buffer is a ``MemoryError`` inside the child,
        which reports it and exits; nothing was allocated in this process. The
        ``memory`` kind (not ``timeout``) is what separates the ceiling firing
        from the deadline giving up on a child that was still inflating.
        """
        started = time.monotonic()
        outcome = pdf_extract.extract_pdf_segments(bomb, max_chars=400_001, deadline=_soon())
        assert outcome == pdf_extract.PdfExtraction((), True, "memory", 0)
        assert outcome.resource_failure
        # Well inside the deadline: the refusal is the first allocation, not the
        # end of a parse.
        assert time.monotonic() - started < _FAR / 2

    def test_the_child_itself_exits_with_the_memory_report_under_the_profile(self, bomb):
        """Same fact one layer down, with no parent-side interpretation.

        Spawns the child exactly as the parent does and reads its raw protocol: an
        ``error: memory`` line and :data:`EXIT_FAILED`, not a kill and not a
        traceback -- the child survived its own refused allocation long enough to
        say what happened.
        """
        proc = sandbox.popen_limited(
            pdf_extract._child_argv(400_001, 10),
            profile=sandbox.RLIMIT_PROFILE_EXTRACTOR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _err = proc.communicate(bomb, timeout=_FAR)
        assert proc.returncode == pdf_extract_child.EXIT_FAILED
        lines = [json.loads(line) for line in out.decode().splitlines() if line]
        assert lines == [{"error": "memory", "detail": "PdfminerException"}]

    def test_the_extractor_profile_is_a_fixed_address_space_ceiling(self):
        spec = sandbox._rlimit_spec(sandbox.RLIMIT_PROFILE_EXTRACTOR)
        assert f"RLIMIT_AS:{1024 * 1024 * 1024}" in spec
        assert "RLIMIT_CPU:60" in spec
        prefix = sandbox.spawn_shim_argv(sandbox.RLIMIT_PROFILE_EXTRACTOR)
        assert any(a == f"--rlimits={spec}" for a in prefix)
        assert "--oom-bias" in prefix

    def test_a_file_handle_is_the_child_stdin(self, tmp_path):
        path = tmp_path / "doc.pdf"
        path.write_bytes(text_pdf("From a handle"))
        with path.open("rb") as fh:
            outcome = pdf_extract.extract_pdf_segments(fh, max_chars=4000, deadline=_soon())
        assert outcome.segments == (("page 1", "From a handle"),)


class TestCallerBudgets:
    def test_a_passed_deadline_spawns_nothing(self, monkeypatch):
        def refuse(*_a, **_k):
            raise AssertionError("spawned")

        monkeypatch.setattr(pdf_extract, "popen_limited", refuse)
        outcome = pdf_extract.extract_pdf_segments(
            text_pdf("x"), max_chars=10, deadline=time.monotonic() - 1
        )
        assert outcome == pdf_extract.PdfExtraction((), True, "timeout", 0)

    def test_a_child_past_the_deadline_is_killed_and_reported(self, monkeypatch):
        monkeypatch.setattr(
            pdf_extract,
            "_child_argv",
            lambda *_a: [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        started = time.monotonic()
        outcome = pdf_extract.extract_pdf_segments(
            text_pdf("x"), max_chars=10, deadline=time.monotonic() + 0.5
        )
        assert outcome == pdf_extract.PdfExtraction((), True, "timeout", 0)
        assert time.monotonic() - started < 5

    def test_non_pdf_bytes_are_refused_before_a_spawn(self, monkeypatch):
        def refuse(*_a, **_k):
            raise AssertionError("spawned")

        monkeypatch.setattr(pdf_extract, "popen_limited", refuse)
        outcome = pdf_extract.extract_pdf_segments(b"<html>", max_chars=10, deadline=_soon())
        assert outcome == pdf_extract.PdfExtraction((), True, "parse", 0)
        assert not outcome.resource_failure

    def test_a_missing_parser_is_reported_not_spawned(self, monkeypatch):
        monkeypatch.setattr(pdf_extract, "pdfplumber_available", lambda: False)
        monkeypatch.setattr(pdf_extract, "popen_limited", lambda *a, **k: pytest.fail("spawned"))
        outcome = pdf_extract.extract_pdf_segments(text_pdf("x"), max_chars=10, deadline=_soon())
        assert outcome.failure == "unavailable"
        assert not outcome.resource_failure

    def test_non_positive_caps_are_a_programming_error(self):
        with pytest.raises(ValueError):
            pdf_extract.extract_pdf_segments(text_pdf("x"), max_chars=0, deadline=_soon())
        with pytest.raises(ValueError):
            pdf_extract.extract_pdf_segments(
                text_pdf("x"), max_chars=1, max_pages=0, deadline=_soon()
            )


class TestRssWatchdog:
    """The child's own peak-RSS ceiling: the bound where the kernel has none (macOS)."""

    def test_watchdog_reports_memory_and_ends_the_process(self, capsys):
        samples = iter([100, 200, 2_000_000_001])
        ended: list[int] = []
        pdf_extract_child.watch_rss(
            2_000_000_000, sample=lambda: next(samples), terminate=ended.append, interval=0
        )
        assert ended == [pdf_extract_child.EXIT_FAILED]
        assert json.loads(capsys.readouterr().out) == {"error": "memory", "detail": "rss"}

    def test_an_unreadable_sample_is_not_a_breach(self, capsys):
        samples = iter([None, None, 5])
        ended: list[int] = []
        pdf_extract_child.watch_rss(
            1, sample=lambda: next(samples), terminate=ended.append, interval=0
        )
        assert ended == [pdf_extract_child.EXIT_FAILED]  # the third sample, not the Nones

    def test_peak_rss_is_bytes_on_every_platform(self):
        peak = pdf_extract_child.peak_rss_bytes()
        assert peak is not None
        # This test process is at least a few MB resident; KiB misread as bytes would not be.
        assert peak > 4 * 1024 * 1024

    def test_the_child_polices_its_own_rss_where_the_kernel_has_no_ceiling(self, bomb):
        """No rlimit at all (``none`` profile) and a 400 MB RSS ceiling: the inflate
        of the bomb crosses it and the child ends itself with the ``memory`` report
        -- the same outcome the kernel ceiling produces, from a different guard."""
        argv = [
            *pdf_extract._child_argv(400_001, 10)[:-1],
            f"--max-rss={400 * 1024 * 1024}",
        ]
        proc = sandbox.popen_limited(
            argv,
            profile=sandbox.RLIMIT_PROFILE_NONE,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _err = proc.communicate(bomb, timeout=_FAR)
        assert proc.returncode == pdf_extract_child.EXIT_FAILED
        lines = [json.loads(line) for line in out.decode().splitlines() if line]
        assert lines == [{"error": "memory", "detail": "rss"}]


class TestWindowsCeiling:
    """Off POSIX the ceiling is a Job object on a suspended child, and it FAILS CLOSED.

    Driven on any platform by flipping ``IS_WINDOWS`` and faking the two Win32
    calls: ``CREATE_SUSPENDED`` is 0 here, so the real child simply runs.
    """

    @pytest.fixture
    def windows(self, monkeypatch):
        monkeypatch.setattr(pdf_extract.platform_compat, "IS_WINDOWS", True)
        calls: dict[str, list] = {"apply": [], "resume": []}
        monkeypatch.setattr(
            pdf_extract.platform_compat,
            "apply_job_limits",
            lambda pid, **kw: calls["apply"].append((pid, kw)) or calls.get("apply_ok", True),
        )
        monkeypatch.setattr(
            pdf_extract.platform_compat,
            "resume_process_main_thread",
            lambda pid: calls["resume"].append(pid) or calls.get("resume_ok", True),
        )
        return calls

    def test_the_job_carries_the_profile_memory_number_and_the_child_runs(self, windows):
        outcome = pdf_extract.extract_pdf_segments(
            text_pdf("under a job"), max_chars=100, deadline=_soon()
        )
        assert outcome.segments == (("page 1", "under a job"),)
        [(pid, kw)] = windows["apply"]
        assert kw == {"max_procs": 1, "max_memory_bytes": sandbox._EXTRACTOR_MAX_AS_BYTES}
        assert windows["resume"] == [pid]

    def test_no_ceiling_means_no_parse(self, windows):
        windows["apply_ok"] = False
        outcome = pdf_extract.extract_pdf_segments(
            text_pdf("never read"), max_chars=100, deadline=_soon()
        )
        assert outcome == pdf_extract.PdfExtraction((), True, "unbounded", 0)
        assert outcome.resource_failure
        assert windows["resume"] == []  # killed, never resumed

    def test_an_unresumable_child_is_killed_and_reported(self, windows):
        windows["resume_ok"] = False
        outcome = pdf_extract.extract_pdf_segments(
            text_pdf("frozen"), max_chars=100, deadline=_soon()
        )
        assert outcome == pdf_extract.PdfExtraction((), True, "spawn", 0)


_ARGV_100_5 = ["--max-chars=100", "--max-pages=5", "--max-rss=1000000000"]
_ARGV_1_1 = ["--max-chars=1", "--max-pages=1", "--max-rss=1000000000"]


class TestChildCaps:
    """The child cuts at its caps and says so, with a fake parser (no child spawn)."""

    @staticmethod
    def _run(pages: list[str | None], *, max_chars: int, max_pages: int, capsys) -> list[dict]:
        events: list[tuple[str, int]] = []

        class FakePage:
            def __init__(self, number: int, text: str | None):
                self.number, self.text = number, text

            def extract_text(self):
                events.append(("extract", self.number))
                return self.text

            def close(self):
                events.append(("close", self.number))

        class FakePdf:
            def __init__(self):
                self.pages = [FakePage(i, t) for i, t in enumerate(pages, 1)]

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        class FakePdfplumber:
            @staticmethod
            def open(_fh):
                return FakePdf()

        pdf_extract_child.extract(
            b"%PDF-", max_chars=max_chars, max_pages=max_pages, pdfplumber_module=FakePdfplumber
        )
        out = capsys.readouterr().out
        records = [json.loads(line) for line in out.splitlines() if line]
        # Every page opened was released, in order, before the next was opened.
        opened = [n for kind, n in events if kind == "extract"]
        assert events == [e for n in opened for e in (("extract", n), ("close", n))]
        return records

    def test_whole_document_under_both_caps(self, capsys):
        records = self._run(["first", None, "third"], max_chars=100, max_pages=10, capsys=capsys)
        assert records == [
            {"label": "page 1", "text": "first"},
            {"label": "page 3", "text": "third"},
            {"end": True, "truncated": False, "pages": 3},
        ]

    def test_the_character_cap_cuts_the_page_and_stops(self, capsys):
        records = self._run(["abcdef", "ghij", "klm"], max_chars=8, max_pages=10, capsys=capsys)
        assert records == [
            {"label": "page 1", "text": "abcdef"},
            {"label": "page 2", "text": "gh"},
            {"end": True, "truncated": True, "pages": 2},
        ]

    def test_a_document_ending_exactly_at_the_cap_is_whole(self, capsys):
        records = self._run(["abcd", "efgh"], max_chars=8, max_pages=10, capsys=capsys)
        assert records[-1] == {"end": True, "truncated": False, "pages": 2}

    def test_a_cap_reached_with_pages_left_is_truncated(self, capsys):
        records = self._run(["abcd", "efgh", "ijkl"], max_chars=8, max_pages=10, capsys=capsys)
        assert records[-1] == {"end": True, "truncated": True, "pages": 2}

    def test_the_page_cap_stops_before_opening_the_next_page(self, capsys):
        records = self._run(["a", "b", "c"], max_chars=100, max_pages=2, capsys=capsys)
        assert records == [
            {"label": "page 1", "text": "a"},
            {"label": "page 2", "text": "b"},
            {"end": True, "truncated": True, "pages": 2},
        ]

    def test_a_legacy_page_without_close_is_flushed(self, capsys):
        flushed = []

        class LegacyPage:
            def extract_text(self):
                return "legacy"

            def flush_cache(self):
                flushed.append(True)

        class FakePdf:
            pages = [LegacyPage()]

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        class FakePdfplumber:
            @staticmethod
            def open(_fh):
                return FakePdf()

        pdf_extract_child.extract(
            b"%PDF-", max_chars=10, max_pages=1, pdfplumber_module=FakePdfplumber
        )
        assert flushed == [True]
        assert json.loads(capsys.readouterr().out.splitlines()[0]) == {
            "label": "page 1",
            "text": "legacy",
        }

    def test_a_wrapped_memory_error_is_classified_as_memory(self):
        try:
            try:
                raise MemoryError("Unable to allocate output buffer.")
            except MemoryError as inner:
                raise RuntimeError(inner)  # implicit __context__, like pdfplumber
        except RuntimeError as wrapped:
            assert pdf_extract_child._is_memory_failure(wrapped)
        assert not pdf_extract_child._is_memory_failure(ValueError("bad xref"))

    @staticmethod
    def _main(
        monkeypatch, capsys, argv: list[str], data: bytes, pdfplumber_module: object
    ) -> tuple[int, list[dict]]:
        """Run the child's ``main`` in-process: stdin and the parser both faked.

        ``main`` arms ``start_rss_watchdog`` first thing, and that watchdog
        ends the PROCESS with ``os._exit`` when the process's peak RSS is over
        ``--max-rss``. In-process here, "the process" is the pytest-xdist
        worker, which has run thousands of tests by the time it reaches this
        file and can well sit above the 1 GB the argv sets -- so the first
        watchdog tick killed the worker (``worker 'gwN' crashed``). The
        watchdog has its own tests in ``TestBound``; these tests are about the
        caps, so the harness stubs it out and records that ``main`` asked for
        it with the argv's limit.
        """
        armed: list[int] = []
        monkeypatch.setattr(
            pdf_extract_child, "start_rss_watchdog", lambda limit: armed.append(limit) or True
        )
        monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(data)})())
        monkeypatch.setitem(sys.modules, "pdfplumber", pdfplumber_module)
        rc = pdf_extract_child.main(argv)
        assert armed, "main() must arm the RSS watchdog before reading anything"
        out = capsys.readouterr().out
        return rc, [json.loads(line) for line in out.splitlines() if line]

    def test_main_streams_pages_and_exits_zero(self, monkeypatch, capsys):
        class FakePage:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

            def close(self):
                pass

        class FakePdf:
            def __init__(self, fh):
                self.pages = [FakePage(fh.read().decode())]

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        fake = type("FakePdfplumber", (), {"open": staticmethod(FakePdf)})
        rc, records = self._main(monkeypatch, capsys, _ARGV_100_5, b"stdin bytes", fake)
        assert rc == 0
        assert records == [
            {"label": "page 1", "text": "stdin bytes"},
            {"end": True, "truncated": False, "pages": 1},
        ]

    def test_main_reports_a_wrapped_memory_error_and_exits_failed(self, monkeypatch, capsys):
        def boom(_fh):
            try:
                raise MemoryError("Unable to allocate output buffer.")
            except MemoryError as inner:
                raise RuntimeError(inner)

        fake = type("FakePdfplumber", (), {"open": staticmethod(boom)})
        rc, records = self._main(monkeypatch, capsys, _ARGV_1_1, b"x", fake)
        assert rc == pdf_extract_child.EXIT_FAILED
        assert records == [{"error": "memory", "detail": "RuntimeError"}]

    def test_main_reports_a_parser_error_as_parse(self, monkeypatch, capsys):
        def refuse(_fh):
            raise ValueError("No /Root object")

        fake = type("FakePdfplumber", (), {"open": staticmethod(refuse)})
        rc, records = self._main(monkeypatch, capsys, _ARGV_1_1, b"x", fake)
        assert rc == pdf_extract_child.EXIT_FAILED
        assert records == [{"error": "parse", "detail": "ValueError"}]

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--max-chars=10", "--max-pages=1"],
            ["--max-chars=0", "--max-pages=1", "--max-rss=1"],
            ["--max-chars=x", "--max-pages=1", "--max-rss=1"],
            ["--max-chars=1", "--max-pages=1", "--max-rss=1", "--other=1"],
            ["--max-chars"],
        ],
    )
    def test_malformed_arguments_are_refused(self, argv):
        with pytest.raises(SystemExit):
            pdf_extract_child._parse_args(argv)


class TestParentReadsNothingOnFaith:
    """``_decode``: the child's stdout is checked against the caps the parent asked for."""

    @staticmethod
    def _lines(*records: dict) -> bytes:
        return b"".join(json.dumps(r).encode() + b"\n" for r in records)

    def test_a_good_stream_decodes(self):
        out = self._lines(
            {"label": "page 1", "text": "hi"}, {"end": True, "truncated": True, "pages": 1}
        )
        assert pdf_extract._decode(out, b"", 0, max_chars=10, max_pages=5) == (
            pdf_extract.PdfExtraction((("page 1", "hi"),), True, None, 1)
        )

    def test_more_text_than_the_cap_is_a_protocol_failure(self):
        out = self._lines(
            {"label": "page 1", "text": "x" * 11}, {"end": True, "truncated": False, "pages": 1}
        )
        assert pdf_extract._decode(out, b"", 0, max_chars=10, max_pages=5).failure == "protocol"

    def test_more_segments_than_pages_is_a_protocol_failure(self):
        out = self._lines(
            {"label": "page 1", "text": "a"},
            {"label": "page 2", "text": "b"},
            {"end": True, "truncated": False, "pages": 2},
        )
        assert pdf_extract._decode(out, b"", 0, max_chars=10, max_pages=1).failure == "protocol"

    def test_a_stream_past_the_byte_ceiling_is_refused_unparsed(self):
        out = b"x" * (10 * pdf_extract._JSON_BYTES_PER_CHAR + 6 * 64 + 1)
        assert pdf_extract._decode(out, b"", 0, max_chars=10, max_pages=5).failure == "protocol"

    def test_a_page_of_non_bmp_text_at_the_cap_is_inside_the_ceiling(self):
        # One emoji is ONE character to ``len`` (what the cap counts) but a
        # surrogate pair to ``json.dumps``: two escapes, twelve bytes. A valid
        # page of them must decode, not trip the byte ceiling as a protocol failure.
        text = "\U0001f600" * 10
        assert len(json.dumps(text)) - 2 == 10 * pdf_extract._JSON_BYTES_PER_CHAR
        out = self._lines(
            {"label": "page 1", "text": text}, {"end": True, "truncated": False, "pages": 1}
        )
        outcome = pdf_extract._decode(out, b"", 0, max_chars=10, max_pages=1)
        assert outcome == pdf_extract.PdfExtraction((("page 1", text),), False, None, 1)

    def test_no_terminal_line_is_a_protocol_failure(self):
        out = self._lines({"label": "page 1", "text": "hi"})
        assert pdf_extract._decode(out, b"", 0, max_chars=10, max_pages=5).failure == "protocol"

    def test_an_error_line_carries_its_kind(self):
        for kind in ("memory", "parse"):
            out = self._lines({"error": kind, "detail": "X"})
            outcome = pdf_extract._decode(out, b"", 3, max_chars=10, max_pages=5)
            assert outcome == pdf_extract.PdfExtraction((), True, kind, 0)
        out = self._lines({"error": "other", "detail": "X"})
        assert pdf_extract._decode(out, b"", 3, max_chars=10, max_pages=5).failure == "protocol"

    def test_signals_are_named(self):
        import signal

        assert (
            pdf_extract._decode(b"", b"", -signal.SIGXCPU, max_chars=1, max_pages=1).failure
            == "cpu"
        )
        assert (
            pdf_extract._decode(b"", b"", -signal.SIGKILL, max_chars=1, max_pages=1).failure
            == "killed"
        )
        assert pdf_extract._decode(b"", b"", -signal.SIGTERM, max_chars=1, max_pages=1).failure == (
            f"signal:{int(signal.SIGTERM)}"
        )

    def test_garbage_lines_are_a_protocol_failure(self):
        for out in (b"not json\n", b"[1, 2]\n", self._lines({"label": 1, "text": "x"})):
            assert pdf_extract._decode(out, b"", 0, max_chars=10, max_pages=5).failure == "protocol"

    def test_a_bad_page_count_is_a_protocol_failure(self):
        for pages in (-1, 6, "3", None):
            out = self._lines({"end": True, "truncated": False, "pages": pages})
            assert pdf_extract._decode(out, b"", 0, max_chars=10, max_pages=5).failure == "protocol"


def test_helpers_build_a_parseable_pdf_without_the_child():
    """The fixture is what the tests say it is: a real one-page PDF."""
    pdfplumber = pytest.importorskip("pdfplumber")
    with pdfplumber.open(io.BytesIO(text_pdf("fixture check"))) as pdf:
        assert [p.extract_text() for p in pdf.pages] == ["fixture check"]
