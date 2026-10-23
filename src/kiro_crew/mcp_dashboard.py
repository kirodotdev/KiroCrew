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
folder, reparent a folder, rename or restyle one (name, icon, color), file a
live session into one, and delete a folder that is EMPTY. The delete asks the endpoint for its empty-only mode
(``?if_empty=true``), which refuses a folder holding a subfolder or a live or
archived session instead of unfiling them, so nothing here can lose a
conversation. It also
controls session TAGS with the same posture: read the vocabulary, create or
update a tag (rename, recolor, status flag), and add or remove tags on a live
session — no tag delete, so nothing here can strip a label from every session
at once, and the assignment is a DELTA the endpoint applies compare-and-set
against the revision this server read, so an agent never clobbers a tag the
person clicked on meanwhile. It can pin or unpin a live session with the same
ownership rules as tagging one. Every tool is a thin proxy over the dashboard's
existing endpoints (loopback +
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
delete a conversation or a folder that holds anything, and the worst outcome is
a sidebar the user has to tidy. Contrast the keystone leaves in ``security.py``
(``computer_use.json``, ``browser-mode-enabled``, the Ops Mission Control mode):
each grants reach OUTSIDE Kiro Crew — desktop input synthesis, the operator's
logged-in browser, writes against production incident tooling — or is the
security floor itself, and each is therefore stored where the agent cannot write.

The rule that follows, and the reason the tool set is ratcheted in
``test_mcp_dashboard_registration.py``: a capability whose blast radius DOES
require authorization needs its own keystone leaf, and being merely unreferenced
in the default spec is not that. Session control (driving or stopping another
session) is that shape.

Identity posture: two resolvers, for two different jobs. The SEL invocation log
(written by ``TABLE.call``) uses the NON-strict resolver — that header is
ATTRIBUTION, and a lenient misattribution there is tolerable. Every decision
that SCOPES what a caller may see or verify-writes — the session-control gate,
``_visible_chat_slots``, ``_refuse_tree_shaping_if_unverifiable``, and
``chat_folder_move_session``'s own caller check (the one tool here that writes
to a session other than the caller's) — asks the frame's caller
(``ctx.caller``), which in production is :func:`mcp_core._resolve_session_key_strict`
through ``require_strict_session_key``, whose first source is the
gateway-injected per-call caller block — the only identity that
holds on a pooled backend serving many sessions (this server advertises
``kirocrew.caller-identity`` so gatewayd injects one; see
``ADVERTISE_CALLER_IDENTITY``). An unverifiable caller is refused by those
paths, never waved through on the lenient walk.

Layout: this module keeps the folder, tag and board rows, ``TABLE`` and the stdio entry
point. The server's helpers, every tool body and the ``_session_tools`` rows live in the
sibling ``mcp_dashboard_*`` owner modules, and this module binds every name they define,
so ``kiro_crew.mcp_dashboard.<name>`` reads, imports and patches each of them.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.mcp_shared import run_mcp_stdio_loop
from kiro_crew.mcp_tools.table import Tool, ToolContext, ToolTable
from kiro_crew.validation import MCP_DASHBOARD_SCHEMAS

logger = logging.getLogger(__name__)

SERVER_VERSION = "1.0.0"


def _folder_tools() -> tuple[Tool, ...]:
    """The sidebar folder tools: read the tree, shape it, file sessions in it."""
    return (
        Tool(
            name="chat_folder_tree",
            description=(
                "Show the user's SIDEBAR folder tree — the folders they organize "
                "their chat sessions in — with the live sessions filed in each one. "
                "Folders are listed in the SAME ORDER the sidebar draws them, in the "
                "person's folder sort mode (custom = their stored positions, name = "
                "natural alphabetical, created = newest first); the header line names "
                "the active mode. In custom mode the sequence you read here is the "
                "one the person sees, which is what makes it safe to pick a "
                "``before``/``after`` anchor for chat_folder_move. In name or created "
                "mode the listing says so and warns that an anchor sets the stored "
                "position without changing the displayed order. Returns per "
                "folder: id, human path, project directory, default "
                "agent, a hidden flag, and the steering directories it DECLARES "
                "(``steering=[…]``; a folder inherits its ancestors' too, "
                "root-first, so read up the tree for the full set); "
                "then one line per live session (slot key + title) nested under it, "
                "and an '(unfiled)' group for sessions at the top level. In each group "
                "the pinned sessions come first, in the order the sidebar draws them, "
                "which is the order chat_session_pin_move's anchors refer to. Use this to "
                "get folder ids/paths and session keys before calling "
                "chat_folder_create / chat_folder_move / chat_folder_move_session / "
                "chat_folder_delete, or when the user asks what their tree looks like. This is the "
                "folder-shaped view; list_sessions is the flat newest-first one."
            ),
            schema={"type": "object", "properties": {}},
            run=_run_chat_folder_tree,
            identity="attribution",
            routes=("GET /api/chat/folders", "GET /api/chat/slots"),
        ),
        Tool(
            name="chat_folder_create",
            description=(
                "Create a sidebar folder (or subfolder) for chat sessions. "
                "``parent`` accepts a folder id OR a '/'-separated human path from "
                "chat_folder_tree; missing path segments are created too (mkdir -p). "
                "Omit ``parent`` (or pass 'root') for a top-level folder. Creating a "
                "folder never moves anything — file sessions into it with "
                "chat_folder_move_session. An app agent may create at the top level "
                "inside a folder it created itself, or directly inside the folder "
                "its own session is filed in; the new folder belongs to it. Creating "
                "anywhere else in the person's folders is refused. A crew member "
                "follows the same rule. An existing same-name folder of yours is "
                "reused; one that is not yours is refused, never duplicated."
            ),
            schema={
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
            run=_run_chat_folder_create,
            identity="attribution",
            routes=("GET /api/chat/slots", "GET /api/chat/folders", "POST /api/chat/folders"),
        ),
        Tool(
            name="chat_folder_move",
            description=(
                "Reparent a sidebar folder AND/OR set its position among its "
                "siblings — nest it under another folder, move it back to the top "
                "level, or just slide it up or down where it already is. Moves the "
                "folder with everything in it (sessions and subfolders travel with "
                "it); nothing is deleted. Cycle-guarded: a folder cannot become its "
                "own descendant. ``folder`` and ``new_parent`` are each a folder id "
                "or human path; omit ``new_parent`` (or pass 'root') for the top "
                "level. For POSITION pass ``before`` or ``after`` (not both) naming "
                "a SIBLING folder to sit next to — with an anchor and no "
                "``new_parent`` the anchor chooses the parent, which is how you "
                "reorder a folder without moving it. chat_folder_tree lists folders "
                "in the same order the sidebar draws them, so read it first to pick "
                "the anchor. A position is a STORED position: the sidebar shows it "
                "in its custom folder order, and when the person has sorted folders "
                "by name or creation date (chat_folder_tree's header says which) an "
                "anchor changes nothing they see until they switch back. An app "
                "agent may move only a folder it created itself, "
                "and only to the top level or under another of its own; positioning "
                "is refused outright when it would renumber siblings the app does "
                "not own. A crew member is bound by the same own-folders-only rule."
            ),
            schema={
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder to move (id or path)."},
                    "new_parent": {
                        "type": "string",
                        "description": "Destination parent folder (id or path). Omit / 'root' for top level.",
                    },
                    "before": {
                        "type": "string",
                        "description": (
                            "Sit immediately BEFORE this sibling folder (id or path). "
                            "Mutually exclusive with 'after'."
                        ),
                    },
                    "after": {
                        "type": "string",
                        "description": (
                            "Sit immediately AFTER this sibling folder (id or path). "
                            "Mutually exclusive with 'before'."
                        ),
                    },
                },
                "required": ["folder"],
            },
            run=_run_chat_folder_move,
            identity="attribution",
            routes=(
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "PATCH /api/chat/folders/{folder}",
                "POST /api/chat/folders/reorder",
            ),
        ),
        Tool(
            name="chat_folder_update",
            description=(
                "Rename a sidebar folder and/or set its icon or color. ``folder`` "
                "is a folder id or '/'-separated human path from chat_folder_tree. "
                "Pass any of ``name`` (max 100 chars, no '/', like "
                "chat_folder_create), ``icon`` (exactly one emoji; '' restores the "
                "default glyph) or ``color`` (a folder palette value such as "
                "'#22c55e'; '' clears it). A name a sibling folder already has is "
                "refused, since the two could not be told apart by path. Metadata "
                "only: sessions and subfolders stay where they are. To reparent or "
                "reorder use chat_folder_move. This tool deliberately cannot set "
                "project_dir, default_agent or steering_dirs (they are unknown "
                "fields here): those fields change the working directory, agent, "
                "memory boundary or standing instructions of every future session "
                "filed in the folder, which is the person's call in the folder "
                "settings, not an agent's. To ask for a steering_dirs change, "
                "propose_change(kind='folder.steering') puts a card in front of "
                "the person. An app agent or crew member may update "
                "only a folder it created. Refused in a session linked to a channel "
                "(a resumed channel conversation, a channel mirror or a linked Slack "
                "thread)."
            ),
            schema={
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder to update (id or path)."},
                    "name": {
                        "type": "string",
                        "description": "New name (max 100 chars, cannot contain '/').",
                    },
                    "icon": {
                        "type": "string",
                        "description": "One emoji for the folder glyph; '' restores the default.",
                    },
                    "color": {
                        "type": "string",
                        "description": (
                            "Folder palette color ('#rrggbb' from the palette); '' clears it."
                        ),
                    },
                },
                "required": ["folder"],
            },
            run=_run_chat_folder_update,
            identity="attribution",
            routes=(
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "PATCH /api/chat/folders/{folder}",
            ),
        ),
        Tool(
            name="chat_folder_move_session",
            description=(
                "File a LIVE chat session into a sidebar folder, or unfile it to the "
                "top level (omit ``folder`` / pass 'root'). ``session`` is a slot key "
                "or 'dashboard:<slot>' session key from chat_folder_tree, or a "
                "session's exact title when that title is unique. ``folder`` is a "
                "folder id or human path — the folder must already exist "
                "(chat_folder_create makes one). Metadata only: the session keeps its "
                "transcript, model, and any running turn. ARCHIVED (history) sessions "
                "cannot be moved — bring one back with session_revive first, then call this. "
                "An app agent may file only its own sessions; a crew member may file "
                "only a session it owns or created."
            ),
            schema={
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
            run=_run_chat_folder_move_session,
            identity="attribution",
            routes=(
                "GET /api/chat/folders",
                "GET /api/chat/slots",
                "PATCH /api/chat/slots/{slot}/folder",
            ),
        ),
        Tool(
            name="chat_folder_delete",
            description=(
                "Delete an EMPTY sidebar folder that THIS session created, to clean "
                "up after your own work. ``folder`` is a folder id or human path "
                "from chat_folder_tree. Refused for a folder the person created, "
                "a same-name folder you reused, another session's folder, or one "
                "the person has since renamed, moved, restyled, hidden, filed a "
                "session into or nested a folder under: those are the person's. "
                "Also refused unless it holds no subfolders, no live sessions and "
                "no archived (history) sessions; the dashboard checks all of this "
                "in the same step as the removal, and never unfiles a session or "
                "lifts a subfolder. An app agent or a crew member is refused."
            ),
            schema={
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder to delete (id or path)."},
                },
                "required": ["folder"],
            },
            run=_run_chat_folder_delete,
            identity="attribution",
            routes=(
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "DELETE /api/chat/folders/{folder}",
            ),
        ),
        Tool(
            name="chat_folder_file_self",
            description=(
                "File THIS session — the one making the call — into a sidebar "
                "folder, or unfile it to the top level (omit ``folder`` / pass "
                "'root'). ``folder`` is a folder id or '/'-separated human path; "
                "missing path segments are created (mkdir -p), the same way "
                "session_create's ``folder`` resolves. Use it when you stand up a "
                "workstream that gets its own folder — a conductor files itself in "
                "the goal's folder first, then creates its workers with "
                '``folder="<goal>/<agent>"``, so the person finds the '
                "conductor and every worker under one heading instead of the "
                "conductor floating at the top level. Writes ONLY the caller's own "
                "placement: to file a different session use "
                "chat_folder_move_session. Metadata only — transcript, model and "
                "any running turn are untouched; already filed there is a no-op."
            ),
            schema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": (
                            "Destination folder id or human path (created if missing). "
                            "Omit / 'root' to unfile."
                        ),
                    },
                },
            },
            run=_run_chat_folder_file_self,
            identity="attribution",
            routes=(
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "POST /api/chat/folders",
                "PATCH /api/chat/slots/{slot}/folder",
            ),
        ),
    )


def _tag_tools() -> tuple[Tool, ...]:
    """Session labels: the tag vocabulary, tagging a session, and pinning one."""
    return (
        Tool(
            name="chat_tag_list",
            description=(
                "List the sidebar's tag vocabulary: every tag's id, name, color and "
                "whether it is a STATUS tag (a status tag is what a Trello-style "
                "column filters on, so a session normally carries one at a time). "
                "Read-only. Call it before chat_tag_assign to see which tags exist, "
                "and chat_tag_create when the one you need does not."
            ),
            schema={"type": "object", "properties": {}},
            run=_run_chat_tag_list,
            identity="attribution",
            routes=("GET /api/chat/tags",),
        ),
        Tool(
            name="chat_tag_create",
            description=(
                "Create a NEW tag in the sidebar's shared vocabulary. ``name`` is "
                "matched case-insensitively against existing tags: an existing name "
                "is returned rather than duplicated, so calling this for a tag that "
                "already exists is a safe no-op. ``color`` is an optional '#rrggbb'; "
                "``status`` marks it a status tag (one a Trello-style column can "
                "filter on). Create only — rename, recolor or reflag with chat_tag_update; "
                "this server can never delete a tag, so nothing here can lose a label "
                "the person put on a session. An app agent and a crew member cannot "
                "write the shared vocabulary at all (a coined tag has no owner in the "
                "person's list); they read it with chat_tag_list and assign existing "
                "tags with chat_tag_assign."
            ),
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tag name (max 60 chars)."},
                    "color": {
                        "type": "string",
                        "description": "Optional '#rrggbb' color; the dashboard default when omitted.",
                    },
                    "status": {
                        "type": "boolean",
                        "description": "Mark as a status tag (default false).",
                    },
                },
                "required": ["name"],
            },
            run=_run_chat_tag_create,
            identity="attribution",
            routes=("GET /api/chat/slots", "POST /api/chat/tags"),
        ),
        Tool(
            name="chat_tag_update",
            description=(
                "Rename, recolor, or toggle the STATUS flag of an existing tag in the "
                "sidebar's shared vocabulary. ``tag`` is a tag id or exact name (see "
                "chat_tag_list); pass any of ``name``, ``color`` ('#rrggbb') or "
                "``status``. Metadata only: every session carrying the tag keeps it, "
                "and columns filtering on it keep filtering on it — a rename changes "
                "the label people see, nothing else. This server can create and "
                "update tags but never delete one, so nothing here can lose a label "
                "the person put on a session. An app agent and a crew member cannot "
                "write the shared vocabulary; they assign existing tags with "
                "chat_tag_assign instead."
            ),
            schema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Tag id or exact tag name."},
                    "name": {"type": "string", "description": "New name (max 60 chars)."},
                    "color": {"type": "string", "description": "New '#rrggbb' color."},
                    "status": {
                        "type": "boolean",
                        "description": "Whether it is a status tag (one a column can filter on).",
                    },
                },
                "required": ["tag"],
            },
            run=_run_chat_tag_update,
            identity="attribution",
            routes=("GET /api/chat/slots", "GET /api/chat/tags", "PATCH /api/chat/tags/{tag}"),
        ),
        Tool(
            name="chat_tag_assign",
            description=(
                "Add and/or remove tags on a LIVE chat session. ``session`` is a slot "
                "key or 'dashboard:<slot>' session key from chat_folder_tree, or a "
                "session's exact title when that title is unique. ``add`` and "
                "``remove`` each take tag ids or exact tag names (see chat_tag_list); "
                "at least one must be non-empty, and a tag must already exist "
                "(chat_tag_create makes one). This is a DELTA on the session's current "
                "tags — tags you do not name are kept — and it is applied "
                "compare-and-set against the tag list this call read, so if the "
                "person changes the session's tags at the same moment the call fails "
                "with the current list instead of overwriting their click; re-read and "
                "retry. Metadata only: the transcript, model and any running turn are "
                "untouched. ARCHIVED (history) sessions cannot be tagged — bring one back "
                "with session_revive first. An app agent may tag only its own sessions; "
                "a crew member may tag only a session it owns or created. The agent "
                "tag-grants policy applies the same way it does to the chat_tag "
                "set_state directive: a tag the person RESERVED (a protected grant row "
                "with policy 'none', or 'add-only' when you try to remove it) is refused "
                "``tag_policy_denied``; a workflow-STATUS tag whose protected identity is "
                "missing is refused ``status_identity_unprotected`` on add as well as "
                "strip, and adding one that would leave the session with two states is "
                "refused ``status_tag_requires_set_state`` (use chat_tag set_state for "
                "workflow states); "
                "and when the grants store is unavailable the refusal is "
                "``tag_grants_unavailable``. An ordinary label with no protected row is "
                "yours to apply. A tag chat_tag_create makes stays rowless until the "
                "owner adopts it, so a freshly-created workflow-STATUS tag may not be "
                "assignable until then."
            ),
            schema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Slot key, 'dashboard:<slot>' session key, or exact unique session title.",
                    },
                    "add": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tag ids or exact names to add.",
                    },
                    "remove": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tag ids or exact names to remove.",
                    },
                },
                "required": ["session"],
            },
            run=_run_chat_tag_assign,
            identity="attribution",
            routes=("GET /api/chat/tags", "GET /api/chat/slots", "PUT /api/chat/slots/{slot}/tags"),
        ),
        Tool(
            name="chat_session_pin",
            description=(
                "Pin or unpin a LIVE chat session in the sidebar. ``session`` is a slot "
                "key or 'dashboard:<slot>' session key from chat_folder_tree, or a "
                "session's exact title when that title is unique. ``pinned`` is true to "
                "pin, false to unpin; asking for the state the session already has "
                "writes nothing and reports it. chat_folder_tree marks pinned sessions "
                "``[pinned]``. Metadata only: the transcript, model and any running "
                "turn are untouched. ARCHIVED (history) sessions cannot be pinned — "
                "bring one back with session_revive first. An app agent may pin only its own "
                "sessions; a crew member may pin only a session it owns or created."
            ),
            schema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Slot key, 'dashboard:<slot>' session key, or exact unique session title.",
                    },
                    "pinned": {
                        "type": "boolean",
                        "description": "true to pin, false to unpin.",
                    },
                },
                "required": ["session", "pinned"],
            },
            run=_run_chat_session_pin,
            identity="attribution",
            routes=("GET /api/chat/slots", "PATCH /api/chat/slots/{slot}/pin"),
        ),
        Tool(
            name="chat_session_pin_move",
            description=(
                "Reorder a PINNED session among the pinned sessions of its sidebar "
                "group (the folder it is filed in, or the top level). Pass "
                "``before`` or ``after`` (exactly one) naming another pinned session "
                "in the same group to sit next to. ``session`` and the anchor are "
                "each a slot key, a 'dashboard:<slot>' session key, or an exact "
                "unique title. chat_folder_tree lists each group's pinned sessions "
                "first, in the order the sidebar draws them, so read it first to "
                "pick the anchor. Pinned sessions the person never reordered follow "
                "in most-recent-activity order, the sidebar's default sort. This "
                "call stores a position for EVERY pinned session, so ones that had "
                "none keep that most-recent-activity order from then on, even for a "
                "person whose sidebar sorts some other way (that sort choice lives "
                "only in their browser). "
                "It only reorders: it does not pin, unpin or move a session between "
                "folders. The pinned order is the person's own sidebar preference, "
                "so an app agent or a crew member cannot change it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "The pinned session to move (slot key, 'dashboard:<slot>' key, or exact unique title).",
                    },
                    "before": {
                        "type": "string",
                        "description": (
                            "Sit immediately BEFORE this pinned session in the same "
                            "group. Mutually exclusive with 'after'."
                        ),
                    },
                    "after": {
                        "type": "string",
                        "description": (
                            "Sit immediately AFTER this pinned session in the same "
                            "group. Mutually exclusive with 'before'."
                        ),
                    },
                },
                "required": ["session"],
            },
            run=_run_chat_session_pin_move,
            identity="attribution",
            routes=("GET /api/chat/slots", "GET /api/chat/folders", "POST /api/chat/pinned-order"),
        ),
    )


def _board_tools() -> tuple[Tool, ...]:
    """The sidebar board: its columns, in the person's own layout."""
    return (
        Tool(
            name="chat_tag_column_list",
            description=(
                "List the sidebar board's columns in board order: each column's id, "
                "name, and what it shows (the tags it filters on, or the live-state "
                "lane it follows). Read-only. Call it before chat_tag_column_move to "
                "see the order, and before chat_tag_column_create to see whether a "
                "column for a tag already exists."
            ),
            schema={"type": "object", "properties": {}},
            run=_run_chat_tag_column_list,
            identity="attribution",
            routes=("GET /api/chat/tag-columns", "GET /api/chat/tags"),
        ),
        Tool(
            name="chat_tag_column_create",
            description=(
                "Append a column to the sidebar board that shows the sessions carrying "
                "``tag`` (a tag id or exact name; see chat_tag_list — the tag must "
                "already exist, chat_tag_create makes one). ``name`` is the column "
                "heading (max 60 chars). A column with the same name that already "
                "filters on exactly that tag is returned instead of duplicated, so "
                "calling this again is a safe no-op. Create only: this server can "
                "never delete a column or change what one filters on, because the "
                "board is the person's own layout. Place the new column with "
                "chat_tag_column_move. An app agent and a crew member cannot write "
                "the board; they read it with chat_tag_column_list."
            ),
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Column heading (max 60 chars)."},
                    "tag": {"type": "string", "description": "Tag id or exact tag name."},
                },
                "required": ["name", "tag"],
            },
            run=_run_chat_tag_column_create,
            identity="attribution",
            routes=(
                "GET /api/chat/slots",
                "GET /api/chat/tags",
                "GET /api/chat/tag-columns",
                "POST /api/chat/tag-columns",
            ),
        ),
        Tool(
            name="chat_tag_column_move",
            description=(
                "Move one sidebar board column so it sits directly before or after "
                "another. ``column`` is the column to move and exactly one of "
                "``before`` / ``after`` names the column to place it next to; each is "
                "a column id or exact name (see chat_tag_column_list). Every other "
                "column keeps its relative order, and nothing a column filters on "
                "changes. An app agent and a crew member cannot write the board."
            ),
            schema={
                "type": "object",
                "properties": {
                    "column": {"type": "string", "description": "Column id or exact name to move."},
                    "before": {
                        "type": "string",
                        "description": "Column id or exact name to place it before.",
                    },
                    "after": {
                        "type": "string",
                        "description": "Column id or exact name to place it after.",
                    },
                },
                "required": ["column"],
            },
            run=_run_chat_tag_column_move,
            identity="attribution",
            routes=(
                "GET /api/chat/slots",
                "GET /api/chat/tag-columns",
                "PUT /api/chat/tag-columns/order",
            ),
        ),
        Tool(
            name="session_move_to_column",
            description=(
                "Move a LIVE session's card into a sidebar board column, the same as "
                "dragging it there. ``session`` is a slot key or 'dashboard:<slot>' "
                "session key from chat_folder_tree, or a session's exact title when "
                "that title is unique. ``column`` is a column id or a column name, "
                "matched case-insensitively (see chat_tag_column_list); a name two "
                "columns share is refused. The session takes the one status tag the "
                "column filters on and loses its other status tags; plain labels "
                "stay. A column with no single status tag, and a live-state lane, "
                "cannot take a card, and the result says so. The rules of chat_tag "
                "set_state apply: the column's status must be one an agent may both "
                "add and remove, so a status the person reserved or made add-only "
                "is refused (tag_policy_denied). An "
                "app agent may move only its own sessions; a crew member only a "
                "session it owns or created."
            ),
            schema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Slot key, 'dashboard:<slot>' session key, or exact unique session title.",
                    },
                    "column": {
                        "type": "string",
                        "description": "Column id, or column name (case-insensitive).",
                    },
                },
                "required": ["session", "column"],
            },
            run=_run_session_move_to_column,
            # A ``session_*`` row: the table's strict gate verifies the caller
            # before the body runs and hands it over as ``ctx.caller_key``,
            # and ``SESSION_CONTROL_TOOLS`` (hence the channel containment
            # check) picks it up.
            identity="strict",
            routes=(
                "GET /api/chat/tag-columns",
                "GET /api/chat/tags",
                "GET /api/chat/slots",
                "POST /api/chat/slots/{slot}/drop",
            ),
        ),
    )


# The owners hold the server's helpers, every tool body and the session-control rows.
# Every name they define is bound here too, so the rows above and ``TABLE`` below read
# them as globals of this module, and ``kiro_crew.mcp_dashboard.<name>`` imports and
# patches each one: :mod:`kiro_crew._facade_mirror` carries a patch through this module
# to every owner that binds the name, and serves the imports only the owners read.
import importlib as _importlib  # noqa: E402
import sys as _sys  # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402

from kiro_crew import _facade_mirror  # noqa: E402
from kiro_crew.mcp_dashboard_caller_scope import (  # noqa: E402, F401
    _DELEGATED_CALLER_PREFIXES,
    SERVER_NAME,
    _caller_app_scope,
    _is_messaging_caller,
    _own_chat_slot,
    _refuse_channel_board_write,
    _refuse_tree_shaping_if_unverifiable,
    _visible_chat_slots,
)
from kiro_crew.mcp_dashboard_folder_runs import (  # noqa: E402, F401
    _run_chat_folder_create,
    _run_chat_folder_delete,
    _run_chat_folder_file_self,
    _run_chat_folder_move,
    _run_chat_folder_move_session,
    _run_chat_folder_tree,
    _run_chat_folder_update,
)
from kiro_crew.mcp_dashboard_folder_tree import (  # noqa: E402, F401
    _ASCII_FOLD,
    _CHAT_FOLDER_ORDER_LIMIT,
    _DIGIT_RUNS,
    _NEG_INF,
    _POS_INF,
    _SIDEBAR_MAX_DRAWN_DEPTH,
    _chat_folder_children,
    _chat_folder_created,
    _chat_folder_name_key,
    _chat_folder_natural_key,
    _chat_folder_order,
    _chat_folder_paths,
    _chat_folder_render_order,
    _chat_folder_siblings,
    _chat_folder_sort_key,
    _chat_folder_sort_mode,
    _free_slot_order,
    _pinned_sidebar_order,
    _read_folder_sort_setting,
    _sidebar_group_order,
    _slot_group,
)
from kiro_crew.mcp_dashboard_references import (  # noqa: E402, F401
    _CHAT_FOLDER_ID_RE,
    _MAX_FOLDER_NAME,
    _ambiguous_segment_error,
    _ensure_chat_folder_path,
    _get_rows,
    _resolve_chat_folder_id,
    _resolve_chat_folder_ref,
    _resolve_chat_slot_key,
    _resolve_chat_tag_column,
    _resolve_chat_tag_ids,
    _walk_chat_folder_segments,
)
from kiro_crew.mcp_dashboard_session_runs import (  # noqa: E402, F401
    _PROFILE_COPY_NOTE,
    GENERATE_TITLE_TIMEOUT_SECS,
    SESSION_STATUS_READ_TIMEOUT_SECS,
    _closed_at_display,
    _create_refusal_remedy,
    _pending_approval_phrase,
    _render_session_summary,
    _resolve_folder_for_new_session,
    _run_session_adopt,
    _run_session_broadcast,
    _run_session_close,
    _run_session_continue,
    _run_session_create,
    _run_session_end_wait,
    _run_session_fork,
    _run_session_generate_title,
    _run_session_handoff,
    _run_session_history_list,
    _run_session_read_message,
    _run_session_release,
    _run_session_reload,
    _run_session_rename,
    _run_session_retry,
    _run_session_revive,
    _run_session_send,
    _run_session_set_model,
    _run_session_status,
    _run_session_stop,
    _run_session_summary,
    _summary_time,
    _worker_capabilities_line,
)
from kiro_crew.mcp_dashboard_session_tools import (  # noqa: E402, F401
    _session_tools,
)
from kiro_crew.mcp_dashboard_tag_runs import (  # noqa: E402, F401
    _DROP_NOT_MOVED,
    _MAX_TAG_NAME,
    _chat_tag_column_label,
    _render_chat_tag_columns,
    _render_chat_tags,
    _run_chat_session_pin,
    _run_chat_session_pin_move,
    _run_chat_tag_assign,
    _run_chat_tag_column_create,
    _run_chat_tag_column_list,
    _run_chat_tag_column_move,
    _run_chat_tag_create,
    _run_chat_tag_list,
    _run_chat_tag_update,
    _run_session_move_to_column,
)


def _session_control_gate(ctx: ToolContext) -> tuple[str, str]:
    """The strict gate the table applies before every session-control row.

    Authorization for these tools is the CALLER'S IDENTITY: the route decides
    what a session may reach from the key sent here. The lenient resolver walks
    /proc ancestors, and a spawned subagent lives under its parent slot's process
    tree — so the walk would hand a subagent its parent's identity and let it
    read, message, or stop the parent's sibling sessions. Only the signed sources
    count.

    The verified key is KEPT: the table hands it to the row as
    ``ctx.caller_key``, and every request the row makes on the caller's
    authority carries it. Gating on the strict resolver and then letting the
    transport resolve again would authorize the check and the action as
    potentially different sessions: the lenient walk reads mutable process
    state, so what it answers at request time need not be what the gate approved.
    """
    return ctx.caller.require_strict_session_key(
        "Error: this session cannot be identified well enough to control another "
        "session. Session control authorizes on the calling session's identity, and "
        "only a gateway-issued key counts — a spawned subagent has none of its own."
    )


def _tool_definitions() -> list[dict[str, Any]]:
    """The tool surface this server advertises, untitled; ``_list_tools`` titles it."""
    return [tool.descriptor() for tool in TABLE]


def _list_tools() -> list[dict[str, Any]]:
    """The tool surface, unconditionally.

    Reaching this process at all means an agent spec referenced this server, so
    the assignment already happened; there is nothing left to gate here.
    """
    return TABLE.list()


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    return TABLE.validate(name, args)


#: Every tool this server has, one row each, in the order ``tools/list`` advertises
#: them. A new dashboard tool is one more row in the family it belongs to: its
#: descriptor, its identity, the routes it reaches, and its ``_run_*`` body.
TABLE = ToolTable(
    SERVER_NAME,
    (*_folder_tools(), *_tag_tools(), *_board_tools(), *_session_tools()),
    validators=MCP_DASHBOARD_SCHEMAS,
    strict_gate=_session_control_gate,
)

#: The session-control half of this server's tool set: the rows the table gates
#: on a strictly verified caller before they run. Read off the table rather than
#: spelled out, because three things must agree on it — the gate, the
#: channel-agent containment list (``CHANNEL_AGENT_BLOCKED_TOOLS``), and the
#: pinned advertised set in the registration tests — and spelling it out per site
#: is how ``session_create`` came to be gated for identity but reachable from a
#: channel agent.
SESSION_CONTROL_TOOLS: tuple[str, ...] = TABLE.names("strict")


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """One tools/call frame: the table validates, audits, gates, then runs the row."""
    return TABLE.call(name, raw_args, ToolContext.for_frame(SERVER_NAME))


#: Whether this server advertises ``kirocrew.caller-identity`` — i.e. whether it
#: consumes the per-call caller block gatewayd injects instead of reading identity
#: from its own process. True here because it does: every scoping decision
#: (``_visible_chat_slots``, ``_refuse_tree_shaping_if_unverifiable``) resolves
#: the caller through :func:`mcp_core._resolve_session_key_strict`, whose first
#: source is that block.
#:
#: Advertising is not cosmetic. ``mcp_gateway/backend_stubs.py`` strips any client-forged
#: caller block from EVERY forwarded request and re-injects its own only when the
#: backend advertised this capability — so without the advertisement the block
#: never arrives, and this server's resolver reads an empty identity no matter how
#: correctly it is written. Nothing declines to POOL an unadvertised backend
#: (``rewriter.UNPOOLABLE_SERVERS`` is empty and documents that the capability is
#: read only to decide injection), so the unadvertised state was not "per-session
#: spawn" — it was pooled AND identity-blind. For this server that fail-closed
#: every scoped tool on a pooled backend: an unverifiable caller is refused, so
#: the tools stopped working for exactly the sessions pooling was built to serve.
#:
#: A module-level constant rather than a bare argument below so the value is
#: readable without executing :func:`run_mcp_server`, and so
#: ``test/test_mcp_managed_caller_identity.py`` can assert it against the argument
#: actually handed to the shim.
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_server() -> None:
    """Run the MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(
        SERVER_NAME,
        SERVER_VERSION,
        _list_tools,
        _call_tool,
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
    )


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.mcp_dashboard_folder_tree",
    "kiro_crew.mcp_dashboard_references",
    "kiro_crew.mcp_dashboard_caller_scope",
    "kiro_crew.mcp_dashboard_session_runs",
    "kiro_crew.mcp_dashboard_session_tools",
    "kiro_crew.mcp_dashboard_folder_runs",
    "kiro_crew.mcp_dashboard_tag_runs",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: the public names of the one module, served ones included.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # the project names this module imported that only its owners read
    from kiro_crew.conductor_patrol import reply_line  # noqa: F401
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401
    from kiro_crew.config.sections import (  # noqa: F401
        AUTOCOMPACT_PCT_MAX,
        AUTOCOMPACT_PCT_MIN,
        FOLDER_SORT_DEFAULT,
        FOLDER_SORT_MODES,
    )
    from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES  # noqa: F401
    from kiro_crew.dashboard.folder_ownership import (  # noqa: F401
        _folder_owner_app,
        _subtree_holds_foreign_folder,
    )
    from kiro_crew.mcp_tools.dashboard_client import DashboardClient, DashboardError  # noqa: F401
    from kiro_crew.platform import redact_via_context as redact  # noqa: F401
    from kiro_crew.sel import sel  # noqa: F401
    from kiro_crew.validation import (  # noqa: F401
        BROADCAST_RESPONSE_MARGIN_SECS,
        BROADCAST_TARGET_ALLOWANCE_SECS,
        MAX_BROADCAST_TARGETS,
        SLACK_THREAD_TS_RE,
    )
