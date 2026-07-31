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
from pathlib import Path
from typing import Iterator
from unittest import mock

import pytest

from kiro_crew.apps.builtins.papyrus.backend import latex


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
    """Records every ``_run`` call and returns a scripted result."""

    def __init__(self, *, output: str = "", code: int = 0) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.output = output
        self.code = code

    async def __call__(self, argv, *, cwd, env, timeout, operation):  # noqa: ANN001
        self.calls.append((argv, operation))
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
        # Trailing colon = "also search the default TEXMF tree".
        assert env["BSTINPUTS"].endswith(":")

    def test_degrades_to_the_default_tree_when_nothing_is_found(self, project: Path) -> None:
        env = latex._search_path_env_sync(project)
        assert env["BSTINPUTS"] == ".:"
        assert env["BIBINPUTS"] == ".:"


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
