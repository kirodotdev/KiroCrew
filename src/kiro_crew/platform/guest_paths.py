"""Paths that name a file in a remote workspace guest, as governance judges them.

A remote workspace's tools act on a Linux guest, so their targets are POSIX paths
whatever OS this gateway runs. Governance's ``path`` matcher normalizes a queried
item with this process's ``os.path``, which on a Windows gateway turns the guest's
``/workspace/app/x`` into ``C:\\workspace\\app\\x``: a deny written for
``/workspace/app/**`` would never meet it. So a guest target carries its flavour in
its TYPE, set by the one producer that knows a call is a workspace call:

* :class:`GuestPathParams` -- the arguments of one workspace tool call, tagged by
  ``build_permission_event`` once the runtime that attached the workspace server
  has identified the call (a trusted identity, not a model-authored field), with
  the guest workdir its relative paths resolve in;
* :class:`GuestPath` -- one target from those arguments, the item
  ``classify_tool_args`` hands the ``path`` matcher, which normalizes it with
  :func:`guest_norm` (``posixpath``) instead of the gateway's ``os.path``.

A guest is Linux except a Windows connected computer, whose workdir is a native
Windows path (a drive letter or a UNC share). The workdir decides the flavour
(:func:`guest_windows`): under a Windows workdir a target is read with ``ntpath``
the way that computer opens it (:func:`guest_norm`), and a deny folds ASCII case
as a deny on a Windows gateway does.

A type, not a dict key: a key would be model-authored, and a local call that set it
could ask to be matched by rules that are not its OS's. Nothing here reads the
environment or the gateway's home folder, because the guest expands neither
(``remote_session/workspace_tools.py``'s ``Workspace.resolve`` joins the path to its
root as written): ``~`` and ``$VAR`` are literal characters to it. A target that
carries one, or a backslash, is still judged as the literal path the guest opens,
and is ALSO reported unverifiable (:func:`guest_unverifiable`), so an allow-mode
confinement refuses it rather than reasoning about a spelling another reader might
expand or split differently.

Stdlib only, with no product imports: the governance evaluator imports it, and the
evaluator loads before any feature package.
"""

from __future__ import annotations

import ntpath
import posixpath
from typing import Any, Mapping

#: The Win32 namespace prefixes, once ``/`` reads as ``\\``: each opens the
#: path after it with no normalization.
_NAMESPACES_UNIFIED = ("\\\\?\\", "\\\\.\\", "\\??\\")


def _namespaced(path: str) -> bool:
    """Whether *path* opens through a Win32 namespace prefix, in any separator spelling."""
    return path.replace("/", "\\").startswith(_NAMESPACES_UNIFIED)


#: The default data stream: ``x::$DATA`` opens ``x``.
_DEFAULT_STREAM = "::$data"


class GuestPathParams(dict[str, Any]):
    """The arguments of one remote-workspace tool call; ``root`` is the guest workdir.

    A plain ``dict`` in every other respect, so every consumer that reads the
    arguments reads them unchanged. ``root`` defaults to empty, which leaves a
    relative target unanchorable (judged unverifiable) rather than anchored at
    this gateway's working directory.

    ``also_local`` marks a call on a remote session that the builder could not
    place for certain -- an identity it does not trust, or a ``workspace``-server
    tool it does not know. Its targets are then judged under BOTH readings, the
    guest's and this gateway's, and every rule must pass under each: a deny binds
    if either reading meets it, and an allow admits only what both readings admit.
    """

    root: str
    also_local: bool
    #: The guest names files case-insensitively (a macOS computer): a deny then
    #: matches a target in any case. Set only by the builder, from the runtime's
    #: own binding of the connection.
    case_insensitive: bool

    def __init__(
        self,
        params: Mapping[str, Any] | None = None,
        /,
        *,
        root: str = "",
        also_local: bool = False,
        case_insensitive: bool = False,
    ) -> None:
        super().__init__(params or {})
        self.root = root if isinstance(root, str) else ""
        self.also_local = bool(also_local)
        self.case_insensitive = case_insensitive is True


class GuestPath(str):
    """One target of a remote-workspace call: a path inside the guest, in its flavour.

    Compares and hashes apart from a plain ``str`` of the same text, so a dedupe of
    governed items (``gate_decision`` merges title- and argument-derived pairs) can
    never keep a plain twin and drop the flavour this item must be matched with.
    """

    root: str
    case_insensitive: bool

    def __new__(cls, value: str, root: str = "", case_insensitive: bool = False) -> "GuestPath":
        item = super().__new__(cls, value)
        item.root = root if isinstance(root, str) else ""
        item.case_insensitive = case_insensitive is True
        return item

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GuestPath) and str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash((GuestPath, str(self)))

    def __reduce__(self) -> tuple[Any, ...]:
        return (GuestPath, (str(self), self.root, self.case_insensitive))


def guest_root(params: object) -> str | None:
    """The guest workdir when *params* are a workspace call's arguments, else None."""
    if isinstance(params, GuestPathParams):
        return params.root
    return None


def guest_case_insensitive(params: object) -> bool:
    """Whether *params*' guest names files case-insensitively (a macOS computer)."""
    return isinstance(params, GuestPathParams) and params.case_insensitive


def guest_also_local(params: object) -> bool:
    """Whether *params*' targets are judged as this gateway's paths as well."""
    return isinstance(params, GuestPathParams) and params.also_local


def _windows_qualified(path: str) -> bool:
    """Whether *path* is a fully qualified Windows path: a drive letter or a UNC share, then a root."""
    if _namespaced(path):
        return False
    drive, rest = ntpath.splitdrive(path)
    lettered = len(drive) == 2 and drive[1] == ":" and drive[0].isascii() and drive[0].isalpha()
    unc = len(drive) > 2 and drive[:2] in ("\\\\", "//")
    return (lettered or unc) and rest[:1] in ("\\", "/")


def guest_windows(root: str) -> bool:
    """Whether *root*, a guest workdir, is a Windows connected computer's native path."""
    return isinstance(root, str) and _windows_qualified(root)


def guest_workdir_absolute(root: object) -> bool:
    """Whether *root* is an absolute guest workdir: POSIX, or a native Windows path."""
    return isinstance(root, str) and (posixpath.isabs(root) or guest_windows(root))


def guest_absolute(path: str, root: str) -> bool:
    """Whether *path* names a guest file without the workdir, as that guest reads it."""
    return _windows_qualified(path) if guest_windows(root) else posixpath.isabs(path)


def guest_anchor(root: str, path: str) -> str:
    """*path* joined to the guest workdir *root* the way the guest tool server joins it.

    An absolute one is left as stated. A relative one is joined and its ``..``
    collapsed, with the guest's own separator rules.
    """
    if guest_windows(root):
        if _windows_qualified(path):
            return path
        return ntpath.normpath(ntpath.join(root, path or "."))
    if posixpath.isabs(path):
        return path
    return posixpath.normpath(posixpath.join(root, path or "."))


def _unaliased(path: str) -> "str | None":
    """*path* without a Win32 namespace prefix or a default-stream suffix it opens through.

    Windows reads ``/`` as ``\\`` in every spelling of a prefix (``\\./C:/x``,
    ``//?\\C:\\x``), so separators are unified first and every one is removed.
    What follows a prefix is absolute: a device (``\\\\.\\PhysicalDrive0``) or
    a volume GUID names no drive-letter or UNC file, so it is None rather than a
    name joined to the workdir.
    """
    path = path.replace("/", "\\")
    for prefix in _NAMESPACES_UNIFIED:
        if path.startswith(prefix):
            path = path[len(prefix) :]
            if path[:4].upper() == "UNC\\":
                path = "\\\\" + path[4:]
            if not _windows_qualified(path):
                return None
            break
    if path.casefold().endswith(_DEFAULT_STREAM):
        path = path[: -len(_DEFAULT_STREAM)]
    return path


def _windows_norm(item: GuestPath) -> str | None:
    """:func:`guest_norm` under a Windows workdir: the file that computer opens.

    Anchored at the workdir as ``ntpath.join`` does (a ``\\x`` stays on the
    workdir's drive, a ``D:x`` has no anchor), a namespace prefix and a
    ``::$DATA`` suffix folded, ``.``, ``..`` and both separators collapsed by
    ``ntpath.normpath``, and the trailing dots and spaces Windows drops from
    every name dropped too, so ``secret.txt.`` meets a deny naming
    ``secret.txt``. The drive letter is upper-cased; no other case changes.
    """
    plain = _unaliased(str(item))
    if plain is None:
        return None
    path = ntpath.join(item.root, plain)
    if not _windows_qualified(path):
        return None
    drive, rest = ntpath.splitdrive(ntpath.normpath(path))
    names = [name.rstrip(". ") if name not in (".", "..") else name for name in rest.split("\\")]
    path = ntpath.normpath(drive + "\\" + "\\".join(name for name in names if name))
    if not _windows_qualified(path):
        return None
    return path[0].upper() + path[1:] if path[1:2] == ":" else path


def guest_alias_windows(item: object) -> bool:
    """Whether *item* is a Windows guest target a deny cannot rule out: one with no
    file to name, or an alias spelling (:func:`_windows_fs_alias`: an 8.3 short
    name, a stream, a namespace prefix, a trailing dot or space), which the
    filesystem may open as a file a deny names in another spelling."""
    return (
        isinstance(item, GuestPath)
        and guest_windows(item.root)
        and (guest_norm(item) is None or _windows_fs_alias(str(item)))
    )


def guest_norm(item: GuestPath) -> str | None:
    """*item* as the guest resolves it, lexically: anchored, ``..`` collapsed.

    Under a Windows workdir this is :func:`_windows_norm`. Otherwise it is the
    POSIX counterpart of governance's ``_norm_item``, with every guard it has:

    1. a relative target is anchored at the guest workdir, where the guest tool
       server resolves it -- never at this gateway's working directory; with no
       absolute workdir to anchor at there is no answer (``None``);
    2. ``posixpath.normpath`` collapses ``.`` and ``..`` and repeated slashes, so a
       traversal cannot satisfy an allow-prefix or dodge a deny;
    3. a leading ``//`` (which ``normpath`` keeps, POSIX leaving it
       implementation-defined) is collapsed to ``/``, as Linux opens it.

    No ``~`` or ``$VAR`` expansion and no case folding: the guest does neither.
    Purely lexical, as the local matcher is (no ``realpath``).
    """
    if guest_windows(item.root):
        return _windows_norm(item)
    path = str(item)
    if not posixpath.isabs(path):
        root = item.root
        if not root or not posixpath.isabs(root):
            return None
        path = posixpath.join(root, path)
    path = posixpath.normpath(path)
    if path.startswith("//") and not path.startswith("///"):
        path = path[1:]
    return path


def guest_unverifiable(item: GuestPath) -> bool:
    """Whether *item* names a file only under one reader's spelling rules.

    True for a target that cannot be anchored (see :func:`guest_norm`), or that
    carries a ``$``, a backslash, or a segment starting with ``~``. The guest reads
    each literally, so the literal path is still what a deny is checked against;
    this flag adds the refusal an allow-mode confinement owes a spelling a shell,
    a ``~`` expander or a Windows reader would turn into a different file.
    """
    text = str(item)
    if guest_norm(item) is None:
        return True
    if guest_windows(item.root):
        return _windows_unverifiable(text)
    return (
        "$" in text or "\\" in text or any(seg.startswith("~") for seg in text.split(posixpath.sep))
    )


def _windows_unverifiable(text: str) -> bool:
    """:func:`guest_unverifiable` under a Windows workdir.

    A separator of either kind is ordinary there. Refused for an allow: a
    namespace prefix (it switches normalization off), a ``%``, ``$`` or ``~``
    (a variable, or an 8.3 short name such as ``PROGRA~1`` that names another
    long name), a ``:`` after the drive (a data stream), and a name that ends in
    a dot or a space (the folded spelling is the reading, not the literal).
    """
    return any(ch in text for ch in "%$") or _windows_fs_alias(text)


def _windows_fs_alias(text: str) -> bool:
    """Whether NTFS may open *text* as a file another spelling names.

    A namespace prefix, an 8.3 short name (``~``), a stream (a ``:`` after the
    drive) or a name ending in a dot or a space. ``%`` and ``$`` are not here:
    the tool server opens them as the literal characters they are, so a deny
    matches them as written (:func:`guest_alias_windows`).
    """
    if _namespaced(text) or "~" in text:
        return True
    drive, rest = ntpath.splitdrive(text)
    if ":" in rest or (not drive and ":" in text):
        return True
    return any(
        name not in (".", "..") and name != name.rstrip(". ")
        for name in rest.replace("/", "\\").split("\\")
    )
