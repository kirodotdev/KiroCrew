"""Windows application launching: an OS catalog, verified against protected roots.

``computer_launch_app`` is the only verb in this package that starts a PROCESS, so
its target resolution is the narrowest thing here. This module answers one question
— "which installed application does this name mean, and may we run it?" — and the
shape of the answer is set by four measurements on Windows 11, all of which say the
obvious approach is unsafe:

* **``HKCU\\…\\App Paths`` is writable by the same unprivileged user the agent runs
  as.** Verified by creating a key and pointing it at another binary.
* **``%LOCALAPPDATA%\\Microsoft\\WindowsApps`` is on ``PATH`` and writable**, and it
  is what ``shutil.which("mspaint")`` resolves to. So a ``PATH`` lookup resolves the
  drawing app through a directory the agent can plant a binary in.
* **The per-user Start Menu is writable too**, so a ``.lnk`` there is agent-authored
  input, not an OS statement.
* **No fully-protected source enumerates a packaged app's AUMID.**
  ``C:\\Program Files\\WindowsApps`` is protected but *not listable*
  (``os.listdir`` → ``WinError 5``), the machine-wide
  ``Windows.Launch\\PackageId`` key holds only system components, and the
  ``StateRepository`` database is unreadable. Paint — the canonical drawing target
  — is a packaged app, so a "trusted catalog only" design cannot reach it at all.

**So the catalog is untrusted INPUT and the verified target is the guarantee.**
:func:`installed_apps` reads every catalog it can, including the writable ones, and
:func:`resolve_target` then refuses anything whose executable does not sit under a
root this user cannot write. That is the same shape ``platform_compat.
trusted_system_bin`` uses — ignore the search path, verify the candidate's
directory — widened from two system directories to the protected install roots,
because an application is not a system binary and does not live in ``System32``.

Three properties make that verification hold rather than merely look strict:

* **``os.path.realpath`` before the prefix test.** A junction is resolved, so a link
  under a writable directory cannot borrow a protected prefix. The reverse — a
  junction *under* a protected root aimed at agent-writable content — was attempted
  and refused by the OS (``mklink /J`` under ``C:\\Program Files`` → "Access is
  denied"), which is what makes the prefix test meaningful in both directions.
* **The basename must match the catalog key.** An agent that rewrites an ``App
  Paths`` value can then only aim it at a file that already exists under a protected
  root *and* is named what the key claims, which reduces the rewrite to "run an app
  that is already installed under its own name". Measured against every real entry
  on this host: 46 of 47 resolvable entries satisfy it, the one exception being a
  deliberate Microsoft alias (``IEDIAG.EXE`` → ``IEDIAGCMD.EXE``), which is refused
  rather than special-cased.
* **No arguments, ever.** The launch argv is exactly ``[executable]``. A document
  path or a URL would make this verb a way to hand attacker-chosen input to an
  arbitrary installed application, which is a different capability from "open the
  drawing app".

Window confirmation is :mod:`apps_windows`' job. The one native call here is the file-owner
lookup in :func:`_owned_by_current_user`, and like ``winreg`` its ``ctypes`` import is
deferred into the function so this module still imports on a Linux CI runner.

**``winreg`` is imported inside the function that uses it**, not at module scope, and
that is required rather than tidy: the module does not exist on Linux or macOS, so a
module-scope import would raise ``ImportError`` there — breaking
``test_every_module_imports_on_any_platform`` and, worse, the collection of every test
that transitively touches ``kiro_crew`` on the CI fleet. It is the same rule the
sibling modules follow for their native libraries, applied to a stdlib module that
happens to be platform-specific.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.computer_use.types import (
    MAX_LAUNCH_SUGGESTIONS,
    AmbiguousLaunchTarget,
    ComputerUseError,
    LaunchIdentity,
    NoSuchLaunchTarget,
)

logger = logging.getLogger(__name__)

#: ``App Paths`` — the OS's own name-to-executable catalog, read from BOTH hives.
#:
#: The HKCU hive is included even though it is WRITABLE, and that is deliberate: it
#: is where every per-user install registers itself (Paint, Notepad, Teams and the
#: Python launchers are all here on the measured host), so excluding it would leave
#: the catalog naming almost nothing a user actually runs. Its writability is
#: neutralised by :func:`resolve_target`'s protected-root + basename check rather
#: than by refusing to read it.
_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
#: Hive ATTRIBUTE NAMES rather than their values, because the values live in
#: ``winreg`` and this module must not touch it at import time (see the docstring).
#: HKLM precedes HKCU so a machine-wide install wins a name collision.
_APP_PATHS_HIVES: "tuple[tuple[str, str], ...]" = (
    ("HKEY_LOCAL_MACHINE", "HKLM"),
    ("HKEY_CURRENT_USER", "HKCU"),
)


def _winreg() -> Any:
    """The ``winreg`` module, imported on first use.

    Deferred for the reason in the module docstring: ``winreg`` is Windows-only, and a
    module-scope import would make this module unimportable on the CI fleet. Typed
    ``Any`` because mypy runs with ``--platform linux``, where the module's attributes
    are genuinely absent — annotating it more precisely would require the platform
    this code is not analysed on.
    """
    import winreg

    return winreg


#: Where the install roots are read FROM, and this is the load-bearing part of the
#: whole boundary: ``HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion``, which an
#: unprivileged user cannot write (verified: ``CreateKeyEx`` → "Access is denied").
#:
#: **NOT ``os.environ``.** ``%ProgramFiles%`` is an ordinary environment variable, so a
#: caller that controls the gateway's environment can point it at a directory it CAN
#: write — and the protected-root check would then admit a planted binary. Measured:
#: with ``ProgramFiles`` set to a temp directory, ``_under_protected()`` answers True for
#: a file written there, which defeats the entire verification in one line. ``platform_compat`` already declines to
#: trust the environment for exactly this reason (see ``_windows_system_dirs``, which
#: prefers ``GetSystemDirectoryW``); this is the same rule applied to the install roots.
_INSTALL_ROOT_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion"
_INSTALL_ROOT_VALUES: tuple[str, ...] = (
    "ProgramFilesDir",
    "ProgramFilesDir (x86)",
    "ProgramW6432Dir",
)
#: Fallbacks for the unexpected case where the registry names no install root — a
#: broken image, or a future Windows that moved these values. Hardcoded conventional
#: paths rather than an environment read, because a fallback that trusts the
#: environment reopens the hole the registry lookup exists to close. An absent
#: directory is simply skipped.
#:
#: ``C:\Windows`` is deliberately NOT here, and that is the difference between a root
#: that looks protected and one that is. The directory itself is unwritable, but three
#: of its descendants are writable by an unprivileged user — measured on this host:
#: ``C:\Windows\Temp``, ``C:\Windows\Tasks`` and
#: ``C:\Windows\System32\spool\drivers\color``. A prefix test against ``C:\Windows``
#: therefore accepts a planted ``C:\Windows\Temp\Evil.exe``, and an ``App Paths`` entry
#: in the writable ``HKCU`` hive can name exactly that. The system binaries that DO
#: belong come from ``platform_compat._windows_system_dirs()`` instead, which names
#: ``System32`` specifically (resolved from ``GetSystemDirectoryW``) rather than the
#: whole Windows tree.
_PROTECTED_FALLBACK_ROOTS: tuple[str, ...] = (
    r"C:\Program Files",
    r"C:\Program Files (x86)",
)

#: Executable suffixes an ``App Paths`` key may carry. Mirrors
#: ``platform_compat._WINDOWS_BIN_SUFFIXES``' reasoning: the key is spelled with the
#: extension (``mspaint.exe``) while a user types the stem (``mspaint``).
_EXE_SUFFIX = ".exe"

#: Win32 constants for the file-owner lookup in :func:`_owned_by_current_user`. Named here
#: rather than inline because they are wire values from ``winnt.h`` / ``accctrl.h`` and a
#: transposed one would silently read the wrong security field.
_TOKEN_QUERY = 0x0008
#: ``TokenUser`` in the ``TOKEN_INFORMATION_CLASS`` enum.
_TOKEN_USER_CLASS = 1
#: ``SE_FILE_OBJECT`` in the ``SE_OBJECT_TYPE`` enum.
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001

#: Name of the throwaway file :func: tries to create. Distinctive
#: so a leftover is attributable, and only ever created in a directory the launch is
#: about to REFUSE (a successful create is the rejection).
_WRITE_PROBE_NAME = ".kirocrew-launch-write-probe"

#: Executables that are refused as launch targets regardless of where they live.
#:
#: **This is not a security boundary and must not be read as one.** The real
#: boundary is the protected-root verification above; this is a narrow guard against
#: the one shape that would turn "open an app" into "get a shell": a command
#: interpreter takes its work from stdin or from a subsequent keystroke, so the
#: no-arguments rule that bounds every other target buys nothing against it. And
#: because computer use is deliberately NOT governance-gated, a shell launched here
#: would then be typed into by ``computer_type_text`` with none of the
#: ``BUILTIN_DENIED_RULES`` the ``bash`` tool passes.
#:
#: Deliberately short and deliberately not claimed to be complete — an IDE with an
#: embedded terminal defeats it, which is the same honest limit
#: ``policy._DENIED_BUNDLE_PREFIXES`` states about itself. Matched on the resolved
#: basename, case-insensitively.
_REFUSED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "powershell_ise.exe",
        "wt.exe",
        "conhost.exe",
        "openconsole.exe",
        "bash.exe",
        "sh.exe",
        "wsl.exe",
        "wscript.exe",
        "cscript.exe",
        "mshta.exe",
        "rundll32.exe",
        "regsvr32.exe",
        "msiexec.exe",
        "reg.exe",
        "regedit.exe",
        "installutil.exe",
        "cmstp.exe",
    }
)

_ERR_SHELL_TARGET = (
    "'{name}' is a command interpreter, so it is not a launch target: a shell takes "
    "its instructions from input rather than from the launch, which is the one thing "
    "the no-arguments rule cannot bound. Launch the application you want to use"
)
_ERR_UNTRUSTED_ROOT = (
    "'{name}' resolves to an executable outside the protected install directories, "
    "so it was not launched. A launch target has to be an application installed "
    "system-wide — a per-user or temporary copy is indistinguishable from one this "
    "agent planted"
)
_ERR_NAME_MISMATCH = (
    "'{name}' resolves to an executable called '{actual}', so it was not launched: "
    "a catalog entry that points somewhere other than its own name cannot be "
    "distinguished from a redirected one"
)


@dataclass(frozen=True)
class InstalledApp:
    """One entry of the OS catalog: how it is named, and what it would run.

    ``key`` is the catalog's own spelling (``mspaint.exe``) and ``name`` is the stem
    a user would type (``mspaint``). Both are matched, because a model handed a
    ``computer_list_apps`` row sees the stem while an operator reading the Start menu
    sees neither.

    ``executable`` is the RAW catalog value, not yet verified. Verification is
    :func:`resolve_target`'s job and is deliberately separate: this record is also
    what the "which apps are installed" listing is built from, and that listing is
    honest only if it shows what the OS actually said.
    """

    key: str
    name: str
    executable: str
    source: str


def _install_roots_from_registry() -> "list[str]":
    """The install roots as ``HKLM`` states them. Never from ``os.environ``.

    THE line the whole boundary rests on. ``%ProgramFiles%`` is an ordinary
    environment variable, so reading it would let anyone who controls the gateway's
    environment nominate a directory they can WRITE as a "protected" root. Measured:
    with ``ProgramFiles`` pointed at a temp directory, :func:`_under_protected` accepts a
    binary planted there, defeating every other check in this module at once.

    ``HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion`` is where Windows itself keeps
    these paths and is not writable by an unprivileged user (verified: ``CreateKeyEx``
    answers "Access is denied").

    Returns an empty list rather than raising when the key or a value is unreadable;
    :func:`_protected_roots` then falls back to the hardcoded conventional paths, which
    is strictly narrower than trusting the environment.
    """
    reg = _winreg()
    try:
        key = reg.OpenKey(reg.HKEY_LOCAL_MACHINE, _INSTALL_ROOT_KEY)
    except OSError:
        logger.debug("computer-use launch: install roots unreadable", exc_info=True)
        return []
    out: list[str] = []
    try:
        for name in _INSTALL_ROOT_VALUES:
            try:
                value, _kind = reg.QueryValueEx(key, name)
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                out.append(value.strip())
    finally:
        reg.CloseKey(key)
    return out


def _protected_roots() -> tuple[str, ...]:
    """Directories a launch target may live under, resolved and de-duplicated.

    Two sources, and NEITHER is the process environment:

    * ``platform_compat._windows_system_dirs()`` for ``System32`` — it resolves that
      from ``GetSystemDirectoryW`` rather than ``%SystemRoot%``, which is exactly the
      input this module declines to trust;
    * :func:`_install_roots_from_registry` for the ``Program Files`` pair, read from a
      registry key an unprivileged user cannot write.

    Every path is ``realpath``-ed here so the comparison in :func:`_under_protected`
    is between two resolved paths. A root that does not exist is dropped rather than
    kept as a prefix that could never match.
    """
    candidates: list[str] = list(platform_compat._windows_system_dirs())
    candidates.extend(_install_roots_from_registry())
    candidates.extend(_PROTECTED_FALLBACK_ROOTS)
    resolved: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or not os.path.isdir(candidate):
            continue
        real = os.path.realpath(candidate)
        folded = real.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        resolved.append(real)
    return tuple(resolved)


def _under_protected(path: str) -> bool:
    """Whether *path* is a file this user could not have planted.

    TWO conditions, and the second is not redundant with the first:

    1. it resolves inside one of the :func:`_protected_roots`;
    2. **its own containing directory is not writable by this user**, tested by
       attempting to create a file there.

    A prefix test alone is not sufficient, and the reason is measured rather than
    theoretical: an unwritable root can contain writable DESCENDANTS. On this host
    ``C:\\Windows\\System32\\spool\\drivers\\color`` and
    ``C:\\Windows\\System32\\Microsoft\\Crypto\\RSA\\MachineKeys`` are both writable by
    an unprivileged user while ``System32`` itself is not — so "under System32" admitted
    a planted binary, and an ``App Paths`` entry in the writable ``HKCU`` hive can name
    one. (``C:\\Windows`` was worse still: ``Temp``, ``Tasks`` and the same colour
    directory. It is no longer a root at all.)

    ``realpath`` FIRST, and that is load-bearing too: a junction under a writable
    directory would otherwise present a protected-looking prefix while resolving into
    agent-controlled content. Resolving both sides means the comparison is between real
    locations.

    The separator is appended to each root so ``C:\\Program FilesEvil`` cannot pass a
    ``C:\\Program Files`` prefix test, and the root itself is accepted as well as its
    children.

    The writability probe is a real create-and-delete rather than an ACL read. That is
    the honest test — an effective-permissions computation has to model group membership,
    inherited denies and privilege elevation, and getting it subtly wrong fails OPEN —
    and it runs at most once per launch, on a path already known to be under a protected
    root. The probe file is created in the directory being judged and removed
    immediately; a directory where the create SUCCEEDS is rejected, so a leftover probe
    can only ever appear somewhere the launch then refuses.

    Fails CLOSED on an unresolvable path or an unexpected error: a target we cannot
    vouch for is not one to run.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    folded = real.casefold()
    for root in _protected_roots():
        root_folded = root.casefold().rstrip(os.sep)
        if folded != root_folded and not folded.startswith(root_folded + os.sep):
            continue
        if _any_directory_is_writable(os.path.dirname(real), root_folded):
            return False
        return not _file_is_replaceable(real)
    return False


def _any_directory_is_writable(directory: str, root_folded: str) -> bool:
    """Whether ANY directory from *directory* up to (and including) the root is writable.

    **Every level, not just the immediate parent**, and that is the difference between a
    check and a formality. Write access to any single directory on the path is enough to
    substitute the code that runs: an agent that owns an intermediate
    ``C:\\Program Files\\Vendor`` can rename its ``App`` child aside and recreate it with its
    own binary inside, and a parent-only probe answers "unwritable" for the new leaf it just
    created. Measured. ``launch_macos._writable_component`` probes every level of a bundle
    for exactly this reason.

    The walk stops at the protected root, which is where the guarantee comes from: the root
    itself is probed, and everything above it is off the caller's path by construction — the
    prefix test has already established that ``directory`` is inside it.

    Bounded by the path's own depth: the loop ends when ``dirname`` stops changing, so a
    malformed path cannot spin. Fails CLOSED via :func:`_directory_is_writable`, which
    answers ``True`` for any error it cannot read as a denial.
    """
    current = directory
    while True:
        if _directory_is_writable(current):
            return True
        if current.casefold().rstrip(os.sep) == root_folded:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            # Reached the filesystem root without meeting the protected root. Should be
            # unreachable (the prefix test ran first), so treat it as unverifiable.
            return True
        current = parent


def _owned_by_current_user(path: str) -> "bool | None":
    """Whether *path*'s owner is this process's user. ``None`` when it cannot be determined.

    The durable half of :func:`_file_is_replaceable`. An owner holds ``WRITE_DAC`` implicitly,
    so "write is denied right now" says nothing about whether it will be denied a moment
    later; "someone else owns this" does, because taking ownership requires a privilege an
    ordinary agent does not have.

    Two Win32 calls, both read-only: ``GetNamedSecurityInfoW`` for the file's owner SID and
    ``GetTokenInformation(TokenUser)`` for this process's, compared with ``EqualSid``. No
    account-name lookup, which would add a domain round-trip and a localisation dependency
    for a comparison the SIDs already answer.

    ``ctypes`` is imported HERE rather than at module scope, for the same reason ``winreg``
    is: this module must import on a Linux CI runner, where ``WinDLL`` does not exist.

    Never raises. Answers ``None`` on any failure — a missing file, a filesystem with no
    security information, a call that errors — and the caller treats that as "assume
    replaceable", so an ownership question this code cannot answer refuses the launch.
    """
    if not platform_compat.IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        # Explicit argtypes: without them ctypes truncates a 64-bit pointer to int on x64,
        # and the comparison would then be between two meaningless values.
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        advapi32.EqualSid.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            return None
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, _TOKEN_USER_CLASS, buffer, size.value, ctypes.byref(size)
            ):
                return None
            # TOKEN_USER is a SID_AND_ATTRIBUTES, whose first member is the SID pointer.
            my_sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            owner_sid = ctypes.c_void_p()
            descriptor = ctypes.c_void_p()
            if (
                advapi32.GetNamedSecurityInfoW(
                    path,
                    _SE_FILE_OBJECT,
                    _OWNER_SECURITY_INFORMATION,
                    ctypes.byref(owner_sid),
                    None,
                    None,
                    None,
                    ctypes.byref(descriptor),
                )
                != 0
            ):
                return None
            try:
                return bool(advapi32.EqualSid(owner_sid, ctypes.c_void_p(my_sid)))
            finally:
                kernel32.LocalFree(descriptor)
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001 - an ownership answer we cannot get must not raise
        logger.debug("computer-use launch: could not read the owner of %s", path, exc_info=True)
        return None


def _file_is_replaceable(path: str) -> bool:
    """Whether this user can REWRITE *path* in place.

    The second permission, and the one the directory probe cannot see: directory writability
    governs create, unlink and rename, while replacing an existing file's bytes needs write
    on the file itself. So a parent that refuses creates can still hold an executable this
    user rewrites with ``open(path, "r+b")`` — measured — and the protected-root test alone
    accepted it. The macOS sibling makes the same distinction
    (``launch_macos._any_executable_is_writable``); a target has to fail BOTH questions to be
    trusted, on either platform.

    **``os.open(.., O_RDWR)``, not ``os.access(.., W_OK)``.** On Windows ``os.access`` reports
    the read-only ATTRIBUTE and never consults the ACL, so it answers True for every file
    that is not flagged read-only — including every binary in ``System32``, which would make
    this refuse the whole catalog. Measured from an unelevated shell: ``os.access`` says
    writable for ``System32\\notepad.exe`` while opening it for write is denied. Opening
    ``O_RDWR`` asks the real question, and it modifies nothing: no ``O_TRUNC``, no write, the
    handle is closed immediately. That matters because this runs on a binary the launch is
    about to ALLOW and must not damage a real installed application.

    **OWNERSHIP is checked first, and it is the decisive half.** A current write denial is not
    durable: the OWNER of a Windows object holds ``WRITE_DAC`` implicitly, so an agent that
    plants a binary and then denies itself write can revoke that deny ACE again with no
    privilege. Measured — ``icacls <file> /deny <me>:(WD)`` makes the open fail and
    ``icacls <file> /remove:d <me>`` makes it succeed again, both as an ordinary user. So a
    file this user OWNS counts as replaceable whatever its ACL currently says, exactly as
    ``launch_macos._file_is_replaceable`` treats ``st_uid``. That still admits the real
    catalog: on the measured host ``System32`` binaries are owned by
    ``NT SERVICE\\TrustedInstaller`` and ``Program Files`` content by ``SYSTEM``.

    Fails CLOSED on anything other than the denial that means "no": a file that cannot be
    examined counts as replaceable, so an unverifiable target is refused rather than run.
    """
    if _owned_by_current_user(path) is not False:
        # True, or None for "could not determine" — both refuse. An ownership answer this
        # code cannot obtain is not evidence that the file belongs to someone else.
        return True
    try:
        fd = os.open(path, os.O_RDWR)
    except PermissionError:
        # The answer we want: write refused on a file whose ACL this user cannot rewrite.
        return False
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        # Nothing there to rewrite. Not evidence of trust either, but the basename and
        # protected-root checks have already vouched for the path's shape.
        return False
    except OSError:
        return True
    os.close(fd)
    return True


def _directory_is_writable(directory: str) -> bool:
    """Whether this process can create a file in *directory*.

    Fails CLOSED — answers ``True`` ("assume writable", so the caller refuses) — for any
    error other than the permission denial that means "no". An unreadable or vanished
    directory is not evidence that a binary in it is trustworthy, and this predicate is
    only ever consulted to decide whether to START A PROCESS.
    """
    probe = os.path.join(directory, _WRITE_PROBE_NAME)
    try:
        with open(probe, "xb"):
            pass
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        # PermissionError is the answer we want: the directory refused the write.
        # The other two mean the directory is not there to write to, which is equally
        # not a place a planted binary could be.
        return False
    except FileExistsError:
        # Something already holds the probe name. Cannot conclude "unwritable" from
        # that, so fail closed.
        return True
    except OSError:
        return True
    try:
        os.unlink(probe)
    except OSError:
        logger.debug("computer-use launch: could not remove the write probe in %s", directory)
    return True


def _app_paths_entries() -> "list[InstalledApp]":
    """Every resolvable ``App Paths`` entry, from both hives, HKLM first.

    HKLM precedes HKCU so a machine-wide install wins a name collision: the per-user
    hive is writable, and while :func:`resolve_target` verifies whatever it returns,
    preferring the unwritable source means an agent cannot even change WHICH installed
    application a name selects.

    Never raises. A hive that will not open, a key without a default value, and a
    value naming a file that does not exist are all ordinary on a real host — an
    unreadable catalog degrades to a smaller one, never to an error inside a tool call.
    """
    reg = _winreg()
    out: list[InstalledApp] = []
    seen: set[str] = set()
    for hive_name, label in _APP_PATHS_HIVES:
        hive = getattr(reg, hive_name)
        try:
            root = reg.OpenKey(hive, _APP_PATHS_KEY)
        except OSError:
            logger.debug("computer-use launch: %s App Paths unavailable", label, exc_info=True)
            continue
        try:
            count = reg.QueryInfoKey(root)[0]
            for position in range(count):
                try:
                    key_name = reg.EnumKey(root, position)
                    with reg.OpenKey(hive, f"{_APP_PATHS_KEY}\\{key_name}") as sub:
                        raw, _kind = reg.QueryValueEx(sub, "")
                except OSError:
                    continue
                # The value is quoted on some entries and bare on others; both are
                # the documented shape, so the quotes are stripped rather than
                # treated as a parse failure.
                executable = str(raw or "").strip().strip('"')
                if not executable or not os.path.isfile(executable):
                    continue
                folded = key_name.casefold()
                if folded in seen:
                    continue
                seen.add(folded)
                stem = key_name[: -len(_EXE_SUFFIX)] if folded.endswith(_EXE_SUFFIX) else key_name
                out.append(
                    InstalledApp(
                        key=key_name, name=stem, executable=executable, source=f"{label} App Paths"
                    )
                )
        finally:
            reg.CloseKey(root)
    return out


def installed_apps() -> "tuple[InstalledApp, ...]":
    """The OS's catalog of installed applications, unverified.

    Unverified on purpose: this is what the "which apps could I launch" answer is
    built from, and that answer is only honest if it reports what the OS said rather
    than what survived our checks. :func:`resolve_target` applies the checks at the
    point a process would actually be created.
    """
    return tuple(_app_paths_entries())


def resolve_target(query: str) -> "tuple[str, str]":
    """Resolve *query* to ``(executable, display_name)``, or raise.

    The one function that decides what may run, applying four checks in the order
    their failures are most legible:

    1. **the QUERY must not name a command interpreter.** Checked before resolution
       and separately from check 3, because those two catch different things — see
       below;
    2. **the catalog must name it** — an exact key or stem match, then a unique
       PREFIX match. A prefix hitting several applications RAISES rather than picking
       one, because launching the wrong application is not undoable;
    3. **the resolved executable must not be a command interpreter** (see
       :data:`_REFUSED_EXECUTABLES` for why that is a narrow guard rather than the
       boundary);
    4. **the executable must sit under a protected root AND be named after the key
       that found it.** This is the boundary. Together the two conditions mean an
       agent that rewrites its own writable catalog entry can still only start an
       application that is already installed system-wide under that same name.

    **Checks 1 and 3 are both needed, and a live run is what proved it.** Check 3
    inspects what the name RESOLVED to, so it cannot see a request for a shell whose
    name resolves elsewhere: asking for ``cmd`` on the measured host matched the
    unrelated ``IEDIAGCMD.EXE`` (an Internet Explorer diagnostic) and LAUNCHED it,
    passing every other check on the way. Check 1 refuses the request by what it
    asked for; check 3 refuses it by what it would run.

    **The fuzzy tier is a PREFIX rather than a substring, for the same reason.** A
    3-character fragment matching inside a 9-character name is a coincidence, not an
    intent — and the ambiguity guard cannot help, because a coincidence usually hits
    exactly one entry. A prefix is what someone typing the beginning of a name
    produces. Where that is too strict the refusal carries the near misses, so a
    model that typed ``paint`` is told about ``mspaint`` rather than being left to
    guess.

    Raises :class:`ComputerUseError` with model-facing prose in every failure case —
    the driver's ``_guarded`` seam turns it into a refusal.
    """
    wanted = (query or "").strip().casefold()
    if not wanted:
        raise NoSuchLaunchTarget()
    # (1) The QUERY, before resolution. See the docstring: a shell name that resolves
    # to something else would otherwise pass the resolved-basename check at (3).
    if wanted in _REFUSED_EXECUTABLES or f"{wanted}{_EXE_SUFFIX}" in _REFUSED_EXECUTABLES:
        raise ComputerUseError(_ERR_SHELL_TARGET.format(name=query))
    catalog = installed_apps()

    matched = [app for app in catalog if wanted in (app.key.casefold(), app.name.casefold())]
    if not matched:
        prefixed = [app for app in catalog if app.name.casefold().startswith(wanted)]
        if len(prefixed) > 1:
            names = ", ".join(sorted({app.name for app in prefixed})[:MAX_LAUNCH_SUGGESTIONS])
            raise AmbiguousLaunchTarget(names, len(prefixed))
        matched = prefixed
    if not matched:
        # No prefix match. A SUBSTRING scan supplies suggestions — never a target —
        # so a query like ``paint`` against ``mspaint`` produces a recoverable refusal
        # instead of a dead end, without loosening what may actually be launched.
        near = sorted({app.name for app in catalog if wanted in app.name.casefold()})
        raise NoSuchLaunchTarget(", ".join(near[:MAX_LAUNCH_SUGGESTIONS]))

    app = matched[0]
    # RESOLVED once, and every later step uses this rather than the raw catalog value. The
    # two differ in the last component whenever a link or an 8.3 alias is involved, and
    # verifying one string while returning the other is how the checks below stop bounding
    # what actually runs.
    try:
        real = os.path.realpath(app.executable)
    except OSError:
        raise ComputerUseError(_ERR_UNTRUSTED_ROOT.format(name=app.name)) from None
    basename = os.path.basename(real)
    # (3) What it would RUN, as opposed to what was asked for at (1).
    if basename.casefold() in _REFUSED_EXECUTABLES:
        raise ComputerUseError(_ERR_SHELL_TARGET.format(name=app.name))
    if not _under_protected(real):
        raise ComputerUseError(_ERR_UNTRUSTED_ROOT.format(name=app.name))
    if basename.casefold() != app.key.casefold():
        # A catalog entry pointing somewhere other than its own name. Legitimate on
        # exactly one measured entry (Microsoft's IEDIAG.EXE -> IEDIAGCMD.EXE alias)
        # and refused anyway: an alias and a redirected entry are indistinguishable
        # from here, and the safe reading of an ambiguous signal is the strict one.
        raise ComputerUseError(_ERR_NAME_MISMATCH.format(name=app.name, actual=basename))
    # The VERIFIED path, not the raw catalog value. Returning the raw one would hand
    # ``spawn_detached`` a string whose last component was never the one checked — a link or
    # an 8.3 alias resolves elsewhere, and the whole verification then bounds a file the OS
    # will not be asked to run. ``launch_macos`` returns its verified bundle path for the
    # same reason.
    return real, app.name


def target_identity(executable: str, display: str) -> LaunchIdentity:
    """Every name the resolved target is known by, for the pre-spawn policy check.

    Windows has two spellings and they are not interchangeable for policy: a launch is
    requested by the stem (``notepad``), while the running process — and therefore every
    computer-use refusal the operator has ever read — is named by the executable
    (``notepad.exe``). An ``extra_denied_apps`` entry written against the latter matched
    nothing pre-spawn while the display name was the only thing checked, so the deny took
    effect only after the process was running.

    The basename comes from the RESOLVED path — ``realpath`` first — because that is the
    string ``resolve_target`` actually verified, and the raw catalog value can differ from
    it in the last component. Measured: an 8.3 alias (present by default on the system
    volume) lets an ``App Paths`` entry name ``…\\SOMEVE~1.EXE`` for a file whose real name
    is ``SomeVeryLongName.exe``. ``resolve_target`` accepts it, since it compares
    ``basename(realpath(...))`` against the key — but reporting the RAW basename here would
    hand the policy ``SOMEVE~1.EXE``, which the operator's ``someverylongname.exe`` rule
    does not match, and the denied application would spawn. A file symlink does the same
    where one can be created.
    """
    return LaunchIdentity(display=display, key=os.path.basename(os.path.realpath(executable)))


def spawn_detached(executable: str) -> None:
    """Start *executable* with NO arguments, fully detached from the gateway.

    Detached on all three axes, each for its own reason:

    * **``creationflags``** — a new process group, so the launched application does
      not receive the console signals the gateway does. Without it, Ctrl-C in a dev
      terminal would take the operator's application down with the gateway.
    * **``stdin``/``stdout``/``stderr`` to ``DEVNULL``** — the child inherits no
      handle on the gateway's pipes. A GUI application that writes to stdout would
      otherwise fill a pipe nobody drains and block, and it would keep the gateway's
      handles alive after it exits.
    * **``cwd`` at the executable's own directory** — never the gateway's. The
      gateway's CWD is the repo checkout under a dev run, and an application that
      writes a file relative to its CWD would drop it there.

    The return is deliberately ``None``: the launched process is NOT tracked, and the
    caller must not read its exit status as the outcome. A packaged-app launcher
    exits immediately and hands off (measured: ``rc=1`` while the application went on
    to open a window 9.9s later), so a launch is confirmed by finding the WINDOW.

    Raises ``OSError`` — the caller turns it into a refusal naming the app.
    """
    subprocess.Popen(  # noqa: S603 - argv is exactly [executable], verified by resolve_target
        [executable],
        cwd=os.path.dirname(executable) or None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
