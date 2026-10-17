"""Detect a standard-library module that resolves from OUTSIDE the interpreter.

Python puts the launch directory at ``sys.path[0]`` -- the current working
directory for ``python -m kiro_crew`` and ``python -c``, the script's directory
for a console script -- unless the interpreter runs with ``-P`` /
``PYTHONSAFEPATH``. Anything in that directory whose name matches a standard
library package (``concurrent/``, ``json/``, ``logging/``, ``types.py`` ...) is
then imported INSTEAD of the real one, ahead of the stdlib. The same holds for
a stdlib-named package left in ``PYTHONPATH`` or installed into site-packages
(the Python 2 ``futures`` backport ships a ``concurrent`` package).

The failure that produces is deliberately not an ImportError: the foreign copy
imports fine and breaks later, on the first API the real module has and the
copy does not -- ``TypeError: ThreadPoolExecutor.__init__() got an unexpected
keyword argument 'thread_name_prefix'`` from a ``~/concurrent/`` directory is
the shape that reached the field, and it cost three rounds of misdiagnosis
(``PYTHONPATH``, a Python 2 site-packages, ``.pth`` files) before anyone looked
at ``concurrent.__file__``. This module asks that one question up front.

Everything here is standard library only: it runs from the process entry
points before the package's dependencies are known to exist. It also imports
NOTHING the entry points have not already imported -- ``os``, ``sys`` and
``site`` are loaded by interpreter start-up, and ``importlib`` by both entry
points before this module (``_bootstrap`` directly, ``__main__`` through
``platform_compat``). A probe that pulled in a fresh stdlib name would execute
that name from the very directory it is about to inspect.

Detection is FAIL-CLOSED towards "not shadowed". A module is reported only
when the ``sys.path`` entry that provided it is one where the standard library
never legitimately lives -- the launch entry, a ``PYTHONPATH`` entry, or a
site-packages directory. A layout this module does not recognise is therefore
never refused; the risk being guarded is a refused start on a healthy install,
which would be strictly worse than the raw ``TypeError`` it replaces.
"""

# No ``from __future__ import annotations``: that statement imports the
# ``__future__`` module at run time, and a ``__future__.py`` in the launch
# directory is exactly the kind of file this module exists to catch. The
# annotations below are all valid at run time on the interpreters this package
# supports (PEP 604 unions, builtin generics), so nothing needs deferring.
import importlib.util
import os
import site
import sys

#: Standard-library names Kiro Crew imports at startup whose shadowing has
#: either been seen in the field (``concurrent``) or is one stray directory
#: away in any home directory. Checked with ``find_spec`` -- located, never
#: imported -- so listing a name here costs nothing at runtime beyond a path
#: lookup and never executes foreign code.
STDLIB_PROBE_MODULES: tuple[str, ...] = (
    "concurrent",
    "asyncio",
    "json",
    "logging",
    "queue",
    "socket",
    "select",
    "ssl",
    "types",
    "typing",
    "dataclasses",
    "enum",
    "subprocess",
    "threading",
    "pathlib",
    "email",
    "http",
    "urllib",
    "sqlite3",
    "tempfile",
    "hashlib",
    "secrets",
    "uuid",
    "signal",
    "argparse",
    "platform",
    "collections",
    "functools",
    "datetime",
    "re",
)

#: Exit status of a start refused because the stdlib is shadowed. The same
#: value argparse uses for an invocation that cannot run as given, because that
#: is the class this belongs to: the environment, not the program, is wrong.
SHADOW_EXIT_STATUS = 2


class ShadowedModule:
    """One stdlib name resolved from a directory the stdlib never lives in.

    A plain class rather than a dataclass on purpose: ``dataclasses`` is itself
    a shadowable stdlib name this module would otherwise be the first to import.
    """

    __slots__ = ("name", "resolved", "path_entry", "entry_kind")

    def __init__(self, name: str, resolved: str, path_entry: str, entry_kind: str) -> None:
        #: The probed module name.
        self.name = name
        #: Where the name actually resolves (``__init__.py`` for a package).
        self.resolved = resolved
        #: The ``sys.path`` entry that supplied it, as it appears on ``sys.path``
        #: (``""`` for the ``-c`` launch entry).
        self.path_entry = path_entry
        #: Which class of entry that is: ``launch directory``, ``PYTHONPATH`` or
        #: ``site-packages``. Human-readable; also what the refusal names.
        self.entry_kind = entry_kind

    def __repr__(self) -> str:
        return (
            f"ShadowedModule(name={self.name!r}, resolved={self.resolved!r}, "
            f"path_entry={self.path_entry!r}, entry_kind={self.entry_kind!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ShadowedModule):
            return NotImplemented
        return (self.name, self.resolved, self.path_entry, self.entry_kind) == (
            other.name,
            other.resolved,
            other.path_entry,
            other.entry_kind,
        )


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path or os.curdir)
    except OSError:
        return path


def _launch_entry() -> str | None:
    """The ``sys.path[0]`` the interpreter inserted for the launch, or None.

    Under ``-P`` / ``PYTHONSAFEPATH`` no entry is inserted, so there is nothing
    to classify. Python 3.10 has no ``safe_path`` flag; ``getattr`` keeps this
    importable there even though the package itself needs 3.12.
    """
    if getattr(sys.flags, "safe_path", False):
        return None
    return sys.path[0] if sys.path else None


def _pythonpath_entries() -> set[str]:
    """The roots ``PYTHONPATH`` puts on ``sys.path``, realpath'd.

    An EMPTY component is not nothing: ``PYTHONPATH=:`` makes the interpreter
    insert the current directory, as an absolute path, ahead of the stdlib --
    and it does so under ``-P`` too, which only suppresses the launch entry.
    So an empty component maps to the cwd here, or a shadow reached that way
    would sit on ``sys.path`` unclassified and escape the check. A wholly
    empty variable inserts nothing and is left alone.
    """
    raw = os.environ.get("PYTHONPATH", "")
    if not raw:
        return set()
    return {_realpath(p or os.curdir) for p in raw.split(os.pathsep)}


def _site_packages_entries() -> set[str]:
    roots: set[str] = set()
    try:
        roots.update(_realpath(p) for p in site.getsitepackages())
    except Exception:  # pragma: no cover - a venv without a site module patched in
        pass
    try:
        roots.add(_realpath(site.getusersitepackages()))
    except Exception:  # pragma: no cover
        pass
    return roots


def _stdlib_roots() -> set[str]:
    """Directories the standard library legitimately loads from.

    ``os`` is imported by the interpreter's own start-up, before the launch
    entry is inserted into ``sys.path``, so its location is the one stdlib path
    no shadow can have forged. The C-extension tree is its fixed sibling:
    ``lib-dynload`` beside the pure-Python tree on POSIX, ``DLLs`` under the
    base prefix on Windows. Derived by hand rather than through ``sysconfig``
    because that module is itself a shadowable stdlib name this function would
    otherwise be the first to import.
    """
    roots: set[str] = set()
    os_file = getattr(os, "__file__", None)
    if os_file:
        stdlib = os.path.dirname(os_file)
        roots.add(_realpath(stdlib))
        roots.add(_realpath(os.path.join(stdlib, "lib-dynload")))
    roots.add(_realpath(os.path.join(sys.base_prefix, "DLLs")))
    return roots


def _entry_kinds() -> dict[str, str | None]:
    """Classify every ``sys.path`` entry once: its kind, or None for a legitimate root.

    A launch directory that IS the stdlib (``cd lib/python3.12 && python -m
    ...``) is not a shadow: the module found under it is the real one.
    """
    launch = _launch_entry()
    stdlib_roots = _stdlib_roots()
    pythonpath = _pythonpath_entries()
    site_pkgs = _site_packages_entries()
    kinds: dict[str, str | None] = {}
    for entry in sys.path:
        if not isinstance(entry, str) or entry in kinds:
            continue
        real = _realpath(entry)
        if real in stdlib_roots:
            kinds[entry] = None
        elif launch is not None and entry == launch:
            kinds[entry] = "launch directory"
        elif real in pythonpath:
            kinds[entry] = "PYTHONPATH"
        elif real in site_pkgs:
            kinds[entry] = "site-packages"
        else:
            kinds[entry] = None
    return kinds


def _provider_dir(origin: str) -> str:
    """The directory a ``sys.path`` entry must BE to have provided *origin*.

    The path finder loads ``<entry>/<name>.py``, ``<entry>/<name>.<abi>.so``
    or ``<entry>/<name>/__init__.py`` -- never anything deeper -- so the
    provider of a module is exactly its file's directory, and of a package
    exactly its ``__init__.py``'s grandparent. Containment would be wrong here:
    with ``<stdlib>/collections`` as the launch directory, the real
    ``collections/__init__.py`` lies WITHIN that entry, yet the entry did not
    provide it, and reporting it would refuse a healthy start.
    """
    real = _realpath(origin)
    parent = os.path.dirname(real)
    if os.path.basename(real).startswith("__init__."):
        return os.path.dirname(parent)
    return parent


def _providing_entry(origin: str, real_entries: list[tuple[str, str]]) -> str | None:
    """The first ``sys.path`` entry whose directory IS *origin*'s provider, in
    ``sys.path`` order (the order the finder searched), or None."""
    provider = _provider_dir(origin)
    for entry, real_entry in real_entries:
        if real_entry == provider:
            return entry
    return None


def find_shadowed_stdlib() -> list[ShadowedModule]:
    """Return every probed stdlib name that resolves from a non-stdlib root.

    Uses :func:`importlib.util.find_spec`, which reports an already-imported
    module's spec and LOCATES an unimported one without executing it, so this
    is safe to call from an entry point before anything else runs. Built-in
    and frozen modules have no file and are never shadowed by a directory.
    """
    kinds = _entry_kinds()
    real_entries = [(entry, _realpath(entry)) for entry in kinds]
    found: list[ShadowedModule] = []
    for name in STDLIB_PROBE_MODULES:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            # A shadow so broken it cannot even be located is reported by the
            # import that trips over it; this probe answers only the located case.
            continue
        origin = getattr(spec, "origin", None) if spec is not None else None
        if (
            not origin
            or origin in ("built-in", "frozen")
            or not getattr(spec, "has_location", False)
        ):
            continue
        entry = _providing_entry(origin, real_entries)
        if entry is None:
            continue
        kind = kinds.get(entry)
        if kind is None:
            continue
        found.append(ShadowedModule(name=name, resolved=origin, path_entry=entry, entry_kind=kind))
    return found


def format_shadow_report(shadows: list[ShadowedModule]) -> str:
    """The refusal text: what resolved where, which entry did it, and the fix.

    Written for the reader who has just seen a bare ``TypeError`` from deep
    inside asyncio and has no reason to suspect their home directory. Names
    the entry and its class so the three usual wrong turns -- PYTHONPATH, a
    Python 2 site-packages, ``.pth`` files -- are ruled in or out on sight.
    """
    lines = ["Refusing to start: the Python standard library is shadowed."]
    for s in shadows:
        entry = s.path_entry or os.getcwd()
        # ascii(): a path is caller-chosen bytes. Control characters are escaped
        # rather than written to the terminal, and non-ASCII is escaped because
        # this prints before the console is UTF-8 on Windows, where a raw
        # non-ASCII path would turn the refusal itself into a UnicodeEncodeError.
        lines.append(f"  {s.name} -> {ascii(s.resolved)}")
        lines.append(f"      provided by sys.path entry {ascii(entry)} ({s.entry_kind})")
    lines.append(
        "A directory or module named like a standard-library module sits on"
        " sys.path ahead of the interpreter's own copy, so Kiro Crew would run on"
        " foreign code and fail later with an unrelated-looking error (typically a"
        " TypeError from asyncio or concurrent.futures)."
    )
    # The remedy names the DETECTED shadow, not the entry: for a launch
    # directory the entry is the cwd itself (often the home directory), and
    # the thing to move is the stdlib-named package or module sitting in it.
    # The path is caller-chosen bytes and the line is meant to be PASTED, so
    # the command is only offered when the path can be quoted for a POSIX
    # shell without any interpretation left over (see ``remedy_command``).
    remedy = remedy_command(shadows[0])
    if remedy is None:
        lines.append(
            "Fix: move or rename the shadowed module or package named above,"
            " or start kirocrew from a different directory."
            " This is not caused by Kiro Crew's configuration or data home."
        )
    else:
        lines.append(
            f"Fix: move or rename it (e.g. `{remedy}`),"
            " or start kirocrew from a different directory."
            " This is not caused by Kiro Crew's configuration or data home."
        )
    return "\n".join(lines)


def _shadow_path(resolved: str) -> str:
    """The file or package directory to move: the package dir for an
    ``__init__`` origin, the module file otherwise."""
    if os.path.basename(resolved).startswith("__init__."):
        return os.path.dirname(resolved)
    return resolved


def _shell_single_quote(text: str) -> str | None:
    """*text* as ONE POSIX shell word, or None when it cannot be made safe to paste.

    Single quotes disable every kind of interpretation -- ``$(...)``, backticks,
    ``$VAR``, globbing, backslashes -- and an embedded ``'`` is spelled ``'"'"'``
    (close, a double-quoted apostrophe, reopen). Nothing is escaped by hand
    beyond that, so there is no second quoting layer to get wrong. Control
    characters and non-ASCII are refused rather than quoted: a terminal may
    interpret the former on display and the latter cannot be shown before the
    console is UTF-8, so such a path gets no pasteable command at all. Hand-rolled
    rather than ``shlex.quote`` because this module must not import a stdlib
    name the entry points have not already loaded.
    """
    if not text or any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in text):
        return None
    return "'" + text.replace("'", "'\"'\"'") + "'"


def remedy_command(shadow: ShadowedModule) -> str | None:
    """A pasteable ``mv <shadow> <shadow>.bak`` for *shadow*, or None when the
    path holds characters that cannot be quoted safely for display and paste."""
    path = _shadow_path(shadow.resolved)
    quoted = _shell_single_quote(path)
    backup = _shell_single_quote(path + ".bak")
    if quoted is None or backup is None:
        return None
    return f"mv {quoted} {backup}"


def refuse_if_stdlib_shadowed() -> None:
    """Exit with :data:`SHADOW_EXIT_STATUS` and a named cause when the stdlib is shadowed.

    Called from every process entry (``python -m kiro_crew``, the ``kirocrew``
    console script) BEFORE the CLI is imported. Continuing is never useful: the
    shadow that is compatible enough to import is exactly the one that breaks
    at an arbitrary later call, so the earliest named refusal is the cheapest.
    The message is ASCII-only because the console-script entry prints before
    ``platform_compat.ensure_utf8_console()`` has run.
    """
    shadows = find_shadowed_stdlib()
    if not shadows:
        return
    print(format_shadow_report(shadows), file=sys.stderr)
    raise SystemExit(SHADOW_EXIT_STATUS)
