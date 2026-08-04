"""Tests for Papyrus's compiler driver and log parser (``latex.py``).

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Every subprocess is mocked — no ``pdflatex``, ``tectonic`` or ``bibtex`` is ever
invoked, so this suite runs on a host with no TeX installation.

Coverage targets:

  * ``parse_log`` — the four message shapes, and specifically that two consecutive
    ``!`` errors do NOT borrow one another's ``l.<n>`` line reference (the bug the
    upstream app fixed and this port must not regress);
  * ``_compiler_argv`` — the SECURITY invariant that ``-no-shell-escape`` is always
    passed to pdflatex and that tectonic is never handed a shell-escape flag;
  * ``compile_project`` — the pass sequence: one pass without a bibliography, the
    four-pass bibtex cycle when the ``.aux`` shows citations, the "Rerun to get"
    retry, and that tectonic is driven with a single invocation;
  * the timeout path — the process tree is killed and the result says so;
  * ``find_compiler_sync`` — PATH preference order, the userspace fallback, and the
    cache.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Iterator
from unittest import mock

import pytest

from kiro_crew import sandbox, security
from kiro_crew.apps.builtins.papyrus.backend import latex, procio, store


def _spawn_mode(wrap: mock.Mock) -> str:
    """The sandbox mode a `sandboxed_spawn_argv` call asked for.

    Reads the POSITIONAL second argument as well as the `mode=` keyword: the
    compiler spawn is offloaded via `functools.partial(..., argv, "strict", ...)`,
    so the mode arrives positionally, while `gitops` passes it (or omits it) as a
    keyword. Checking one form only made this assertion silently unable to fail.
    """
    args, kwargs = wrap.call_args.args, wrap.call_args.kwargs
    if "mode" in kwargs:
        return str(kwargs["mode"])
    return str(args[1]) if len(args) > 1 else "standard"


@pytest.fixture(autouse=True)
def _clear_compiler_cache() -> Iterator[None]:
    """The compiler path is cached process-wide; isolate every test from it."""
    latex.reset_compiler_cache()
    yield
    latex.reset_compiler_cache()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "paper"
    proj.mkdir()
    (proj / "main.tex").write_text(r"\documentclass{article}", encoding="utf-8")
    return proj


# ── log parsing ─────────────────────────────────────────────────────────────


class TestParseLog:
    def test_file_line_form(self) -> None:
        entries = latex.parse_log("./main.tex:42: Undefined control sequence.")
        assert len(entries) == 1
        entry = entries[0]
        assert entry.file == "./main.tex"
        assert entry.line == 42
        assert entry.level == "error"
        assert entry.message == "Undefined control sequence."

    def test_bang_error_finds_the_line_after_a_blank(self) -> None:
        """pdflatex puts a blank line between `! Error` and `l.N`; we still match."""
        log = (
            "! LaTeX Error: File `missing.sty' not found.\n"
            "\n"
            "l.7 \\usepackage{missing}\n"
        )
        entries = latex.parse_log(log)
        assert len(entries) == 1
        assert entries[0].line == 7
        assert entries[0].level == "error"
        assert "missing.sty" in entries[0].message

    def test_two_bangs_do_not_share_a_line(self) -> None:
        """Consecutive bang errors must each get their OWN `l.N`, never borrow.

        The lookup is bounded to the text before the next `^!` line. Without that
        bound the second error inherits the first's line number and the editor
        jumps to the wrong place — which is worse than no line at all, because it
        looks authoritative.
        """
        log = (
            "! Error one.\n"
            "l.10 first\n"
            "\n"
            "! Error two.\n"
            "l.20 second\n"
        )
        entries = latex.parse_log(log)
        assert [e.line for e in entries] == [10, 20]

    def test_bang_without_a_line_reference_is_still_reported(self) -> None:
        entries = latex.parse_log("! Emergency stop.\n")
        assert len(entries) == 1
        assert entries[0].line is None
        assert entries[0].level == "error"

    def test_warning_with_an_input_line(self) -> None:
        entries = latex.parse_log("LaTeX Warning: Reference `fig:1' on input line 12 undefined.")
        assert len(entries) == 1
        assert entries[0].level == "warning"
        assert entries[0].line == 12

    def test_package_warning(self) -> None:
        entries = latex.parse_log("Package natbib Warning: Citation `smith' undefined on page 3.")
        assert len(entries) == 1
        assert entries[0].level == "warning"

    def test_overfull_box_is_a_typesetting_hint(self) -> None:
        entries = latex.parse_log("Overfull \\hbox (12.34pt too wide) at lines 100--102")
        assert len(entries) == 1
        assert entries[0].level == "typesetting"
        assert entries[0].line == 100

    def test_underfull_box(self) -> None:
        entries = latex.parse_log("Underfull \\vbox (badness 10000) at line 55")
        assert len(entries) == 1
        assert entries[0].level == "typesetting"
        assert entries[0].line == 55

    def test_distinct_repeats_are_both_kept(self) -> None:
        """Two file:line errors with the SAME message are two real problems."""
        log = "./main.tex:10: Missing $ inserted.\n./main.tex:42: Missing $ inserted.\n"
        entries = latex.parse_log(log)
        assert {e.line for e in entries} == {10, 42}

    def test_empty_log_yields_nothing(self) -> None:
        assert latex.parse_log("") == []

    def test_output_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A broken preamble can emit thousands of warnings; the list is a UI, not a log."""
        monkeypatch.setattr(latex, "MAX_DIAGNOSTICS", 5)
        log = "\n".join(f"./main.tex:{i}: Missing $ inserted." for i in range(1, 50))
        assert len(latex.parse_log(log)) == 5

    def test_diagnostic_serializes_the_wire_shape(self) -> None:
        payload = latex.parse_log("./main.tex:1: Bad.")[0].to_dict()
        assert set(payload) == {"level", "message", "line", "file"}


# ── argv construction (the security invariant) ───────────────────────────────


class TestCompilerArgv:
    def test_pdflatex_always_disables_shell_escape(self, project: Path) -> None:
        """With shell escape ON, a `\\write18` in an untrusted .tex is RCE.

        The document here is untrusted content by construction — the agent writes
        it and a cloned repository supplies it wholesale — so this flag must be
        passed explicitly on every invocation, never left to the site default.
        """
        argv = latex._compiler_argv("/usr/bin/pdflatex", project / "main.tex", project)
        assert "-no-shell-escape" in argv
        assert not any("shell-escape" in a and a != "-no-shell-escape" for a in argv)

    def test_pdflatex_never_enables_shell_escape(self, project: Path) -> None:
        argv = latex._compiler_argv("/usr/bin/pdflatex", project / "main.tex", project)
        assert "-shell-escape" not in argv
        assert "--shell-escape" not in argv

    def test_pdflatex_runs_non_interactively(self, project: Path) -> None:
        """Without this the compiler blocks on a prompt and the timeout is the only exit."""
        argv = latex._compiler_argv("/usr/bin/pdflatex", project / "main.tex", project)
        assert "-interaction=nonstopmode" in argv

    def test_pdflatex_passes_the_document_after_a_double_dash(self, project: Path) -> None:
        """So a filename that begins with a dash can never be read as an option."""
        argv = latex._compiler_argv("/usr/bin/pdflatex", project / "main.tex", project)
        assert argv[-2] == "--"

    def test_tectonic_is_not_given_shell_escape(self, project: Path) -> None:
        argv = latex._compiler_argv("/usr/local/bin/tectonic", project / "main.tex", project)
        assert not any("shell-escape" in a for a in argv)
        assert argv[0].endswith("tectonic")


# ── compiler discovery ──────────────────────────────────────────────────────


class TestFindCompiler:
    def test_prefers_pdflatex_over_tectonic(self) -> None:
        with mock.patch.object(latex.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"), \
                mock.patch.object(latex.os.path, "isfile", return_value=True), \
                mock.patch.object(latex.os, "access", return_value=True):
            assert latex.find_compiler_sync() == "/usr/bin/pdflatex"

    def test_falls_back_to_tectonic(self) -> None:
        def which(name: str) -> str | None:
            return "/usr/local/bin/tectonic" if name == "tectonic" else None

        with mock.patch.object(latex.shutil, "which", side_effect=which), \
                mock.patch.object(latex.os.path, "isfile", return_value=True), \
                mock.patch.object(latex.os, "access", return_value=True):
            assert latex.find_compiler_sync() == "/usr/local/bin/tectonic"

    def test_returns_none_when_nothing_is_installed(self) -> None:
        with mock.patch.object(latex.shutil, "which", return_value=None), \
                mock.patch("glob.glob", return_value=[]):
            assert latex.find_compiler_sync() is None

    def test_finds_a_userspace_texlive_install(self) -> None:
        """The no-sudo TeX Live route lands under ~/texlive and is not on PATH."""
        found = "/home/u/texlive/2026/bin/x86_64-linux/pdflatex"
        with mock.patch.object(latex.shutil, "which", return_value=None), \
                mock.patch("glob.glob", side_effect=lambda p: [found] if "texlive" in p else []), \
                mock.patch.object(latex.os.path, "isfile", return_value=True), \
                mock.patch.object(latex.os, "access", return_value=True):
            assert latex.find_compiler_sync() == found

    def test_result_is_cached(self) -> None:
        with mock.patch.object(latex.shutil, "which", return_value="/usr/bin/pdflatex") as which, \
                mock.patch.object(latex.os.path, "isfile", return_value=True), \
                mock.patch.object(latex.os, "access", return_value=True):
            latex.find_compiler_sync()
            latex.find_compiler_sync()
            assert which.call_count == 1

    def test_a_negative_result_is_also_cached(self) -> None:
        with mock.patch.object(latex.shutil, "which", return_value=None) as which, \
                mock.patch("glob.glob", return_value=[]):
            assert latex.find_compiler_sync() is None
            assert latex.find_compiler_sync() is None
            # 2 names probed once, not twice.
            assert which.call_count == len(latex.COMPILER_NAMES)

    def test_rejects_a_non_executable_hit(self) -> None:
        with mock.patch.object(latex.shutil, "which", return_value="/usr/bin/pdflatex"), \
                mock.patch.object(latex.os.path, "isfile", return_value=True), \
                mock.patch.object(latex.os, "access", return_value=False), \
                mock.patch("glob.glob", return_value=[]):
            assert latex.find_compiler_sync() is None


# ── compile_project ─────────────────────────────────────────────────────────


class _RunRecorder:
    """Records every ``_run`` call and returns a scripted result.

    ``per_operation`` overrides the default for one operation kind (``"compile"`` /
    ``"bibtex"``), which is what lets a test fail ONLY the bibtex pass while the
    surrounding pdflatex passes still succeed — the shape of the bug where a failed
    bibliography was reported as a successful compile.
    """

    def __init__(
        self,
        *,
        output: str = "",
        code: int = 0,
        per_operation: dict[str, tuple[int, str]] | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.output = output
        self.code = code
        self.per_operation = per_operation or {}

    async def __call__(self, argv, *, cwd, env, timeout, operation):  # noqa: ANN001
        self.calls.append((argv, operation))
        if operation in self.per_operation:
            return self.per_operation[operation]
        return self.code, self.output

    @property
    def operations(self) -> list[str]:
        return [op for _argv, op in self.calls]


@pytest.mark.asyncio
class TestCompileProject:
    async def test_missing_main_file_is_reported_without_spawning(self, project: Path) -> None:
        (project / "main.tex").unlink()
        recorder = _RunRecorder()
        with mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert "not found" in result.log
        assert recorder.calls == []

    async def test_no_compiler_is_reported_without_spawning(self, project: Path) -> None:
        recorder = _RunRecorder()
        with mock.patch.object(latex, "find_compiler", mock.AsyncMock(return_value=None)), \
                mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert "No LaTeX compiler" in result.log
        assert recorder.calls == []

    async def test_single_pass_without_a_bibliography(self, project: Path) -> None:
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder()
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is True
        assert recorder.operations == ["compile"]

    async def test_runs_the_four_pass_bibtex_cycle_when_the_aux_cites(self, project: Path) -> None:
        """pdflatex -> bibtex -> pdflatex -> pdflatex.

        The first pass writes \\citation into the .aux; bibtex turns it into a
        .bbl; the last two integrate it and resolve the \\cite references. Cutting
        the cycle short leaves `[?]` in the PDF.
        """
        (project / "main.aux").write_text("\\citation{smith2024}", encoding="utf-8")
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder()
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_find_bibtex", return_value="/usr/bin/bibtex"), \
                mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is True
        assert recorder.operations == ["compile", "bibtex", "compile", "compile"]

    async def test_a_fatal_bibtex_failure_fails_the_compile(self, project: Path) -> None:
        """A failed bibliography must not be reported as a successful compile.

        bibtex's result was discarded, so a missing `.bst` produced no `.bbl`, the two
        later pdflatex passes still exited 0, and `ok` was True — while the PDF carried
        `[?]` for every citation and an empty bibliography. The user saw green and a
        silently wrong paper. Note the pdflatex passes SUCCEED here: that is the whole
        point, and why watching `code` alone could never catch it.
        """
        (project / "main.aux").write_text("\\citation{smith2024}", encoding="utf-8")
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder(
            per_operation={"bibtex": (2, "I couldn't open style file plainnat.bst")}
        )
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_find_bibtex", return_value="/usr/bin/bibtex"), \
                mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert "BibTeX failed" in result.log
        assert "plainnat.bst" in result.log, "the user cannot act without bibtex's own message"
        # It stops at the failure rather than running the remaining passes for nothing.
        assert recorder.operations == ["compile", "bibtex"]

    async def test_a_bibtex_warning_does_not_fail_the_compile(self, project: Path) -> None:
        """The bar is NOT "any non-zero exit".

        bibtex exits 1 for WARNINGS, which are routine on a healthy document (an
        undefined cross-reference, a missing `journal` field). Failing on that would
        refuse most real bibliographies — so exit 1 must still compile.
        """
        (project / "main.aux").write_text("\\citation{smith2024}", encoding="utf-8")
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder(
            per_operation={"bibtex": (1, "Warning--empty journal in smith2024")}
        )
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_find_bibtex", return_value="/usr/bin/bibtex"), \
                mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is True
        assert recorder.operations == ["compile", "bibtex", "compile", "compile"]

    async def test_a_bibtex_timeout_fails_the_compile(self, project: Path) -> None:
        """`(-1, "")` is `_run`'s documented timeout contract, and a bibliography that
        never finished is not one that succeeded."""
        (project / "main.aux").write_text("\\citation{x}", encoding="utf-8")
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder(per_operation={"bibtex": (-1, "")})
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_find_bibtex", return_value="/usr/bin/bibtex"), \
                mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert "timed out" in result.log

    async def test_skips_bibtex_when_the_aux_has_no_citations(self, project: Path) -> None:
        (project / "main.aux").write_text("\\relax", encoding="utf-8")
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder()
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_find_bibtex", return_value="/usr/bin/bibtex"), \
                mock.patch.object(latex, "_run", recorder):
            await latex.compile_project(project, "main.tex")
        assert recorder.operations == ["compile"]

    async def test_skips_bibtex_when_no_bibtex_binary_exists(self, project: Path) -> None:
        (project / "main.aux").write_text("\\citation{x}", encoding="utf-8")
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder()
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_find_bibtex", return_value=None), \
                mock.patch.object(latex, "_run", recorder):
            await latex.compile_project(project, "main.tex")
        assert recorder.operations == ["compile"]

    async def test_retries_once_on_rerun_to_get(self, project: Path) -> None:
        """A table of contents or a \\ref settles on the SECOND pass."""
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder(output="LaTeX Warning: Rerun to get cross-references right.")
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", recorder):
            await latex.compile_project(project, "main.tex")
        assert recorder.operations == ["compile", "compile"]

    async def test_does_not_retry_when_the_pass_failed(self, project: Path) -> None:
        """A failing pass that also asks to rerun is broken, not merely unsettled."""
        recorder = _RunRecorder(output="Rerun to get cross-references right.", code=1)
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", recorder):
            await latex.compile_project(project, "main.tex")
        assert recorder.operations == ["compile"]

    async def test_tectonic_drives_its_own_cycle_in_one_call(self, project: Path) -> None:
        (project / "main.aux").write_text("\\citation{x}", encoding="utf-8")
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder()
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/local/bin/tectonic")
        ), mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is True
        assert recorder.operations == ["compile"]

    async def test_a_missing_pdf_means_failure_even_on_exit_zero(self, project: Path) -> None:
        """pdflatex can exit 0 having produced nothing usable."""
        recorder = _RunRecorder(code=0)
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False

    async def test_diagnostics_are_parsed_from_the_output(self, project: Path) -> None:
        recorder = _RunRecorder(output="./main.tex:9: Undefined control sequence.", code=1)
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert [d.line for d in result.diagnostics] == [9]

    async def test_log_is_truncated_to_the_tail(self, project: Path, monkeypatch) -> None:
        monkeypatch.setattr(latex, "MAX_LOG_CHARS", 20)
        recorder = _RunRecorder(output="x" * 500, code=1)
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", recorder):
            result = await latex.compile_project(project, "main.tex")
        assert len(result.log) == 20

    async def test_timeout_is_reported_as_a_timeout(self, project: Path) -> None:
        async def timed_out(argv, *, cwd, env, timeout, operation):  # noqa: ANN001
            return -1, ""

        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", timed_out):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert "timed out" in result.log

    async def test_result_serializes_the_wire_shape(self, project: Path) -> None:
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        recorder = _RunRecorder()
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", recorder):
            payload = (await latex.compile_project(project, "main.tex")).to_dict()
        assert set(payload) == {"ok", "log", "errors", "duration_ms"}


# ── the spawn helper ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRunHelper:
    async def test_routes_through_the_sandbox_chokepoint(self, project: Path) -> None:
        """The compiler runs untrusted document content, so it MUST be sandboxed."""
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(return_value=(b"out", b"err"))
        proc.returncode = 0
        with mock.patch.object(
            latex, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ) as wrap, mock.patch(
            "asyncio.create_subprocess_exec", mock.AsyncMock(return_value=proc)
        ):
            code, output = await latex._run(
                ["pdflatex", "main.tex"], cwd=project, env={}, timeout=5, operation="compile"
            )
        assert wrap.called
        assert code == 0
        assert output == "outerr"

    async def test_hides_credential_dirs_from_the_compiler(self, project: Path) -> None:
        """The compiler spawn must be STRICT, not the ``standard`` default.

        Standard mode deliberately leaves ``~/.aws`` and ``~/.ssh`` readable so
        git-over-SSH and the AWS CLI keep working. TeX needs neither, and it CAN
        read a file and typeset its contents — so under standard mode an
        ``\\input{../../../../.aws/credentials}`` in a cloned paper renders the
        operator's keys into the output PDF. ``-no-shell-escape`` does not help:
        that is an ordinary file read, not a shell escape.
        """
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        with mock.patch.object(
            latex, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ) as wrap, mock.patch(
            "asyncio.create_subprocess_exec", mock.AsyncMock(return_value=proc)
        ):
            await latex._run(
                ["pdflatex", "main.tex"], cwd=project, env={}, timeout=5, operation="compile"
            )
        assert _spawn_mode(wrap) == "strict", (
            "the LaTeX compiler must run in strict sandbox mode; `standard` leaves "
            "~/.aws and ~/.ssh readable to an untrusted .tex"
        )

    async def test_hides_kirocrews_own_trust_root_from_the_compiler(
        self, project: Path
    ) -> None:
        """Strict mode alone is NOT enough — it misses KiroCrew's own secrets.

        ``_STRICT_DIRS`` covers third-party credential locations plus
        ``~/.kiro/crew/.env``, but the gateway's ``.local_secret``,
        ``sel_hmac.key``, ``security_policy.json`` and ``profiles/`` sit beside it
        and are NOT in that list. TeX reads files, so
        ``\\verbatiminput{~/.kiro/crew/.local_secret}`` would typeset the gateway's
        own callback credential into the PDF.

        Asserted against the LIVE floor rather than a hardcoded sample, so a path
        added to ``sensitive_home_dirs()`` later is required here automatically.
        """
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        with mock.patch.object(
            latex, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ) as wrap, mock.patch(
            "asyncio.create_subprocess_exec", mock.AsyncMock(return_value=proc)
        ):
            await latex._run(
                ["pdflatex", "main.tex"], cwd=project, env={}, timeout=5, operation="compile"
            )
        hidden = set(wrap.call_args.kwargs.get("extra_hidden_dirs") or ())
        assert hidden, "the compiler spawn names no extra hidden dirs"
        home = os.path.expanduser("~")
        missing = [
            rel
            for rel in security.sensitive_home_dirs()
            if os.path.join(home, rel) not in hidden
        ]
        assert not missing, f"sensitive paths readable by the compiler: {missing}"

        # And the hiding must not be self-cancelling: `wrap_argv` DROPS a hidden
        # entry that contains any `extra_visible_dirs` path, so naming the project
        # tree or the app data dir as visible would silently undo all of the above.
        visible = set(wrap.call_args.kwargs.get("extra_visible_dirs") or ())
        for hidden_path in hidden:
            assert not sandbox._hidden_path_contains_visible_path(
                hidden_path, tuple(visible)
            ), f"{hidden_path} is cancelled by an extra_visible_dirs entry"

    async def test_applies_a_resource_ceiling(self, project: Path) -> None:
        """A runaway macro expansion must hit a kernel limit, not the host's RAM.

        Via ``create_subprocess_limited``, which applies the limits AFTER exec.
        A post-fork ``preexec_fn`` would fork the threaded gateway and run
        Python in the child first — the hazard ``test_spawn_preexec_guard``
        exists to keep out.
        """
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        spawn = mock.AsyncMock(return_value=proc)
        with mock.patch.object(
            latex, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ), mock.patch.object(latex, "create_subprocess_limited", spawn):
            await latex._run(
                ["pdflatex"], cwd=project, env={}, timeout=5, operation="compile"
            )
        assert spawn.await_args is not None
        assert "preexec_fn" not in spawn.await_args.kwargs

    async def test_a_timeout_kills_the_process_tree(self, project: Path) -> None:
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(side_effect=asyncio.TimeoutError)
        proc.wait = mock.AsyncMock(return_value=0)
        proc.returncode = None
        proc.pid = 4321
        with mock.patch.object(
            latex, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ), mock.patch(
            "asyncio.create_subprocess_exec", mock.AsyncMock(return_value=proc)
        ), mock.patch.object(
            latex.platform_compat, "kill_process_tree_async", mock.AsyncMock(return_value=True)
        ) as kill:
            code, output = await latex._run(
                ["pdflatex"], cwd=project, env={}, timeout=0.01, operation="compile"
            )
        assert kill.await_args is not None
        assert kill.await_args.args[0] == 4321
        assert (code, output) == (-1, "")

    async def test_cleans_up_the_sandbox_profile(self, project: Path, tmp_path: Path) -> None:
        """The sandbox launcher/profile is a temp file the caller owns."""
        cleanup = tmp_path / "profile.sb"
        cleanup.write_text("", encoding="utf-8")
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        with mock.patch.object(
            latex, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, str(cleanup))
        ), mock.patch("asyncio.create_subprocess_exec", mock.AsyncMock(return_value=proc)):
            await latex._run(["pdflatex"], cwd=project, env={}, timeout=5, operation="compile")
        assert not cleanup.exists()


# ── search-path env ─────────────────────────────────────────────────────────


class TestSearchPathEnv:
    def test_extends_bst_and_bib_inputs_with_every_holding_directory(self, project: Path) -> None:
        """A conference template stashes its .bst under templates/<conf>/.

        Without this, bibtex fails with "I couldn't open style file" on papers
        whose style file is not at the project root.
        """
        (project / "templates" / "acl").mkdir(parents=True)
        (project / "templates" / "acl" / "acl_natbib.bst").write_text("", encoding="utf-8")
        (project / "references.bib").write_text("", encoding="utf-8")

        env = latex._search_path_env_sync(project)
        assert str(project / "templates" / "acl") in env["BSTINPUTS"]
        assert str(project) in env["BIBINPUTS"]
        # Trailing separator = "also search the default TEXMF tree".
        assert env["BSTINPUTS"].endswith(os.pathsep)

    def test_degrades_to_the_default_tree_when_nothing_is_found(self, project: Path) -> None:
        env = latex._search_path_env_sync(project)
        assert env["BSTINPUTS"] == "." + os.pathsep
        assert env["BIBINPUTS"] == "." + os.pathsep

    def test_the_separator_is_the_platforms_own(self, project: Path) -> None:
        """`;` on Windows, `:` elsewhere — and a drive letter must not split a path.

        A hardcoded `":"` was both the wrong delimiter on Windows and a splitter of
        `C:\\proj\\bib` into two useless fragments, which is exactly the
        "I couldn't open style file" failure this env var exists to prevent.
        """
        nested = project / "templates" / "acl"
        nested.mkdir(parents=True)
        (nested / "acl_natbib.bst").write_text("", encoding="utf-8")
        (project / "second.bst").write_text("", encoding="utf-8")

        entries = latex._search_path_env_sync(project)["BSTINPUTS"].split(os.pathsep)
        # Both holding directories survive the round trip INTACT — the assertion
        # that a `:`-join on Windows could not satisfy.
        assert str(nested) in entries
        assert str(project) in entries


class TestBaseEnv:
    def test_does_not_pass_the_gateways_whole_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child running untrusted document content must not see unrelated secrets."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-not-for-the-compiler")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
        env = latex._base_env({})
        assert "SLACK_BOT_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env

    def test_passes_tex_specific_variables_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEXMFHOME", "/home/u/texmf")
        assert latex._base_env({})["TEXMFHOME"] == "/home/u/texmf"

    def test_extra_values_win(self) -> None:
        env = latex._base_env({"BSTINPUTS": ".:/x:"})
        assert env["BSTINPUTS"] == ".:/x:"

    def test_windows_location_hints_survive_the_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Windows child needs these to start at all — and they are not secrets.

        ``minimal_env``'s allowlist was POSIX-only, which fails early and opaquely
        rather than loudly: a Windows process without ``SystemRoot`` typically dies
        before ``main()`` (DLL/crypto init resolves through it), and without
        ``USERPROFILE`` TeX cannot resolve ``TEXMFHOME``. Asserted here because the
        compiler spawn is the caller that made it user-visible.
        """
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        monkeypatch.setenv("USERPROFILE", r"C:\Users\me")
        monkeypatch.setenv("TEMP", r"C:\Temp")
        env = latex._base_env({})
        # Looked up case-insensitively, because the CASE is platform-dependent and
        # is not what this test is about: on Windows `os.environ` upper-cases every
        # key, so `monkeypatch.setenv("SystemRoot", ...)` is readable back only as
        # `SYSTEMROOT` — asserting the mixed-case spelling failed on the one platform
        # these entries exist for. (That upper-casing is exactly the bug the
        # allowlist fold fixes; see `TestMinimalEnvHonorsWindowsCaseInsensitivity`.)
        folded = {k.upper(): v for k, v in env.items()}
        assert folded["SYSTEMROOT"] == r"C:\Windows"
        assert folded["USERPROFILE"] == r"C:\Users\me"
        assert folded["TEMP"] == r"C:\Temp"

    def test_widening_the_allowlist_did_not_admit_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows keys are location hints; the scrub property must be unchanged."""
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_reach_the_compiler")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "nope")
        env = latex._base_env({})
        assert "GITHUB_TOKEN" not in env
        assert "AWS_SESSION_TOKEN" not in env


class TestNeedsBibtex:
    def test_true_for_a_citation(self, project: Path) -> None:
        aux = project / "main.aux"
        aux.write_text("\\citation{smith}", encoding="utf-8")
        assert latex._needs_bibtex(aux) is True

    @pytest.mark.parametrize("marker", ["\\bibdata{refs}", "\\bibstyle{plainnat}"])
    def test_true_for_bibdata_or_bibstyle(self, project: Path, marker: str) -> None:
        aux = project / "main.aux"
        aux.write_text(marker, encoding="utf-8")
        assert latex._needs_bibtex(aux) is True

    def test_false_without_a_bibliography(self, project: Path) -> None:
        aux = project / "main.aux"
        aux.write_text("\\relax", encoding="utf-8")
        assert latex._needs_bibtex(aux) is False

    def test_false_when_the_aux_is_absent(self, project: Path) -> None:
        assert latex._needs_bibtex(project / "absent.aux") is False


class TestFindBibtex:
    def test_prefers_the_binary_beside_the_compiler(self) -> None:
        """A userspace TeX Live install's bibtex is not on PATH."""
        with mock.patch.object(latex.os.path, "isfile", return_value=True), \
                mock.patch.object(latex.os, "access", return_value=True):
            found = latex._find_bibtex("/home/u/texlive/2026/bin/x86_64-linux/pdflatex")
        # Compared as a Path, not a literal string: `_find_bibtex` builds the
        # sibling with pathlib, so the separator is the host's and a hardcoded
        # "/" assertion fails on Windows for a completely correct answer.
        assert Path(found or "") == Path("/home/u/texlive/2026/bin/x86_64-linux") / (
            latex._BIBTEX_BASENAME
        )

    def test_the_sibling_probe_uses_the_platform_executable_name(self) -> None:
        """The sibling probe is a bare `stat`, so unlike `shutil.which` it does
        NOT apply Windows' PATHEXT — it must ask for `bibtex.exe` by name there
        or a Windows TeX install's own bibtex is silently never found."""
        expected = "bibtex.exe" if os.name == "nt" else "bibtex"
        assert latex._BIBTEX_BASENAME == expected
        probed: list[str] = []

        def isfile(path: str) -> bool:
            probed.append(path)
            return True

        with mock.patch.object(latex.os.path, "isfile", side_effect=isfile), \
                mock.patch.object(latex.os, "access", return_value=True):
            latex._find_bibtex(str(Path("/opt/tex/bin") / "pdflatex"))
        assert Path(probed[0]).name == expected

    def test_falls_back_to_path(self) -> None:
        def isfile(path: str) -> bool:
            return "texlive" not in path

        with mock.patch.object(latex.os.path, "isfile", side_effect=isfile), \
                mock.patch.object(latex.os, "access", return_value=True), \
                mock.patch.object(latex.shutil, "which", return_value="/usr/bin/bibtex"):
            assert latex._find_bibtex("/home/u/texlive/2026/bin/x86_64-linux/pdflatex") == "/usr/bin/bibtex"

    def test_returns_none_when_absent(self) -> None:
        with mock.patch.object(latex.os.path, "isfile", return_value=False), \
                mock.patch.object(latex.shutil, "which", return_value=None):
            assert latex._find_bibtex("/usr/bin/pdflatex") is None


@pytest.mark.asyncio
class TestCapturedOutputIsBounded:
    """The compiler's output must not be buffered without a bound.

    `MAX_LOG_CHARS` limits what is DISPLAYED, not what is accumulated getting there:
    `communicate()` buffers both streams whole, and a `.tex` file decides how much
    its compiler prints (`\\message` in a loop, `\\tracingall`, a package erroring
    once per line). With a 120s compile timeout that is a long window to write to
    memory at pipe speed — inside the gateway's own process.
    """

    @staticmethod
    async def _reader(payload: bytes) -> asyncio.StreamReader:
        """A real `StreamReader` at EOF holding *payload*.

        A real one, not a mock: the capping path is guarded on
        `isinstance(..., asyncio.StreamReader)`, so a double would silently take the
        `communicate()` fallback and the test would assert nothing.
        """
        stream = asyncio.StreamReader()
        stream.feed_data(payload)
        stream.feed_eof()
        return stream

    async def test_output_past_the_cap_is_discarded(self) -> None:
        cap = 1024
        proc = mock.AsyncMock()
        proc.stdout = await self._reader(b"o" * (cap * 4))
        proc.stderr = await self._reader(b"e" * (cap * 4))

        out, err = await procio.read_capped(proc, cap)

        assert len(out) == cap, "stdout was not capped"
        assert len(err) == cap, "stderr was not capped"
        # The retained bytes are the real prefix, not a placeholder.
        assert out == b"o" * cap
        # Reaped, or the child is left a zombie holding its pipes.
        assert proc.wait.await_count == 1

    async def test_output_under_the_cap_is_returned_whole(self) -> None:
        proc = mock.AsyncMock()
        proc.stdout = await self._reader(b"short out")
        proc.stderr = await self._reader(b"short err")

        out, err = await procio.read_capped(proc, latex.MAX_CAPTURED_OUTPUT_BYTES)

        assert out == b"short out"
        assert err == b"short err"

    async def test_both_pipes_are_drained_concurrently(self) -> None:
        """The property `communicate()` provides and a sequential read destroys.

        Draining stdout to EOF first deadlocks the moment the child fills the stderr
        pipe buffer and blocks — so this pins that stderr is consumed even while
        stdout is still delivering.
        """
        cap = latex.MAX_CAPTURED_OUTPUT_BYTES
        stdout = asyncio.StreamReader()
        stderr = await self._reader(b"from stderr")
        proc = mock.AsyncMock()
        proc.stdout = stdout
        proc.stderr = stderr

        task = asyncio.create_task(procio.read_capped(proc, cap))
        # stdout stays open briefly; a sequential implementation would be parked on
        # it and could not have finished stderr.
        await asyncio.sleep(0)
        stdout.feed_data(b"from stdout")
        stdout.feed_eof()

        out, err = await asyncio.wait_for(task, timeout=5)
        assert out == b"from stdout"
        assert err == b"from stderr"

    async def test_the_cap_is_generous_relative_to_what_is_displayed(self) -> None:
        """An OOM backstop, not a display limit: the diagnostics parser needs a real
        tail, so the byte cap must stay well above the char cap it feeds."""
        assert latex.MAX_CAPTURED_OUTPUT_BYTES > latex.MAX_LOG_CHARS * 10

    async def test_an_unrecognized_stdio_shape_falls_back(self) -> None:
        """Without this, an object whose `read()` never signals EOF spins the drain
        loop forever inside the gateway."""
        proc = mock.AsyncMock()
        proc.stdout = object()
        proc.stderr = object()
        proc.communicate = mock.AsyncMock(return_value=(b"via communicate", b""))

        out, err = await procio.read_capped(proc, 10)
        assert out == b"via communicate"
        assert proc.communicate.await_count == 1

    async def test_run_actually_uses_the_capped_reader(self) -> None:
        """The wiring, not just the helper.

        The unit tests above call `_read_capped` directly, so they all still pass if
        `_run` goes back to a bare `communicate()` — which is exactly the regression
        to prevent. An AST check because the alternative (a real subprocess emitting
        4MB) is far too slow for the per-commit gate.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(latex))
        run = next(
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "_run"
        )
        body = ast.dump(run)
        assert "read_capped" in body, (
            "_run must drain the compiler's pipes through procio.read_capped; a bare "
            "communicate() buffers both streams with no bound"
        )
        assert "attr='communicate'" not in body, (
            "_run calls communicate() directly again — that is the unbounded read"
        )


class TestSensitivePathsFollowTheLiveDataHome:
    """`sensitive_home_dirs()` names paths relative to `$HOME`, so its `.kiro/crew/*`
    entries describe the DEFAULT data home.

    With `KIROCREW_HOME` pointed elsewhere — a dev instance, a pod, an operator who
    moved it — the real `sel_hmac.key`, `token_signing.key`, `.local_secret` and
    `security_policy.json` live somewhere that list never mentions. Nothing hid them, so
    a hostile `.tex` could `\\verbatiminput` any of them into the rendered PDF.
    """

    def test_a_custom_data_home_is_covered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "moved-home"))
        from kiro_crew.config.loader import config_dir

        data_home = str(config_dir())
        hidden = latex._sensitive_hidden_dirs()

        covered = [h for h in hidden if h.startswith(data_home)]
        assert covered, f"nothing hides the live data home {data_home}"
        # The files that actually matter, by name.
        names = {Path(h).name for h in covered}
        for secret in (
            "sel_hmac.key",
            "token_signing.key",
            ".local_secret",
            "security_policy.json",
        ):
            assert secret in names, f"{secret} is not hidden under a custom data home"

    def test_the_default_home_entries_are_kept_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both anchors, not one instead of the other: the default location may still
        hold files from before the move, and hiding an absent path is free."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "moved-home"))
        hidden = latex._sensitive_hidden_dirs()
        # Compared by BASENAME under the real home, not by a reconstructed path.
        # `sensitive_home_dirs()` returns POSIX-style relatives, so the default-anchored
        # entries keep their forward slashes while `os.path.join` produces backslashes on
        # Windows — the reconstruction matched on POSIX and failed the Windows shard.
        home = os.path.expanduser("~")
        default_anchored = [h for h in hidden if h.startswith(home) and "sel_hmac.key" in h]
        assert default_anchored, "the default-home entries were dropped"

    def test_no_duplicates_when_the_home_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no override the two anchors coincide, and the list must not double."""
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        hidden = latex._sensitive_hidden_dirs()
        assert len(hidden) == len(set(hidden))


@pytest.mark.asyncio
class TestCompileRefusesSymlinkedArtifacts:
    """The compiler opens its outputs for writing BY NAME, and a cloned repository can
    ship any of those names as a link.

    `main.pdf -> main.tex` makes pdflatex TRUNCATE THE USER'S SOURCE the moment it opens
    its output — the document destroyed by compiling it, with no error and nothing to
    recover from. A link pointing outside the project is the same write aimed anywhere
    the gateway can reach.

    Not fixable downstream: `pdf_path`'s containment decides what may be SERVED, and by
    then the write has happened. The only place to stop it is before the spawn.
    """

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    async def test_a_pdf_symlinked_to_the_source_refuses_to_compile(
        self, project: Path
    ) -> None:
        os.symlink(project / "main.tex", project / "main.pdf")
        source_before = (project / "main.tex").read_text(encoding="utf-8")

        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", mock.AsyncMock()) as run:
            result = await latex.compile_project(project, "main.tex")

        assert result.ok is False
        assert "symbolic links" in result.log
        assert "main.pdf" in result.log
        # The compiler was never spawned, and the source is untouched.
        assert not run.called, "the compiler ran despite a symlinked output"
        assert (project / "main.tex").read_text(encoding="utf-8") == source_before

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    async def test_a_link_at_a_name_no_suffix_list_predicts_is_refused(
        self, project: Path
    ) -> None:
        """The guard must not be keyed on the names the app expects.

        Two earlier versions enumerated suffixes — a hand-written tuple of 8, then the 17
        derived from `store.ARTIFACT_SUFFIXES` — and both were unsound for the same
        reason: the DOCUMENT picks the filename. `\\openout\\ch=chapter1` writes
        `chapter1.tex`, and no suffix list contains that. This is the case a name list
        cannot reach by construction, which is why the guard now refuses on ANY link.
        """
        os.symlink(project / "main.tex", project / "chapter1.tex")
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", mock.AsyncMock()) as run:
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert "chapter1.tex" in result.log
        assert not run.called, "the compiler ran despite an in-project symlink"

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    async def test_a_link_in_a_subdirectory_is_refused(self, project: Path) -> None:
        """The walk descends: `\\openout` takes a relative path, so `sections/x.tex` is a
        write the compiler can aim just as well as one at the project root."""
        (project / "sections").mkdir()
        os.symlink(project / "main.tex", project / "sections" / "linked.tex")
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", mock.AsyncMock()) as run:
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert "sections/linked.tex" in result.log
        assert not run.called

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    async def test_a_symlink_cycle_does_not_hang_the_walk(self, project: Path) -> None:
        """`a -> ..` is a link, so it is reported rather than descended into. A walk that
        followed it would recurse forever and wedge the request."""
        os.symlink(project, project / "loop")
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", mock.AsyncMock()) as run:
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert not run.called

    async def test_the_walk_is_bounded(self, project: Path) -> None:
        """Shares `list_files`'s ceiling rather than introducing a second number. An
        unbounded walk over a pathological tree would stall the compile request."""
        assert store.MAX_PROJECT_FILES > 0
        for i in range(12):
            (project / f"f{i}.tex").write_text("x", encoding="utf-8")
        with mock.patch.object(store, "MAX_PROJECT_FILES", 3):
            links, complete = latex._symlinked_artifacts(project)
            # Bounded — and it SAYS the answer is partial rather than looking clean.
            assert links == []
            assert complete is False

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    async def test_an_exhausted_budget_reports_an_incomplete_scan(
        self, project: Path
    ) -> None:
        """The first version of this returned a bare list, so "did not finish looking"
        and "found nothing" were the same value — and the caller read the former as the
        latter."""
        for i in range(12):
            (project / f"aaa{i:03d}.tex").write_text("x", encoding="utf-8")
        os.symlink(project / "main.tex", project / "zzz.pdf")
        with mock.patch.object(store, "MAX_PROJECT_FILES", 4):
            _links, complete = latex._symlinked_artifacts(project)
        assert complete is False, "an unfinished scan reported itself as complete"
        # With the real budget the same tree is scanned to the end and the link is seen.
        links, complete = latex._symlinked_artifacts(project)
        assert complete is True
        assert "zzz.pdf" in links

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    async def test_compile_refuses_when_the_scan_could_not_finish(
        self, project: Path
    ) -> None:
        """The attack the bound created: pad a cloned repo with more than
        `MAX_PROJECT_FILES` earlier-sorting files and the budget is gone before the walk
        reaches `main.pdf -> main.tex`. Treating that as "no links found" is the one way
        this guard can be defeated without defeating anything — so it must REFUSE.
        """
        for i in range(12):
            (project / f"aaa{i:03d}.tex").write_text("x", encoding="utf-8")
        os.symlink(project / "main.tex", project / "zzz-main.pdf")
        with mock.patch.object(store, "MAX_PROJECT_FILES", 4), mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", mock.AsyncMock()) as run:
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert not run.called, "the compiler ran despite an unfinished symlink scan"
        assert (project / "main.tex").read_text(encoding="utf-8") == r"\documentclass{article}"

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    async def test_a_git_checkout_does_not_trip_the_guard(self, project: Path) -> None:
        """`.git` is skipped. A real checkout can hold links in there, and refusing to
        compile every cloned project would make the guard unusable."""
        (project / ".git" / "objects").mkdir(parents=True)
        os.symlink(project / "main.tex", project / ".git" / "objects" / "link")
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", mock.AsyncMock(return_value=(0, ""))) as run:
            await latex.compile_project(project, "main.tex")
        assert run.called, "a link inside .git blocked the compile"

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    @pytest.mark.parametrize(
        "suffix", [".aux", ".log", ".bbl", ".synctex.gz", ".nav", ".lof", ".fls"]
    )
    async def test_every_generated_name_is_checked(
        self, project: Path, suffix: str
    ) -> None:
        """Not just the PDF: the compiler writes all of these, so a link at any one of
        them is the same overwrite."""
        os.symlink(project / "main.tex", project / f"main{suffix}")
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", mock.AsyncMock()) as run:
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert not run.called

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
    async def test_a_dangling_symlink_is_also_refused(self, project: Path) -> None:
        """`is_symlink()` does not follow the link, which is correct here: the compiler
        would CREATE the target wherever it points."""
        os.symlink(project / "does-not-exist.tex", project / "main.pdf")
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", mock.AsyncMock()) as run:
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert not run.called

    async def test_an_ordinary_pdf_still_compiles(self, project: Path) -> None:
        """A REGULAR generated file is the normal case — every recompile overwrites one,
        so the refusal must be scoped to links only."""
        (project / "main.pdf").write_bytes(b"%PDF-1.4 previous build")
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "_run", mock.AsyncMock(return_value=(0, "ok"))) as run:
            await latex.compile_project(project, "main.tex")
        assert run.called, "a regular pre-existing PDF must not block the compile"


class TestCompilerCacheIsRevalidated:
    """The cache is process-wide and never expires.

    A compiler removed after it was found — a `brew uninstall`, a TeX Live upgrade
    relocating the binary, a managed install deleted by hand — left the stale path in
    place, and the next compile spawned a binary that is not there: an `OSError` out of
    the transport and a 500, instead of the actionable "no compiler found" message this
    function exists to produce.
    """

    def test_a_vanished_cached_compiler_is_re_probed(self, tmp_path: Path) -> None:
        gone = tmp_path / "pdflatex"          # never created
        latex.reset_compiler_cache()
        with mock.patch.object(latex, "_compiler_cache", str(gone)):
            with mock.patch.object(latex.shutil, "which", return_value=None), mock.patch(
                "glob.glob", return_value=[]
            ):
                assert latex.find_compiler_sync() is None

    def test_a_still_present_cached_compiler_is_returned_without_probing(
        self, tmp_path: Path
    ) -> None:
        """The revalidation is a `stat`, not a re-probe: PATH and the userspace globs
        must not be walked on every call."""
        real = tmp_path / "pdflatex"
        real.write_text("#!/bin/sh", encoding="utf-8")
        real.chmod(0o755)
        latex.reset_compiler_cache()
        with mock.patch.object(latex, "_compiler_cache", str(real)):
            with mock.patch.object(latex.shutil, "which") as which:
                assert latex.find_compiler_sync() == str(real)
            assert not which.called, "PATH was re-walked for a still-valid cache entry"

    def test_the_negative_answer_is_not_re_probed(self) -> None:
        """Re-probing the negative would walk PATH and every glob on each call. It is
        cleared explicitly by `reset_compiler_cache` after a provision instead."""
        latex.reset_compiler_cache()
        with mock.patch.object(latex, "_compiler_cache", ""):
            with mock.patch.object(latex.shutil, "which") as which:
                assert latex.find_compiler_sync() is None
            assert not which.called


class TestOpenoutIsConfinedToTheProject:
    """`\\openout` is how a document writes a file, and it is NOT a shell escape — so
    `-no-shell-escape` does not bound it. TeX bounds it with `openout_any`.

    pdflatex already defaults to `p` ("paper", confined to the CWD subtree), but that
    default lives in the host's `texmf.cnf` and an operator or a bundled TeX Live can set
    `a` ("any"). Relying on it means our containment depends on a file we do not control;
    the env var overrides `texmf.cnf`, which makes the guarantee ours.
    """

    def test_the_compiler_env_confines_writes_and_reads(self) -> None:
        env = latex._base_env({})
        assert env["openout_any"] == "p"
        # The read direction too — the `\input{../../.aws/credentials}` case. Two
        # independent layers: the sandbox hides known-sensitive paths, this bounds
        # everything outside the project.
        assert env["openin_any"] == "p"

    def test_it_does_not_replace_the_symlink_refusal(self) -> None:
        """`openout_any` is enforced on the path TeX is GIVEN, and an in-project symlink
        is a legitimate path inside the subtree whose target is elsewhere. The two guards
        cover the two halves, so neither may be dropped for the other."""
        env = latex._base_env({})
        assert env["openout_any"] == "p"
        # The refusal is reachable and refuses — `openout_any` did not replace it.
        assert callable(latex._symlinked_artifacts)


class TestSandboxRefusalIsReportedNotSwallowed:
    """A host with no sandbox backend must say so, not answer "internal error".

    ``wrap_argv`` fails closed when it cannot build an OS-level sandbox, and that
    is CORRECT for this spawn: the compiler reads an untrusted ``.tex`` that may
    have arrived by ``git clone``, and ``strict`` mode is what stops
    ``\\input{../../.aws/credentials}`` from typesetting the operator's keys into
    the PDF. So these tests pin that the refusal is *translated*, never bypassed.

    Windows is the host that makes this reachable on every compile: it has no
    sandbox backend at all (user namespaces are Linux, ``sandbox-exec`` is macOS),
    so before this the app's every compile raised an unhandled 500 that named no
    remedy — even though ``docs/WINDOWS_CHANGES.md`` documents the one config flag
    that fixes it.
    """

    @pytest.mark.asyncio
    async def test_compile_reports_the_refusal_as_a_result(self, project: Path) -> None:
        boom = sandbox.SandboxUnavailableError(
            "no backend here", "no_backend", "simulated: no sandbox backend"
        )
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "sandboxed_spawn_argv", side_effect=boom):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        # Carried in its OWN field, so the route layer can answer with a distinct
        # code instead of reporting an environment problem as a document problem.
        assert "no backend here" in result.sandbox_error
        # And NOT reported as a compile failure — `log` is what the UI renders as
        # compiler output, and there was no compiler output because none ran.
        assert result.log == ""

    @pytest.mark.asyncio
    async def test_the_refusal_is_not_bypassed(self, project: Path) -> None:
        """The strict-mode wrap is still requested — no silent downgrade on refusal."""
        wrap = mock.Mock(
            side_effect=sandbox.SandboxUnavailableError(
                "denied", "no_backend", "simulated: no sandbox backend"
            )
        )
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(latex, "sandboxed_spawn_argv", wrap):
            result = await latex.compile_project(project, "main.tex")
        assert result.ok is False
        assert wrap.call_count == 1
        # `strict`, not a weaker tier chosen to make the spawn succeed.
        assert _spawn_mode(wrap) == "strict"
