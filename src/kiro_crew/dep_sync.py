"""Dependency-only sync for a checkout whose console script cannot be replaced.

Windows holds a mandatory lock on a running executable's image, so pip cannot
rewrite ``Scripts\\kirocrew.exe`` while the gateway is served by the very venv it
is reinstalling into -- the ordinary single-checkout layout. ``pip install -e .``
fails there even when the revision being synced changed nothing about the console
script, because a reinstall rewrites it unconditionally.

An editable install needs no reinstall for a source change: ``src`` is already on
``sys.path``, so merged code is live the moment the merge lands. What a source
change CAN require is a dependency the venv does not have yet, and installing a
dependency never touches the project's own console script. Syncing dependencies
alone is therefore the whole of what pip is needed for.

The step is deliberately shaped as PARITY with the reinstall it stands in for, not
as an improvement on it. Every other platform runs ``fetch -> merge ->
pip install -e .``; this runs ``fetch -> merge -> pip install <the project's
requirements>``. The scope matches too: a plain ``pip install -e .`` resolves
``install_requires`` and nothing else, so this installs exactly that and leaves
extras alone. Anything beyond parity was tried and removed -- inferring which
extras the operator requested (pip records no such thing, so the inference has
unavoidable false negatives) and proving in advance that the merge cannot fail
(which needs a growing set of preconditions and still cannot be complete).

That parity includes one exposure, stated plainly rather than guarded against: the
merge lands first, so a failed dependency install leaves the checkout on a revision
whose dependencies are not satisfied, and the operator has to finish by hand. That
is exactly what happens on every other platform when the reinstall step fails, and
it is the accepted behaviour of this workflow rather than something this substitute
introduces. Raising that bar is worth doing for ALL platforms at once, not for this
one path.

Satisfaction is left to pip rather than computed here: every declared requirement
is handed to ``pip install`` verbatim, which no-ops the ones already satisfied and
evaluates specifiers and environment markers with the same code
``pip install -e .`` would have run. Deciding it locally would need a PEP 508
parser this package does not depend on (``packaging`` is not an install
requirement) and would drift from pip's own answer.

Two things a dependency-only install cannot deliver that a reinstall does, so both
are checked rather than left silent: the ``requires-python`` gate pip applies when
it builds the project, and a rewrite of the console script when the incoming
revision repoints it.

WHERE THIS LIVES, AND WHAT IT MAY IMPORT
----------------------------------------
This module is core rather than app-local because the reinstall it stands in for
is not a Dev Fleet idea: the same ``pip install -e .`` into the venv serving the
running gateway is spelled out by the dashboard update handler, the Slack
gateway's auto-update, ``kirocrew update``, and the console-script bootstrap. Four
of those five callers are core, so a Dev Fleet home would mean core importing from
a builtin app.

It imports the standard library ONLY, and that is an invariant a caller depends on
rather than a stylistic preference. ``kiro_crew._bootstrap`` reaches for this
module precisely when a declared dependency is missing from the venv -- that is the
condition it exists to heal -- so a third-party import here would fail in exactly
the case the module is needed. That is also the concrete argument against deciding
requirement satisfaction with ``packaging.SpecifierSet``: it is the better parser,
and adopting it would break the one caller that cannot assume its own
dependencies are installed. ``tomllib`` is imported through a guarded ``tomli``
fallback for 3.10 for the same reason -- a missing fallback degrades this module,
it never breaks the import.

The corollary for callers: import this module BEFORE the step that moves the
working tree. Every caller merges or resets first, so an import deferred until
after that point parses the file the incoming revision shipped -- and a revision
that raises the ``requires-python`` floor and uses newer syntax anywhere would
then die with a ``SyntaxError`` instead of reaching the floor refusal written for
exactly that case. An already-imported module is served from
``sys.modules`` and cannot be re-parsed, which is what makes the refusal
reachable.
"""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

try:  # pragma: no cover - exercised by whichever interpreter runs this
    import tomllib as _toml
except ImportError:  # Python 3.10, which this project still supports
    try:
        import tomli as _toml  # type: ignore[no-redef,import-not-found]
    except ImportError:
        _toml = None  # type: ignore[assignment]


#: How a caller receives this module's own messages: ``(message, is_error)``.
#: Positional rather than keyword so a caller can pass a plain two-argument
#: function without importing anything from here.
Emit = Callable[[str, bool], None]

#: Characters that terminate the distribution name at the head of a PEP 508
#: requirement (version specifier, extras bracket, marker separator, or the
#: whitespace some declarations put before the specifier).
_NAME_END = re.compile(r"[\s<>=!~;\[(]")

#: The project's only console script. It is the wrapper the operator restarts
#: through, so it is the one whose staleness has to be reported rather than
#: silently tolerated.
_SCRIPT = "kirocrew"

#: This project's own distribution name, normalized. Asking pip for it is the one
#: request that would rewrite the locked console script.
_PROJECT = "kirocrew"

#: ``requires-python`` lower bounds. Only the floor is enforced: it is the bound
#: a revision raises when it starts using newer syntax, and the one whose breach
#: makes the merged tree unimportable under this interpreter. Upper bounds and
#: exclusions are left to pip on the next real reinstall rather than
#: reimplemented here without a PEP 440 parser. ``~=`` is included because a
#: compatible-release clause declares its own lower bound, ``==`` and ``===`` are
#: included because an equality clause pins the version it names and so declares
#: that version as the floor, and ``>`` is kept distinct from ``>=`` because it
#: excludes the version it names. ``===`` precedes ``==`` in the alternation so
#: the longer operator wins; ``>=`` precedes ``>`` for the same reason.
_PY_FLOOR = re.compile(
    r"(?P<op>===|==|~=|>=|>)\s*(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<micro>\d+))?"
)

#: A plain PEP 508 distribution name -- letters, digits, and the separators PEP
#: 503 normalizes. Anything else at the head of a requirement is not a name: a
#: path (``.``, ``./x``, ``/abs``, ``C:\x``), a URL scheme, an archive filename, or
#: a stray option. Those are what could make pip install the project itself.
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Distribution-archive suffixes. pip reads a bare token ending in one of these as
#: a local file when the file exists, so such a token is refused rather than left
#: to be disambiguated by the working directory's contents.
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")


def locked_console_scripts(target_py: Path) -> list[str]:
    """The venv's ``kirocrew`` console scripts that cannot currently be rewritten.

    This is the probe every caller uses to decide whether a reinstall is even
    possible, so it lives beside the substitute rather than beside any one caller.

    Windows holds a mandatory lock on a running executable's image, so when the
    process is served BY the very venv pip is about to reinstall into -- the
    ordinary single-checkout setup -- pip cannot replace ``Scripts\\kirocrew.exe``,
    and the reinstall can never succeed.

    That matters well beyond one failed step. pip's uninstall is not atomic: by the
    time it reaches the locked script it has already renamed the dist-info aside
    and deleted the editable ``.pth`` that puts ``src`` on ``sys.path``, and it
    rolls back neither. The venv is left unable to import the package at all, which
    also kills the console script the gateway is restarted through. The running
    process survives on already-imported modules, so the damage stays invisible
    until the next restart fails.

    Probing with an ``r+b`` open is non-destructive and discriminates correctly: a
    running executable refuses it while every other script in the same directory
    opens fine. It does not model every way a delete can fail -- an opener that
    permits writes but denies deletes would pass this probe -- so a clean result
    means "no known blocker", not a guarantee. A miss simply leaves the previous
    behaviour, which is why this is worth doing even though it cannot be
    exhaustive.

    POSIX returns nothing: an executing binary can be unlinked there, which is why
    pip has always been able to replace it.

    ``sys.platform`` is read directly rather than through the package's platform
    helper to keep this module's stdlib-only import discipline (see the module
    docstring) -- ``_bootstrap`` calls into here when an import is already failing.
    """
    if sys.platform != "win32":
        return []
    locked: list[str] = []
    for exe in sorted(Path(target_py).parent.glob("kirocrew*.exe")):
        try:
            with exe.open("r+b"):
                pass
        except PermissionError:
            locked.append(str(exe))
        except OSError:
            # Unreadable for some other reason. Let pip be the judge rather than
            # skipping a step that might well have succeeded.
            continue
    return locked


def normalize(name: str) -> str:
    """Normalize a distribution name per PEP 503 so lookups compare equal."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def rejected_specs(specs: list[str]) -> list[str]:
    """Requirements that must never reach pip, with the reason for each.

    The module's whole premise is that pip is asked for DEPENDENCIES and never for
    the project, because installing the project is what rewrites the locked console
    script. Without this the property would hold only because this repository's
    declarations happen not to name a path: a declaration of ``.`` would have pip
    install the checkout itself, remove the editable install, and then fail on the
    locked executable -- leaving the venv unable to import the package at all,
    which is the exact damage the step exists to avoid.

    Two shapes are refused: a head that is not a plain distribution name (a path,
    a URL, an archive, a leftover option), and any requirement that normalizes to
    this project's own name however it is spelled.
    """
    bad: list[str] = []
    for spec in specs:
        head = _NAME_END.split(spec.strip(), 1)[0].strip()
        if not _PLAIN_NAME.match(head):
            bad.append(f"{spec!r} is not a plain requirement name")
        elif head.lower().endswith(_ARCHIVE_SUFFIXES):
            # A bare `foo.whl` is a legal NAME by character set, but pip resolves it
            # as a local file whenever one exists in the working directory, so it is
            # refused rather than disambiguated by what happens to be on disk.
            bad.append(f"{spec!r} names an archive rather than a distribution")
        elif normalize(head) == _PROJECT:
            bad.append(f"{spec!r} names this project, whose reinstall is the blocker")
    return bad


def _requirement_lines(raw: str) -> list[str]:
    """Requirement lines from one setup.cfg value, comments dropped.

    setup.cfg carries a full-line ``#`` comment for most requirements here, and
    configparser keeps them, so they have to be stripped before the value is
    handed to pip. A trailing comment on a requirement line is NOT stripped: a
    PEP 508 marker can legitimately contain ``#`` inside a quoted string, and this
    project's declarations put their commentary on their own lines.
    """
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


def read_text(repo: Path, name: str) -> str | None:
    """``<repo>/<name>`` as text, or ``None`` when it cannot be read.

    The working tree is the right source here because this step runs AFTER the
    merge, so the tree already IS the revision being synced -- the same tree
    ``pip install -e .`` would have read on any other platform.
    """
    try:
        return (repo / name).read_text(encoding="utf-8")
    except OSError:
        return None


def declared_requirements(repo: Path) -> list[str] | None:
    """The project's ``install_requires``, or ``None`` if it cannot be read.

    Extras are deliberately not collected. ``pip install -e .`` installs no extra
    either, so re-resolving them here would give this path a behaviour the
    reinstall it substitutes for does not have.
    """
    text = read_text(repo, "setup.cfg")
    if text is None:
        return None
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(text)
    except configparser.Error:
        return None
    if not cfg.has_option("options", "install_requires"):
        return None
    return _requirement_lines(cfg.get("options", "install_requires"))


def dependency_authority_moved(repo: Path) -> str | None:
    """Why setup.cfg is no longer where the requirements live, if it moved.

    This module reads ONE file. A revision that migrates requirements into
    pyproject's ``[project]`` table would leave setup.cfg stale, and reading it
    would install yesterday's set while reporting success. setuptools treats a
    field listed in ``[tool.setuptools.dynamic]`` as still coming from setup.cfg,
    so only a NON-dynamic declaration in ``[project]`` means the move happened.

    ``dynamic`` is matched item by item rather than as a substring: the list
    ``["optional-dependencies"]`` CONTAINS the text ``dependencies`` while
    declaring nothing about ``dependencies`` itself, and reading that as "still
    dynamic" is what would let an explicit ``[project].dependencies`` be ignored
    in favour of a stale setup.cfg.
    """
    text = read_text(repo, "pyproject.toml")
    if text is None:
        return None
    table = project_table(repo)
    if table is not None:
        if "dependencies" not in table:
            return None
        if "dependencies" in _as_str_list(table.get("dynamic")):
            return None
        return "pyproject declares dependencies non-dynamically, so setup.cfg is stale"
    project = _section(text, "[project]")
    if not re.search(r"^\s*dependencies\s*=", project, re.MULTILINE):
        return None
    if "dependencies" in _dynamic_fields(project):
        return None
    return "pyproject declares dependencies non-dynamically, so setup.cfg is stale"


def _as_str_list(value: object) -> list[str]:
    """*value* as a list of strings, empty for anything else."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _dynamic_fields(project: str) -> set[str]:
    """The field names in ``[project]``'s ``dynamic`` list, unquoted."""
    match = re.search(r"^\s*dynamic\s*=\s*\[(?P<items>[^\]]*)\]", project, re.MULTILINE)
    if not match:
        return set()
    return {item.strip().strip("\"'") for item in match.group("items").split(",")}


def requires_python(repo: Path) -> str | None:
    """The interpreter floor the checkout declares, from whichever file owns it.

    pyproject's ``[project].requires-python`` is read FIRST because setuptools
    reads it from there and IGNORES setup.cfg's ``python_requires`` whenever a
    ``[project]`` table exists -- this repository says so in pyproject itself, and
    carries the value in both files, so a revision that raises the floor in the
    authoritative one would leave the setup.cfg copy stale. Reading the file the
    build backend ignores is how this gate would silently stop firing.

    setup.cfg remains the fallback for a checkout with no ``[project]`` table, or
    one that declares the field dynamic. A ``[project]`` table that declares the
    field statically is the whole answer, so a table that OMITS it declares no
    floor -- reading setup.cfg's ``python_requires`` there would enforce a copy
    setuptools itself ignores, and a stale one can only over-refuse a revision
    that is in fact installable. Unlike the console-script answer this needs no
    sentinel: "no floor declared" and "the floor could not be read" both mean the
    same thing to the caller, which is that this gate does not fire.
    """
    text = read_text(repo, "pyproject.toml")
    if text is not None:
        table = project_table(repo)
        if table is not None:
            if "requires-python" not in _as_str_list(table.get("dynamic")):
                spec = table.get("requires-python")
                if isinstance(spec, str) and spec.strip():
                    return spec.strip()
                return None
        else:
            project = _section(text, "[project]")
            if "requires-python" not in _dynamic_fields(project):
                match = re.search(
                    r"^\s*requires-python\s*=\s*[\"'](?P<spec>[^\"']+)[\"']",
                    project,
                    re.MULTILINE,
                )
                if match:
                    return match.group("spec").strip() or None
    text = read_text(repo, "setup.cfg")
    if text is None:
        return None
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(text)
    except configparser.Error:
        return None
    if not cfg.has_option("options", "python_requires"):
        return None
    return cfg.get("options", "python_requires").strip() or None


def _probe_interpreter(
    target_py: Path, code: str, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run *code* under *target_py*, isolated from the caller's CWD and env.

    Every probe here asks a question about the VENV -- where it resolves this
    package, which interpreter version it runs, what its console script
    dispatches to -- so the answer must not depend on the process asking.
    Without isolation it does, by two routes: ``python -c`` puts the child's
    CWD at ``sys.path[0]``, so a caller running from a checkout's ``src/``
    (the Dev Fleet backend does) makes ``find_spec`` resolve this package from
    that directory for ANY target interpreter; and the child inherits
    ``PYTHONPATH``, which shadows site-packages the same way.

    ``-I`` closes both routes at once -- no CWD entry on ``sys.path``, no
    ``PYTHONPATH``, no user-site -- while still honoring the venv's own
    site-packages, where an editable install's ``.pth`` lives. Because ``-I``
    implies ``-E``, an encoding request must ride the argv rather than the
    environment: ``-X utf8`` pins the child's stdout to UTF-8 (matching the
    decode below) where a ``PYTHONIOENCODING`` entry would be ignored,
    keeping a non-ASCII checkout path from mangling -- or crashing -- the
    answer on a non-UTF-8 locale. ``cwd`` is pinned to the interpreter's own
    directory as well: under ``-I`` the working directory is never on
    ``sys.path``, whatever it contains, so the pin exists to keep the child's
    working directory valid even when the caller's has been deleted. The
    interpreter path is absolutized first (without resolving symlinks, which
    would erase a venv's identity), because a relative path -- which
    ``shutil.which`` can return for a relative ``PATH`` entry -- would
    otherwise be re-resolved against the changed cwd and spawn nothing.

    ``code`` MUST be a fixed literal owned by the caller: this helper is the
    spawn-audit allowlist's designated isolated-probe entry point, and its
    "fixed argv" justification stops holding the moment caller-derived text
    reaches the child. ``timeout`` is forwarded to :func:`subprocess.run`
    for callers on an interactive path (the doctor, the STT toolchain scan)
    that must not hang on a wedged interpreter; ``None`` keeps the sync
    path's unbounded wait.
    """
    target = Path(os.path.abspath(target_py))
    return subprocess.run(
        [str(target), "-I", "-X", "utf8", "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=target.parent,
        timeout=timeout,
    )


def interpreter_version(target_py: Path) -> tuple[int, int, int] | None:
    """``(major, minor, micro)`` of *target_py*, or ``None`` if it cannot be asked."""
    proc = _probe_interpreter(target_py, "import sys;print('%d.%d.%d' % sys.version_info[:3])")
    if proc.returncode != 0:
        return None
    try:
        major, minor, micro = proc.stdout.strip().split(".")
        return int(major), int(minor), int(micro)
    except ValueError:
        return None


def python_floor_breach(spec: str, version: tuple[int, int, int]) -> str | None:
    """The highest ``requires-python`` floor *version* fails, if it fails one.

    ``pip install -e .`` doubles as this project's interpreter gate: a revision
    that raises its floor and starts using newer syntax is refused by pip rather
    than installed. A dependency-only sync builds nothing, so pip applies no such
    check and the gate has to be applied here or the merged revision becomes
    unimportable under the interpreter that has to import it.

    Four spellings all declare a floor and all have to be read as one, because a
    floor this misses is a gate that does not fire: ``>=`` names the floor
    directly, ``~=`` (compatible release) names it as its own lower bound, ``==``
    and ``===`` pin the version they name and so make it the floor too, and ``>``
    names a floor the interpreter must EXCEED rather than merely meet.
    Comparison is at three components, so ``>=3.10.5`` is not truncated to the
    minor and then passed by a 3.10.0 interpreter.
    """
    breached: tuple[int, int, int] | None = None
    for match in _PY_FLOOR.finditer(spec):
        floor = (
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("micro") or 0),
        )
        exclusive = match.group("op") == ">"
        fails = version <= floor if exclusive else version < floor
        if fails and (breached is None or floor > breached):
            breached = floor
    if breached is None:
        return None
    return f"{breached[0]}.{breached[1]}.{breached[2]}"


def installed_package_origin(target_py: Path) -> str | None:
    """Where *target_py*'s venv resolves this project's package FROM.

    Read through the target interpreter because the venv being written to is not
    necessarily this process's own. ``find_spec`` rather than an import, so nothing
    in the package runs; and the spec rather than installed metadata, because the
    metadata only proxies for this answer -- a working PEP 660 editable install can
    have no ``direct_url.json`` at all, and refusing that venv would refuse the
    ordinary single-checkout layout this step exists to serve. The import path is
    what the installed dependencies will be imported alongside, so it is the thing
    worth checking.

    Runs through :func:`_probe_interpreter`, so the answer describes the venv
    rather than the caller: an unisolated probe resolves the package from the
    calling process's CWD (or a ``PYTHONPATH`` entry) ahead of the venv's
    site-packages, and the consumer then refuses a perfectly healthy venv as
    "serving a different checkout".

    Returns ``None`` when the package cannot be located at all -- including when
    the interpreter itself cannot be run. That case is not theoretical: the caller
    resolved this path by a filesystem check, and a venv deleted between that check
    and this probe would otherwise raise ``OSError`` out of a request handler. The
    callers read ``None`` as "cannot be shown to serve this checkout" and refuse,
    which is the right answer to an unrunnable interpreter as well.
    """
    probe = (
        "import importlib.util as u, os;"
        "s=u.find_spec('kiro_crew');"
        "print(os.path.abspath(s.origin) if s and s.origin else '')"
    )
    try:
        proc = _probe_interpreter(target_py, probe)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def venv_not_mapped_to(origin: str | None, repo: Path) -> str | None:
    """Why the venv at *origin* cannot be shown to serve *repo*, if it cannot.

    The venv is addressed as ``<repo>/.venv``, but that is a location, not a
    binding: nothing stops it from being an install of a DIFFERENT checkout.
    Installing this revision's dependencies into such a venv upgrades the runtime
    another checkout is served by, which is how a working checkout gets broken by a
    sync that never touched it.

    The test is whether that venv imports this project from inside *repo*. An
    unreadable answer refuses too -- unproven is not the same as safe.
    """
    if origin is None:
        return (
            "the target venv does not resolve this project's package at all, so it "
            "cannot be shown to serve this checkout"
        )
    mapped = os.path.normcase(str(Path(origin).resolve()))
    root = os.path.normcase(str(Path(repo).resolve()))
    if mapped != root and not mapped.startswith(root + os.sep):
        return (
            f"the target venv imports this project from {Path(origin).resolve()}, "
            f"which is outside {Path(repo).resolve()}, so a dependency install "
            "would change a runtime this sync does not own"
        )
    return None


def project_table(repo: Path) -> dict[str, Any] | None:
    """pyproject's ``[project]`` table, PARSED, or ``None`` if it cannot be.

    Every question this module asks of pyproject -- where the requirements live,
    what the interpreter floor is, what the console script dispatches to -- was
    first answered by matching text, and each round of review found another
    spelling where matching text and reading TOML disagree: a list item whose name
    merely CONTAINS another (`optional-dependencies`), and a table header with a
    trailing comment (``[project] # comment``). Those are not three bugs, they are
    one: a hand-rolled reader answering a question only a parser can answer.

    So a parser answers it wherever one exists -- ``tomllib`` on 3.11+, ``tomli``
    if the venv happens to carry it, exactly the ladder ``onboarding_import``
    already uses. ``None`` means neither was importable (a 3.10 venv without
    ``tomli``), and each caller then falls back to its text reader, which is
    best-effort by nature; that residual is stated in the PR rather than hidden.
    """
    if _toml is None:
        return None
    text = read_text(repo, "pyproject.toml")
    if text is None:
        return None
    try:
        parsed = _toml.loads(text)
    except Exception:
        # A pyproject this module cannot parse is not a pyproject it should guess
        # about; the caller's text reader is no better informed, so say so once.
        return None
    project = parsed.get("project")
    return project if isinstance(project, dict) else None


def _section(toml_text: str, header: str) -> str:
    """The body of one top-level TOML table, by literal header line.

    A regex read of one table, not a TOML parse: ``tomllib`` is 3.11+ and this
    project supports 3.10, so a dependency-free read of two well-known tables is
    preferred over adding a parser for it.
    """
    lines = toml_text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if inside:
                break
            # `[project] # comment` is a valid header. Comparing the whole line
            # would read it as a different table and skip the body underneath.
            inside = stripped.split("#", 1)[0].strip() == header
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


#: What :func:`console_script_target` returns when the authoritative declaration
#: EXISTS and does not name the script: the entry point is REMOVED as of the
#: merged revision. Distinct from ``None``, which means the declaration could not
#: be read at all -- collapsing the two would let a removal pass silently, and a
#: wrapper left dispatching to a target the revision deleted is exactly the
#: mismatch this comparison exists to report. A truthy string that can never be a
#: valid ``module:attr`` (it contains spaces), so the comparison in :func:`main`
#: treats a removal like any other disagreement.
SCRIPT_REMOVED = "removed by the merged revision"

#: Exit code meaning "this stopped before touching the venv". Distinct from 1
#: because the two demand opposite handling: a caller that reacts to a failed
#: install by repairing what it can must NOT run that repair after a REFUSED,
#: since the refusal's whole point is that this venv is not ours to write to.
#: Every ``_refuse`` path returns it, as does a usage error -- in both cases
#: nothing was installed.
#:
#: An install that RAN and failed returns 1, never pip's own exit code. pip
#: spends 2 on UNKNOWN_ERROR, so propagating it would announce "nothing was
#: touched" for a run that may well have changed the venv -- the one reading a
#: caller must not get wrong. pip's real code is reported in the message.
REFUSED = 2


def console_script_target(repo: Path, script: str) -> str | None:
    """The ``module:attr`` *script* is declared to dispatch to, or ``None``.

    pyproject's ``[project.scripts]`` is read FIRST because that is what setuptools
    builds the wrapper from whenever ``scripts`` is not listed as dynamic; setup.cfg
    is the fallback for a checkout that still declares them there.

    setup.cfg is consulted only where setuptools itself would consult it: a
    checkout with no ``[project]`` table (pre-PEP 621 metadata), or one that
    declares ``scripts`` dynamic. A ``[project]`` table that declares ``scripts``
    statically is the whole answer, so a table that does not name *script* means
    the entry point was removed rather than that it should be looked for
    elsewhere -- reading setup.cfg's copy anyway would compare a stale
    declaration against the installed wrapper, agree with it, and report success
    on a script the revision deleted. That case returns
    :data:`SCRIPT_REMOVED`.

    The removal answer is only reached on the parsed path. The text fallback (3.10
    without ``tomli``, per :func:`project_table`) cannot tell an absent
    ``[project.scripts]`` table from an empty one, and cannot see the inline-table
    or dotted-key spellings of the same declaration, so it would report a removal
    for a script that is declared. It keeps the older best-effort behaviour of
    falling through to setup.cfg instead: a missed removal there is the same gap
    that path already has on every other question, and is preferable to refusing a
    sync over a declaration it merely failed to read.
    """
    pyproject = read_text(repo, "pyproject.toml")
    if pyproject:
        table = project_table(repo)
        if table is not None:
            if "scripts" not in _as_str_list(table.get("dynamic")):
                scripts = table.get("scripts")
                target = scripts.get(script) if isinstance(scripts, dict) else None
                if isinstance(target, str) and target.strip():
                    return target.strip()
                return SCRIPT_REMOVED
        else:
            scripts_text = _section(pyproject, "[project.scripts]")
            match = re.search(
                rf'^\s*["\']?{re.escape(script)}["\']?\s*=\s*["\'](?P<target>[^"\']+)["\']',
                scripts_text,
                re.MULTILINE,
            )
            if match:
                return match.group("target").strip()
    cfg_text = read_text(repo, "setup.cfg")
    if cfg_text is None:
        return None
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(cfg_text)
    except configparser.Error:
        return None
    if not cfg.has_option("options.entry_points", "console_scripts"):
        return None
    for line in _requirement_lines(cfg.get("options.entry_points", "console_scripts")):
        name, _, target = line.partition("=")
        if name.strip() == script:
            return target.strip() or None
    return None


def installed_console_script_target(target_py: Path, script: str) -> str | None:
    """The ``module:attr`` *script* currently dispatches to in *target_py*'s venv.

    Reads the entry points off the ``kirocrew`` distribution rather than the
    module-level ``entry_points()`` selector: that function returns a group->list
    mapping on 3.10/3.11 and an ``EntryPoints`` sequence on 3.12+, and this project
    supports both. A distribution's own ``entry_points`` is a sequence in every
    supported version, and scoping the lookup to the distribution also keeps a
    same-named script from another package out of the answer.

    Returns ``None`` when the answer cannot be read at all. The caller only
    compares two KNOWN values, so an unreadable probe stays quiet instead of
    reporting a problem it has no evidence for.
    """
    probe = (
        "import importlib.metadata as m;"
        "d=m.distribution('kirocrew');"
        "print(next((e.value for e in d.entry_points"
        f" if e.group=='console_scripts' and e.name=={script!r}), ''))"
    )
    proc = _probe_interpreter(target_py, probe)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _print_emit(message: str, error: bool) -> None:
    """Report to the console -- the shape a subprocess caller reads."""
    print(message, file=sys.stderr if error else sys.stdout)


def _refuse(
    emit: Emit, message: str, target_py: Path, repo: Path, remedy: str | None = None
) -> int:
    if remedy is None:
        remedy = (
            "Stop the gateway and finish the sync from a terminal: "
            f'"{target_py}" -m pip install -e "{repo}"'
        )
    emit(f"dep-sync: {message} No dependency was installed. {remedy}", True)
    return REFUSED


def sync(
    repo: Path, target_py: Path, emit: Emit = _print_emit, timeout: float | None = None
) -> int:
    """Install what ``repo`` declares into ``target_py``'s venv. 0 when synced.

    Callers in the same process pass ``emit`` to receive every message they would
    otherwise have to scrape from a subprocess's streams -- the refusals in
    particular, each of which names the remedy the operator has to run by hand.
    pip's own output is captured and handed to ``emit`` too, RAW: it echoes the
    index URL it resolved against, and an authenticated index carries a credential
    there, so it must reach the caller that knows where it is about to be published
    rather than whatever this process's stderr happens to be.

    ``timeout`` bounds the pip invocation. ``None`` leaves it unbounded, which is
    what the module-run path uses: there the step already runs under the
    supervision of whatever launched it.
    """
    # Establish that the venv about to be written to serves THIS checkout before
    # anything is installed. `<repo>/.venv` is where the interpreter was found,
    # which says nothing about what it is an install of.
    foreign = venv_not_mapped_to(installed_package_origin(target_py), repo)
    if foreign:
        return _refuse(
            emit,
            f"{foreign}.",
            target_py,
            repo,
            remedy=(
                "Give this checkout its own editable install, or run the sync from "
                "the checkout that venv serves."
            ),
        )

    # This module reads one file for the requirements, so a revision that moved
    # them elsewhere has to stop the step rather than have it install a stale set.
    moved = dependency_authority_moved(repo)
    if moved:
        return _refuse(emit, f"{moved}, so this step would install a stale set.", target_py, repo)

    specs = declared_requirements(repo)
    if specs is None:
        return _refuse(
            emit,
            "cannot read install_requires from setup.cfg, so the requirements to "
            "install are unknown.",
            target_py,
            repo,
        )

    # The interpreter gate `pip install -e .` applies while building the project.
    floor_spec = requires_python(repo)
    version = interpreter_version(target_py) if floor_spec else None
    if floor_spec and version:
        breach = python_floor_breach(floor_spec, version)
        if breach:
            return _refuse(
                emit,
                f"the merged revision requires Python {floor_spec} but the target "
                f"venv runs {version[0]}.{version[1]}.{version[2]}, so its code "
                "cannot be imported.",
                target_py,
                repo,
            )

    if not specs:
        emit("dep-sync: no requirements declared; nothing to install", False)
    else:
        rejected = rejected_specs(specs)
        if rejected:
            return _refuse(
                emit,
                "the merged revision declares requirements this step will not hand "
                f"to pip: {'; '.join(rejected)}.",
                target_py,
                repo,
            )
        emit(
            f"dep-sync: installing {len(specs)} declared requirements; extras are "
            "left alone, exactly as a plain `pip install -e .` leaves them",
            False,
        )
        # Hand every spec to pip and let it decide: an already-satisfied requirement
        # is a no-op, so this installs what is new and leaves the rest alone without
        # this module ever comparing a version or evaluating a marker. The project
        # itself is deliberately absent -- installing it is what would rewrite the
        # locked console script.
        # `--` ends pip's option parsing, so a declared requirement can never be
        # read as a flag.
        #
        # CAPTURED, not streamed to the inherited stdio. pip echoes the index URL
        # it resolved against, and an authenticated index carries its credential in
        # that URL -- inherited stderr on this path is the gateway's own log, so
        # streaming writes the credential there in the clear. Routing it through
        # `emit` hands it to the caller that knows where it is about to be
        # published and is the one redacting it, which is what the reinstall branch
        # in `sync_or_reinstall` already does. The cost is that a long install stops
        # being observable line-by-line; that is the right trade against writing a
        # credential to a log file nobody reviews.
        try:
            proc = subprocess.run(
                [str(target_py), "-m", "pip", "install", "--", *specs],
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # NOT _refuse(): that says "No dependency was installed", and a run
            # killed mid-install may well have installed some of the set. A killed
            # install is rerunnable and a partial set is this module's already
            # accepted exposure; an unbounded one hangs the caller instead.
            emit(
                f"dep-sync: installing the declared requirements exceeded "
                f"{timeout}s and was stopped, so the set may be partially "
                f"installed. Finish the sync from a terminal: "
                f'"{target_py}" -m pip install -e "{repo}"',
                True,
            )
            return 1
        if proc.returncode != 0:
            # Bytes, then an explicit lossy decode, for the same reason as the
            # reinstall branch: the locale's codec is not guaranteed to decode
            # pip's output, and `text=True` would raise instead of reporting.
            detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            emit(
                f"dep-sync: pip exited {proc.returncode} installing the declared "
                "requirements" + (f": {detail}" if detail else ""),
                True,
            )
            # 1, NOT pip's own code. pip uses 2 for UNKNOWN_ERROR, which is the
            # value REFUSED carries, and the two mean opposite things to a caller:
            # this install RAN and may have changed the venv, so a caller that
            # repairs after a failure must still repair. Propagating pip's code
            # would tell it nothing was touched. pip's actual code is in the
            # message above, where it is diagnostic rather than load-bearing.
            return 1

    # The one thing a dependency-only install cannot deliver: if the merged
    # revision REPOINTED the console script -- or dropped it -- the wrapper on
    # disk still dispatches to the old target and no amount of dependency
    # installing refreshes it. The dependencies are installed by now, so this
    # reports rather than refuses.
    declared = console_script_target(repo, _SCRIPT)
    installed = installed_console_script_target(target_py, _SCRIPT)
    if declared and installed and declared != installed:
        if declared == SCRIPT_REMOVED:
            disagreement = (
                f"script is no longer declared by the merged revision, while the "
                f"installed wrapper still calls {installed}"
            )
        else:
            disagreement = (
                f"script is repointed to {declared} by the merged revision while "
                f"the installed wrapper still calls {installed}"
            )
        emit(
            f"dep-sync: dependencies are installed, but the {_SCRIPT!r} console "
            f"{disagreement}. That wrapper cannot be "
            f"rewritten while a process is running from it: stop the gateway and run "
            f'"{target_py}" -m pip install -e "{repo}" before restarting.',
            True,
        )
        return 1
    return 0


def sync_or_reinstall(
    repo: Path,
    target_py: Path,
    emit: Emit = _print_emit,
    timeout: float | None = None,
) -> int:
    """Bring ``target_py``'s venv up to date with ``repo``. 0 when it succeeded.

    The one entry point for a caller that wants the editable reinstall wherever it
    can still run and the dependency-only substitute only where it cannot. The
    reinstall stays the default deliberately: it is the fuller operation, and it is
    the one that also rewrites a console script the incoming revision repointed --
    the single thing the substitute can only report. So this does not replace the
    reinstall, it stops the reinstall from being attempted in the one case where
    attempting it does damage: pip's uninstall is not atomic, so a run that dies on
    the locked script has already deleted the editable ``.pth`` (see
    :func:`locked_console_scripts`).

    ``timeout`` bounds the pip invocation on EITHER branch. The substitute is not
    left unbounded on the reasoning that a killed dependency install is worse than
    a hung one: a killed one is rerunnable, and a partially installed set is
    already this module's accepted failure mode (see the exposure stated at the top
    of this file), while an unbounded install on a wedged index hangs the surface
    that called it with no story at all.

    pip's output is captured and handed to ``emit`` RAW. A caller that publishes it
    anywhere a user can see -- a dashboard progress feed, a log -- owns redacting
    it first: pip echoes index URLs, which carry credentials when the operator
    configured an authenticated index.
    """
    # Establish that the venv about to be written to serves THIS checkout BEFORE
    # the branch, so both paths are covered. `sync()` asks the same question again
    # on the substitute path, and that duplication is deliberate: `sync()` is also
    # reached directly (as a module run by path, which is how the Dev Fleet sync
    # invokes it), so it cannot delegate its own safety to a caller. The cost is
    # one extra short interpreter probe against an install measured in seconds.
    #
    # Without this, the reinstall branch would repeat the exact asymmetry this
    # change fixes at the Dev Fleet endpoint -- a guarded substitute beside an
    # unguarded reinstall -- and three of this function's four callers take the
    # checkout from configuration, so a repointed venv is reachable on all three.
    foreign = venv_not_mapped_to(installed_package_origin(target_py), repo)
    if foreign:
        return _refuse(
            emit,
            f"{foreign}.",
            target_py,
            repo,
            remedy=(
                "Give this checkout its own editable install, or run the update "
                "from the checkout that venv serves."
            ),
        )

    locked = locked_console_scripts(target_py)
    if locked:
        emit(
            f"dep-sync: {', '.join(locked)} locked by a running process; "
            "substituting a dependency-only sync for the editable reinstall",
            False,
        )
        return sync(repo, target_py, emit, timeout=timeout)

    # The same requires-python gate the dependency-only substitute applies, and
    # for the same reason -- except here pip WOULD enforce it, by refusing the
    # build with "Requires-Python". Refusing first turns an opaque pip failure on
    # an already-merged revision into a named reason plus the recovery hint
    # `_refuse` prints.
    floor_spec = requires_python(repo)
    floor_version = interpreter_version(target_py) if floor_spec else None
    if floor_spec and floor_version:
        breach = python_floor_breach(floor_spec, floor_version)
        if breach:
            return _refuse(
                emit,
                f"the merged revision requires Python {floor_spec} but the target "
                f"venv runs {floor_version[0]}.{floor_version[1]}.{floor_version[2]}, "
                "so pip will refuse to install it.",
                target_py,
                repo,
            )

    # `-e <repo>` rather than `-e .` with a cwd: the target is explicit in the argv
    # instead of implied by the working directory this happens to run in.
    argv = [str(target_py), "-m", "pip", "install", "-e", str(repo), "--quiet"]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        emit(f"dep-sync: pip install -e timed out after {timeout}s", True)
        return 1
    if proc.returncode != 0:
        # Bytes, then an explicit lossy decode: `text=True` decodes with the
        # locale's codec, and pip's output is not guaranteed to be in it -- on a
        # non-UTF-8 console that raises UnicodeDecodeError and loses the error
        # message that was the point of capturing.
        detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        emit(
            f"dep-sync: pip install -e exited {proc.returncode}"
            + (f": {detail}" if detail else ""),
            True,
        )
        # 1, not pip's own code, for the same reason as the substitute branch:
        # pip's 2 (UNKNOWN_ERROR) is REFUSED's value, and this install ran.
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: dep_sync <repo> <target-python>", file=sys.stderr)
        return REFUSED
    return sync(Path(args[0]), Path(args[1]))


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
