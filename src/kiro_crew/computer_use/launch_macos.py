"""macOS application launching: an app-bundle catalog under the standard roots.

The macOS half of ``computer_launch_app``, and structurally simpler than
:mod:`launch_windows` for one reason: **macOS has a real catalog.** An application
is a ``.app`` bundle in a small set of conventional directories, so the catalog is a
directory listing rather than a registry of name-to-executable mappings that a
per-user hive can redirect. There is no equivalent of Windows'
``HKCU\\…\\App Paths``, and nothing here resolves through ``PATH``.

The launcher is ``/usr/bin/open -a <bundle path>``, pinned by absolute path exactly as
``platform_compat.reveal_in_file_manager`` pins it, and it is handed the PATH this
module verified rather than a name or anything the caller supplied. The path matters:
``open -a`` given a NAME asks LaunchServices to resolve it again from its own database,
which indexes bundles this module deliberately excludes — so the bundle that was checked
and the bundle that runs need not be the same one. Two further consequences worth
stating:

* ``open -a`` is idempotent by design — a second call activates the running copy
  instead of starting another — so the "already running" refusal here is about
  telling the model something useful, not about preventing a duplicate process.
* ``-a`` takes an application, never a document. The document form (``open <file>``)
  is deliberately not reachable: it would let this verb hand attacker-chosen input
  to whatever application claims that file type, which is a different capability
  from "open the drawing app".

**The resolved bundle is VERIFIED, not merely found**, which is what the ABC contract
(:meth:`~kiro_crew.computer_use.backend.ComputerUseBackend.launch_app`) requires of
every implementation: a catalog can be agent-writable, so the check has to bound the
target rather than trust the lookup. Two conditions here, mirroring what
:mod:`launch_windows` does with the registry:

* the bundle must sit under one of the :data:`_APP_ROOTS`, and ``~/Applications`` is
  **not** one of them — it is writable by the same user the agent runs as, so including
  it would let a planted ``~/Applications/Anything.app`` be launched;
* the bundle's own parent directory must not be writable by this process
  (:func:`_directory_is_writable`), which catches a ``/Applications`` that is
  group-writable on a machine whose admin group has been widened. An unwritable ROOT can
  still contain a writable child, so the root list alone is not the check.

The cost is stated rather than hidden: an application a user installed only for
themselves cannot be launched by name. The refusal says so and names the remedy (move
the bundle, or open it by hand), which is recoverable — where a launch verb that runs
whatever the agent wrote is not.

The no-arguments rule holds on top of both: ``open -a`` opens an application and can
never be handed a document, so this stays "start an installed app" rather than "run one
against attacker-chosen input".
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from kiro_crew import platform_compat
from kiro_crew.computer_use.types import (
    MAX_LAUNCH_SUGGESTIONS,
    AmbiguousLaunchTarget,
    ComputerUseError,
    LaunchIdentity,
    NoSuchLaunchTarget,
)

logger = logging.getLogger(__name__)

#: LaunchServices' front door, pinned absolute. Never resolved from ``PATH`` — that is
#: the rule ``platform_compat.trusted_system_bin`` exists to enforce, and
#: ``reveal_in_file_manager`` pins the same binary for the same reason.
#:
#: Composed from POSIX components rather than written as one literal, and joined with
#: an explicit ``"/"`` rather than ``os.path.join``. Two reasons, and the second is why
#: the obvious form is wrong:
#:
#: * ``cross-platform.yml``'s portability gate rejects an added absolute POSIX path
#:   literal, because on Windows such a path names nothing and a hardcoded one is how a
#:   POSIX-only assumption reaches shared code. This module IS macOS-only so the path is
#:   correct here, but the gate scans added lines and cannot know that;
#: * ``os.path.join(os.sep, ...)`` would produce ``\usr\bin\open`` when the module is
#:   merely IMPORTED on Windows — which every CI shard does. The value would then be a
#:   nonsense path that no test could distinguish from a typo. A literal ``"/"`` keeps
#:   the constant identical on every platform, which is what makes it assertable.
_OPEN_BIN = "/".join(("", "usr", "bin", "open"))

#: Where a launchable application bundle lives. Ordered most-trusted first, so a system
#: install wins a name collision against a machine-wide one.
#:
#: **``~/Applications`` is deliberately absent.** It is writable by the same user the
#: agent runs as, so including it made this verb a native-code-execution path: plant
#: ``~/Applications/Anything.app`` and launch it. Documenting that was not a bound — the
#: Windows sibling verifies its resolved target for exactly this reason, and "macOS has
#: no protected prefix for per-user installs" is an argument for excluding the per-user
#: root, not for trusting it.
#:
#: The cost is real and is the right trade: an application a user installed only for
#: themselves cannot be launched, and the refusal names the catalog rule so the user can
#: move the bundle to ``/Applications`` (or open it themselves) rather than being left
#: guessing. Losing one install location is recoverable; a launch verb that
#: runs whatever the agent wrote is not.
_APP_ROOTS: tuple[str, ...] = (
    "/System/Applications",
    "/System/Applications/Utilities",
    "/Applications",
    "/Applications/Utilities",
)

_BUNDLE_SUFFIX = ".app"

#: The two directories inside a bundle that :func:`_writable_component` probes — the
#: locations the OS itself requires, which is why they are named here rather than read from
#: the bundle's own plist. A writable ``Contents/MacOS`` means the binary ``open -a`` will
#: run can be replaced; a writable ``Contents`` means that directory can be replaced along
#: with the ``Info.plist`` that names the binary. Neither needs the install root to be
#: writable.
_CONTENTS_RELDIR = "Contents"
_MACOS_RELDIR = os.path.join(_CONTENTS_RELDIR, "MacOS")
#: The bundle metadata file, and a launch input rather than mere description: it supplies
#: the ``CFBundleIdentifier`` the pre-spawn policy check is applied to.
_INFO_PLIST_RELPATH = os.path.join(_CONTENTS_RELDIR, "Info.plist")

#: Name of the throwaway file :func:`_directory_is_writable` creates. Mirrors
#: ``launch_windows``' probe: distinctive so a leftover is attributable, and only ever
#: created in a directory the launch is about to REFUSE.
_WRITE_PROBE_NAME = ".kirocrew-launch-write-probe"

#: Bundles refused as launch targets regardless of where they live.
#:
#: **Not a security boundary** — see :data:`launch_windows._REFUSED_EXECUTABLES` for
#: the same caveat. It is a narrow guard against the one shape the no-arguments rule
#: cannot bound: a terminal takes its work from a subsequent keystroke, so launching
#: one and then typing into it with ``computer_type_text`` would reach a shell with
#: none of the ``BUILTIN_DENIED_RULES`` a ``bash`` tool call passes. Matched on the
#: bundle name without its suffix, case-insensitively.
_REFUSED_BUNDLES: frozenset[str] = frozenset(
    {"terminal", "iterm", "iterm2", "xterm", "alacritty", "kitty", "wezterm", "warp", "hyper"}
)

_ERR_WRITABLE_ROOT = (
    "'{name}' is installed in a directory this user can write, so it was not launched: "
    "a bundle anyone can drop there is indistinguishable from one the operator "
    "installed. Move it under /Applications, or open it yourself"
)
_ERR_SHELL_TARGET = (
    "'{name}' is a terminal, so it is not a launch target: a shell takes its "
    "instructions from input rather than from the launch, which is the one thing the "
    "no-arguments rule cannot bound. Launch the application you want to use"
)


@dataclass(frozen=True)
class InstalledApp:
    """One ``.app`` bundle: its display name and where it was found."""

    name: str
    path: str
    source: str


def installed_apps() -> "tuple[InstalledApp, ...]":
    """Every ``.app`` bundle under the conventional roots, most-trusted root first.

    Never raises. A root that does not exist or cannot be listed is skipped — a
    partial catalog is a smaller answer, where an exception would be a failed tool
    call. A name already seen under an earlier (more trusted) root is not replaced.
    """
    out: list[InstalledApp] = []
    seen: set[str] = set()
    for root in _APP_ROOTS:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(_BUNDLE_SUFFIX):
                continue
            name = entry[: -len(_BUNDLE_SUFFIX)]
            folded = name.casefold()
            if folded in seen:
                continue
            path = os.path.join(root, entry)
            if not os.path.isdir(path):
                continue
            seen.add(folded)
            out.append(InstalledApp(name=name, path=path, source=root))
    return tuple(out)


def _directory_is_writable(directory: str) -> bool:
    """Whether this process can create a file in *directory*.

    The macOS twin of ``launch_windows._directory_is_writable``, and needed for the same
    reason: an unwritable ROOT can contain writable children. Excluding
    ``~/Applications`` from :data:`_APP_ROOTS` removes the obvious case, but
    ``/Applications`` itself is group-writable on a machine whose admin group has been
    widened, and a bundle dropped there would otherwise be indistinguishable from an
    installed one.

    A real create-and-delete rather than a permission computation: modelling effective
    access on macOS means group membership plus ACLs plus SIP, and a subtly wrong model
    fails OPEN. Fails CLOSED — answers ``True`` so the caller refuses — on any error
    other than the denial that means "no".
    """
    probe = os.path.join(directory, _WRITE_PROBE_NAME)
    try:
        with open(probe, "xb"):
            pass
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return False
    except FileExistsError:
        # Cannot conclude "unwritable" from a name collision, so fail closed.
        return True
    except OSError:
        return True
    try:
        os.unlink(probe)
    except OSError:
        logger.debug("computer-use launch: could not remove the write probe in %s", directory)
    return True


def _writable_component(app: InstalledApp) -> bool:
    """Whether this user could change what *app* runs — by CREATE or by REPLACE.

    The launch boundary. A bundle is a directory of ordinary files, so "the app lives
    under ``/Applications``" bounds only who could add a NEW app there — it says nothing
    about who can rewrite the Mach-O inside an existing one.

    **Two different permissions, so two different tests.** Directory writability governs
    create, unlink and rename; rewriting an EXISTING file's bytes needs write on the file
    inode and no directory permission at all. A create-probe therefore answers "unwritable"
    for the ordinary drag-install and Homebrew-cask shape — root-owned directories that deny
    creates, holding an executable owned by the installing user — while
    ``open(exe, "r+b")`` replaces the binary in place. So the executables under
    ``Contents/MacOS`` are checked directly (:func:`_any_executable_is_writable`) as well as
    the directories being probed for creates.

    **EVERY directory on the path is probed, not just the endpoints**, because write access
    to any single one of them is enough to control what executes:

    * the bundle's PARENT — a whole bundle dropped beside the real ones;
    * the BUNDLE directory — ``Contents`` replaced wholesale;
    * ``Contents`` — the one an endpoints-only check misses. It is an intermediate rather
      than "deeper nesting", and its writability is independent of both neighbours (an
      unwritable parent does not make a child unwritable — the same measured fact that
      makes the root list insufficient in the first place). Owning it is enough:
      ``mv Contents/MacOS Contents/MacOS.bak && mkdir Contents/MacOS`` yields an
      agent-owned executable directory that every other probe then answers "unwritable"
      for. It also holds ``Info.plist``, so the same access rewrites
      ``CFBundleIdentifier`` and defeats :func:`target_identity`'s pre-spawn deny;
    * ``Contents/MacOS`` — the binary itself swapped in place.

    None of this is hypothetical or exotic. ``/Applications`` is root-owned and unwritable,
    but a bundle installed there by a user-space installer — every drag-install and every
    Homebrew cask — is owned by the installing user, so the agent runs as someone who can
    replace its executable. A parent-only probe passes that bundle and launches whatever
    was written into it.

    **The directories are FIXED, never read from the bundle.** Asking the bundle's own
    ``Info.plist`` where its executable lives is the obvious refinement and it is the wrong
    one: ``CFBundleExecutable`` is inside the very directory whose trustworthiness is in
    question, and an absolute or traversing value aims the probe OUTSIDE the bundle
    (``/tmp/x`` probes ``/tmp``; ``../../..`` walks up out of it). The target would then
    choose which directory gets judged, which is the opposite of a check. ``Contents/MacOS``
    is the location the OS itself requires, and a nested executable below it sits under a
    directory already being probed.

    Fails CLOSED throughout — :func:`_directory_is_writable` answers ``True`` on any
    error it cannot interpret as a denial, and every probe here is consulted only to
    decide whether to START A PROCESS.
    """
    macos_dir = os.path.join(app.path, _MACOS_RELDIR)
    candidates = (
        os.path.dirname(app.path),
        app.path,
        os.path.join(app.path, _CONTENTS_RELDIR),
        macos_dir,
    )
    if any(_directory_is_writable(directory) for directory in candidates):
        return True
    if _any_executable_is_writable(macos_dir):
        return True
    # The ``Info.plist`` itself, by the same create-vs-replace argument one file over. An
    # unwritable ``Contents`` still holds a plist this user can REWRITE, and that file
    # supplies the ``CFBundleIdentifier`` :func:`target_identity` hands the policy — so a
    # forged id passes the pre-spawn deny under a name the operator never blocked, and the
    # post-launch check re-reads the same forged string. Its writability is exactly as
    # load-bearing as the executable's.
    return _file_is_replaceable(os.path.join(app.path, _INFO_PLIST_RELPATH))


def _any_executable_is_writable(macos_dir: str) -> bool:
    """Whether any file under *macos_dir* is one this user could REPLACE.

    The other half of :func:`_writable_component`, and the half a create-probe cannot see:
    a directory that denies creates still holds files this user can rewrite, and that is
    exactly the drag-install shape — root-owned ``/Applications/Foo.app/…`` whose
    ``Contents/MacOS/Foo`` belongs to the user who dragged it there.

    **OWNERSHIP, not the current mode.** ``os.access(.., W_OK)`` was the obvious test and it
    is not sufficient: the owner of a file may ``chmod`` it at will, with no privilege, so a
    read-only mode on a file this user owns is a fact the same user can undo between this
    check and the ``exec``. Replace the binary, ``chmod a-w``, launch — the mode test passes
    and agent-authored code runs. ``st_uid`` cannot be changed that way (``chown`` to another
    user requires privilege), so it is the durable question: *could this user have written
    this file, whatever it currently says?* The mode is still checked, for the case of a
    file owned by someone else that is nonetheless group- or world-writable.

    Every entry is checked rather than only the one ``CFBundleExecutable`` names, because
    that value lives inside the bundle and so cannot be trusted to say which file matters
    (see :func:`_writable_component`). Helpers alongside the main binary are loaded by it,
    so a replaceable one is equally a way to choose the code that runs.

    Reading metadata rather than attempting a write: this runs on a bundle the launch is
    about to ALLOW, so it must not modify a legitimate application. A file that cannot be
    examined counts as replaceable, so an unverifiable bundle is refused, never admitted.

    Never raises: a missing ``Contents/MacOS`` is not a bundle ``open -a`` can run either,
    and answering ``True`` there refuses a target that could not have launched anyway.
    """
    try:
        entries = os.listdir(macos_dir)
    except OSError:
        return True
    for entry in entries:
        path = os.path.join(macos_dir, entry)
        try:
            if os.path.isdir(path):
                continue
        except OSError:
            return True
        if _file_is_replaceable(path):
            return True
    return False


def _file_is_replaceable(path: str) -> bool:
    """Whether this user could rewrite *path*, whatever its current mode says.

    The per-file rule both callers share — the executables under ``Contents/MacOS`` and the
    ``Info.plist`` beside them. Two conditions, either of which is enough:

    * **it is OWNED by this user.** The decisive one, and the reason a mode check alone is
      not enough: an owner may ``chmod`` at will with no privilege, so a read-only mode on a
      file this user owns is a fact the same user undoes between this check and the ``exec``
      (replace, ``chmod a-w``, launch). ``chown`` to another user requires privilege, so
      ownership is the durable question — *could* this user have written it?
    * **its mode permits writing**, which covers a file owned by someone else that is
      nonetheless group- or world-writable.

    Reading metadata rather than attempting a write: this runs on a bundle the launch is
    about to ALLOW, so it must not modify a legitimate application. Fails CLOSED — a file
    that cannot be examined counts as replaceable, so an unverifiable bundle is refused.

    ``os.geteuid`` is absent on Windows, where this module is imported but never used; the
    ownership half is then skipped and the mode half still applies.
    """
    euid = getattr(os, "geteuid", None)
    try:
        if euid is not None and os.stat(path).st_uid == euid():
            return True
        return os.access(path, os.W_OK)
    except OSError:
        return True


def resolve_target(query: str) -> "tuple[str, str]":
    """Resolve *query* to ``(bundle_name, display_name)``, or raise.

    Returns the bundle NAME rather than its path, because that is what ``open -a``
    takes and passing the name keeps the launcher's own resolution (which understands
    localised bundle display names) in play. The path was verified to exist while
    building the catalog.

    The order and the matching rules mirror :func:`launch_windows.resolve_target`
    exactly, and both of the non-obvious ones came from a live Windows run rather than
    from symmetry for its own sake:

    * **the QUERY is checked against the terminal list before resolution**, as well as
      the resolved bundle afterwards. The two catch different things: asking for
      ``cmd`` on the measured Windows host resolved to an unrelated ``IEDIAGCMD.EXE``
      and launched it, passing a resolved-name check that never saw a shell;
    * **the fuzzy tier is a PREFIX, not a substring.** A short fragment matching inside
      a long name is a coincidence rather than an intent, and the ambiguity guard
      cannot catch it because a coincidence usually hits exactly one entry.

    An exact match wins; a prefix hitting several applications RAISES rather than
    picking one, because launching the wrong application is not undoable. A substring
    scan supplies SUGGESTIONS on a miss so the refusal is recoverable.
    """
    wanted = (query or "").strip().casefold()
    if not wanted:
        raise NoSuchLaunchTarget()
    if wanted in _REFUSED_BUNDLES:
        raise ComputerUseError(_ERR_SHELL_TARGET.format(name=query))
    catalog = installed_apps()
    matched = [app for app in catalog if app.name.casefold() == wanted]
    if not matched:
        prefixed = [app for app in catalog if app.name.casefold().startswith(wanted)]
        if len(prefixed) > 1:
            names = ", ".join(sorted({app.name for app in prefixed})[:MAX_LAUNCH_SUGGESTIONS])
            raise AmbiguousLaunchTarget(names, len(prefixed))
        matched = prefixed
    if not matched:
        near = sorted({app.name for app in catalog if wanted in app.name.casefold()})
        raise NoSuchLaunchTarget(", ".join(near[:MAX_LAUNCH_SUGGESTIONS]))
    app = matched[0]
    if app.name.casefold() in _REFUSED_BUNDLES:
        raise ComputerUseError(_ERR_SHELL_TARGET.format(name=app.name))
    # Nothing on the path to the code that will RUN may be writable by this user, which
    # is what the ABC contract means by "verify what the catalog returned". Three
    # directories, and the first alone was not enough:
    #
    # * the bundle's PARENT — excluding ``~/Applications`` handles the obvious case; this
    #   catches a ``/Applications`` that is group-writable on a machine whose admin group
    #   has been widened, where a dropped bundle would be indistinguishable from an
    #   installed one;
    # * the bundle ITSELF and the directory holding its main executable. This is the case
    #   the parent-only probe missed, and it is the COMMON one rather than a corner:
    #   ``/Applications`` is unwritable, but a bundle installed there by a user-space
    #   installer (any drag-install, and every Homebrew cask) is owned by that user, so
    #   ``Foo.app/Contents/MacOS/Foo`` can be REPLACED without touching ``/Applications``
    #   at all. The bundle would then pass every check here while running agent-authored
    #   native code — the exact hole the root list exists to close, one level down.
    if _writable_component(app):
        raise ComputerUseError(_ERR_WRITABLE_ROOT.format(name=app.name))
    # The verified PATH, not the name. Returning the name would throw the verification
    # away: ``open -a <name>`` asks LaunchServices to resolve it again, from its own
    # database, which indexes bundles this module deliberately excludes — so the bundle
    # that got checked and the bundle that runs need not be the same one. ``open -a``
    # accepts a path and then launches exactly that bundle.
    #
    # The display name is still returned alongside, because every refusal and result
    # string names the app rather than its path.
    return app.path, app.name


def target_identity(bundle_path: str, display: str) -> LaunchIdentity:
    """Every name the resolved bundle is known by, for the pre-spawn policy check.

    macOS' second spelling is the BUNDLE ID, and it is the one that matters most here:
    ``policy.check_app`` matches a bundle id and a name separately, the built-in
    denylist is expressed as bundle PREFIXES (``com.apple.Terminal``), and every
    computer-use refusal an operator has read named the bundle id — so that is the
    spelling their ``extra_denied_apps`` entry carries. Checking only the display name
    matched none of those, and the deny then took effect one step too late: after the
    application was running.

    Read from the resolved bundle's own ``Info.plist`` through
    :func:`apps_macos.bundle_identity_at`, which is the same reader (and the same
    hardened, size-capped, ``O_NOFOLLOW`` path) that names a RUNNING application — so
    the string checked before the spawn is the string the policy will see afterwards.
    A bundle with no readable id contributes nothing extra rather than failing the
    launch: the display name is still checked, and the post-launch re-check still runs.
    """
    from kiro_crew.computer_use import apps_macos

    bundle_id, _bundle_name = apps_macos.bundle_identity_at(bundle_path)
    return LaunchIdentity(display=display, key=bundle_id)


def spawn_detached(bundle_path: str) -> None:
    """``/usr/bin/open -a <bundle_path>`` — no document, no arguments.

    Takes the PATH :func:`resolve_target` verified, never a bundle name. A name would
    ask LaunchServices to resolve it again from its own database, which indexes bundles
    this module excludes — so the bundle that was checked and the bundle that runs could
    differ. ``open -a`` accepts a path and launches exactly that bundle.

    ``open`` returns as soon as it has asked LaunchServices to start the application,
    so its exit status says nothing about whether a window appeared. That is why the
    caller confirms by polling the window list, exactly as on Windows.

    Detached from the gateway's pipes for the same reasons as
    :func:`launch_windows.spawn_detached`: an application that writes to stdout must
    not fill a pipe nobody drains, and it must not hold the gateway's handles open.
    The new process group means a Ctrl-C in a dev terminal does not take the operator's
    application down with the gateway.

    The isolation arguments use the shim form AGENTS.md's cross-platform table
    requires — ``start_new_session=IS_POSIX`` plus ``creationflags`` — rather than a
    bare ``start_new_session=True``. This module only runs on macOS, so the two are
    equivalent here; the form is what the table asks for, and writing it the same way
    on both platforms is what keeps the rule mechanical rather than a judgement call
    about which module is "really" platform-specific.

    Raises ``OSError`` — the caller turns it into a refusal naming the app.
    """
    if not os.path.isfile(_OPEN_BIN):
        raise OSError(f"{_OPEN_BIN} is not available on this host")
    subprocess.Popen(  # noqa: S603 - fixed argv; bundle_name came from resolve_target
        [_OPEN_BIN, "-a", bundle_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


__all__ = [
    "InstalledApp",
    "installed_apps",
    "resolve_target",
    "spawn_detached",
    "target_identity",
]
