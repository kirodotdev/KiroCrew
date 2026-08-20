"""Windows security-descriptor reads for the provider-CLI trust policy.

``github_runner``'s trust question is "could anything other than the gateway
user or the system have replaced this binary". On POSIX that is answered from
``st_uid`` plus the group/other write bits. On Windows those fields carry no
information -- ``os.stat`` reports ``st_uid == 0`` and ``st_mode == 0o777`` for
every path -- so the ownership half of the POSIX predicate always passes and
the permission half always refuses, independent of the file examined. This
module supplies the real answer from the object's security descriptor instead.

It deliberately holds NO policy: it reads an owner SID and the set of
principals that hold a right capable of REPLACING the object, and hands both to
``github_runner`` to judge. Keeping the read separate from the decision is what
makes the policy testable against synthetic ACLs.

Only ``ctypes`` is used. ``pywin32`` would be a new platform-conditional
dependency for five calls that the standard library can already make.

Substitution rights, and why the POSIX predicate cannot be ported bit-for-bit
=============================================================================

Two access-mask bits mean different things depending on the object type::

    bit 0x2   FILE = FILE_WRITE_DATA      DIRECTORY = FILE_ADD_FILE
    bit 0x4   FILE = FILE_APPEND_DATA     DIRECTORY = FILE_ADD_SUBDIRECTORY

POSIX collapses every directory mutation into one ``w`` bit, so "the parent
directory is writable" really does imply "the entry can be swapped" -- unlink
and rename come with it. Windows decomposes them, and neither ``FILE_ADD_FILE``
nor ``FILE_ADD_SUBDIRECTORY`` lets the holder touch an *existing* entry.
Replacing one needs ``FILE_DELETE_CHILD`` on the parent, or ``DELETE`` /
``WRITE_DAC`` / ``WRITE_OWNER`` on the entry itself.

The distinction is load-bearing rather than pedantic: the default ACL on ``C:\\``
grants ``NT AUTHORITY\\Authenticated Users`` the ``ADD_SUBDIRECTORY`` right, so a
predicate that reads that bit as a write grant refuses every stock Windows
install once the walk reaches the drive root.
"""

from __future__ import annotations

import ctypes as C
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ``from ctypes import wintypes`` RAISES on Linux, so importing it unguarded
# would make this module unimportable off Windows -- and with it every test of
# the policy that consumes it, which is exactly the suite that must run on the
# Ubuntu CI runner. The fallbacks below are layout-identical to the wintypes
# aliases, so the structure definitions are byte-compatible either way; nothing
# in this module actually CALLS into the API off Windows (see ``_load``).
try:  # pragma: no cover - branch depends on the host platform
    from ctypes import wintypes as W
except (ImportError, ValueError):  # pragma: no cover - non-Windows hosts

    class W:  # type: ignore[no-redef]
        """Layout-compatible stand-ins for the wintypes aliases used below."""

        BYTE = C.c_byte
        WORD = C.c_ushort
        DWORD = C.c_ulong
        BOOL = C.c_long
        HANDLE = C.c_void_p
        LPWSTR = C.c_wchar_p
        LPCWSTR = C.c_wchar_p
        LPDWORD = C.POINTER(C.c_ulong)


# ``ctypes.WinDLL`` and ``ctypes.get_last_error`` are Windows-only in typeshed,
# and CI type-checks this file on Linux, where mypy analyses the non-Windows
# branch and cannot see either symbol. The two shims below keep ONE readable
# implementation rather than scattering per-line ignores through every API call.
# Neither weakens anything: the loaded libraries are opaque handles this module
# never introspects, and `_load` still refuses off Windows before any of it runs.
_DLL = Any


def _last_error() -> int:
    """``GetLastError`` for the current thread, fetched opaquely for the checker.

    Falls back to 0 where the symbol does not exist -- ``ctypes.get_last_error``
    is Windows-only. The value only ever decorates an error message, and off
    Windows there is no thread-local Win32 error to report; degrading keeps the
    module's failure paths reachable by a test that injects fake DLL handles,
    which is how they are covered on the Linux runner.
    """
    getter = getattr(C, "get_last_error", None)
    return int(getter()) if getter is not None else 0


__all__ = [
    "AclUnavailable",
    "ComponentSecurity",
    "WELL_KNOWN_TRUSTED_SIDS",
    "Writer",
    "describe",
]


class AclUnavailable(RuntimeError):
    """The security descriptor could not be read.

    Callers treat this as a refusal, never as "no problem found": a trust check
    that cannot see the ACL has not cleared the binary.
    """


# Principals whose write access does not make a binary untrustworthy, because
# holding them already means owning the host. The direct analog of POSIX
# ``(0, uid)``: LocalSystem and Administrators stand in for root, and
# TrustedInstaller owns everything Windows Update places under Program Files.
#
# That premise -- "holding this SID means owning THIS host" -- is what makes the
# set safe, and it holds only for a path on a LOCAL volume. These are
# machine-local alias SIDs: the same literal string denotes a different
# principal on every machine, so the descriptor of a file on a remote share
# names the FILE SERVER's SYSTEM and Administrators, not this host's. Callers
# must therefore establish that a component is local before consulting this set
# -- see :func:`drive_type`.
WELL_KNOWN_TRUSTED_SIDS = frozenset(
    {
        "S-1-5-18",  # NT AUTHORITY\SYSTEM
        "S-1-5-32-544",  # BUILTIN\Administrators
        # NT SERVICE\TrustedInstaller
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
)

# GetDriveTypeW return values. Implementation detail of the volume read below --
# the policy sees only the resulting ``volume_is_local`` bool. Local kinds are
# allowlisted rather than remote ones denylisted, so an unexpected or future
# value is treated as not-local instead of passing.
_DRIVE_REMOTE = 4
_LOCAL_DRIVE_TYPES = frozenset({2, 3, 5, 6})  # removable, fixed, cdrom, ramdisk

SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004

ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
INHERIT_ONLY_ACE = 0x08

# Rights that let a holder replace a FILE's contents.
_SUBSTITUTION_BITS_FILE: dict[int, str] = {
    0x00000002: "WRITE_DATA",
    0x00000004: "APPEND_DATA",
    0x00010000: "DELETE",
    0x00040000: "WRITE_DAC",
    0x00080000: "WRITE_OWNER",
    0x10000000: "GENERIC_ALL",
    0x40000000: "GENERIC_WRITE",
}
# Rights that let a holder replace an entry inside a DIRECTORY. ADD_FILE (0x2)
# and ADD_SUBDIRECTORY (0x4) are deliberately absent -- they create new entries
# and cannot overwrite an existing one. See the module docstring.
_SUBSTITUTION_BITS_DIR: dict[int, str] = {
    0x00000040: "FILE_DELETE_CHILD",
    0x00010000: "DELETE",
    0x00040000: "WRITE_DAC",
    0x00080000: "WRITE_OWNER",
    0x10000000: "GENERIC_ALL",
}


@dataclass(frozen=True)
class Writer:
    """A principal holding a substitution-capable right on one component."""

    sid: str
    name: str
    rights: tuple[str, ...]

    def describe(self) -> str:
        return f"{self.name} ({self.sid}) [{','.join(self.rights)}]"


@dataclass(frozen=True)
class ComponentSecurity:
    """What the trust policy needs to know about one path component."""

    owner_sid: str
    owner_name: str
    #: True when the object has a NULL DACL, which grants EVERYONE full
    #: control. Distinct from an empty DACL (which grants nobody anything), and
    #: the reason "no writers found" must never be read as "clean" on its own.
    null_dacl: bool
    writers: tuple[Writer, ...]
    #: ACE types this module does not know how to parse. Non-empty means the
    #: descriptor was only partially understood, so the caller must refuse
    #: rather than trust an incomplete ``writers`` tuple.
    unparsable_ace_types: tuple[int, ...]
    #: True when the component sits on a volume attached to THIS machine.
    #:
    #: Load-bearing for :data:`WELL_KNOWN_TRUSTED_SIDS`, whose members are
    #: machine-local ALIAS SIDs -- ``S-1-5-18`` and ``S-1-5-32-544`` are the same
    #: literal string on every machine and denote a different principal on each.
    #: So the descriptor of a file on a remote share names the FILE SERVER's
    #: SYSTEM and Administrators, and trusting them would mean "whoever
    #: administers that server may replace the binary this gateway runs".
    #:
    #: It travels here, on the descriptor, rather than being a second call the
    #: policy makes: that keeps the platform-bound reading in ONE place and
    #: leaves the policy a pure function of this dataclass, which is what makes
    #: it testable on a non-Windows CI runner.
    volume_is_local: bool


class _ACL(C.Structure):
    _fields_ = [
        ("AclRevision", W.BYTE),
        ("Sbz1", W.BYTE),
        ("AclSize", W.WORD),
        ("AceCount", W.WORD),
        ("Sbz2", W.WORD),
    ]


class _ACE_HEADER(C.Structure):
    _fields_ = [("AceType", W.BYTE), ("AceFlags", W.BYTE), ("AceSize", W.WORD)]


class _ACCESS_ACE(C.Structure):
    """Layout shared by ACCESS_ALLOWED_ACE and ACCESS_DENIED_ACE.

    ``SidStart`` is the first DWORD of a variable-length SID, so its struct
    offset is where the SID begins.
    """

    _fields_ = [("Header", _ACE_HEADER), ("Mask", W.DWORD), ("SidStart", W.DWORD)]


def _install_prototypes(advapi32: _DLL, kernel32: _DLL) -> None:  # pragma: no cover
    """Declare the argument and return types for every call this module makes.

    Excluded from coverage as a whole: it only ever runs against real `WinDLL`
    handles, and off Windows there are none to configure. What it declares IS
    covered -- the tests inject plain Python handles and drive the same parsing
    code, so a mistake in how a result is consumed still fails there. A mistake
    in a prototype itself is only observable against the real API, which is what
    the Windows-only real-descriptor suite is for.
    """
    advapi32.GetNamedSecurityInfoW.argtypes = [
        W.LPCWSTR,
        W.DWORD,
        W.DWORD,
        C.POINTER(C.c_void_p),
        C.POINTER(C.c_void_p),
        C.POINTER(C.POINTER(_ACL)),
        C.POINTER(C.POINTER(_ACL)),
        C.POINTER(C.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = W.DWORD
    advapi32.GetAce.argtypes = [C.POINTER(_ACL), W.DWORD, C.POINTER(C.c_void_p)]
    advapi32.GetAce.restype = W.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = W.BOOL
    advapi32.LookupAccountSidW.argtypes = [
        W.LPCWSTR,
        C.c_void_p,
        W.LPWSTR,
        W.LPDWORD,
        W.LPWSTR,
        W.LPDWORD,
        W.LPDWORD,
    ]
    advapi32.LookupAccountSidW.restype = W.BOOL

    kernel32.LocalFree.argtypes = [C.c_void_p]
    kernel32.LocalFree.restype = C.c_void_p
    kernel32.GetDriveTypeW.argtypes = [C.c_wchar_p]
    kernel32.GetDriveTypeW.restype = C.c_uint


def _load() -> tuple[_DLL, _DLL]:
    """Acquire the two DLL handles this module reads descriptors through.

    This is the module's SINGLE platform seam, which is what makes everything it
    configures testable off Windows: a test substitutes plain Python objects for
    the two handles and the real parsing runs anywhere. The acquisition itself
    cannot -- `WinDLL` does not exist on Linux -- so it is excluded from coverage
    rather than pretended to be reachable, while the refusal below stays counted
    and has its own test.
    """
    if sys.platform != "win32":
        raise AclUnavailable("Windows security descriptors are not available on this platform")
    try:  # pragma: no cover - WinDLL is unavailable off Windows
        advapi32 = getattr(C, "WinDLL")("advapi32", use_last_error=True)
        kernel32 = getattr(C, "WinDLL")("kernel32", use_last_error=True)
    except OSError as exc:  # pragma: no cover - a Windows without advapi32
        raise AclUnavailable(f"cannot load the Windows security API: {exc}") from exc
    _install_prototypes(advapi32, kernel32)
    return advapi32, kernel32

    advapi32.GetNamedSecurityInfoW.argtypes = [
        W.LPCWSTR,
        W.DWORD,
        W.DWORD,
        C.POINTER(C.c_void_p),
        C.POINTER(C.c_void_p),
        C.POINTER(C.POINTER(_ACL)),
        C.POINTER(C.POINTER(_ACL)),
        C.POINTER(C.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = W.DWORD
    advapi32.GetAce.argtypes = [C.POINTER(_ACL), W.DWORD, C.POINTER(C.c_void_p)]
    advapi32.GetAce.restype = W.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = W.BOOL
    advapi32.LookupAccountSidW.argtypes = [
        W.LPCWSTR,
        C.c_void_p,
        W.LPWSTR,
        W.LPDWORD,
        W.LPWSTR,
        W.LPDWORD,
        W.LPDWORD,
    ]
    advapi32.LookupAccountSidW.restype = W.BOOL

    kernel32.LocalFree.argtypes = [C.c_void_p]
    kernel32.LocalFree.restype = C.c_void_p
    kernel32.GetDriveTypeW.argtypes = [C.c_wchar_p]
    kernel32.GetDriveTypeW.restype = C.c_uint
    return advapi32, kernel32


def _sid_to_string(advapi32: _DLL, kernel32: _DLL, psid: C.c_void_p) -> str:
    out = W.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(psid, C.byref(out)):
        raise AclUnavailable(f"ConvertSidToStringSid failed (error {_last_error()})")
    try:
        return out.value or ""
    finally:
        kernel32.LocalFree(out)


def _sid_to_name(advapi32: _DLL, psid: C.c_void_p) -> str:
    """Best-effort display name. Never load-bearing: the policy compares SIDs,
    and an unresolvable SID (a deleted account, a domain that cannot be
    reached) must not turn into a hard failure of the trust check."""
    name = C.create_unicode_buffer(256)
    domain = C.create_unicode_buffer(256)
    name_len = W.DWORD(256)
    domain_len = W.DWORD(256)
    use = W.DWORD()
    ok = advapi32.LookupAccountSidW(
        None, psid, name, C.byref(name_len), domain, C.byref(domain_len), C.byref(use)
    )
    if not ok:
        return "<unresolved>"
    if domain.value:
        return f"{domain.value}\\{name.value}"
    return name.value or "<unnamed>"


def _substitution_rights(mask: int, *, is_dir: bool) -> tuple[str, ...]:
    table = _SUBSTITUTION_BITS_DIR if is_dir else _SUBSTITUTION_BITS_FILE
    return tuple(label for bit, label in table.items() if mask & bit)


def _volume_is_local(kernel32: _DLL, path: Path) -> bool:
    """Whether *path* sits on a volume attached to this machine.

    Two remote shapes have to be caught, and they look nothing alike -- which is
    why this asks the OS instead of inspecting the string:

    * a **UNC** path (``\\\\server\\share\\gh.exe``, or its ``\\\\?\\UNC\\...``
      extended form), which has no drive letter at all; and
    * a **mapped network drive** (``Z:\\gh.exe``), the more common enterprise
      shape, which ``os.path.splitdrive`` reports as plain ``Z:`` and which no
      amount of string matching separates from a local disk.

    ``GetDriveTypeW`` answers both: it reports a UNC root and a mapped drive
    alike as remote. A volume it cannot classify is reported as NOT local, since
    an unclassifiable volume has cleared nothing.
    """
    # GetDriveTypeW wants a ROOT. splitdrive gives "C:" for a letter path and the
    # whole "\\\\server\\share" for a UNC one, so appending a separator produces
    # the root in both cases.
    drive = os.path.splitdrive(os.path.abspath(str(path)))[0]
    if not drive:
        # No drive component: on Windows `abspath` always supplies one, so this
        # is the off-Windows shape, where there is no volume to classify and
        # "not local" is the fail-closed answer.
        return False
    # Windows path semantics from here on: `posixpath.splitdrive` never returns a
    # drive, so these two lines are unreachable off Windows and are covered by
    # the real-ACL suite there rather than by the injected-handle tests.
    root = drive if drive.endswith(os.sep) else drive + os.sep  # pragma: no cover
    return int(kernel32.GetDriveTypeW(root)) in _LOCAL_DRIVE_TYPES  # pragma: no cover


def describe(path: Path) -> ComponentSecurity:
    """Read one path component's owner and its substitution-capable writers.

    *path* is examined as it is: a directory is judged with the directory right
    semantics and a file with the file ones, because the same mask bits mean
    different things for each.
    """
    advapi32, kernel32 = _load()
    is_dir = path.is_dir()
    volume_is_local = _volume_is_local(kernel32, path)

    owner = C.c_void_p()
    dacl = C.POINTER(_ACL)()
    descriptor = C.c_void_p()
    rc = advapi32.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
        C.byref(owner),
        None,
        C.byref(dacl),
        None,
        C.byref(descriptor),
    )
    if rc != 0:
        raise AclUnavailable(f"cannot read the security descriptor (error {rc})")
    try:
        owner_sid = _sid_to_string(advapi32, kernel32, owner)
        owner_name = _sid_to_name(advapi32, owner)
        if not dacl:
            # NULL DACL: everyone has full control. Report it explicitly so the
            # caller cannot mistake the empty writers tuple for a clean result.
            return ComponentSecurity(
                owner_sid=owner_sid,
                owner_name=owner_name,
                null_dacl=True,
                writers=(),
                unparsable_ace_types=(),
                volume_is_local=volume_is_local,
            )

        writers: list[Writer] = []
        unparsable: list[int] = []
        for index in range(dacl.contents.AceCount):
            ace_pointer = C.c_void_p()
            if not advapi32.GetAce(dacl, index, C.byref(ace_pointer)):
                raise AclUnavailable(f"GetAce({index}) failed (error {_last_error()})")
            ace = C.cast(ace_pointer, C.POINTER(_ACCESS_ACE)).contents
            ace_type = int(ace.Header.AceType)
            if ace_type == ACCESS_DENIED_ACE_TYPE:
                # A deny ACE can only narrow access. Ignoring it is the
                # conservative direction: we may name a writer that is in fact
                # denied, which refuses a binary that would have been fine, and
                # never the reverse.
                continue
            if ace_type != ACCESS_ALLOWED_ACE_TYPE:
                # Object and callback ACEs carry extra fields ahead of the SID,
                # so this layout would read the wrong bytes. Fail closed by
                # reporting it rather than silently skipping a possible grant.
                unparsable.append(ace_type)
                continue
            if ace.Header.AceFlags & INHERIT_ONLY_ACE:
                # Applies to children of this object, not to the object itself.
                continue
            rights = _substitution_rights(ace.Mask, is_dir=is_dir)
            if not rights:
                continue
            # GetAce succeeded, so the pointer is set; refuse rather than skip
            # if it somehow is not, so a missed grant can never read as clean.
            base = ace_pointer.value
            if base is None:  # pragma: no cover - GetAce contract
                raise AclUnavailable(f"GetAce({index}) returned a null ACE pointer")
            sid_pointer = C.c_void_p(base + _ACCESS_ACE.SidStart.offset)
            writers.append(
                Writer(
                    sid=_sid_to_string(advapi32, kernel32, sid_pointer),
                    name=_sid_to_name(advapi32, sid_pointer),
                    rights=rights,
                )
            )
        return ComponentSecurity(
            owner_sid=owner_sid,
            owner_name=owner_name,
            null_dacl=False,
            writers=tuple(writers),
            unparsable_ace_types=tuple(sorted(set(unparsable))),
            volume_is_local=volume_is_local,
        )
    finally:
        kernel32.LocalFree(descriptor)
