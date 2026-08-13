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
"""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised by whichever interpreter runs this
    import tomllib as _toml
except ImportError:  # Python 3.10, which this project still supports
    try:
        import tomli as _toml  # type: ignore[no-redef,import-not-found]
    except ImportError:
        _toml = None  # type: ignore[assignment]

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
#: compatible-release clause declares its own lower bound, and ``>`` is kept
#: distinct from ``>=`` because it excludes the version it names.
_PY_FLOOR = re.compile(r"(?P<op>~=|>=|>)\s*(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<micro>\d+))?")

#: A plain PEP 508 distribution name -- letters, digits, and the separators PEP
#: 503 normalizes. Anything else at the head of a requirement is not a name: a
#: path (``.``, ``./x``, ``/abs``, ``C:\x``), a URL scheme, an archive filename, or
#: a stray option. Those are what could make pip install the project itself.
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Distribution-archive suffixes. pip reads a bare token ending in one of these as
#: a local file when the file exists, so such a token is refused rather than left
#: to be disambiguated by the working directory's contents.
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")


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
    one that declares the field dynamic.
    """
    text = read_text(repo, "pyproject.toml")
    if text is not None:
        table = project_table(repo)
        if table is not None:
            spec = table.get("requires-python")
            if isinstance(spec, str) and "requires-python" not in _as_str_list(
                table.get("dynamic")
            ):
                return spec.strip() or None
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


def interpreter_version(target_py: Path) -> tuple[int, int, int] | None:
    """``(major, minor, micro)`` of *target_py*, or ``None`` if it cannot be asked."""
    proc = subprocess.run(
        [str(target_py), "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
        capture_output=True,
        text=True,
    )
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

    Three spellings all declare a floor and all have to be read as one, because a
    floor this misses is a gate that does not fire: ``>=`` names the floor
    directly, ``~=`` (compatible release) names it as its own lower bound, and
    ``>`` names a floor the interpreter must EXCEED rather than merely meet.
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

    Returns ``None`` when the package cannot be located at all.
    """
    probe = (
        "import importlib.util as u, os;"
        "s=u.find_spec('kiro_crew');"
        "print(os.path.abspath(s.origin) if s and s.origin else '')"
    )
    proc = subprocess.run(
        [str(target_py), "-c", probe],
        capture_output=True,
        text=True,
    )
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


def console_script_target(repo: Path, script: str) -> str | None:
    """The ``module:attr`` *script* is declared to dispatch to, or ``None``.

    pyproject's ``[project.scripts]`` is read FIRST because that is what setuptools
    builds the wrapper from whenever ``scripts`` is not listed as dynamic; setup.cfg
    is the fallback for a checkout that still declares them there.
    """
    pyproject = read_text(repo, "pyproject.toml")
    if pyproject:
        table = project_table(repo)
        if table is not None:
            scripts = table.get("scripts")
            if isinstance(scripts, dict):
                target = scripts.get(script)
                if isinstance(target, str) and target.strip():
                    return target.strip()
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
    proc = subprocess.run(
        [str(target_py), "-c", probe],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _refuse(message: str, target_py: Path, repo: Path, remedy: str | None = None) -> int:
    if remedy is None:
        remedy = (
            "Stop the gateway and finish the sync from a terminal: "
            f'"{target_py}" -m pip install -e "{repo}"'
        )
    print(
        f"dep-sync: {message} No dependency was installed. {remedy}",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: dep_sync <repo> <target-python>", file=sys.stderr)
        return 2
    repo, target_py = Path(args[0]), Path(args[1])

    # Establish that the venv about to be written to serves THIS checkout before
    # anything is installed. `<repo>/.venv` is where the interpreter was found,
    # which says nothing about what it is an install of.
    foreign = venv_not_mapped_to(installed_package_origin(target_py), repo)
    if foreign:
        return _refuse(
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
        return _refuse(f"{moved}, so this step would install a stale set.", target_py, repo)

    specs = declared_requirements(repo)
    if specs is None:
        return _refuse(
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
                f"the merged revision requires Python {floor_spec} but the target "
                f"venv runs {version[0]}.{version[1]}.{version[2]}, so its code "
                "cannot be imported.",
                target_py,
                repo,
            )

    if not specs:
        print("dep-sync: no requirements declared; nothing to install")
    else:
        rejected = rejected_specs(specs)
        if rejected:
            return _refuse(
                "the merged revision declares requirements this step will not hand "
                f"to pip: {'; '.join(rejected)}.",
                target_py,
                repo,
            )
        print(
            f"dep-sync: installing {len(specs)} declared requirements; extras are "
            "left alone, exactly as a plain `pip install -e .` leaves them"
        )
        # Hand every spec to pip and let it decide: an already-satisfied requirement
        # is a no-op, so this installs what is new and leaves the rest alone without
        # this module ever comparing a version or evaluating a marker. The project
        # itself is deliberately absent -- installing it is what would rewrite the
        # locked console script.
        # `--` ends pip's option parsing, so a declared requirement can never be
        # read as a flag.
        proc = subprocess.run([str(target_py), "-m", "pip", "install", "--", *specs])
        if proc.returncode != 0:
            return proc.returncode

    # The one thing a dependency-only install cannot deliver: if the merged
    # revision REPOINTED the console script, the wrapper on disk still dispatches
    # to the old target and no amount of dependency installing refreshes it. The
    # dependencies are installed by now, so this reports rather than refuses.
    declared = console_script_target(repo, _SCRIPT)
    installed = installed_console_script_target(target_py, _SCRIPT)
    if declared and installed and declared != installed:
        print(
            f"dep-sync: dependencies are installed, but the {_SCRIPT!r} console "
            f"script is repointed to {declared} by the merged revision while the "
            f"installed wrapper still calls {installed}. That wrapper cannot be "
            "rewritten while a process is running from it: stop the gateway and run "
            f'"{target_py}" -m pip install -e "{repo}" before restarting.',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
