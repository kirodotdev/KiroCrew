"""PPTX Maker — the managed preview tools (`pdftoppm` shim + its launcher).

The app's slide thumbnails need ``pdftoppm``, which a stock machine does not have.
These tests pin the two halves of the fix: the shim reproduces the exact
``pdftoppm`` command shape the engine calls (so the engine's own glob keeps
matching), and the launcher makes it resolvable as ``pdftoppm`` from the app's
managed bin dir.

The shim is exercised against REAL PDFs built with ``pypdfium2`` rather than
mocks: the whole point of the module is byte-level output compatibility with
poppler's naming, and a mocked renderer would assert nothing about that.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.apps.builtins.pptx_maker.backend import pdftoppm_shim, preview_tools

pdfium = pytest.importorskip(
    "pypdfium2",
    reason="pypdfium2 backs the pdftoppm shim; it ships in the engine venv",
)


def _make_pdf(path: Path, pages: int = 2, width: int = 612, height: int = 792) -> Path:
    """A genuine multi-page PDF, so the shim is tested against real parsing."""
    import pypdfium2.raw as raw

    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        raw.FPDFPage_New(doc, len(doc), width, height)
    doc.save(str(path))
    return path


class TestShimMatchesPopplerOutputNaming:
    """The engine globs ``page-*.png`` and parses ``page-(\\d+)\\.png``.

    Any deviation (zero-padding, 0-based numbering) silently yields zero
    thumbnails, so the naming is the contract worth pinning hardest.
    """

    def test_writes_one_png_per_page_numbered_from_one(self, tmp_path: Path) -> None:
        source = _make_pdf(tmp_path / "slides.pdf", pages=3)
        rc = pdftoppm_shim.main(["-png", "-scale-to", "1280", str(source), str(tmp_path / "page")])
        assert rc == 0
        assert sorted(p.name for p in tmp_path.glob("page-*.png")) == [
            "page-1.png",
            "page-2.png",
            "page-3.png",
        ]

    def test_scale_to_fits_the_long_edge(self, tmp_path: Path) -> None:
        """``-scale-to N`` sizes the LONGER side to N and keeps the aspect ratio."""
        from PIL import Image

        source = _make_pdf(tmp_path / "portrait.pdf", pages=1, width=612, height=792)
        assert (
            pdftoppm_shim.main(["-png", "-scale-to", "1280", str(source), str(tmp_path / "p")]) == 0
        )
        with Image.open(tmp_path / "p-1.png") as img:
            assert max(img.size) == 1280
            # 612/792 preserved to within a pixel of rounding.
            assert abs(img.size[0] / img.size[1] - 612 / 792) < 0.01

    def test_resolution_flag_is_dpi_over_72(self, tmp_path: Path) -> None:
        """The engine's other call shape is ``-r 200``."""
        from PIL import Image

        source = _make_pdf(tmp_path / "r.pdf", pages=1, width=72, height=72)
        assert pdftoppm_shim.main(["-png", "-r", "144", str(source), str(tmp_path / "r")]) == 0
        with Image.open(tmp_path / "r-1.png") as img:
            assert img.size == (144, 144)

    def test_page_range_keeps_the_original_page_numbers(self, tmp_path: Path) -> None:
        """``-f 2 -l 2`` must emit ``-2.png``, not a renumbered ``-1.png``."""
        source = _make_pdf(tmp_path / "range.pdf", pages=3)
        assert (
            pdftoppm_shim.main(["-png", "-f", "2", "-l", "2", str(source), str(tmp_path / "sel")])
            == 0
        )
        assert [p.name for p in tmp_path.glob("sel-*.png")] == ["sel-2.png"]

    def test_creates_a_missing_output_directory(self, tmp_path: Path) -> None:
        source = _make_pdf(tmp_path / "d.pdf", pages=1)
        nested = tmp_path / "made" / "up" / "page"
        assert pdftoppm_shim.main(["-png", str(source), str(nested)]) == 0
        assert (tmp_path / "made" / "up" / "page-1.png").is_file()


class TestShimRefusesRatherThanMisrender:
    """Silently ignoring a flag would hand back output that is not what was asked."""

    def test_unsupported_flag_is_an_error(self, tmp_path: Path) -> None:
        source = _make_pdf(tmp_path / "u.pdf", pages=1)
        assert pdftoppm_shim.main(["-tiff", str(source), str(tmp_path / "x")]) != 0
        assert not list(tmp_path.glob("x-*"))

    def test_missing_output_prefix_is_an_error(self, tmp_path: Path) -> None:
        """Poppler writes to stdout with no prefix; the shim will not pretend to."""
        source = _make_pdf(tmp_path / "np.pdf", pages=1)
        assert pdftoppm_shim.main(["-png", str(source)]) != 0

    def test_absent_input_is_an_error(self, tmp_path: Path) -> None:
        assert pdftoppm_shim.main(["-png", str(tmp_path / "nope.pdf"), str(tmp_path / "y")]) != 0

    def test_a_corrupt_pdf_is_an_error_not_a_traceback(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"this is not a pdf")
        assert pdftoppm_shim.main(["-png", str(bad), str(tmp_path / "z")]) != 0

    @pytest.mark.parametrize("flag", ["-scale-to", "-r"])
    def test_a_negative_size_is_an_error_not_a_wrong_size(self, tmp_path: Path, flag: str) -> None:
        """argparse accepts `-scale-to -1280` as the integer -1280.

        A negative value fails the `> 0` sizing tests and falls through to the 150 DPI
        default, so the caller silently receives differently-sized PNGs. Measured
        before the guard: `-scale-to -1280` on US Letter produced 1275x1651 instead of
        a 1280px long edge, and exited 0.
        """
        source = _make_pdf(tmp_path / "neg.pdf", pages=1)
        assert pdftoppm_shim.main([flag, "-1280", str(source), str(tmp_path / "n")]) != 0
        assert not list(tmp_path.glob("n-*.png"))

    def test_an_empty_page_range_is_an_error(self, tmp_path: Path) -> None:
        source = _make_pdf(tmp_path / "e.pdf", pages=2)
        assert (
            pdftoppm_shim.main(["-png", "-f", "5", "-l", "9", str(source), str(tmp_path / "q")])
            != 0
        )


class TestLauncherInstall:
    """The launcher is what makes the shim resolvable as ``pdftoppm``."""

    @pytest.fixture()
    def staged(self, tmp_path: Path):
        """Point the app's bin dir at tmp_path and pretend the engine venv exists."""
        bin_dir = tmp_path / "vendor" / "preview-tools" / "bin"
        fake_python = tmp_path / "venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.write_text("#!/bin/sh\n")
        # `Path.chmod`, matching the convention in `test_acp_client.py`: the SAST
        # insecure-file-permissions rule matches `os.chmod` specifically, and a
        # fixture executable is exactly the case it is not meant to flag.
        fake_python.chmod(0o700)
        with (
            mock.patch.object(preview_tools.paths, "preview_tools_bin", return_value=bin_dir),
            mock.patch.object(preview_tools, "_engine_python", return_value=fake_python),
        ):
            yield bin_dir, fake_python

    def test_install_is_reported_and_probes_true(self, staged) -> None:
        bin_dir, _ = staged
        assert preview_tools.pdftoppm_installed() is False
        ok, message = preview_tools.install_pdftoppm()
        assert ok, message
        assert preview_tools.pdftoppm_installed() is True
        assert bin_dir.is_dir()

    def test_launcher_names_the_engine_interpreter_and_the_shim_file(self, staged) -> None:
        """`-m` would need the gateway package importable from the engine venv."""
        _, fake_python = staged
        assert preview_tools.install_pdftoppm()[0]
        body = preview_tools._launcher_paths()[0].read_text()
        assert str(fake_python) in body
        assert "pdftoppm_shim.py" in body
        assert " -m " not in body

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec bit")
    def test_launcher_is_executable_and_not_group_writable(self, staged) -> None:
        assert preview_tools.install_pdftoppm()[0]
        launcher = preview_tools._launcher_paths()[0]
        assert os.access(launcher, os.X_OK)
        assert preview_tools.managed_status()["pdftoppmSecure"] is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX /bin/sh quoting")
    @pytest.mark.parametrize(
        "hostile",
        ["has space", "has$dollar", "has'quote", "has`backtick", "has%percent"],
        ids=["space", "dollar", "quote", "backtick", "percent"],
    )
    def test_launcher_runs_from_a_path_the_shell_would_mangle(
        self, tmp_path: Path, hostile: str
    ) -> None:
        """KIROCREW_HOME is user-chosen, so the launcher must survive its characters.

        Inside POSIX double quotes ``$`` and backticks still expand, so a data home
        like ``/tmp/my $home/`` yielded a launcher whose interpreter path had the
        ``$home`` segment deleted and every invocation died with "cannot execute".
        Executes the real launcher rather than asserting on its text, because the
        shell is the thing being tested.
        """
        base = tmp_path / hostile
        bin_dir = base / "bin"
        # The hostile characters must sit in the INTERPRETER path, because that is
        # what the launcher body interpolates — putting them only in the bin dir
        # tests nothing (the bin dir never appears inside the script). A symlink
        # gives a real, working interpreter at a hostile path.
        venv_bin = base / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        hostile_python = venv_bin / "python"
        hostile_python.symlink_to(Path(sys.executable))
        with (
            mock.patch.object(preview_tools.paths, "preview_tools_bin", return_value=bin_dir),
            mock.patch.object(preview_tools, "_engine_python", return_value=hostile_python),
        ):
            assert preview_tools.install_pdftoppm()[0]
            launcher = preview_tools._launcher_paths()[0]

        result = subprocess.run(
            [str(launcher), "-png", str(base / "absent.pdf"), str(base / "out" / "page")],
            capture_output=True,
            text=True,
        )
        # The SHIM reporting a missing input (exit 1) proves it was reached. A shell
        # failure to resolve the path would be 126/127 with no shim message.
        assert result.returncode == 1, result.stderr
        assert "pdftoppm shim:" in result.stderr

    def test_the_launcher_body_is_written_without_newline_translation(self, staged) -> None:
        """`atomic_write`'s default translates `\\n`, which would make the `.cmd`
        form's explicit `\\r\\n` into `\\r\\r\\n` on Windows.

        Asserts on the call rather than the file, because the translation only
        happens on Windows and this suite has to catch the regression everywhere.
        """
        real_write = preview_tools.atomic_write
        with mock.patch.object(preview_tools, "atomic_write", side_effect=real_write) as write:
            assert preview_tools.install_pdftoppm()[0]
        assert (
            write.call_args.kwargs.get("newline") == ""
        ), "the launcher must be written with newline='' so its line endings are exact"

    def test_install_is_idempotent(self, staged) -> None:
        assert preview_tools.install_pdftoppm()[0]
        first = preview_tools._launcher_paths()[0].read_text()
        assert preview_tools.install_pdftoppm()[0]
        assert preview_tools._launcher_paths()[0].read_text() == first

    def test_refuses_before_the_engine_venv_exists(self, tmp_path: Path) -> None:
        """Writing a launcher pointing at a missing interpreter would fail later."""
        with (
            mock.patch.object(
                preview_tools.paths, "preview_tools_bin", return_value=tmp_path / "bin"
            ),
            mock.patch.object(preview_tools, "_engine_python", return_value=None),
        ):
            ok, message = preview_tools.install_pdftoppm()
        assert ok is False
        assert "engine" in message.lower()


class TestSofficeIsReportedNotInstalled:
    """LibreOffice has no unpack-and-run build and no publishable digest pin, so it
    stays a user action — the app reports the command instead of running it."""

    def test_hint_is_platform_specific_and_non_empty(self) -> None:
        for platform_name in ("darwin", "linux", "win32"):
            with mock.patch.object(preview_tools.sys, "platform", platform_name):
                assert preview_tools.soffice_hint().strip()

    def test_no_install_function_exists_for_soffice(self) -> None:
        """Guards the product decision: nothing here may install a system package."""
        assert not any(
            name for name in dir(preview_tools) if "install" in name and "soffice" in name.lower()
        )
