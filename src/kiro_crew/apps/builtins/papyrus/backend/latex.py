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
import functools
import glob
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat, security
from kiro_crew.apps.builtins.papyrus.backend import procio, store, tectonic
from kiro_crew.apps.registry import minimal_env
from kiro_crew.config.loader import config_dir
from kiro_crew.executors import subprocess_executor
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

#: Re-exported from `procio`, which owns the bound now that `gitops` shares it.
MAX_CAPTURED_OUTPUT_BYTES = procio.MAX_CAPTURED_OUTPUT_BYTES


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
        # REVALIDATE a positive answer. The cache is process-wide and never expires, so a
        # compiler removed after it was found (a `brew uninstall`, a TeX Live upgrade
        # relocating the binary, a managed install deleted by hand) left the stale path
        # in place — and the next compile spawned a binary that is not there, raising an
        # OSError out of the transport and answering 500 instead of the actionable
        # "no compiler found" message this function exists to produce.
        #
        # Only the POSITIVE answer is rechecked: `_usable` is a `stat` plus an
        # `access`, cheap next to a compile, whereas re-probing the negative would walk
        # PATH and every userspace glob on each call. The negative is still cleared
        # explicitly by `reset_compiler_cache` after a successful provision, which is
        # the one event that changes it.
        if _compiler_cache and not _usable(_compiler_cache):
            logger.info("papyrus: cached compiler %s is gone; re-probing", _compiler_cache)
            _compiler_cache = None
        else:
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
    # `openout_any=p` / `openin_any=p` are set EXPLICITLY, not left to the default.
    #
    # `\openout` is how a document writes a file, and it is not a shell escape — so
    # `-no-shell-escape` does not bound it. TeX bounds it with `openout_any`: `p`
    # ("paper") confines writes to the CWD subtree and forbids absolute paths and `..`.
    # pdflatex already defaults to `p`, but that default lives in the host's `texmf.cnf`
    # and an operator (or a bundled TeX Live) can set `a` ("any") — so relying on it
    # means our containment depends on a file we do not control. The env var overrides
    # `texmf.cnf`, which makes the guarantee ours.
    #
    # `openin_any=p` for the read direction, which is the `\input{../../.aws/credentials}`
    # case the sandbox hides paths for: two independent layers, since the sandbox covers
    # known-sensitive paths and this covers everything outside the project.
    #
    # This does NOT replace the symlink refusal in `compile_project`: `openout_any` is
    # enforced on the path TeX is given, and an in-project symlink is a legitimate path
    # inside the subtree whose TARGET is elsewhere. The two guards cover the two halves.
    return minimal_env(**passthrough, openout_any="p", openin_any="p", **extra)


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


def _sensitive_hidden_dirs() -> tuple[str, ...]:
    """The read+write-blocked home paths, as absolute dirs to hide from the child.

    Derived from :func:`security.sensitive_home_dirs` rather than listing leaves
    here, so a path added to that floor is hidden from the compiler automatically.
    That list is home-RELATIVE, and the sandbox's own ``_STRICT_DIRS`` are joined
    to ``$HOME`` the same way, so this resolves them identically.

    Resolved per call, not cached at import: ``KIROCREW_HOME`` is set per test and
    per dev instance, so a module-level snapshot would hide the wrong home.
    """
    home = os.path.expanduser("~")
    rels = security.sensitive_home_dirs()
    paths = [os.path.join(home, rel) for rel in rels]
    # RE-ANCHOR the data-home leaves under the LIVE data home as well.
    #
    # `sensitive_home_dirs()` returns paths relative to `$HOME`, so its `.kiro/crew/*`
    # entries name the DEFAULT data home. With `KIROCREW_HOME` pointed elsewhere — a dev
    # instance, a pod, an operator who moved it — the real `sel_hmac.key`,
    # `token_signing.key`, `.local_secret` and `security_policy.json` live somewhere the
    # list never mentions, so nothing hid them and a hostile `.tex` could
    # `\verbatiminput` any of them into the rendered PDF. Verified: with a custom home,
    # ZERO of the 52 entries covered it.
    #
    # Both are kept rather than swapping one for the other: the default location may
    # still hold files from before the move, and hiding a path that does not exist is
    # free (the launcher skips it, and a seatbelt rule for an absent path is inert).
    data_home = str(config_dir())
    prefix = f".kiro{os.sep}crew{os.sep}"
    for rel in rels:
        normalized = rel.replace("/", os.sep)
        if normalized.startswith(prefix):
            paths.append(os.path.join(data_home, normalized[len(prefix):]))
    return tuple(dict.fromkeys(paths))


async def _run(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float, operation: str
) -> tuple[int, str]:
    """Spawn *argv* under the sandbox chokepoint and await it.

    Returns ``(returncode, combined_output)``. On timeout the whole process tree
    is killed and ``(-1, "")`` is returned — the caller reports a timeout rather
    than an empty success.

    ``mode="strict"``, NOT the ``"standard"`` default. Standard mode deliberately
    leaves ``~/.aws`` and ``~/.ssh`` readable so git-over-SSH and the AWS CLI keep
    working — a TeX compiler needs neither, and the input it runs on is a ``.tex``
    file that may have arrived by ``git clone`` from anywhere. TeX can read a file
    and typeset its contents, so ``\\input{../../../../.aws/credentials}`` under
    standard mode would render the operator's keys into the output PDF. That is a
    read no ``-no-shell-escape`` can stop, because it is not a shell escape.

    Strict on its own is NOT enough. Its credential list covers third-party
    locations (``~/.aws``, ``~/.gnupg``, ``~/.config/gcloud``) plus
    ``~/.kiro/crew/.env`` — but not the REST of KiroCrew's own trust root. The
    gateway's ``.local_secret``, ``sel_hmac.key``, ``security_policy.json``,
    ``profiles/`` and the other keystone files sit beside it, and TeX reads files:
    ``\\verbatiminput{~/.kiro/crew/.local_secret}`` would typeset the gateway's own
    callback credential into the PDF. So :func:`_sensitive_hidden_dirs` adds the
    read+write floor to ``extra_hidden_dirs``.

    Hiding the data home WHOLESALE and re-exposing this app's subtree does not
    work, and quietly does nothing: ``_hidden_path_contains_visible_path`` DROPS a
    hidden entry that contains any ``extra_visible_dirs`` path, so naming the
    project tree as visible would have cancelled the hiding altogether. The
    entries are therefore the individual sensitive paths, which do not contain the
    app's own data dir.

    ``gitops`` keeps the standard mode on purpose — pushing over SSH is the one
    place the key material is the point.

    The chokepoint itself is called OFF the loop. ``sandboxed_spawn_argv`` →
    ``wrap_argv`` → ``detect_backend`` can cold-probe the sandbox backend with a
    synchronous ``subprocess.run(..., timeout=5)``, and on macOS nothing warms
    that cache first: ``prewarm_backend()`` returns early on non-Linux. So the
    first compile of the gateway's lifetime would stall the single loop — every
    chat session, cron tick and the liveness heartbeat — for up to five seconds.
    Linux is safe by construction (``_probe_unshare`` refuses to probe on the loop
    and defers to a thread), but the fix cannot be platform-conditional. Same form
    and same reason as ``apps/builtins/dev_fleet/server.py``.
    """
    wrapped, scrubbed, cleanup = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(),
        functools.partial(
            sandboxed_spawn_argv,
            argv,
            "strict",
            env=env,
            extra_hidden_dirs=_sensitive_hidden_dirs(),
        ),
    )
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
        stdout, stderr = await asyncio.wait_for(
            procio.read_capped(proc, MAX_CAPTURED_OUTPUT_BYTES), timeout=timeout
        )
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
            # Off the loop like the spawn itself. One unlink is a small syscall, but
            # the rule is about the shape, not the size: an inline syscall here is
            # what the AST guard in test_papyrus_routes.py refuses, and exempting
            # "small" ones is how the next one gets in.
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), partial(Path(cleanup).unlink, missing_ok=True)
            )
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


#: How many symlinks to name in the refusal message. The refusal is BLOCKING on the
#: first one, so the rest are only there to save the user a second Compile; a repo with
#: hundreds of links should not paste hundreds of names into the log pane.
_MAX_REPORTED_LINKS = 10


def _symlinked_artifacts(project: Path) -> tuple[list[str], bool]:
    """Every symlink in the project tree. BLOCKING when non-empty.

    ``is_symlink()`` does NOT follow the link, so a dangling one is reported too — which
    is correct: the compiler would create the target wherever it points.

    **Why the whole tree and not the generated-artifact names.** Two earlier versions of
    this guard enumerated *names* — first a hand-written tuple of 8 suffixes, then the 17
    derived from ``store.ARTIFACT_SUFFIXES``. Both were unsound for the same reason, and
    the second only looked safer: a name list can answer "is ``main.pdf`` a link?" but
    the question the compiler actually poses is "is the file I am about to open a link?",
    and **the document chooses that filename**. ``\\openout\\ch=chapter1`` writes
    ``chapter1.tex``; ``\\jobname`` can be redirected; ``.bbl``/``.idx`` tool chains write
    names no suffix list predicts. Enumerating names loses that race by construction, so
    each round closed one instance and left the class open.

    Refusing on ANY link inverts it: the guard no longer has to predict what the compiler
    will write, because nothing beneath the project is a link at all. That is also the
    stance ``store.list_files`` already takes ("a link is not a file the editor should
    follow — and following one is how a tree walk escapes containment"), so a symlinked
    file was already invisible in the file list. This makes Compile agree with it rather
    than quietly following what the editor refuses to show.

    Bounded by ``store.MAX_PROJECT_FILES`` and does not descend INTO links, so a
    ``a -> ..`` cycle cannot spin it — same walk shape as ``list_files``.

    Returns ``(links, complete)``. **``complete`` is load-bearing and the caller MUST
    refuse when it is False.** The bound makes the walk terminate, but an exhausted
    budget means "did not finish looking", NOT "found nothing" — and the first version of
    this returned a bare list, so the two were indistinguishable and the caller read an
    unfinished scan as a clean one. That FAILED OPEN in precisely the case an attacker
    controls: a cloned repo padded with ``MAX_PROJECT_FILES`` earlier-sorting files
    exhausts the budget before the walk reaches ``main.pdf -> main.tex``, so the guard
    passed and pdflatex truncated the user's source. Demonstrated with the budget
    monkeypatched low, which is the same condition a padded repo produces.
    """
    found: list[str] = []
    stack = [project]
    seen = 0
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > store.MAX_PROJECT_FILES:
                # Out of budget with the tree unexhausted. Report what was found AND that
                # the answer is partial; never let this look like "no links".
                return found, False
            try:
                if entry.is_symlink():
                    found.append(entry.relative_to(project).as_posix())
                    continue
                # `.git` holds the checkout's own machinery, not document sources, and a
                # link in there is not something the compiler will open by name. Walking
                # it also dominates the budget on any real repository.
                if entry.is_dir() and entry.name != ".git":
                    stack.append(entry)
            except OSError:  # pragma: no cover - defensive
                continue
    return found, True


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
    # `is_file()` is a stat, so it is offloaded like every other syscall on this
    # path — see the AST guard in test_papyrus_routes.py.
    if not await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), tex.is_file
    ):
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

    # Refuse to run if ANYTHING beneath the project is a PRE-EXISTING SYMLINK.
    #
    # The compiler opens files for writing BY NAME, and a cloned repository can ship any
    # name as a link. `main.pdf -> main.tex` makes pdflatex TRUNCATE THE USER'S SOURCE the
    # moment it opens its output — the document is destroyed by compiling it, with no
    # error and nothing to recover from. A link pointing outside the project is the same
    # write, aimed anywhere the gateway can reach.
    #
    # The check is "any link", not "any link among the names we expect", because the
    # DOCUMENT picks the filename: `\openout` takes an arbitrary one, so no suffix list
    # can enumerate the targets (see `_symlinked_artifacts`). `openout_any=p` bounds
    # those writes to the project SUBTREE, but a link inside the subtree is a legitimate
    # path whose target is not — the two guards cover different halves and neither
    # substitutes for the other.
    #
    # This cannot be solved downstream: `pdf_path`'s containment check decides what may
    # be SERVED, and by then the write has happened. The only place to stop it is before
    # the spawn.
    #
    # Removing the links instead of refusing was considered and rejected: deleting files
    # a repository shipped, as a side effect of pressing Compile, is its own surprise.
    # The compiler also recreates them as regular files on the next run once the user
    # removes them, so refusing is recoverable and silent deletion is not.
    blocked, scan_complete = await asyncio.to_thread(_symlinked_artifacts, project)
    if blocked:
        shown = ", ".join(blocked[:_MAX_REPORTED_LINKS])
        if len(blocked) > _MAX_REPORTED_LINKS:
            shown += f", and {len(blocked) - _MAX_REPORTED_LINKS} more"
        return CompileResult(
            ok=False,
            log=(
                f"Refusing to compile: {shown} "
                "exist as symbolic links. The compiler writes files by name, so "
                "compiling could overwrite whatever they point at. Delete them and try "
                "again."
            ),
        )
    if not scan_complete:
        # An unfinished scan is NOT a clean one. Padding a repo with more than
        # `MAX_PROJECT_FILES` earlier-sorting entries would otherwise exhaust the budget
        # before the walk reached the link, and treating that as "no links found" is the
        # one way this guard can be defeated without defeating anything.
        return CompileResult(
            ok=False,
            log=(
                f"Refusing to compile: this project has more than "
                f"{store.MAX_PROJECT_FILES} files, so it cannot be checked for symbolic "
                "links before compiling. Split it into smaller projects, or remove files "
                "the document does not need."
            ),
        )

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
            bib_code, bib_output = await _run(
                [bibtex_bin, "--", aux_stem],
                cwd=project,
                env=env,
                timeout=BIBTEX_TIMEOUT_SEC,
                operation="bibtex",
            )
            # A bibtex failure is REPORTED, not swallowed. Its result used to be
            # discarded, so a missing `.bst` or an unparseable `.bib` produced no `.bbl`,
            # the two later pdflatex passes still exited 0, and the compile was reported
            # SUCCESSFUL while the PDF carried `[?]` for every citation and an empty
            # bibliography. The user saw a green compile and a silently wrong paper.
            #
            # The bar is deliberately NOT "any non-zero exit". bibtex exits 1 for
            # WARNINGS, which are routine on a healthy document (an undefined cross
            # reference, a missing `journal` field), so failing on that would refuse
            # most real bibliographies. Only the two unambiguous cases fail the compile:
            # a timeout (`-1` with no output, `_run`'s documented contract), and an exit
            # >= 2, which is bibtex's own "fatal / could not proceed" tier.
            if bib_code == -1 and not bib_output:
                return CompileResult(
                    ok=False, log=f"BibTeX timed out after {BIBTEX_TIMEOUT_SEC:.0f}s"
                )
            if bib_code >= 2:
                bib_tail = bib_output[-MAX_LOG_CHARS:]
                return CompileResult(
                    ok=False,
                    log=(
                        "BibTeX failed, so the bibliography and every citation would be "
                        f"missing from the PDF:\n\n{bib_tail}"
                    ),
                    diagnostics=parse_log(bib_tail),
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
    # `None` when the emitted name is not contained (a symlinked `main.pdf` in a
    # cloned repo), which is reported as a failed compile rather than served.
    #
    # OFF the loop. `pdf_path` used to be pure path arithmetic, so calling it inline was
    # free — adding the containment check gave it a `Path.resolve()` and a
    # sensitive-path probe, i.e. real syscalls, and on a project under a stalled network
    # mount (`KIROCREW_HOME` on NFS/SMB) those block for as long as the mount takes to
    # answer. The `is_file()` beside it was already offloaded for exactly this reason;
    # the security fix quietly put a second syscall in front of it.
    pdf = await asyncio.to_thread(store.pdf_path, project, main_file)
    ok = code == 0 and pdf is not None and await asyncio.to_thread(pdf.is_file)
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
