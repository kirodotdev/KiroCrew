"""Tests for /api/file-grep's workbook pass: the expansion cap and what it still reads."""

from __future__ import annotations

import zipfile

from kiro_crew.dashboard.handlers import files as f


class TestWorkbookExpansion:
    def test_a_workbook_that_expands_past_the_cap_never_reaches_openpyxl(
        self, tmp_path, monkeypatch
    ):
        """The shared inventory vet bounds member COUNT and central-directory
        size, not expansion, so a single hugely-compressed member passed it and
        reached openpyxl's XML parser."""
        path = tmp_path / "bomb.xlsx"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
            zf.writestr("xl/big.bin", b"\0" * (2 * 1024 * 1024))
        opened: list[str] = []
        monkeypatch.setattr(f, "_SHEET_MAX_EXPANDED_BYTES", 1024)

        real_import = __import__

        def tracking_import(name, *args, **kwargs):
            if name == "openpyxl":
                opened.append(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", tracking_import)
        parse = f._grep_xlsx_segments(path.read_bytes(), str(path), f.time.monotonic() + 30)
        assert parse == ((), True)
        # The refusal is BEFORE the parser: reaching openpyxl and bailing out
        # later would already have paid the expansion this cap exists to refuse.
        assert opened == []

    def test_a_workbook_within_the_cap_is_still_read(self, tmp_path):
        import openpyxl

        path = tmp_path / "small.xlsx"
        book = openpyxl.Workbook()
        book.active.title = "Ledger"
        book.active.append(["note"])
        book.active.append(["the WIDGET invoice"])
        book.save(path)
        segments, whole = f._grep_xlsx_segments(
            path.read_bytes(), str(path), f.time.monotonic() + 30
        )
        assert whole is True
        assert any("WIDGET" in text for _label, text in segments)

    def test_rows_past_a_lying_dimension_record_are_still_searched(self, tmp_path):
        """A writer can leave a stale dimension ref (A1:A1) on a populated sheet, and
        the read-only reader trusts it, so rows past it were never searched."""
        import io
        import re

        import openpyxl

        book = openpyxl.Workbook()
        book.active.title = "Ledger"
        book.active["A1"] = "header"
        book.active["C5"] = "the WIDGET invoice"
        raw = io.BytesIO()
        book.save(raw)
        path = tmp_path / "stale.xlsx"
        forged = False
        with zipfile.ZipFile(io.BytesIO(raw.getvalue())) as src, zipfile.ZipFile(path, "w") as out:
            for item in src.infolist():
                payload = src.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    payload, n = re.subn(
                        rb'<dimension ref="[^"]*"/>', b'<dimension ref="A1:A1"/>', payload
                    )
                    forged = n == 1
                out.writestr(item, payload)
        assert forged, "fixture did not carry a dimension record to forge"

        segments, whole = f._grep_xlsx_segments(
            path.read_bytes(), str(path), f.time.monotonic() + 30
        )
        assert whole is True
        assert ("Ledger · row 5", "the WIDGET invoice") in segments
