"""Papyrus — LaTeX compilation and compiler-log parsing.

Compilation is the app's one genuinely expensive operation: a paper with a
bibliography needs four compiler passes and can take tens of seconds. The
gateway runs everything on ONE asyncio loop, so **nothing here may block it** —
every child process is spawned with :func:`asyncio.create_subprocess_exec` and
awaited, and the two synchronous filesystem helpers (compiler discovery, the
``.bst``/``.bib`` search-path walk) are offloaded with :func:`asyncio.to_thread`
by their callers in this module.

Security notes that must not be relaxed:

* ``-no-shell-escape`` is passed **explicitly** on every pdflatex invocation.
  With shell escape enabled a ``\\write18{...}`` inside a ``.tex`` file is
  arbitrary command execution — and a ``.tex`` file here is untrusted content
  (the agent writes it, and a cloned repository supplies it wholesale). Tectonic
  does not enable shell escape unless asked (``-Z shell-escape``), and we never
  ask.
* The compiler spawn is routed through :func:`kiro_crew.sandbox.sandboxed_spawn_argv`
  — the OS-level sandbox + credential-scrubbed env chokepoint — and carries
  :func:`kiro_crew.sandbox.create_subprocess_limited`, so a runaway macro expansion
  gets a kernel-enforced ceiling instead of the host's whole memory.
* Every invocation is bounded by a wall-clock timeout and the process tree is
  killed on expiry.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.papyrus.backend import store, tectonic
from kiro_crew.apps.registry import minimal_env
from kiro_crew.sandbox import create_subprocess_limited, sandboxed_spawn_argv
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.papyrus")

#: Compilers we know how to drive, in preference order.
COMPILER_NAMES = ("pdflatex", "tectonic")

#: Userspace install locations probed when neither compiler is on ``PATH``. A
#: TeX Live install script (the usual no-sudo route) lands under ``~/texlive``.
USERSPACE_COMPILER_GLOBS = (
    "~/texlive/*/bin/*/pdflatex",
    "~/.local/bin/tectonic",
)

#: Wall-clock ceiling for one compiler pass. A large thesis legitimately takes
#: tens of seconds; beyond this the run is a wedge, not slow work.
COMPILE_TIMEOUT_SEC = 120.0

#: Wall-clock ceiling for one bibtex pass (much cheaper than a LaTeX pass).
BIBTEX_TIMEOUT_SEC = 60.0

#: The bibliography processor, as a ``PATH`` lookup name and as an on-disk file
#: name. The two differ on Windows: :func:`shutil.which` applies ``PATHEXT`` and
#: so resolves the bare name, but the compiler-local probe in
#: :func:`_find_bibtex` is a plain ``stat`` that does not — it must ask for
#: ``bibtex.exe`` by name or a Windows TeX install's own bibtex is never found.
_BIBTEX_NAME = "bibtex"
_BIBTEX_BASENAME = f"{_BIBTEX_NAME}.exe" if platform_compat.IS_WINDOWS else _BIBTEX_NAME

#: The ``log`` a compile carries when the host has no compiler at all. Points at
#: the one-click managed install first (``POST /compiler/provision``, which the UI
#: offers as a button) and keeps the manual routes as the fallback for a host with
#: no pinned build — see :mod:`.tectonic`.
NO_COMPILER_LOG = (
    "No LaTeX compiler found. Install the bundled Tectonic compiler from the "
    "Papyrus page, or install TeX Live (pdflatex) or tectonic yourself."
)

#: How much of the compiler's output is kept and parsed. The tail is where the
#: errors are; the head is banner noise.
MAX_LOG_CHARS = 20000

#: Cap on parsed diagnostics returned to the client. A broken preamble can emit
#: thousands of near-identical warnings; the list is a UI affordance, not a log.
MAX_DIAGNOSTICS = 200

#: How far past a ``! error`` line we look for its ``l.<n>`` line reference.
_BANG_CONTEXT_CHARS = 600

#: Environment variables a LaTeX child legitimately needs beyond the minimal base.
_LATEX_ENV_PASSTHROUGH = ("TEXMFHOME", "TEXMFVAR", "TEXMFCONFIG", "TEXINPUTS", "SOURCE_DATE_EPOCH")

_DIAGNOSTIC_ERROR = "error"
_DIAGNOSTIC_WARNING = "warning"
_DIAGNOSTIC_TYPESETTING = "typesetting"

# Compiled once — these run over every compile's log tail.
_RE_FILE_LINE = re.compile(r"^([^\s:]+\.\w+):(\d+):\s*(.+?)$", re.MULTILINE)
_RE_BANG = re.compile(r"^!\s+(.+?)$", re.MULTILINE)
_RE_BANG_LINE = re.compile(r"l\.(\d+)")
_RE_WARNING = re.compile(r"^(?:LaTeX|Package \S+)\s+Warning:\s*(.+?)$", re.MULTILINE)
_RE_WARNING_LINE = re.compile(r"(?:on input line|line)\s+(\d+)")
_RE_BOX = re.compile(r"^((?:Over|Under)full \\[hv]box .+?) at lines? (\d+)", re.MULTILINE)
_RE_RERUN = re.compile(r"Rerun to get|Label\(s\) may have changed")

_compiler_cache: str | None = None


@dataclass(frozen=True)
class Diagnostic:
    """One parsed compiler message."""

    level: str
    message: str
    line: int | None = None
    file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "message": self.message,
            "line": self.line,
            "file": self.file,
        }


@dataclass
class CompileResult:
    """Outcome of one compile request."""

    ok: bool
    log: str = ""
    diagnostics: list[Diagnostic] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "log": self.log,
            "errors": [d.to_dict() for d in self.diagnostics],
            "duration_ms": self.duration_ms,
        }


def _usable(path: str) -> bool:
    """True when *path* is a regular, executable file we can spawn."""
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_compiler_sync() -> str | None:
    """Locate a LaTeX compiler. Synchronous — call via :func:`asyncio.to_thread`.

    Resolution order, widest-trust first:

    1. ``PATH`` — ``pdflatex``, then ``tectonic``;
    2. the userspace install locations (:data:`USERSPACE_COMPILER_GLOBS`);
    3. the app's own **managed** Tectonic install (:mod:`.tectonic`), which the
       user provisions from the UI when the host has no TeX at all.

    The managed install is probed LAST on purpose: a user who has installed a
    real TeX distribution must keep using it, so provisioning a managed copy can
    never displace their ``pdflatex``. The result is cached process-wide —
    including the negative — so a successful provision MUST call
    :func:`reset_compiler_cache` or the stale "no compiler" answer sticks.
    """
    global _compiler_cache
    if _compiler_cache is not None:
        return _compiler_cache or None
    found = ""
    for name in COMPILER_NAMES:
        candidate = shutil.which(name)
        if _usable(candidate or ""):
            found = candidate or ""
            break
    if not found:
        for pattern in USERSPACE_COMPILER_GLOBS:
            matches = sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)
            if matches and _usable(matches[0]):
                found = matches[0]
                break
    if not found and tectonic.binary_installed():
        found = str(tectonic.binary_path())
    _compiler_cache = found
    return found or None


def reset_compiler_cache() -> None:
    """Forget the cached compiler path (tests, and after a compiler install)."""
    global _compiler_cache
    _compiler_cache = None


async def find_compiler() -> str | None:
    """Locate a LaTeX compiler off the event loop."""
    return await asyncio.to_thread(find_compiler_sync)


def _search_path_env_sync(project: Path) -> dict[str, str]:
    """Build ``BSTINPUTS``/``BIBINPUTS`` covering every project subfolder.

    Conference templates stash ``acl_natbib.bst`` under ``templates/<conf>/``
    rather than at the project root, and bibtex then fails with "I couldn't open
    style file". Extending the search path with every directory that holds a
    ``.bst``/``.bib`` reproduces what a hosted LaTeX service does implicitly. The
    trailing colon means "also search the default TEXMF tree".

    Synchronous ``rglob`` over the project — call via :func:`asyncio.to_thread`.
    """

    def dirs_for(pattern: str) -> str:
        found = sorted({str(p.parent) for p in project.rglob(pattern) if p.is_file()})
        return ".:" + ":".join(found) + ":" if found else ".:"

    return {"BSTINPUTS": dirs_for("*.bst"), "BIBINPUTS": dirs_for("*.bib")}


def _base_env(extra: dict[str, str]) -> dict[str, str]:
    """A minimal environment for a LaTeX child, plus TeX-specific passthrough.

    Deliberately NOT the gateway's whole environment: unrelated secrets must
    never reach a child running untrusted document content.
    """
    passthrough = {k: os.environ[k] for k in _LATEX_ENV_PASSTHROUGH if k in os.environ}
    return minimal_env(**passthrough, **extra)


def _audit(operation: str, target: str, outcome: str, *, error: str = "") -> None:
    """SEL event for every compiler spawn. Fire-and-forget."""
    sel().log_api_access(
        caller="core:papyrus",
        operation=f"papyrus.{operation}",
        outcome=outcome,
        source="builtin-app",
        resources=target[:200],
        error=error[:200] if error else "",
    )


async def _run(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float, operation: str
) -> tuple[int, str]:
    """Spawn *argv* under the sandbox chokepoint and await it.

    Returns ``(returncode, combined_output)``. On timeout the whole process tree
    is killed and ``(-1, "")`` is returned — the caller reports a timeout rather
    than an empty success.
    """
    wrapped, scrubbed, cleanup = sandboxed_spawn_argv(argv, env=env)
    proc: asyncio.subprocess.Process | None = None
    try:
        # `create_subprocess_limited`, not `create_subprocess_exec` +
        # `preexec_fn`: a post-fork preexec forks the threaded gateway and runs
        # Python in the child before exec. The shim applies the same limits
        # AFTER exec, where the process is single-threaded.
        proc = await create_subprocess_limited(
            *wrapped,
            cwd=str(cwd),
            env=scrubbed,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            try:
                await platform_compat.kill_process_tree_async(
                    proc.pid, platform_compat.SIGKILL
                )
            except (ProcessLookupError, OSError, ValueError):
                logger.debug("papyrus: %s already gone before kill", operation)
            # Reap so the child is not left a zombie holding its pipes.
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                logger.warning("papyrus: %s did not exit after SIGKILL", operation)
        _audit(operation, argv[0], "failure", error=f"timeout after {timeout}s")
        return -1, ""
    except OSError as exc:
        _audit(operation, argv[0], "failure", error=str(exc))
        raise
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)
    _audit(operation, argv[0], "ok" if proc.returncode == 0 else "failure")
    combined = (stdout or b"").decode("utf-8", "replace") + (stderr or b"").decode("utf-8", "replace")
    return proc.returncode or 0, combined


def parse_log(log_text: str) -> list[Diagnostic]:
    """Parse a LaTeX log tail into structured diagnostics.

    Four shapes, in the order a reader cares about them:

    1. ``file:line: message`` — the most reliable form (pdflatex with
       ``-file-line-error``, and the stex-family compilers by default).
    2. ``! message`` followed by an ``l.<n>`` line reference. The lookup is
       bounded to the text before the NEXT ``!`` line so consecutive errors
       cannot borrow each other's line number.
    3. ``LaTeX Warning`` / ``Package <p> Warning``, with the line embedded in the
       message text when present.
    4. Over/underfull boxes — typesetting hints, never fatal.
    """
    out: list[Diagnostic] = []

    for match in _RE_FILE_LINE.finditer(log_text):
        out.append(
            Diagnostic(
                level=_DIAGNOSTIC_ERROR,
                message=match.group(3).strip(),
                line=int(match.group(2)),
                file=match.group(1),
            )
        )

    bangs = list(_RE_BANG.finditer(log_text))
    for index, match in enumerate(bangs):
        next_start = bangs[index + 1].start() if index + 1 < len(bangs) else len(log_text)
        block = log_text[match.end() : min(next_start, match.end() + _BANG_CONTEXT_CHARS)]
        line_match = _RE_BANG_LINE.search(block)
        out.append(
            Diagnostic(
                level=_DIAGNOSTIC_ERROR,
                message=match.group(1).strip(),
                line=int(line_match.group(1)) if line_match else None,
            )
        )

    for match in _RE_WARNING.finditer(log_text):
        message = match.group(1).strip()
        line_match = _RE_WARNING_LINE.search(message)
        out.append(
            Diagnostic(
                level=_DIAGNOSTIC_WARNING,
                message=message,
                line=int(line_match.group(1)) if line_match else None,
            )
        )

    for match in _RE_BOX.finditer(log_text):
        out.append(
            Diagnostic(
                level=_DIAGNOSTIC_TYPESETTING,
                message=match.group(1),
                line=int(match.group(2)),
            )
        )

    return out[:MAX_DIAGNOSTICS]


def _compiler_argv(compiler: str, tex: Path, project: Path) -> list[str]:
    """Build the compiler argv for one pass.

    ``-no-shell-escape`` is explicit and MUST stay: with shell escape on, a
    ``\\write18`` in an untrusted ``.tex`` is arbitrary command execution.
    Tectonic keeps shell escape off unless ``-Z shell-escape`` is passed, and we
    never pass it.
    """
    if "tectonic" in os.path.basename(compiler):
        return [compiler, "--keep-logs", "--", str(tex)]
    return [
        compiler,
        "-interaction=nonstopmode",
        "-no-shell-escape",
        "-file-line-error",
        "-output-directory",
        str(project),
        "--",
        str(tex),
    ]


async def compile_project(project: Path, main_file: str) -> CompileResult:
    """Compile *main_file* inside *project* and return the parsed outcome.

    Runs the standard bibliography cycle when the first pass shows the document
    cites anything: ``pdflatex -> bibtex -> pdflatex -> pdflatex``. The first
    pass writes ``\\citation``/``\\bibdata`` into the ``.aux``; bibtex turns those
    into a formatted ``.bbl``; the last two passes integrate it and resolve the
    ``\\cite`` references. Tectonic drives that cycle itself, so it is skipped
    there. Without a bibliography we still re-run once when the log asks
    ("Rerun to get..."), which is how a table of contents or a ``\\ref`` settles.
    """
    tex = project / main_file
    if not tex.is_file():
        return CompileResult(ok=False, log=f"{main_file} not found")

    compiler = await find_compiler()
    if not compiler:
        return CompileResult(ok=False, log=NO_COMPILER_LOG)

    search_env = await asyncio.to_thread(_search_path_env_sync, project)
    env = _base_env(search_env)
    argv = _compiler_argv(compiler, tex, project)
    is_tectonic = "tectonic" in os.path.basename(compiler)
    aux_stem = Path(main_file).stem
    aux_path = project / f"{aux_stem}.aux"

    start = time.monotonic()
    code, output = await _run(
        argv, cwd=project, env=env, timeout=COMPILE_TIMEOUT_SEC, operation="compile"
    )
    if code == -1 and not output:
        return CompileResult(ok=False, log=f"Compilation timed out after {COMPILE_TIMEOUT_SEC:.0f}s")

    ran_bibtex = False
    if not is_tectonic:
        # Order matters for cost, not correctness: reading the .aux is one small
        # file read, while locating bibtex probes the filesystem and may fall back
        # to a PATH scan. A paper with no bibliography — the common case — should
        # pay neither, so the cheap question is asked first.
        needs_bib = await asyncio.to_thread(_needs_bibtex, aux_path)
        bibtex_bin = await asyncio.to_thread(_find_bibtex, compiler) if needs_bib else None
        if bibtex_bin:
            await _run(
                [bibtex_bin, "--", aux_stem],
                cwd=project,
                env=env,
                timeout=BIBTEX_TIMEOUT_SEC,
                operation="bibtex",
            )
            await _run(argv, cwd=project, env=env, timeout=COMPILE_TIMEOUT_SEC, operation="compile")
            code, output = await _run(
                argv, cwd=project, env=env, timeout=COMPILE_TIMEOUT_SEC, operation="compile"
            )
            ran_bibtex = True

    if not is_tectonic and not ran_bibtex and code == 0 and _RE_RERUN.search(output):
        code, output = await _run(
            argv, cwd=project, env=env, timeout=COMPILE_TIMEOUT_SEC, operation="compile"
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    log_tail = output[-MAX_LOG_CHARS:]
    pdf = store.pdf_path(project, main_file)
    ok = code == 0 and await asyncio.to_thread(pdf.is_file)
    return CompileResult(
        ok=ok, log=log_tail, diagnostics=parse_log(log_tail), duration_ms=duration_ms
    )


def _find_bibtex(compiler: str) -> str | None:
    """Locate ``bibtex``, preferring the one beside the chosen compiler.

    The sibling probe is an explicit ``stat``, not a ``PATH`` lookup, so unlike
    :func:`shutil.which` it does NOT apply Windows' ``PATHEXT`` — a bare
    ``bibtex`` never matches ``bibtex.exe``. The executable suffix is therefore
    added by hand on Windows, or a MiKTeX/TeX Live install there would fall
    through to the ``PATH`` lookup and silently lose the compiler-local bibtex
    that the whole function exists to prefer.

    Synchronous — call via :func:`asyncio.to_thread`.
    """
    sibling = str(Path(compiler).parent / _BIBTEX_BASENAME)
    if _usable(sibling):
        return sibling
    found = shutil.which(_BIBTEX_NAME)
    return found if _usable(found or "") else None


def _needs_bibtex(aux_path: Path) -> bool:
    """True when the ``.aux`` shows the document has a bibliography.

    Synchronous file read — call via :func:`asyncio.to_thread`.
    """
    if not aux_path.is_file():
        return False
    try:
        aux = aux_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "\\citation" in aux or "\\bibdata" in aux or "\\bibstyle" in aux
