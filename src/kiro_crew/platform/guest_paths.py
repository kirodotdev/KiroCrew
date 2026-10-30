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

import posixpath
from typing import Any, Mapping


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

    def __init__(
        self,
        params: Mapping[str, Any] | None = None,
        /,
        *,
        root: str = "",
        also_local: bool = False,
    ) -> None:
        super().__init__(params or {})
        self.root = root if isinstance(root, str) else ""
        self.also_local = bool(also_local)


class GuestPath(str):
    """One target of a remote-workspace call: a POSIX path inside the guest.

    Compares and hashes apart from a plain ``str`` of the same text, so a dedupe of
    governed items (``gate_decision`` merges title- and argument-derived pairs) can
    never keep a plain twin and drop the flavour this item must be matched with.
    """

    root: str

    def __new__(cls, value: str, root: str = "") -> "GuestPath":
        item = super().__new__(cls, value)
        item.root = root if isinstance(root, str) else ""
        return item

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GuestPath) and str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash((GuestPath, str(self)))

    def __reduce__(self) -> tuple[Any, ...]:
        return (GuestPath, (str(self), self.root))


def guest_root(params: object) -> str | None:
    """The guest workdir when *params* are a workspace call's arguments, else None."""
    if isinstance(params, GuestPathParams):
        return params.root
    return None


def guest_also_local(params: object) -> bool:
    """Whether *params*' targets are judged as this gateway's paths as well."""
    return isinstance(params, GuestPathParams) and params.also_local


def guest_norm(item: GuestPath) -> str | None:
    """*item* as the guest resolves it, lexically: anchored, ``..`` collapsed.

    The POSIX counterpart of governance's ``_norm_item``, with every guard it has:

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
    return (
        "$" in text or "\\" in text or any(seg.startswith("~") for seg in text.split(posixpath.sep))
    )
