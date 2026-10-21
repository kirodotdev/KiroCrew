"""The ``kirocrew`` launcher a generated spec runs, and first-run setup.

Which ``kirocrew`` binary the managed MCP servers are spawned through, and how each is
invoked; the PATH link ``kirocrew setup`` installs; and the setup a first boot runs.
New work on finding or installing the launcher goes here.

Composed onto ``kiro_crew.agent``, the import path and patch target of every name
here; see :mod:`kiro_crew.agent_parts`.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.agent import (
        _in_ephemeral_tree,
        _in_linked_git_worktree,
        _user_dir,
        logger,
        mkdirs_owner_only,
        platform_compat,
        write_text_owner_only,
    )


# kirocrew binary path — resolved lazily to handle gateway restarts
# where PATH may not include the virtualenv at import time.
_KIROCREW_BIN: str | None = None


def _interpreter_runnable(candidate: Path) -> bool:
    """Return True if *candidate* could actually be exec'd as an interpreter.

    Existence is not enough: a present-but-not-executable interpreter fails at
    exec time (``EACCES`` — "bad interpreter: Permission denied"), so a launcher
    naming one is exactly as dead as a launcher naming a reaped path. Keeping
    this stricter than ``exists()`` is what lets :func:`_bin_is_usable` promise it
    narrows only by provably-dead targets.

    POSIX-only narrowing by construction: Windows has no execute bit and
    ``os.access(f, X_OK)`` is True for any existing file, so this degrades to an
    existence check there rather than validating anything extra.
    """
    return candidate.is_file() and os.access(candidate, os.X_OK)


def _bin_is_usable(path: Path) -> bool:
    """Return True if *path* is a readable launcher whose interpreter still exists.

    Readability alone is not usability. A launcher is a thin wrapper around an
    interpreter living elsewhere, so it OUTLIVES the thing it needs: a reaped work
    directory, a removed ``.venv``, or a pruned bundle leaves an executable file
    that fails at run time with "virtual environment not found". Accepting one
    makes ``ensure_kirocrew_on_path`` publish a machine-wide ``kirocrew`` that is
    broken from the moment it is written — and that function runs on EVERY gateway
    start, so it would keep re-publishing it.

    Two launcher shapes, judged differently because only one of them states the
    answer: a pip console script names its interpreter in the shebang, which stays
    correct for every install layout (a venv, ``python3.12 -m pip install`` into
    ``~/.local/bin``, a distro package), so it is read directly. A shell wrapper's
    shebang names the SHELL, so its interpreter is resolved relative to the
    wrapper instead.

    Nothing is executed, and a launcher naming no interpreter of ours is accepted,
    so this only ever narrows the set by provably-dead targets.
    """
    try:
        with open(path, "rb") as stream:
            head = stream.read(4096)
    except OSError:
        return False
    if not head.startswith(b"#!"):
        # Compiled launcher (pip's Windows .exe, a frozen binary) or a Windows
        # batch shim (`bin\kirocrew.cmd` starts with `@`). The shim DOES name
        # its interpreter (`"%~dp0..\python.exe"`), but we choose not to parse
        # the batch body here; the consumer that spawns it
        # (`_kirocrew_mcp_invocation`) resolves and validates that sibling
        # interpreter itself, mirroring website/electron/main.js.
        return True
    text = head.decode("utf-8", errors="replace")

    shebang = text.splitlines()[0][2:].strip()
    interpreter = shebang.split()[0] if shebang else ""
    # `#!/usr/bin/env python3` names the FINDER, not the interpreter, so it says
    # nothing about a specific path; only an absolute python path is decisive.
    # `is_absolute()` rather than a leading "/" so a native Windows path
    # (`C:\...\python.exe`) is recognised there too -- pip ships a compiled
    # `.exe` launcher on Windows, which returns above, but a shebang script that
    # does reach here must not be judged by a POSIX-only shape.
    candidate = Path(interpreter)
    if candidate.is_absolute() and candidate.name.startswith("python"):
        return _interpreter_runnable(candidate)

    bin_dir = path.parent
    # `<venv>/bin/kirocrew` (already inside the venv) vs `<root>/bin/kirocrew`
    # (the repo launcher and the packaged bundle's wrapper, beside the venv).
    venv_root = bin_dir.parent
    if venv_root.name != ".venv":
        venv_root = venv_root / ".venv"
    checks: list[tuple[str, tuple[Path, ...]]] = [
        (".venv", (venv_root / "bin" / "python", venv_root / "Scripts" / "python.exe")),
        # Packaged PBS bundle: `<root>/bin/python3.12`, beside the launcher. The
        # marker identifies that LAYOUT, not merely a version, and it stays a
        # literal on purpose: widening it to `python3\.\d+` also matches shebangs
        # that name a version while keeping their interpreter somewhere else
        # entirely -- a deployment wrapper's `#!<wrapper> $ENVROOT/python3.10/bin/
        # python3.10` is one, and it then gets held to a sibling `python3.10`
        # that was never supposed to exist, so a working launcher is judged dead.
        # Broadening the marker broadens the OBLIGATION it imposes, which is the
        # opposite of what a liveness check should do when it cannot identify the
        # shape. The same literal appears in `packaging/build-desktop.sh`, which
        # builds this layout; unifying the two is its own change.
        ("python3.12", (bin_dir / "python3.12", venv_root / "bin" / "python3.12")),
    ]
    for marker, candidates in checks:
        if marker not in text:
            continue
        if not any(_interpreter_runnable(c) for c in candidates):
            return False
    return True


def _launcher_works(path: Path) -> bool:
    """Return True if *path* is a launcher that would actually run today.

    Combines the two halves of the question asked of any launcher we did not
    write ourselves: the file is present and executable, AND the interpreter it
    delegates to still exists (:func:`_bin_is_usable`). It decides whether an
    ``~/.local/bin/kirocrew`` that points somewhere ELSE is a working install's
    launcher — which must be left alone — or a dead one we should replace.

    Deliberately not folded into ``ensure_kirocrew_on_path``'s gate on its OWN
    resolved target: that gate additionally requires an absolute path, and its
    interpreter check already happened inside :func:`_resolve_kirocrew_bin`.
    """
    return path.is_file() and os.access(path, os.X_OK) and _bin_is_usable(path)


def _kirocrew_bin_subpath(root: Path) -> Path:
    """The console-script path under an install ``root`` for this OS.

    A venv exposes its entry points under ``bin/kirocrew`` on POSIX but
    ``Scripts/kirocrew.exe`` on Windows — pip generates a ``.exe`` launcher
    there from the ``console_scripts`` entry point. Resolving the POSIX layout
    on Windows finds nothing, which silently drops the built-in
    ``kirocrew-cron`` / ``kirocrew-core`` MCP servers (``command not found:
    .../bin/kirocrew``). Branch on the platform so both layouts resolve.

    On Windows a relocatable ``bin\\kirocrew.cmd`` shim is preferred over the
    pip-generated ``Scripts\\kirocrew.exe`` when it exists. The desktop bundle
    (``packaging/build-desktop.sh``) ships BOTH: pip drops a console-script
    ``.exe`` in ``Scripts\\``, but distlib embeds the ABSOLUTE interpreter path
    of the machine that built it, so inside a shipped bundle that ``.exe``
    points at a build-agent path that does not exist on the user's machine.
    The ``.cmd`` shim resolves the interpreter via ``%~dp0`` and is the only
    relocatable launcher of the two. The Electron resolver
    (``website/electron/find-bin.js``) ranks them the same way — keep the two
    in sync. Plain pip installs ship no ``bin\\kirocrew.cmd``, so they keep
    resolving ``Scripts\\kirocrew.exe`` via the fallback.
    """
    if platform_compat.IS_WINDOWS:
        cmd_shim = root / "bin" / "kirocrew.cmd"
        if cmd_shim.is_file():
            return cmd_shim
        return root / "Scripts" / "kirocrew.exe"
    return root / "bin" / "kirocrew"


def _through_stable_link(path: str) -> str:
    """*path* as a launcher that outlives this process names it.

    On a managed venv the running tree is one versioned tree among several, and
    a later update's prune may delete it once no process runs from it; the
    stable link follows every promotion (see
    :func:`kiro_crew.platform.tree_liveness.through_stable_link`). Only for the
    ``~/.local/bin`` shim: :func:`_resolve_kirocrew_bin` itself keeps the running
    tree, because what this process hands its own children (the built-in MCP
    servers, the jail re-exec) must run the version this process runs, not one a
    promotion made current before this process restarted.
    """
    from kiro_crew.platform.tree_liveness import through_stable_link

    return through_stable_link(path)


def _resolve_kirocrew_bin() -> str:
    """Resolve the absolute path of the ``kirocrew`` executable.

    Resolution order (first existing + executable wins):

    1. A sibling ``.venv`` entrypoint, for a source-tree install (an editable
       install next to its own venv, e.g. ``project/src/kiro_crew`` plus
       ``project/.venv``). Bounded by the first ``pyvenv.cfg`` walking up, so a
       pip-into-venv install falls through to step 2 instead.
    2. Same install as the current process: walk up from ``kiro_crew.__file__``
       looking for a sibling console script (see
       :func:`_kirocrew_bin_subpath` for the per-OS layout). Covers venv-based
       installs, pip installs, source-tree dev trees, and the desktop app —
       whose bundled interpreter is a python-build-standalone tree exposing a
       launcher at its root, reached by this walk from the bundle's
       ``site-packages``.
    3. The running interpreter's own install prefix (``sys.exec_prefix``). Same
       intent as step 2 — the install this process belongs to — for layouts
       where the console script is not an ancestor-sibling of the package and
       the parent walk therefore cannot reach it.
    4. ``shutil.which('kirocrew')`` — respects PATH order.
    5. Bare ``"kirocrew"`` — last resort, may fail but surfaces the problem
       instead of caching a known-bad absolute path.

    Every candidate is validated with ``is_file()`` and ``os.access(X_OK)``
    before being returned, so stale paths from previous installs are skipped.

    The cached answer is re-validated on every call, not trusted for the life
    of the process. A gateway outlives the install it started from: a managed
    update installs the next version beside it and later prunes the old
    directory, and a path cached before the prune would keep being written into
    ``kirocrew.json`` as the launch of ``kirocrew-core`` / ``kirocrew-cron`` --
    which then fail on every spawn until a restart. A cached launcher that no
    longer works is dropped and resolution runs again; steps 1-3 are anchored on
    the (now missing) running package, so they fail and the walk reaches the
    current install through PATH.
    """
    global _KIROCREW_BIN
    if _KIROCREW_BIN:
        if _launcher_works(Path(_KIROCREW_BIN)):
            return _KIROCREW_BIN
        logger.warning(
            "cached kirocrew binary %s no longer works (install pruned?); re-resolving",
            _KIROCREW_BIN,
        )
        _KIROCREW_BIN = None

    def _usable(p: str | Path) -> bool:
        sp = str(p)
        # The empty-string guard is this resolver's own concern: its candidates
        # come from config and env, where "" means "unset". Everything after it is
        # the shared predicate, so the two cannot drift apart.
        return bool(sp) and _launcher_works(Path(sp))

    # 1. Prefer the venv entrypoint for source-tree installs (editable
    #    install with a sibling .venv directory, e.g. project/src/kiro_crew
    #    + project/.venv/bin/kirocrew).
    #    NOTE: For pip-into-venv installs where pkg_dir is inside .venv/,
    #    the pyvenv.cfg guard below breaks early and step 2 handles it.
    try:
        # Circular import: kiro_crew.agent is loaded during kiro_crew
        # package initialization, so importing kiro_crew at module level
        # would create a circular dependency. Deferring here resolves
        # after the package is fully loaded.
        import kiro_crew as _mc  # noqa: PLC0415  circular import

        pkg_dir = Path(_mc.__file__).resolve().parent
        for parent in pkg_dir.parents:
            venv_candidate = _kirocrew_bin_subpath(parent / ".venv")
            if _usable(venv_candidate):
                _KIROCREW_BIN = str(venv_candidate)
                return _KIROCREW_BIN
            if (parent / "pyvenv.cfg").exists():
                break
    except Exception:
        logger.debug("kirocrew venv bin check failed", exc_info=True)

    # 2. Walk up from the running package to find the console script
    try:
        import kiro_crew as _mc  # noqa: PLC0415  circular import

        pkg_dir = Path(_mc.__file__).resolve().parent
        for parent in pkg_dir.parents:
            candidate = _kirocrew_bin_subpath(parent)
            if _usable(candidate):
                _KIROCREW_BIN = str(candidate)
                return _KIROCREW_BIN
            if (parent / "pyvenv.cfg").exists():
                break  # reached venv root without finding the binary
    except Exception:
        logger.debug("kirocrew bin walk failed", exc_info=True)

    # 3. The running interpreter's own install prefix.
    #
    #    Step 2 asks "which install does this process belong to?" but answers it
    #    by walking the package's PARENTS, so it only sees a console script that
    #    sits above ``site-packages``. Layouts that put the two in sibling trees
    #    are invisible to it — a prefix-style runtime can have the package at
    #    ``<root>/lib/python3.12/site-packages/kiro_crew`` and the script at
    #    ``<root>/python3.12/bin/kirocrew``, which is not an ancestor of the
    #    package dir at all. The walk then finds nothing and resolution falls
    #    through to PATH, where an unrelated ``kirocrew`` from some earlier
    #    install wins and gets written into ``kirocrew.json`` as the command for
    #    the built-in MCP servers.
    #
    #    ``sys.exec_prefix`` IS the install root for the interpreter actually
    #    running — the venv root inside a venv, the runtime root otherwise — so
    #    handing it to :func:`_kirocrew_bin_subpath` yields the same directory
    #    ``sysconfig.get_path("scripts")`` would, and keeps the per-OS naming
    #    and the Windows ``.cmd``-over-``.exe`` ranking in one place. Derived
    #    from ``sys`` (already imported, and immune to import shadowing) rather
    #    than by importing ``sysconfig`` here: this module is imported during
    #    ``kiro_crew`` package init, which can run with a user project on
    #    ``sys.path``, and a project-local ``sysconfig.py`` would then execute.
    try:
        candidate = _kirocrew_bin_subpath(Path(sys.exec_prefix))
        if _usable(candidate):
            _KIROCREW_BIN = str(candidate)
            return _KIROCREW_BIN
    except Exception:
        logger.debug("kirocrew exec-prefix bin check failed", exc_info=True)

    # 4. PATH lookup (also validated)
    found = shutil.which("kirocrew")
    if found and _usable(found):
        _KIROCREW_BIN = found
        return _KIROCREW_BIN

    # 5. Last resort — don't cache, so a future call can retry
    logger.warning(
        "Could not resolve kirocrew binary to an existing file; "
        "falling back to bare 'kirocrew' (MCP probes may fail)"
    )
    return "kirocrew"


def _kirocrew_mcp_invocation(subcommand: str) -> tuple[str, list[str]]:
    """Resolve a CWD- and shebang-independent invocation for a built-in
    MCP server (``kirocrew-cron`` / ``kirocrew-core``).

    Prefers a standalone ``kirocrew`` binary when one resolves. Falls back
    to ``<interpreter> [-s] -P -m kiro_crew <subcommand>`` when
    :func:`_resolve_kirocrew_bin` cannot find a usable standalone binary --
    e.g. an install whose launcher is not on the service PATH (the gateway
    running as a systemd user service is the common case): there
    ``_resolve_kirocrew_bin`` returns the bare ``"kirocrew"`` sentinel, the
    command fails to validate, and the server gets dropped from
    ``kirocrew.json`` on every config refresh.

    ``sys.executable`` is the absolute path of the running interpreter, so it
    needs no PATH entry and ignores any broken launcher. ``python -P -m
    kiro_crew`` dispatches the same CLI as the ``kirocrew`` console script
    while keeping the spawn CWD off ``sys.path``.

    A resolved ``bin\\kirocrew.cmd`` (the Windows bundle's relocatable shim,
    see :func:`_kirocrew_bin_subpath`) is unwrapped to the sibling
    interpreter — ``<root>\\python.exe -s -P -m kiro_crew <sub>`` — instead of
    being emitted verbatim. This mirrors ``website/electron/main.js``, which
    refuses to spawn the shim it resolved (Node's ``spawn()`` rejects
    ``.cmd``/``.bat`` without ``shell:true``, CVE-2024-27980 hardening) and
    substitutes exactly this invocation. Whether kiro-cli's spawner handles a
    batch file is its own implementation detail; emitting the interpreter
    directly removes the question — the shim exists for humans and find-bin
    identity, the process tree runs ``python.exe``. When the sibling
    interpreter is missing (corrupted bundle), fall back to
    ``sys.executable``, which inside the bundle IS that interpreter.
    """
    bin_path = _resolve_kirocrew_bin()
    if bin_path == "kirocrew":  # unresolved sentinel from _resolve_kirocrew_bin
        argv = platform_compat.isolated_python_argv("-P", "-m", "kiro_crew", subcommand)
        return argv[0], argv[1:]
    if bin_path.endswith(".cmd"):
        interpreter = Path(bin_path).parent.parent / "python.exe"
        if _interpreter_runnable(interpreter):
            # ``-P`` keeps the spawn CWD off ``sys.path`` on every supported
            # interpreter, so a project package cannot shadow this install.
            # The bundle also pins ``-s`` because its package never relies on
            # per-user site-packages. Keep the shared ``-s -P -m`` order used
            # whenever the helper adds user-site isolation to a fallback.
            argv = platform_compat.isolated_python_argv(
                "-s", "-P", "-m", "kiro_crew", subcommand, executable=interpreter
            )
            return argv[0], argv[1:]
        argv = platform_compat.isolated_python_argv("-P", "-m", "kiro_crew", subcommand)
        return argv[0], argv[1:]
    return bin_path, [subcommand]


def ensure_kirocrew_on_path(
    bin_dir: Path | None = None, *, claim_existing: bool = False
) -> str | None:
    """Ensure a ``kirocrew`` launcher is reachable on the user's PATH.

    The source ``install.sh`` symlinks ``~/.local/bin/kirocrew`` → the venv
    entry point, but install paths that don't run it (notably the packaged
    Electron app) leave no ``kirocrew`` on PATH — breaking the ``kirocrew``
    terminal command. This mirrors that symlink step in Python so it runs from
    ``kirocrew setup``. Best-effort and idempotent:

    * No-op if ``kirocrew`` already resolves on PATH to the same binary.
    * No-op if no concrete binary can be resolved (nothing to point at).
    * No-op if a launcher for a DIFFERENT install is there and still works,
      unless ``claim_existing`` says the user asked for this one by name.
    * Otherwise (re)create ``<bin_dir>/kirocrew`` → the resolved binary.

    Args:
        bin_dir: Target directory for the shim. Defaults to ``~/.local/bin``.
        claim_existing: Take the name over from another install's working
            launcher. ``kirocrew setup`` passes True because the user named this
            install; gateway startup must NOT, since it runs unattended on every
            start and would make the last install to boot win.

    Returns:
        The shim path if one was created/updated, else ``None``.
    """
    # Windows has no ~/.local/bin symlink convention, and creating a symlink
    # there needs Developer Mode or elevation — a normal session raises
    # OSError [WinError 1314] mid-wizard. pip's Scripts\kirocrew.exe console
    # script is already the supported Windows launcher (docs/guides/windows-install.md),
    # so this POSIX install.sh mirror has nothing to do here. Return before any
    # filesystem attempt so `kirocrew setup` never prints a traceback for it.
    if platform_compat.IS_WINDOWS:
        return None

    target = _resolve_kirocrew_bin()
    if os.path.isabs(target):
        # The shim outlives this process, so it follows promotions.
        target = _through_stable_link(target)
    # Nothing concrete to point at — bare "kirocrew" or a non-executable file.
    if not (os.path.isabs(target) and os.path.isfile(target) and os.access(target, os.X_OK)):
        return None

    # Never aim the user's machine-wide launcher at a linked git worktree. A
    # worktree is ephemeral by construction: `git worktree remove` deletes its
    # `.venv` along with the tree, and the shim is then a dangling symlink, so
    # `kirocrew` stops working EVERYWHERE — not just in the tree that went away.
    # Any process running out of a worktree's venv (a pod gateway, a dev run, a
    # `kirocrew setup` invoked from that tree) resolves its own venv entrypoint
    # here, so without this guard routine worktree work silently hijacks the
    # global command. `instances/token_mint.py` documents the same hazard from
    # the consuming side. Declining leaves whatever already worked in place.
    #
    # `.resolve()` first: the ancestry walk is LEXICAL, and the resolved target
    # is frequently itself a symlink into a worktree (a PATH entry, or the very
    # shim we are about to rewrite). Walking the symlink's own parents would find
    # no `.git` marker and wave the worktree through — reopening this hole.
    if _in_linked_git_worktree(Path(target).resolve()):
        logger.info(
            "Not installing a kirocrew launcher: %s is inside a linked git worktree, "
            "which is ephemeral (removing the worktree would break `kirocrew` "
            "machine-wide). Install from your primary clone, or link it yourself: "
            "ln -sfn <clone>/.venv/bin/kirocrew ~/.local/bin/kirocrew",
            target,
        )
        return None

    # Same hazard from the other direction: an AppImage's runtime mount and a
    # scratch tree under the temp dir are both reaped out from under a launcher
    # that points into them — and this function runs on EVERY gateway start, so
    # it would re-create that dangling link every time. Declining leaves
    # whatever already worked in place; a package install (fixed path under
    # /opt) or a venv install is the shape that can carry a durable launcher.
    if _in_ephemeral_tree(Path(target).resolve()):
        logger.info(
            "Not installing a kirocrew launcher: %s is inside an ephemeral tree (an "
            "AppImage runtime mount, or the system temp directory), which is reaped "
            "out from under the link. Install the deb/rpm package for a durable "
            "`kirocrew` on PATH, or link a persistent install yourself.",
            target,
        )
        return None

    # Already reachable on PATH as the same binary? Then there's nothing to do.
    existing = shutil.which("kirocrew")
    if existing and os.path.realpath(existing) == os.path.realpath(target):
        return None

    # Ownership, checked on PATH before the target path: a working `kirocrew`
    # ANYWHERE on PATH already belongs to some install — a pipx bin dir, a distro
    # package, /usr/local/bin — and writing <bin_dir>/kirocrew would shadow it or
    # be shadowed by it depending on PATH order, which is not a decision an
    # unattended start gets to make. The per-path check further down is still
    # needed and is not redundant with this one: it catches a working launcher
    # sitting AT <bin_dir>/kirocrew while <bin_dir> is not on PATH at all.
    if existing and not claim_existing:
        existing_on_path = Path(os.path.realpath(existing))
        if _launcher_works(existing_on_path):
            logger.info(
                "Leaving `kirocrew` on PATH alone: %s -> %s still works and belongs "
                "to another install. Run `kirocrew setup` from the install you want "
                "on PATH to switch it deliberately.",
                existing,
                existing_on_path,
            )
            return None

    bin_dir = bin_dir or (Path.home() / ".local" / "bin")
    link = bin_dir / "kirocrew"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            existing_target = Path(os.path.realpath(link))
            if os.path.realpath(link) == os.path.realpath(target):
                return None
            # A launcher that still WORKS belongs to another install — typically
            # the cli.sh wheel under ~/.kiro/crew-venv — and taking the name from
            # it is not a repair. This runs on EVERY gateway start, so whichever
            # install booted last would win, and the losing installer's upgrades
            # would then land on a path nothing points at: `kirocrew` keeps
            # working, silently at the wrong version, which is worse than a
            # visible break. The documented Linux pairing (cli.sh for the CLI,
            # deb/rpm for the desktop shell) puts both on one machine by design,
            # so this is the ordinary configuration rather than a corner case.
            #
            # An explicit `kirocrew setup` DOES claim the name: the user named
            # this install. A dangling or otherwise dead launcher is replaced on
            # either path — that vacuum is what this function exists to fill.
            if not claim_existing and _launcher_works(existing_target):
                logger.info(
                    "Leaving the existing kirocrew launcher alone: %s -> %s still "
                    "works and belongs to another install. Run `kirocrew setup` "
                    "from the install you want on PATH to switch it deliberately.",
                    link,
                    existing_target,
                )
                return None
            link.unlink()
        link.symlink_to(target)
    except OSError as exc:
        # A best-effort PATH convenience must never dump a traceback into the
        # interactive setup wizard (which runs without logging.basicConfig, so
        # exc_info would hit Python's lastResort handler and print the stack).
        logger.warning("Could not create kirocrew shim at %s: %s", link, exc)
        return None
    logger.info("Linked kirocrew shim: %s -> %s", link, target)
    return str(link)


# One-time migrations performed automatically on gateway first-run (so the
# desktop app, which never runs `kirocrew setup`, still gets them). Lazy
# accessors (same import-side-effect reason as _user_dir in spec_build).
def _migrations_dir() -> Path:
    return _user_dir() / ".migrations"


def _stale_mcp_purge_marker() -> Path:
    return _migrations_dir() / "stale_managed_mcp_purged"


def run_first_run_setup() -> None:
    """Deliver the install-time steps the desktop app needs without a terminal.

    The Electron app only runs ``kirocrew gateway`` — never ``kirocrew
    setup`` — yet several concerns aren't covered by the gateway's agent-config
    rebuild. This is invoked from gateway startup to close that gap:

    * **PATH shim** — ``ensure_kirocrew_on_path()`` is idempotent and only
      writes ``~/.local/bin/kirocrew``, so it runs on every start. It is called
      WITHOUT ``claim_existing`` for exactly that reason: running unattended on
      every start, it must fill an empty or broken slot only, never take the
      command away from another install that still works.
    * **Default-on builtin backfill** — ``defaultEnabled`` is applied only on an
      app's FIRST registration, so a builtin promoted to default-on later never
      reaches installs that already registered it. Runs ONCE, guarded by its own
      marker file, because re-running it would override a user's own disable.
    * **Retired conductor skill cleanup** — removes only byte-exact generated
      revisions of the always-on conductor skill. It runs on every start so a
      package upgrade takes effect without requiring a terminal setup command;
      user-authored and edited files remain untouched.
    * **Stale predecessor MCP purge** — ``clean_stale_managed_mcp()`` mutates
      the user's *global* ``~/.kiro/settings/mcp.json``, so it runs ONCE,
      guarded by a marker file, to honor the "Kiro Crew owns only the agent
      file" boundary (no global rewrite on subsequent starts).

    Best-effort: never raises — any failure is logged and startup continues.
    """
    # 1. PATH shim — safe and idempotent on every start.
    try:
        shim = ensure_kirocrew_on_path()
        if shim:
            logger.info("First-run: linked kirocrew shim at %s", shim)
    except Exception:
        logger.warning("First-run: shim install failed", exc_info=True)

    # 2. Admission-policy seed — one-time, self-guarded by its OWN marker.  Run
    #    BEFORE the stale-MCP early return below so an EXISTING install (which
    #    already has the stale-MCP marker) still gets seeded on its next start;
    #    otherwise those installs would have no policy file and newly fail closed.
    try:
        from kiro_crew.platform.admission import seed_default_policy  # noqa: PLC0415

        if seed_default_policy():
            logger.info("First-run: seeded default admission policy")
    except Exception:
        logger.warning("First-run: admission policy seed failed", exc_info=True)

    # 3. Default-on builtin backfill — one-shot per app, self-recorded on the
    #    app's own installed.json (no marker file: the flag and the state it
    #    guards must land in one atomic write). Placed BEFORE the stale-MCP early
    #    return for the same reason step 2 is, and here the reason is the whole
    #    point: an EXISTING install already holds the stale-MCP marker, and an
    #    existing install is the ONLY kind this step has anything to do (a fresh
    #    one registers these apps enabled and already flagged).
    try:
        from kiro_crew.apps.manager import (  # noqa: PLC0415
            backfill_default_on_builtins,
        )

        flipped = backfill_default_on_builtins()
        if flipped:
            logger.info("First-run: enabled default-on builtin(s): %s", flipped)
    except Exception:
        logger.warning("First-run: default-on builtin backfill failed", exc_info=True)

    # 4. Retired conductor skill cleanup — safe and idempotent on every start.
    try:
        from kiro_crew.skills import remove_retired_conductor_skill  # noqa: PLC0415

        if remove_retired_conductor_skill():
            logger.info("First-run: removed retired conductor skill")
    except Exception:
        logger.warning("First-run: retired conductor skill cleanup failed", exc_info=True)

    # 5. Stale managed-MCP purge — one-time, marker-guarded.
    stale_marker = _stale_mcp_purge_marker()
    if stale_marker.exists():
        return
    try:
        from kiro_crew.mcp_cleanup import clean_stale_managed_mcp  # noqa: PLC0415

        removed = clean_stale_managed_mcp()
        if removed:
            logger.info("First-run: purged stale managed MCP entries: %s", removed)
        # Mark done even when nothing was removed, so the global mcp.json is
        # never re-read/rewritten on later starts.
        mkdirs_owner_only(_migrations_dir())
        write_text_owner_only(stale_marker, datetime.now(timezone.utc).isoformat() + "\n")
    except Exception:
        logger.warning("First-run: stale MCP purge failed", exc_info=True)
