"""Bundle-derived paths reach a terminal in the report too, not just in the archive."""

from __future__ import annotations

import inspect
import re

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact

HOSTILE = "workspace/ev\x1b[2Jil.md"


class TestTheRedactionReportIsEscaped:
    def test_a_hostile_path_cannot_repaint_the_report(self, capsys) -> None:
        """The report is what the operator reads to judge the upload, so it is a target."""
        report = redact.RedactionReport()
        report.replacements[HOSTILE] = 3
        report.dropped.append(HOSTILE)
        report.skipped_unreadable.append(f"{HOSTILE} (unreadable)")

        snap._report_redaction(report)

        out = capsys.readouterr().out
        assert "\x1b" not in out, "an escape sequence from a filename reached the terminal"
        assert "il.md" in out, "the path was escaped into uselessness"

    def test_every_path_in_the_report_goes_through_the_escaper(self) -> None:
        """Structural, because the three lists are three chances to forget one."""
        src = inspect.getsource(snap._report_redaction)
        bare = re.findall(r"\{(?!_safe_name)(rel|d|s)\}", src)
        assert bare == [], f"a report path is printed unescaped: {bare}"
        assert (
            src.count("_safe_name(") == 3
        ), "each of replacements / dropped / skipped_unreadable must be escaped"

    def test_the_dry_run_listing_is_escaped_too(self) -> None:
        src = inspect.getsource(snap.restore_main)
        assert (
            "_safe_name(f.relative_to(snap).as_posix())" in src
        ), "the dry-run list prints archive-derived paths raw"
