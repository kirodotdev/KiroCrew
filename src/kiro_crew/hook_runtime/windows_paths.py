"""The Windows path screens: UNC shape, the extended-length fold, the trusted-root
probe gate, OS-layer representability and one link target's normalization.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _DRIVE_ABS_RE,
        _DRIVE_PREFIX_RE,
        _MAX_SCREENED_PATH_DEPTH,
        _unc_agents_root,
        _unc_data_home_root,
    )


def is_unc_shape(raw: str) -> bool:
    """True for a UNC-shaped path: two leading separators, either style.

    One exception, because Windows ``os.readlink`` returns an ordinary local target in
    EXTENDED-LENGTH form -- ``\\\\?\\C:\\Users\\...`` -- which starts with two separators and
    would otherwise be judged a network share, refusing every local symlink as if it reached a
    host over SMB. A real UNC in that form is ``\\\\?\\UNC\\server\\share``; ``\\\\?\\C:`` is a
    LOCAL drive path. So a ``\\\\?\\`` prefix whose remainder is drive-absolute (``C:\\...``) is
    NOT a share. The distinction is exactly the one the readlink-chain walker already draws
    (``\\\\?\\UNC\\`` -> share, ``\\\\?\\<drive>:`` -> local): ``\\\\?\\UNC\\...`` stays a share,
    and other extended namespaces (``\\\\?\\GLOBALROOT\\...``, ``\\\\?\\Volume{guid}\\...``,
    device paths) stay shaped-as-UNC so they are refused fail-closed rather than admitted as
    local. The fold is case-insensitive because the OS honours the ``UNC`` component that way.
    """
    if len(raw) >= 4 and raw[:4] == "\\\\?\\":
        # Extended-length prefix. A drive-absolute remainder is a plain local path, not a share;
        # ``\\?\UNC\...`` and every other extended namespace remain UNC-shaped (refused).
        return not _DRIVE_ABS_RE.match(raw[4:])
    return len(raw) >= 2 and raw[0] in "\\/" and raw[1] in "\\/"


def _fold_extended_length_local(raw: str) -> str:
    r"""Fold a ``\\?\<drive>:\...`` extended-length LOCAL path to plain ``<drive>:\...``.

    Only a DRIVE-absolute remainder is folded. ``\\?\UNC\...`` and every other
    extended namespace (``\\?\GLOBALROOT\...``, ``\\?\Volume{guid}\...``, device
    paths) are returned unchanged, so ``is_unc_shape`` still reports them
    UNC-shaped and the UNC trusted-root gate refuses them fail-closed --
    stripping the prefix there would launder a share (or a kernel object) into a
    local-looking string. The lexical twin of the readlink-target ``\\?\`` fold
    in :func:`validate_file_path`; a cheap string test with no filesystem or
    network I/O.
    """
    if len(raw) >= 4 and raw[:4] == "\\\\?\\" and _DRIVE_ABS_RE.match(raw[4:]):
        return raw[4:]
    return raw


def unc_probe_allowed(raw: str) -> bool:
    """Whether a UNC-shaped path may touch the filesystem on Windows.

    A UNC path names a HOST, so resolving or stat-ing untrusted text
    (``\\\\evil\\share\\x.png`` or ``//evil/share/x.png`` echoed in any message
    or query) makes Windows open an SMB connection to that host -- an outbound
    credential probe the attacker controls. Filesystem access is therefore
    restricted to UNC paths under directories this gateway itself writes to:
    the data home (on a roaming profile the home directory is itself a UNC
    share, the one legitimate source of UNC attachment paths), the temp
    directory (channel-side image staging), and the kiro agents directory
    (``apps.bridges._register_agents`` and ``agent.rebuild_agent_config``
    write the managed specs there -- see ``kiro_agents_dir()``'s docstring;
    on a roaming profile it sits on the same UNC share as the data home, and
    without it every user-level agent spec read is silently refused).
    The comparison is purely lexical (``normpath``/``normcase``) and BOTH
    resolving roots are memoized per configuration (``_unc_data_home_root``,
    ``_unc_agents_root``), so this check never touches the network itself.

    The data home is memoized for the same reason as the agents dir, and the
    omission was load-bearing rather than cosmetic: ``data_home()`` resolves
    ``KIROCREW_HOME`` on every call when that override is set, which is
    precisely the roaming-profile configuration in which the override names a
    share. Calling it per gate check put an SMB round-trip inside a predicate
    documented as lexical.
    """
    try:
        cand = os.path.normcase(os.path.normpath(raw))
    except (ValueError, OSError):
        return False
    roots: tuple[Path, ...] = (Path(tempfile.gettempdir()),)
    for extra in (_unc_data_home_root(), _unc_agents_root()):
        if extra is not None:
            roots += (extra,)
    for root in roots:
        rootn = os.path.normcase(os.path.normpath(str(root)))
        if not is_unc_shape(rootn):
            continue
        if cand == rootn or cand.startswith(rootn.rstrip("\\/") + os.sep):
            return True
    return False


def _is_representable_path(raw: str) -> bool:
    """Can the OS path layer represent this string at all?

    ``realpath``/``lstat`` raise on a string the filesystem cannot carry:
    ``ValueError`` for an embedded NUL, and ``UnicodeEncodeError`` (a
    ``ValueError`` subclass) for a lone surrogate the platform's own error
    handler cannot round-trip. Callers of :func:`validate_file_path` treat only
    ``None`` as a refusal, so such a path reached ``validate_file_path``'s resolution and
    surfaced from the dashboard file handlers as an uncaught HTTP 500 rather than
    the 400 it is.

    Scoped to strings that are genuinely unrepresentable, and nothing else. This
    is a SHARED chokepoint -- ``safe_read_file_bytes_nolink`` routes through it,
    and its callers include diagnostics that deliberately enumerate a file whose
    name holds a control character in order to report on it (an agent-writeable
    directory can contain one, and the reporting layer escapes the name for
    display). Refusing a broader class here would turn such a report into "could
    not be compared" and so suppress the finding it exists to make. A path whose
    control characters must be refused is refused by the boundary that receives
    it, not here: see ``_validate_dashboard_path`` in the dashboard file
    handlers. Only NUL is refused here, because no file can be named with one, so
    no consumer loses a real name.

    Nor is this "refuse anything a sanitizer would alter". A canonically
    decomposed name is the form macOS stores and a name may legally end in a
    space; both differ from their sanitized form and both resolve correctly.

    The encoding attempt is the discriminator rather than a character list,
    because it asks the question the syscall will ask: it accepts a surrogate the
    platform's own error handler round-trips -- ``surrogateescape`` for a POSIX
    name holding non-UTF-8 bytes, ``surrogatepass`` for an unpaired surrogate in
    a legal NTFS name -- and rejects one it cannot. Both the encoding and the
    error handler are read from ``sys``, which is what makes this the same
    operation as ``os.fsencode`` on every platform rather than only on POSIX.
    ``sys`` rather than ``os`` because ``hooks.os`` -- the ``os`` this function
    reads -- is substituted wholesale by tests exercising the Windows gates, and a
    check a stub can silently remove is not a check.
    """
    if "\x00" in raw:
        return False
    try:
        raw.encode(sys.getfilesystemencoding(), sys.getfilesystemencodeerrors())
    except (UnicodeError, ValueError):
        return False
    return True


def _normalize_windows_link_target(link_path: str, raw_target: str) -> str | None:
    r"""Normalize one Windows link target without traversing through the link.

    The return value is safe to screen as a new path. Untrusted UNC targets,
    ambiguous drive/root-relative targets, and extended device namespaces are
    refused before any filesystem probe can follow them.
    """
    target = raw_target
    if target[:8].upper() == "\\\\?\\UNC\\":
        target = "\\\\" + target[8:]
    elif target.startswith("\\\\?\\"):
        if not _DRIVE_ABS_RE.match(target[4:]):
            return None
        target = target[4:]

    if is_unc_shape(target):
        if not unc_probe_allowed(target):
            return None
    elif _DRIVE_ABS_RE.match(target):
        pass
    elif target[:1] in "\\/" or _DRIVE_PREFIX_RE.match(target):
        return None
    else:
        target = os.path.normpath(os.path.join(os.path.dirname(link_path), target))

    if target.count("\\") + target.count("/") > _MAX_SCREENED_PATH_DEPTH:
        return None
    return target
