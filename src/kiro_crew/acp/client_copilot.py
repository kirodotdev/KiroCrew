"""The GitHub Copilot CLI parts of an ``AcpClient`` session's permission handling.

Copilot CLI names a write's targets only in its per-file permission frame, an MCP
call's server only in its ``tool_call`` title, and is verified on one release, so
three things here make its requests judgeable by the shared gates: the edit-target
seed (and its restore), the MCP-identity confirmation against the permission frame,
and the handshake refusal of an unverified release. Every function is reached only
on a Copilot session (harness-parity H13).

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_tool_call_diff_path``, ``_tool_call_mcp_server``,
``_tool_call_params``, ``_tool_call_tool_name``.

Instance fields read: ``_copilot_ambiguous_mcp_calls``, ``_copilot_unmatched_titles``,
``_tool_call_is_shell``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.harness.copilot import note_copilot_identity_record
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk.backends import COPILOT_VERIFIED_VERSION
from kiro_crew.platform.tool_paths import WRITE_PLANE_KINDS, target_paths

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient


def _refuse_unverified_copilot_version(self: AcpClient) -> None:
    """Refuse a Copilot CLI session on any release but the verified one, at the handshake.

    Copilot's ``allow_all`` read-back, the mode tripwire and the edit-target checks
    refuse on their own when the wire changes shape; the lists do not -- a new slash
    command that turns approval off, a new project config file Copilot acts on
    unprompted, a new env var it reads -- so on another release they would let a
    repository's own commands run with no permission request, silently. A log line
    does not stop that, so the session is refused, fail closed as the project-config
    refusal is, with the pinned install command as the remedy. The release read is
    the one the session RUNS (``agentInfo.version`` off ``initialize``), and an
    initialize that names no version is refused too. Raised as
    ``AcpToolGateUnroutable`` -- on such a release Crew cannot tell that every tool
    call reaches its gate -- so the start path ends the child WITHOUT the one respawn
    a transport fault earns (a fresh process reports the same version), and as
    user-worded and non-transient so every surface shows the install command.
    Copilot only (harness-parity H13).
    """
    if not self._is_copilot:
        return
    version = self.agent_version
    if version == COPILOT_VERIFIED_VERSION:
        return
    # Two sentences, for the person reading it in a chat: what happened, then the
    # command that fixes it. Why the pin exists is this docstring's, not theirs.
    what = (
        f"Copilot CLI {version} is not the version Kiro Crew verified "
        f"({COPILOT_VERIFIED_VERSION}), so this chat was refused."
        if version
        else "Copilot CLI did not report its version, so Kiro Crew could not confirm "
        f"it is the verified one ({COPILOT_VERIFIED_VERSION}) and refused this chat."
    )
    raise AcpToolGateUnroutable(
        f"{what} Install the verified release, then start a new chat: "
        f"npm i -g @github/copilot@{COPILOT_VERIFIED_VERSION}",
        transient=False,
        user_worded=True,
    )


#: Marks a cache entry that was absent before a Copilot edit seed overwrote it.
_MISSING = object()

#: The target a Copilot CLI edit frame is judged by when it names no absolute path.
#: Relative on purpose: the edit gates read a relative diff-block path as UNANCHORED
#: and deny, so a frame whose target Crew cannot see is refused, never let through
#: as a call with no target. It is always the FIRST target, the one the event's
#: ``diff_path`` carries, because only that slot is read as unanchored.
_COPILOT_UNRESOLVED_EDIT_TARGET = "copilot-edit-target-unresolved"

#: ACP kinds a Copilot frame is a write under even without write-shaped params:
#: the shared write plane plus ``move``, which ACP defines and the shared set lacks.
_COPILOT_WRITE_KINDS = WRITE_PLANE_KINDS | {"move"}


def _copilot_diff_header_paths(diff: object) -> tuple[list[str], bool]:
    """The absolute paths a Copilot permission frame's ``diff`` names, and whether
    any header could not be read.

    Copilot renders each file's change as a git diff of absolute paths with the
    leading ``/`` folded into the ``a/`` / ``b/`` prefix (``--- a/w/src.txt``). A
    move is one frame whose ``fileName`` is the DESTINATION only (measured on a
    ``*** Move to:`` patch); the source it removes is named only in the ``---``
    header, so the headers are read too. Only header lines count: a file section
    opens at ``diff --git`` (or the start) and its header ends at the first
    ``@@``, so a body line that happens to start ``-- a/`` is not read as one.
    Lines split on ``\n`` alone with one trailing ``\r`` dropped, as the shared
    patch reader does; a header that still holds another break character, a
    ``---``/``+++`` line in any other form (quoted, unprefixed), or a ``rename``
    line makes the diff unreadable, and the caller denies. A path carrying a tab
    (git's suffix after a name with spaces) is judged both whole and cut at the
    tab. ``/dev/null`` is the git spelling of "no file" and is not a target.
    """
    if not isinstance(diff, str):
        return [], False
    found: list[str] = []
    unreadable = False
    in_header = True
    for line in diff.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if line.startswith("diff --git "):
            in_header = True
            continue
        if not in_header:
            continue
        if line.startswith("@@"):
            in_header = False
            continue
        if line.startswith(("rename ", "copy ")):
            unreadable = True
            continue
        if not line.startswith(("---", "+++")):
            continue
        prefix = "--- a/" if line.startswith("---") else "+++ b/"
        if not line.startswith(prefix) or (line and line.splitlines() != [line]):
            unreadable = True
            continue
        rest = line[len(prefix) :]
        # A Windows drive path keeps its drive (``a/C:/w/x``), never a leading ``/``.
        is_drive = len(rest) > 2 and rest[0].isalpha() and rest[1] == ":" and rest[2] in "/\\"
        path = rest if is_drive else "/" + rest
        for spelling in (path, path.split("\t", 1)[0]):
            if spelling != "/dev/null" and spelling not in found:
                found.append(spelling)
    return found, unreadable


def _confirm_copilot_mcp_identity(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Hold a recovered Copilot MCP identity to the permission frame's own name.

    The identity :meth:`_recover_copilot_mcp_identity` read off the ``tool_call``
    title is trusted only while the permission frame asks about the same tool: Copilot
    names a served tool there by its bare name (``marker`` for ``probe-marker``,
    measured). A frame naming anything else means the two frames disagree about what
    is running, so the call is treated as one whose server cannot be established and
    refused (:func:`_refuse_identity_drift`), rather than judged as either.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        return
    tool_call_id = tool_call.get("toolCallId")
    if not isinstance(tool_call_id, str):
        return
    title = tool_call.get("title")
    ambiguous = getattr(self, "_copilot_ambiguous_mcp_calls", None)
    if not self._tool_call_mcp_server.get(tool_call_id):
        # No placed name matched the tool_call title, yet that title reads
        # ``<server>-<this tool>``: a served tool whose server name Copilot spelled
        # differently from Crew's. Its server cannot be named, so it is refused.
        # Read, not consumed: a re-ask for the same id must meet the same refusal.
        # The record is bounded and released when the call finishes.
        unmatched = (getattr(self, "_copilot_unmatched_titles", None) or {}).get(tool_call_id)
        if (
            ambiguous is not None
            and isinstance(title, str)
            and title
            and isinstance(unmatched, str)
            and unmatched.endswith(f"-{title}")
        ):
            note_copilot_identity_record(ambiguous, ambiguous, tool_call_id)
        return
    if title == self._tool_call_tool_name.get(tool_call_id):
        return
    self._tool_call_mcp_server[tool_call_id] = ""
    self._tool_call_tool_name[tool_call_id] = ""
    if ambiguous is not None:
        note_copilot_identity_record(ambiguous, ambiguous, tool_call_id)


def _seed_copilot_edit_targets(
    self: AcpClient, msg: JsonRpcMessage
) -> tuple[str, object, object] | None:
    """Put a Copilot CLI edit's real targets where the shared permission parser reads them.

    Copilot CLI 1.0.100 streams its ``apply_patch`` call with a STRING ``rawInput``
    (the patch text), which the tool-call cache drops, and asks permission once PER
    FILE with the file's absolute path as ``rawInput.fileName`` and in ``locations``
    -- keys the shared target extraction does not read -- and names a move's source
    only in its diff headers. Without this the permission event carried no write
    target, so a ``filesystem.write`` ceiling and the edit gates judged an edit with
    nothing to judge. Measured on a two-file patch: two frames under one
    ``toolCallId``, each naming its own file.

    A frame is seeded when its kind is a write kind (``edit``, ``delete``, ``move``)
    or its params are write-shaped (``fileName`` or ``diff``), and its tool_call was
    not identified as a shell or MCP call: routing either onto the write plane would
    trade its command or server checks for the edit gate's, so those keep the shared
    parser's reading. A read (``{path}``) is left alone too. Nothing is rewritten
    about the call's kind or shell classification. The targets are MERGED into the
    trusted params the preceding ``tool_call`` frame cached, under ``path``, beside
    any content-block path it cached; the first becomes the diff-block path every
    gate routes on, and the always-enforced checks read the whole trusted set. The
    seed is transient: the caller restores the overwritten entries once the event is
    built (:func:`_restore_copilot_edit_seed`), so each frame is seeded from the
    ``tool_call`` frame's own params, is judged by its own file, and leaves nothing
    retained. A frame
    naming no absolute path, any relative path, or a diff Crew cannot read puts the
    relative marker first, which the gates read as unanchored and deny. Called only
    for copilot (the caller tests it, harness-parity H13).
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        return None
    raw = tool_call.get("rawInput")
    write_shaped = isinstance(raw, dict) and ("fileName" in raw or "diff" in raw)
    if tool_call.get("kind") not in _COPILOT_WRITE_KINDS and not write_shaped:
        return None
    tool_call_id = tool_call.get("toolCallId")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    if self._tool_call_is_shell.get(tool_call_id) or self._tool_call_mcp_server.get(tool_call_id):
        return None
    base = self._tool_call_params.get(tool_call_id, _MISSING)
    base_diff = self._tool_call_diff_path.get(tool_call_id, _MISSING)
    restore = (tool_call_id, base, base_diff)
    if base is _MISSING:
        base = None
    if base_diff is _MISSING:
        base_diff = None
    unresolved = False
    candidates: list[object] = [base_diff] if base_diff else []
    if isinstance(raw, dict):
        if raw.get("fileName") is not None:
            candidates.append(raw.get("fileName"))
        # Every accepted path spelling at any depth, as the shared parser reads them.
        nested = target_paths(raw)
        unresolved = nested.truncated
        candidates.extend(nested)
        header_paths, unreadable = _copilot_diff_header_paths(raw.get("diff"))
        unresolved = unresolved or unreadable
        candidates.extend(header_paths)
    locations = tool_call.get("locations")
    for location in locations if isinstance(locations, list) else []:
        if isinstance(location, dict) and location.get("path") is not None:
            candidates.append(location.get("path"))
    targets: list[str] = []
    for candidate in candidates:
        if not (isinstance(candidate, str) and os.path.isabs(candidate)):
            unresolved = True
        elif candidate not in targets:
            targets.append(candidate)
    if unresolved or not targets:
        targets.insert(0, _COPILOT_UNRESOLVED_EDIT_TARGET)
    merged: dict = dict(base) if isinstance(base, dict) else {}
    prior = merged.get("path")
    if isinstance(prior, list):
        prior_paths = list(prior)
    elif prior is None:
        prior_paths = []
    else:
        prior_paths = [prior]
    # The seeded targets go first so the marker, when present, stays the first target.
    merged["path"] = [*targets, *prior_paths]
    self._tool_call_params[tool_call_id] = merged
    self._tool_call_diff_path[tool_call_id] = targets[0]
    return restore


def _restore_copilot_edit_seed(self: AcpClient, restore: tuple[str, object, object]) -> None:
    """Put back the cache entries :func:`_seed_copilot_edit_targets` overwrote.

    The seed exists only for the one event being built, which keeps its own copy of
    the targets, so nothing of it is retained past that build: the caches hold exactly
    what the ``tool_call`` frame put there (and its own bound applies), and a later
    frame for the same id is seeded afresh from that same base.
    """
    tool_call_id, params, diff_path = restore
    params_cache: dict[str, Any] = self._tool_call_params
    diff_cache: dict[str, Any] = self._tool_call_diff_path
    for cache, original in ((params_cache, params), (diff_cache, diff_path)):
        if original is _MISSING:
            cache.pop(tool_call_id, None)
        else:
            cache[tool_call_id] = original
