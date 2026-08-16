"""The dashboard-control MCP server — the agent's hands on the dashboard's own
organization surfaces.

Deliberately NOT part of ``kirocrew-core``. Core is the tool surface EVERY
session carries, and kiro-cli reads ``tools/list`` once per session, so anything
listed there spends context in every request of every session forever — whether
or not the user ever wants it. Reorganizing the sidebar is something a user asks
for on purpose, occasionally; it does not belong in the always-on surface.

So this is its own server, and it is an ASSIGNABLE SET: an agent gets these
tools when its own kiro spec carries both the ``mcpServers`` entry and the
matching ``@kirocrew-dashboard`` reference in ``tools`` — kiro-cli loads a server
only when something references it. The default agent's spec carries neither, so
a session on the default agent pays nothing for a capability it never uses, and
an agent that should reorganize the dashboard is granted it deliberately.

Assignment is per SERVER, not per tool: a spec that references this server gets
every tool in it. That is the unit to keep in mind when adding one — a capability
that must be grantable separately belongs in a server of its own.

What it controls today is the chat (sidebar) folder tree: read it, create a
folder, reparent a folder, and file a live session into one. Create and move
only — no delete and no rename, so nothing here can lose a conversation. Every
tool is a thin proxy over the dashboard's existing endpoints (loopback +
``X-Internal-Secret``); the endpoints keep owning every tree invariant, and the
gateway audits each write with the caller's declared component name — this
server's requests carry ``X-Internal-Caller: kirocrew-dashboard`` (attached
centrally by ``run_mcp_stdio_loop`` + the ``mcp_core`` request helpers), which
``chat_folders._audit_origin`` validates against its known-caller set — so the
log can tell an agent's move from the user's own, and from any future internal
caller's.

Why the set needs no second gate behind the assignment: these tools grant no
read the agent does not already have (``list_sessions`` in ``kirocrew-core`` is
always available and already returns every session's title and key), they cannot
delete a folder or a conversation, and the worst outcome is a sidebar the user
has to tidy. Contrast the keystone leaves in ``security.py``
(``computer_use.json``, ``browser-mode-enabled``, the Ops Mission Control mode):
each grants reach OUTSIDE Kiro Crew — desktop input synthesis, the operator's
logged-in browser, writes against production incident tooling — or is the
security floor itself, and each is therefore stored where the agent cannot write.

The rule that follows, and the reason the tool set is ratcheted in
``test_mcp_dashboard_registration.py``: a capability whose blast radius DOES
require authorization needs its own keystone leaf, and being merely unreferenced
in the default spec is not that. Session control (driving or stopping another
session) is that shape.

Identity posture: these tools use the NON-strict session resolver (inherited
from the ``mcp_core`` request helpers). The header is ATTRIBUTION here, not
authorization — the endpoints authorize on the internal secret, and the session
a move targets is named explicitly by slot key, never derived from the caller's
identity. The strict resolver would fail closed in sandboxed sessions without
protecting anything, since no effect here reads the header.
"""

from __future__ import annotations

import logging
import re as _re
from typing import Any
from urllib.parse import quote

# Same cross-module reuse as ``mcp_computer``: the authenticated loopback client
# to the gateway lives in ``mcp_core``. Importing it costs 341ms/40MB in this
# process (measured) — under ``mcp_computer``'s own import cost, because
# mcp_core's heavy dependencies are function-local.
from kiro_crew.mcp_core import (
    _get,
    _patch,
    _post,
    _resolve_session_key,
    _resolve_session_key_strict,
)
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import (
    CHAT_FOLDER_CREATE_SCHEMA,
    CHAT_FOLDER_MOVE_SCHEMA,
    CHAT_FOLDER_MOVE_SESSION_SCHEMA,
    CHAT_FOLDER_TREE_SCHEMA,
    MCP_DASHBOARD_SCHEMAS,
    validate_tool_args,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-dashboard"
SERVER_VERSION = "1.0.0"

# The folder endpoints store ``name[:100]``. Mirroring the number here is what
# lets this server refuse an overlong name instead of writing one it cannot
# address afterwards; a mismatch shows up as the duplicate-creation the
# too-long-segment test pins.
_MAX_FOLDER_NAME = 100


def _tool_definitions() -> list[dict[str, Any]]:
    """The tool surface this server advertises."""
    return [
        {
            "name": "chat_folder_tree",
            "description": (
                "Show the user's SIDEBAR folder tree — the folders they organize "
                "their chat sessions in — with the live sessions filed in each one. "
                "Returns per folder: id, human path, project directory, default "
                "agent, and how many archived (history) sessions are filed there; "
                "then one line per live session (slot key + title) nested under it, "
                "and an '(unfiled)' group for sessions at the top level. Use this to "
                "get folder ids/paths and session keys before calling "
                "chat_folder_create / chat_folder_move / chat_folder_move_session, "
                "or when the user asks what their tree looks like. This is the "
                "folder-shaped view; list_sessions is the flat newest-first one."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "chat_folder_create",
            "description": (
                "Create a sidebar folder (or subfolder) for chat sessions. "
                "``parent`` accepts a folder id OR a '/'-separated human path from "
                "chat_folder_tree; missing path segments are created too (mkdir -p). "
                "Omit ``parent`` (or pass 'root') for a top-level folder. Creating a "
                "folder never moves anything — file sessions into it with "
                "chat_folder_move_session. Not available to an app agent: the folder "
                "tree is shared with the person and every other app, so an app files "
                "its own sessions into folders that already exist."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Folder name (max 100 chars). Cannot contain '/' — that "
                            "would render like a nested path and be unaddressable."
                        ),
                    },
                    "parent": {
                        "type": "string",
                        "description": "Parent folder id or human path. Omit / 'root' for top level.",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "chat_folder_move",
            "description": (
                "Reparent a sidebar folder — nest it under another folder, or move it "
                "back to the top level. Moves the folder with everything in it "
                "(sessions and subfolders travel with it); nothing is deleted. "
                "Cycle-guarded: a folder cannot become its own descendant. "
                "``folder`` and ``new_parent`` are each a folder id or human path; "
                "omit ``new_parent`` (or pass 'root') for the top level. Not available "
                "to an app agent — reparenting reshapes a tree shared with the person "
                "and every other app."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder to move (id or path)."},
                    "new_parent": {
                        "type": "string",
                        "description": "Destination parent folder (id or path). Omit / 'root' for top level.",
                    },
                },
                "required": ["folder"],
            },
        },
        {
            "name": "chat_folder_move_session",
            "description": (
                "File a LIVE chat session into a sidebar folder, or unfile it to the "
                "top level (omit ``folder`` / pass 'root'). ``session`` is a slot key "
                "or 'dashboard:<slot>' session key from chat_folder_tree, or a "
                "session's exact title when that title is unique. ``folder`` is a "
                "folder id or human path — the folder must already exist "
                "(chat_folder_create makes one). Metadata only: the session keeps its "
                "transcript, model, and any running turn. ARCHIVED (history) sessions "
                "cannot be moved — revive one into the sidebar first, then call this."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Slot key, 'dashboard:<slot>' session key, or exact unique session title.",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Destination folder id or human path. Omit / 'root' to unfile.",
                    },
                },
                "required": ["session"],
            },
        },
    ]


def _list_tools() -> list[dict[str, Any]]:
    """The tool surface, unconditionally.

    Reaching this process at all means an agent spec referenced this server, so
    the assignment already happened; there is nothing left to gate here.
    """
    return _tool_definitions()


def _get_rows(path: str) -> tuple[list[dict], str | None]:
    """GET a gateway endpoint whose success body is a JSON **array**.

    ``_get`` is written for object bodies and signals failure with
    ``{"error": ...}``, so an array endpoint (``/api/chat/folders``,
    ``/api/chat/slots``) needs the two shapes split apart. Returns
    ``(rows, None)`` on success and ``([], error)`` otherwise. A body that is
    neither an array nor an error object is reported as an error rather than
    read as empty: "the tree is empty" and "the endpoint is broken" must not
    render identically.
    """
    payload: object = _get(path)
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)], None
    if isinstance(payload, dict):
        err = payload.get("error")
        if err:
            return [], str(err)
    return [], f"unexpected response shape from {path}"


# Sidebar folder ids are minted as ``uuid.uuid4().hex[:12]``
# (``chat_folders.api_chat_folder_create``), so an id-shaped reference is
# recognizable and must never be auto-created as a folder NAME.
_CHAT_FOLDER_ID_RE = _re.compile(r"[0-9a-f]{12}")


def _chat_folder_paths(folders: list[dict]) -> dict[str, str]:
    """Map each sidebar folder id to its ``parent/child`` human path.

    Chat folders persist only ``parent_id`` (unlike artifact folders, whose
    store computes ``path`` server-side), so the path is derived here. Walks
    parents with a visited-set guard so a pre-existing cycle in
    ``folders.json`` — which ``_is_descendant`` in ``chat_folders.py`` also
    defends against — cannot hang the tool.
    """
    by_id = {str(f.get("id") or ""): f for f in folders if f.get("id")}
    paths: dict[str, str] = {}
    for fid in by_id:
        segments: list[str] = []
        seen: set[str] = set()
        cur = fid
        while cur and cur in by_id and cur not in seen:
            seen.add(cur)
            segments.append(str(by_id[cur].get("name") or "?"))
            cur = str(by_id[cur].get("parent_id") or "")
        paths[fid] = "/".join(reversed(segments))
    return paths


def _chat_folder_children(folders: list[dict], parent_id: str, name: str) -> list[dict]:
    """Every DIRECT child of ``parent_id`` whose name equals ``name`` (case-insensitive).

    Chat folder names are not unique within a parent — the sidebar happily holds
    two folders called ``0811`` under the same parent — so a path segment can be
    genuinely ambiguous. Returning the whole match list is what keeps the two
    callers honest: taking the first match would patch an arbitrary sibling, or
    file a session into whichever duplicate happened to be created first.
    """
    target = str(name).strip().lower()
    return [
        f
        for f in folders
        if str(f.get("parent_id") or "") == parent_id
        and str(f.get("name", "")).strip().lower() == target
    ]


def _ambiguous_segment_error(seg: str, matches: list[dict]) -> str:
    """Refusal naming the duplicate folders, so the caller can pick one by id."""
    ids = ", ".join(str(m.get("id") or "?") for m in matches)
    return (
        f"{len(matches)} folders named {redact(seg)} share the same parent "
        f"({ids}) — pass the folder id instead of a path"
    )


def _resolve_chat_folder_ref(
    ref: str, folders: list[dict], *, create_missing: bool
) -> tuple[str, list[str], str | None]:
    """Resolve a sidebar-folder reference to a folder id. THE resolution chokepoint.

    Every tool addresses a folder through here, so create, move and move-session
    can never disagree about what a reference means. Returns
    ``(folder_id, created_names, error)``; ``""`` = top level (empty ref or
    ``"root"``). ``created_names`` is returned even alongside an error so a
    partial mkdir -p is reported rather than silently left behind.

    Three things a chat-folder reference can mean, in order:

    * **An id.** Unambiguous. An id-shaped ref that does not exist is a lookup
      failure even when ``create_missing`` — ids are minted server-side, so
      creating a folder literally named after the hex id is never what a caller
      meant.
    * **A full human path.** A folder NAME may itself contain ``/`` (the
      sidebar permits it), so ``A/B`` can be one folder named ``A/B`` as well as
      ``B`` inside ``A``, and the tree renders both identically. Both readings
      are computed; when they disagree, or when either is itself duplicated, the
      reference is refused rather than resolved to whichever came first.
    * **A path to walk**, segment by segment, creating what is missing when
      ``create_missing``.

    Pure apart from the creation leg: the caller already holds the folder list,
    and re-fetching per reference would let the tree shift between the two
    lookups of a single move.
    """
    ref = str(ref or "").strip()
    if not ref or ref.lower() == "root":
        return "", [], None
    if any(str(f.get("id") or "") == ref for f in folders):
        return ref, [], None
    if _CHAT_FOLDER_ID_RE.fullmatch(ref):
        return "", [], f"folder not found: {redact(ref)}"

    # Reading 2: a folder whose own name (or ancestry) renders to exactly this path.
    paths = _chat_folder_paths(folders)
    exact = sorted(fid for fid, p in paths.items() if p.strip().lower() == ref.lower())
    if len(exact) > 1:
        return "", [], (
            f"{len(exact)} folders render the same path {redact(ref)} "
            f"({', '.join(exact)}) — pass the folder id instead of a path"
        )

    # Reading 3: walk the segments. When the exact reading already resolved, the
    # walk runs resolve-only — there is nothing to create, and creating before
    # the two readings are compared would mutate the tree on a reference we are
    # about to refuse.
    walked, created, walk_err = _walk_chat_folder_segments(
        ref, folders, create_missing=create_missing and not exact
    )
    if walk_err:
        return "", created, walk_err

    if exact and walked and walked != exact[0]:
        return "", [], (
            f"{redact(ref)} is ambiguous: it is both a folder's own name "
            f"({exact[0]}) and a nested path ({walked}) — pass the folder id"
        )
    if exact:
        return exact[0], [], None
    if walked:
        return walked, created, None
    if not create_missing:
        return "", [], f"folder not found: {redact(ref)}"
    # create_missing with nothing walked means every segment was created and the
    # walk returned the leaf, so this is unreachable via the tools; keep the
    # refusal rather than returning the library root by accident.
    return "", created, f"folder not found: {redact(ref)}"


def _walk_chat_folder_segments(
    ref: str, folders: list[dict], *, create_missing: bool
) -> tuple[str, list[str], str | None]:
    """Walk a ``/``-separated path segment by segment. ONE walk, two modes.

    Returns ``(folder_id, created_names, error)``; ``folder_id`` is ``""`` when a
    segment is missing and ``create_missing`` is false. Duplicate siblings are
    refused in BOTH modes — resolving to the first match would act on an
    arbitrary folder, and creating under it would bury the new folder in
    whichever duplicate happened to come first.

    ``folders`` is appended in place for each created row so a later path render
    sees it. Created names come back even alongside an error, so a partial
    mkdir -p is reported rather than silently left behind.
    """
    walked = ""
    parent = ""
    created: list[str] = []
    for raw in [s.strip() for s in ref.split("/") if s.strip()]:
        # Redact BEFORE the lookup, not only before the write. The name is
        # agent-authored and lands in durable state the sidebar re-renders on
        # every visit, so it gets the egress pass (same reason issue-radar
        # findings are redacted before they persist) — which means the STORED
        # name is the redacted one. Matching on the raw text would therefore
        # never find the folder this walk itself created, and every repeated
        # call would add another sibling. One value for both halves.
        seg = redact(raw)
        # The endpoint stores ``name[:100]``, so a longer segment would come back
        # under a name this walk cannot match — and the next call, still not
        # matching, would create ANOTHER truncated sibling. Refuse instead: a
        # caller who is told the limit can shorten the name, while a silent
        # truncation buries duplicates under a path nobody asked for. Measured
        # on the redacted form, since that is what gets stored and truncated.
        if len(seg) > _MAX_FOLDER_NAME:
            return (
                "",
                created,
                f"folder name too long ({len(seg)} chars): "
                f"`{seg[:40]}…` — keep each path segment to "
                f"{_MAX_FOLDER_NAME} characters or fewer",
            )
        matches = _chat_folder_children(folders, parent, seg)
        if len(matches) > 1:
            return "", created, _ambiguous_segment_error(seg, matches)
        if matches:
            walked = str(matches[0].get("id") or "")
            parent = walked
            continue
        if not create_missing:
            return "", created, None
        made = _post("/api/chat/folders", {"name": seg, "parent_id": parent})
        if made.get("error"):
            return "", created, str(made["error"])
        folders.append(made)
        created.append(str(made.get("name") or seg))
        walked = str(made.get("id") or "")
        parent = walked
    return walked, created, None


def _resolve_chat_folder_id(ref: str, folders: list[dict]) -> tuple[str, str | None]:
    """Resolve an EXISTING sidebar folder (id or human path) to its id."""
    fid, _created, err = _resolve_chat_folder_ref(ref, folders, create_missing=False)
    return fid, err


def _ensure_chat_folder_path(ref: str, folders: list[dict]) -> tuple[str, list[str], str | None]:
    """Resolve a parent-folder reference, creating missing segments (mkdir -p)."""
    return _resolve_chat_folder_ref(ref, folders, create_missing=True)


def _resolve_chat_slot_key(ref: str, slots: list[dict]) -> tuple[str, str | None]:
    """Resolve a session reference to the slot key the folder endpoint takes.

    Accepts the slot key itself, a ``dashboard:<slot>`` session key (what
    ``search_chat_history`` and the session tools hand out), or a session's
    exact title when that title is unique. Title matching is exact and
    case-insensitive — never a substring — so an ambiguous or partial name
    fails loudly with the candidate keys instead of filing the wrong session.
    """
    ref = str(ref or "").strip()
    if not ref:
        return "", "session required"
    explicit_key = ref.lower().startswith("dashboard:")
    bare = ref[len("dashboard:") :] if explicit_key else ref
    by_key = {str(s.get("key") or ""): s for s in slots if s.get("key")}
    titled = [
        str(s.get("key") or "")
        for s in slots
        if str(s.get("title") or "").strip().lower() == ref.lower()
    ]
    if bare in by_key:
        # A tab can be TITLED with another session's key, so a bare reference can
        # match one session by key and a different one by title. Preferring the
        # key silently would file the wrong session; the ``dashboard:`` prefix is
        # the caller's way to say "this is a key".
        rivals = [k for k in titled if k != bare]
        if rivals and not explicit_key:
            return "", (
                f"{redact(ref)} is one session's key and another's title "
                f"({', '.join([bare] + rivals)}) — prefix with 'dashboard:' to "
                "select the key, or pass the other session's key"
            )
        return bare, None
    if explicit_key:
        # ``dashboard:`` asserted a key, and no slot has it. Falling through to
        # title matching would honour the opposite of what the caller said and
        # could file a session that merely happens to be TITLED with that key.
        return "", (
            f"no live session has the key {redact(bare)} — call chat_folder_tree "
            "for slot keys. An ARCHIVED session cannot be moved: revive it into "
            "the sidebar first"
        )
    if len(titled) == 1:
        return titled[0], None
    if len(titled) > 1:
        return "", (
            f"{len(titled)} live sessions share the title {redact(ref)} "
            f"({', '.join(titled)}) — pass the slot key instead"
        )
    return "", (
        f"no live session matches {redact(ref)} — call chat_folder_tree for slot "
        "keys. An ARCHIVED session cannot be moved: revive it into the sidebar first"
    )


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_DASHBOARD_SCHEMAS.get(name)
    if schema:
        return validate_tool_args(args, schema)
    return args


#: Caller-key prefixes whose bearer is DELEGATED work — it runs on behalf of
#: whatever created it, so "matches no chat slot" cannot be read as "has no app
#: to be confined to". A subagent runs under its spawner; a cron can be created
#: by an app (``CronSDK`` tags those jobs), so a cron key can carry an app's
#: reach without carrying its slot.
#:
#: This list is KNOWINGLY INCOMPLETE, and that is a deliberate position rather
#: than an oversight. It enumerates the delegated key forms that exist today; a
#: key form added later will read as unscoped until it is added here. The sound
#: shape is the inverse — grant authority only on POSITIVE confirmation that the
#: caller is the person, and refuse everything unplaceable — but that also takes
#: these tools from callers who legitimately have no slot and no app (Slack
#: threads, channel sessions, and the person's own crons), which is a behaviour
#: change with its own tradeoff. Until that inversion is taken, adding a prefix
#: here is the cheap half and the gap is documented rather than hidden.
_DELEGATED_CALLER_PREFIXES = ("subagent:", "cron:")


def _caller_app_scope(caller_key: str, rows: list[dict]) -> str | None:
    """App owning the calling session, "" when it owns none, ``None`` to refuse.

    Located by finding the CALLER's own row: a slot created by an app carries
    that app in ``app`` (App Kit §5.2), and a session the person started carries
    none — which is what keeps "organize my sessions" working for the user's own
    agent while confining an app's.

    Searched over the unfiltered rows on purpose: the caller's own session may
    itself be incognito, and it is still the caller.

    An unlocatable caller is unscoped only when it is the kind of session that
    never had an app to be confined to — a Slack thread or a channel session has
    no dashboard slot, and refusing those would deny the tools to callers no app
    could have been attached to.

    A **delegated** caller is the exception (see
    ``_DELEGATED_CALLER_PREFIXES``): absence proves nothing about who it runs
    for, so it is refused rather than granted the person's authority.

    A ``dashboard:`` caller is refused for a DIFFERENT reason, which is why it
    is not in that tuple: it is not delegated work, it NAMES a slot. So absence
    is never the "never had a slot to be confined to" case that makes a Slack
    thread unscoped — it means the named slot is not there, which happens when
    the tab was closed while this call was still in flight (the slot is popped
    synchronously, without draining in-flight MCP calls) or when the key is
    simply wrong. An app-owned session going through that race would otherwise
    hand its agent the authority the app itself does not have.
    """
    slot = caller_key.split(":", 1)[-1] if ":" in caller_key else caller_key
    for r in rows:
        if str(r.get("key") or "") == slot:
            return str(r.get("app") or "")
    if caller_key.startswith(_DELEGATED_CALLER_PREFIXES):
        return None
    if caller_key.startswith("dashboard:"):
        return None
    return ""


def _visible_chat_slots() -> tuple[list[dict], str | None]:
    """The live sessions these tools may see, private and foreign ones removed.

    Two filters, at the ONE place the list enters this server — both the tree
    render and the session resolver read the result, so neither can expose a
    title or key it should not, and neither can file such a session.

    **Private.** A slot whose ``memory_mode`` is not ``persistent`` — incognito
    or temporary — is a session the user chose to keep out of the record. The
    endpoint returns it like any other. An absent field reads as persistent,
    matching the slot default.

    **Foreign.** The endpoint is not app-scoped, so an app agent holding this
    set would otherwise enumerate every session's title and key across every
    other app and the user's own. Identity is resolved STRICTLY (see
    ``chat_folder_move_session`` for why the lenient walk is unsafe) and the
    list is narrowed to the caller's own app; an unverifiable caller is refused
    rather than handed a list it cannot be scoped against.
    """
    rows, err = _get_rows("/api/chat/slots")
    if err:
        return [], err
    live = [r for r in rows if str(r.get("memory_mode") or "persistent") == "persistent"]
    caller_key = _resolve_session_key_strict()
    if not caller_key:
        return [], (
            "cannot verify which session is calling, so the session list is "
            "withheld — these tools scope what they show to the caller"
        )
    scope = _caller_app_scope(caller_key, rows)
    if scope is None:
        return [], (
            "cannot establish what this caller is allowed to see, so the session "
            "list is withheld — a subagent or a scheduled job runs on behalf of "
            "whatever created it and cannot be granted more than that"
        )
    if not scope:
        return live, None
    return [r for r in live if str(r.get("app") or "") == scope], None


def _refuse_tree_shaping_if_app_scoped(verb: str) -> str | None:
    """Error text when the caller may not reshape the folder tree, else None.

    Folders are ONE tree per instance with no owner field, so there is no "this
    app's folder" to confine a write to: creating, renaming, reparenting or
    deleting one lands in the person's sidebar and in every other app's view of
    it. An app-scoped caller is therefore refused the tree-shaping verbs
    outright rather than given authority that cannot be bounded.

    What an app KEEPS is everything already scoped to it: reading the tree, and
    filing its OWN sessions into a folder that exists. The person's own agent is
    unscoped and keeps full authority — reorganising sessions is the point of
    these tools.

    Identity is resolved strictly, and an unverifiable caller is refused too: a
    write to shared structure is not the place to assume the caller is the human.
    """
    caller_key = _resolve_session_key_strict()
    if not caller_key:
        return (
            f"Error: cannot verify which session is calling, so {verb} is "
            "refused — reshaping the shared folder tree requires a caller "
            "identity the gateway can vouch for."
        )
    rows, err = _get_rows("/api/chat/slots")
    if err:
        return f"Error: {err}"
    scope = _caller_app_scope(caller_key, rows)
    if scope is None:
        return (
            f"Error: cannot establish what this caller is allowed to change, so "
            f"{verb} is refused — a subagent or a scheduled job runs on behalf of "
            "whatever created it and cannot be granted more than that."
        )
    if scope:
        return (
            f"Error: {verb} is not available to an app — the folder tree is "
            "shared with the person and every other app, and folders carry no "
            "owner, so there is no app-private folder to change. Filing your "
            "own sessions into an existing folder still works."
        )
    return None


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Dispatch one validated tool call."""
    if name == "chat_folder_tree":
        validate_tool_args(args, CHAT_FOLDER_TREE_SCHEMA)
        chat_folders, folders_err = _get_rows("/api/chat/folders")
        if folders_err:
            return f"Error: {folders_err}"
        chat_slots, slots_err = _visible_chat_slots()
        if slots_err:
            return f"Error: {slots_err}"
        tree_paths = _chat_folder_paths(chat_folders)
        # Group live sessions by folder up front so an id that no longer has a
        # folder row (a slot pointing at a deleted folder) still surfaces under
        # "(unfiled)" instead of vanishing from the tree.
        known_ids = set(tree_paths)
        by_folder: dict[str, list[dict]] = {}
        for slot_row in chat_slots:
            fid = str(slot_row.get("folder_id") or "")
            by_folder.setdefault(fid if fid in known_ids else "", []).append(slot_row)

        def _session_line(row: dict, indent: str) -> str:
            bits = []
            if row.get("running"):
                bits.append("running")
            if row.get("pinned"):
                bits.append("pinned")
            if row.get("app"):
                bits.append(f"app:{row['app']}")
            suffix = f"  [{', '.join(bits)}]" if bits else ""
            title = str(row.get("title") or "(untitled)")
            return f"{indent}· {row.get('key', '?')}  {title}{suffix}"

        tree_lines = [
            f"\U0001f5c2\ufe0f Sidebar folder tree — {len(chat_folders)} folder"
            f"{'' if len(chat_folders) == 1 else 's'}, {len(chat_slots)} live session"
            f"{'' if len(chat_slots) == 1 else 's'}:"
        ]
        for fid, fpath in sorted(tree_paths.items(), key=lambda kv: kv[1].lower()):
            row = next((f for f in chat_folders if str(f.get("id")) == fid), {})
            depth = fpath.count("/")
            meta_bits = []
            if row.get("project_dir"):
                meta_bits.append(f"project={row['project_dir']}")
            if row.get("default_agent"):
                meta_bits.append(f"agent={row['default_agent']}")
            # No archived count. The invariant this server holds is that nothing it
            # emits discloses a non-persistent session — and the folders endpoint's
            # ``history_count`` covers archived transcripts with no memory_mode to
            # filter on, so a folder holding one filed incognito conversation would
            # report it as a number. The live list is filtered in
            # ``_visible_chat_slots``; a count this server cannot prove clean is
            # simply not rendered.
            if row.get("hidden"):
                meta_bits.append("hidden")
            meta = f"  ({' · '.join(meta_bits)})" if meta_bits else ""
            tree_lines.append(f"{'  ' * depth}{fid}  {fpath}{meta}")
            for slot_row in by_folder.get(fid, []):
                tree_lines.append(_session_line(slot_row, "  " * depth + "  "))
        unfiled = by_folder.get("", [])
        if unfiled:
            tree_lines.append("(unfiled — top level)")
            for slot_row in unfiled:
                tree_lines.append(_session_line(slot_row, "  "))
        if not chat_folders and not chat_slots:
            return "No sidebar folders and no live sessions."
        return redact("\n".join(tree_lines))

    if name == "chat_folder_create":
        args = validate_tool_args(args, CHAT_FOLDER_CREATE_SCHEMA)
        gate = _refuse_tree_shaping_if_app_scoped("creating a folder")
        if gate:
            return gate
        # A '/' in a NAME is what makes a rendered path ambiguous (a folder named
        # "A/B" renders exactly like B inside A). The resolver refuses that
        # ambiguity; this tool must not manufacture more of it. The sidebar keeps
        # its freedom — a human can still name a folder anything.
        if "/" in str(args["name"]):
            return (
                "Error: a folder name cannot contain '/' — it would render "
                "identically to a nested path and become unaddressable by path. "
                "Create the parent and child separately, or use a different name."
            )
        chat_folders, folders_err = _get_rows("/api/chat/folders")
        if folders_err:
            return f"Error: {folders_err}"
        # mkdir -p over the parent path: resolve as far as the tree already
        # goes, then create each missing segment.
        parent_id, created_segments, parent_err = _ensure_chat_folder_path(
            str(args.get("parent") or ""), chat_folders
        )
        made_note = (
            f" (created parent path: {'/'.join(created_segments)})" if created_segments else ""
        )
        if parent_err:
            return redact(f"Error: {parent_err}{made_note}")
        # Agent-authored name landing in durable, re-rendered state — redact
        # before the write, like the created parent segments above.
        #
        # Then check the LENGTH of the redacted form, because that is what gets
        # stored: the schema caps the caller's `name` at _MAX_FOLDER_NAME, but
        # redaction can make a string LONGER (a credential becomes a placeholder),
        # so a name that passed validation can still overrun. The endpoint stores
        # ``name[:100]``, and a silently truncated name is one no later path can
        # match — the same mismatch that makes the walk refuse an overlong
        # segment, so it is refused the same way here.
        safe_name = redact(args["name"])
        if len(safe_name) > _MAX_FOLDER_NAME:
            return (
                f"Error: folder name too long after redaction ({len(safe_name)} "
                f"chars): `{safe_name[:40]}…` — keep it to {_MAX_FOLDER_NAME} "
                "characters or fewer"
            )
        body = {"name": safe_name, "parent_id": parent_id}
        d = _post("/api/chat/folders", body)
        if d.get("error"):
            return redact(f"Error: {d['error']}{made_note}")
        chat_folders.append(d)
        new_id = str(d.get("id") or "?")
        new_path = _chat_folder_paths(chat_folders).get(new_id) or str(d.get("name") or "?")
        return redact(f"Created folder `{new_path}` (id={new_id}).{made_note}")

    if name == "chat_folder_move":
        args = validate_tool_args(args, CHAT_FOLDER_MOVE_SCHEMA)
        gate = _refuse_tree_shaping_if_app_scoped("moving a folder")
        if gate:
            return gate
        chat_folders, folders_err = _get_rows("/api/chat/folders")
        if folders_err:
            return f"Error: {folders_err}"
        fld_id, fld_err = _resolve_chat_folder_id(args["folder"], chat_folders)
        if fld_err:
            return f"Error: {fld_err}"
        if not fld_id:
            return "Error: 'root' is not a folder — name the folder to move."
        dest_id, dest_err = _resolve_chat_folder_id(args.get("new_parent") or "", chat_folders)
        if dest_err:
            return f"Error: {dest_err}"
        d = _patch(f"/api/chat/folders/{fld_id}", {"parent_id": dest_id})
        if d.get("error"):
            # The endpoint owns the cycle guard (a folder cannot move into its
            # own descendant) — surface its verdict rather than re-deriving it.
            return f"Error: {d['error']}"
        moved: list[dict] = [f for f in chat_folders if str(f.get("id")) != fld_id]
        moved.append({**d, "id": fld_id})
        dest_path = _chat_folder_paths(moved).get(fld_id) or "(top level)"
        return redact(f"Moved folder (id={fld_id}) to `{dest_path}`.")

    if name == "chat_folder_move_session":
        args = validate_tool_args(args, CHAT_FOLDER_MOVE_SESSION_SCHEMA)
        chat_folders, folders_err = _get_rows("/api/chat/folders")
        if folders_err:
            return f"Error: {folders_err}"
        fld_id, fld_err = _resolve_chat_folder_id(args.get("folder") or "", chat_folders)
        if fld_err:
            return f"Error: {fld_err}"
        chat_slots, slots_err = _visible_chat_slots()
        if slots_err:
            return f"Error: {slots_err}"
        slot_key, slot_err = _resolve_chat_slot_key(args["session"], chat_slots)
        if slot_err:
            # The refusal echoes candidate slot keys, and a slot key can be a
            # folded human name — redact like every other egress here.
            return redact(f"Error: {slot_err}")
        # This is the one tool here that writes to a session OTHER than the
        # caller's, so it resolves identity STRICTLY: only the gateway-injected
        # per-call caller context, the injected env var, or an HMAC-verified pid
        # count. The lenient resolver's /proc ancestor walk would resolve a
        # subagent to its parent slot, handing it the parent's authority — and
        # an unresolved identity reaches the endpoint as no header at all, where
        # it reads as the unconfined dashboard user. Refuse instead of writing
        # with an authority we cannot name.
        caller_key = _resolve_session_key_strict()
        if not caller_key:
            return (
                "Error: cannot verify which session is calling, so this move is "
                "refused — filing another session requires a caller identity the "
                "gateway can vouch for."
            )
        # The verified key is passed through unchanged: re-resolving inside the
        # helper would let the write carry a different session's authority than
        # the one checked here.
        d = _patch(
            f"/api/chat/slots/{quote(slot_key, safe='')}/folder",
            {"folder_id": fld_id},
            session_key=caller_key,
        )
        if d.get("error"):
            return f"Error: {d['error']}"
        if not fld_id:
            return redact(f"Unfiled session `{slot_key}` to the top level.")
        folder_label = _chat_folder_paths(chat_folders).get(fld_id, fld_id)
        return redact(f"Moved session `{slot_key}` into `{folder_label}` (id={fld_id}).")
    return f"Error: unknown tool '{name}'"


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Guarded entry point — schema validation and SEL audit live in the wrapper."""
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key=_resolve_session_key() or SERVER_NAME,
        downstream_service=SERVER_NAME,
    )


def run_mcp_server() -> None:
    """Run the MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(SERVER_NAME, SERVER_VERSION, _list_tools, _call_tool)
