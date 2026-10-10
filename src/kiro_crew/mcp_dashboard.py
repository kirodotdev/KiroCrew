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
"""

from __future__ import annotations

import logging
import re as _re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import FOLDER_SORT_DEFAULT, FOLDER_SORT_MODES
from kiro_crew.dashboard.chat_folders import (
    _folder_owner_app,
    _subtree_holds_foreign_folder,
)
from kiro_crew.mcp_shared import run_mcp_stdio_loop

# The authenticated loopback client to the gateway is ``mcp_core``'s request
# helpers, reached through the ``DashboardClient`` port. Importing them costs
# 341ms/40MB in this process (measured) — under ``mcp_computer``'s own import
# cost, because mcp_core's heavy dependencies are function-local.
from kiro_crew.mcp_tools.dashboard_client import DashboardClient, DashboardError
from kiro_crew.mcp_tools.table import Tool, ToolContext, ToolTable
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.sel import sel
from kiro_crew.validation import (
    BROADCAST_RESPONSE_MARGIN_SECS,
    BROADCAST_TARGET_ALLOWANCE_SECS,
    MAX_BROADCAST_TARGETS,
    MCP_DASHBOARD_SCHEMAS,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-dashboard"
SERVER_VERSION = "1.0.0"

# The folder endpoints store ``name[:100]``. Mirroring the number here is what
# lets this server refuse an overlong name instead of writing one it cannot
# address afterwards; a mismatch shows up as the duplicate-creation the
# too-long-segment test pins.
_MAX_FOLDER_NAME = 100

# Same mirror for tags: the tag endpoints store ``name[:60]``
# (``chat_tags._NAME_MAX``), and a silently truncated name is one no later
# ``chat_tag_assign`` name lookup can match.
_MAX_TAG_NAME = 60


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
                "agent, and a hidden flag; "
                "then one line per live session (slot key + title) nested under it, "
                "and an '(unfiled)' group for sessions at the top level. Use this to "
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
                "settings, not an agent's. An app agent or crew member may update "
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
    )


def _session_tools() -> tuple[Tool, ...]:
    """Session control: open, fork, steer, stop and read other sessions."""
    return (
        Tool(
            name="session_create",
            description=(
                "Open a NEW chat session, pre-named and bound to the agent you pick, so a "
                "separate workstream has a home of its own. The new session appears in "
                "the user's sidebar like any other — they can read it, take it over and "
                "close it — so use it to stand up a workstream alongside this one (watch a "
                "build, grind a long refactor) rather than to hide work. It starts empty: "
                "the person is the one who types the first message into it. Returns its "
                "key; pass that as `target` to the other session tools. To open a session "
                "that already CARRIES a transcript (splitting your own investigation into "
                "several sessions), use session_fork instead."
            ),
            schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "Short name for the session, shown in the sidebar. Say what the "
                            "session is FOR so the user can tell your sessions apart."
                        ),
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "Agent to bind the session to. Omitting it inherits the "
                            "CALLER'S OWN agent, not a global default: create_session "
                            "falls back to the calling slot's agent so the child stays in "
                            "this workspace's memory boundary. A conductor that omits it "
                            "therefore gets a second conductor, which has no fs_write and "
                            "cannot do the work. Name the agent the child needs "
                            "explicitly \u2014 kirocrew-worker for a leaf work item."
                        ),
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Sidebar folder to file the new session into, atomically with "
                            "creation — a folder id or a '/'-separated human path. Missing "
                            "path segments are created (mkdir -p), like chat_folder_create's "
                            "`parent`. Omit to leave the session at the top level."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Model the new session starts on, pinned exactly as if the "
                            "person had picked it in the session's model dropdown: an id "
                            "from that same list, or 'auto'. Omit to use the agent's or "
                            "the global default. A well-formed id the account cannot serve "
                            "is accepted here but withheld at the session's first turn: "
                            "the turn succeeds on the account's default model, and the only "
                            "signal is a notice in that session's transcript. The person "
                            "can still change it later."
                        ),
                    },
                },
                "required": [],
            },
            run=_run_session_create,
            identity="strict",
            routes=(
                "POST /api/session-control/create",
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "POST /api/chat/folders",
            ),
        ),
        Tool(
            name="session_fork",
            description=(
                "Open a NEW chat session that CARRIES the transcript of an existing one — "
                "the same thing as the dashboard's Fork button. The child holds a copy of "
                "the source's messages up to and including the fork point (the whole "
                "transcript by default), so it starts with the context already built "
                "instead of empty; use it to split a long investigation into several "
                "sessions that each know what was found so far. By default the source is "
                "YOUR OWN session. The child inherits the source's agent, model, memory "
                "store and mode, project and folder exactly as a human fork does — there "
                "is deliberately no agent, model or mode override here (a fork must stay "
                "inside the memory boundary its transcript came from); to open a session "
                "bound to a different agent use session_create. It starts IDLE: the copied "
                "messages are history, and no turn runs until you session_send into it or "
                "the person types. It appears in the user's sidebar like any other session, "
                "for them to read, take over and close. Returns its key; pass that as "
                "`target` to the other session tools."
            ),
            schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "The session to copy from: a session key from list_sessions, a "
                            "slot key, or its exact unique title. Omit to fork YOUR OWN "
                            "session. Forking another session requires the same "
                            "authorization as reading it with session_read_message."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Short name for the new session, shown in the sidebar. Omit to "
                            "start from `Fork of <source title>`, which is then renamed "
                            "automatically after the latest question the copy holds."
                        ),
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Sidebar folder to file the new session into — a folder id or a "
                            "'/'-separated human path, created if missing (mkdir -p), the same "
                            "as session_create's `folder`. Omit to leave it in the source's "
                            "folder."
                        ),
                    },
                    "at_message_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Fork point: the position of the LAST message to carry, counting "
                            "the source's user and assistant messages from 0 (system and "
                            "tool rows are not counted). Messages after it are left behind. "
                            "Omit to carry the whole transcript."
                        ),
                    },
                },
                "required": [],
            },
            run=_run_session_fork,
            identity="strict",
            routes=(
                "POST /api/session-control/fork",
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "POST /api/chat/folders",
            ),
        ),
        Tool(
            name="session_stop",
            description=(
                "Stop another session's in-flight turn — the same thing as pressing Stop "
                "in that tab. Use it when a peer session is working on something you now "
                "know is wrong or already done, and letting it finish would waste the run "
                "or make a conflicting change. The stop is cooperative and safe to "
                "re-send: a repeat within two minutes reports that the earlier stop is "
                "still in progress instead of escalating to a hard kill, so retrying a "
                "call that timed out cannot discard the target's queued messages. "
                "A stop card appears in the "
                "target's transcript so the person reading it sees what happened. Stopping "
                "discards the turn's work, so read the session first when you are not sure "
                "what it is doing."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_stop,
            identity="strict",
            routes=("POST /api/session-control/stop",),
        ),
        Tool(
            name="session_end_wait",
            description=(
                "Wake a session you created that is sleeping in the `wait` tool, "
                "without cancelling its turn: the same thing as pressing End wait on "
                "its countdown. The target's wait returns a normal result that names "
                "your session as the one that ended it, and its turn carries on. Use "
                "it when the thing a worker is waiting for has already happened. "
                "Only sessions you created are reachable. "
                "Unlike session_stop nothing is discarded, and unlike session_send "
                "no text reaches the target. A target that is not waiting gets an "
                "informational reply, not an error, so there is nothing to retry. "
                "The wake lands on the target's next keepalive ping, within about "
                "five seconds."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_end_wait,
            identity="strict",
            routes=("POST /api/session-control/end-wait",),
        ),
        Tool(
            name="session_retry",
            description=(
                "Re-run another session's last turn when it FAILED: it ended in an "
                "error (for example 'Request initialize timed out' at start) or with no "
                "reply. Does the same thing as pressing Resume in that tab: the session "
                "picks up its most recent request, and no second copy of the prompt is "
                "added. Use this instead of re-sending the prompt with session_send. "
                "Refused while the target is running, busy or has queued messages, and "
                "refused when its last turn finished normally or was stopped, so it "
                "cannot regenerate a good answer. After two failed starts in a row it is "
                "refused with session_start_repeat: the host needs attention, not a "
                "third retry."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_retry,
            identity="strict",
            routes=("POST /api/session-control/retry",),
        ),
        Tool(
            name="session_set_model",
            description=(
                "Change the model and/or reasoning effort another session runs on; "
                "pass either or both. Only an IDLE session takes the change: if the "
                "target has a turn or sub-agents in flight the call fails with "
                "'session busy, model not changed' and nothing changes. To force it, "
                "stop the target with session_stop first, then retry. The change is "
                "applied when the target's next turn starts, after the same permission "
                "check runs again; if the target has become channel-linked or otherwise "
                "out of reach by then, the change is dropped. A model or effort the "
                "user picks in the meantime wins. The conversation is kept. 'auto', "
                "'Auto (Jev)' and sessions bound to a remote crew are refused."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Model to switch to: a canonical key or provider id, e.g. "
                            "'sonnet' or 'opus'. 'auto' is owner-only. Omit it to keep "
                            "the target's model and change only reasoning_effort."
                        ),
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": (
                            "Reasoning effort for the target, one of the five standard "
                            "levels: 'low', 'medium', 'high', 'xhigh', 'max'. Omit it to "
                            "keep the target's current level. Other levels, and going "
                            "back to the model's default, are set from the dropdown."
                        ),
                    },
                },
                "required": ["target"],
            },
            run=_run_session_set_model,
            identity="strict",
            routes=("POST /api/session-control/set-model",),
        ),
        Tool(
            name="session_reload",
            description=(
                "Relaunch the agent process of a session you created, the same thing as "
                "Reload session in that tab's menu. Use it after a change that a running "
                "session cannot see: a newly granted or enabled MCP server, an MCP "
                "config edit, or an agent-spec fix. The new process re-reads its agent "
                "spec, environment and MCP servers; the conversation is kept, and the "
                "target's transcript shows a notice naming your session. Only an IDLE "
                "session can be reloaded: a running or starting turn, queued messages "
                "or attached sub-agents fail with 'session busy, not reloaded' and "
                "nothing changes. You cannot reload yourself, and the agent, model and "
                "workspace stay as they are."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_reload,
            identity="strict",
            routes=("POST /api/session-control/reload",),
        ),
        Tool(
            name="session_close",
            description=(
                "Close another session — the same thing as pressing the ✕ on that tab. "
                "The conversation is archived to history (session_revive brings it back); "
                "this is NOT a permanent delete, but it does dismiss the live tab and, "
                "if the target is mid-turn, cancels that turn first and discards its "
                "work. Use it to tidy up a peer session you created and are done with "
                "(a finished watcher, a workstream you handed off), not to interrupt one "
                "you might still need — for that, session_stop only cancels the turn and "
                "leaves the tab open. Read the session first when you are unsure what it "
                "is doing. You may also pass your OWN session key to close yourself once "
                "your one job is finished; that cancels this turn, so make it your last "
                "call and write anything the person should read before it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_close,
            identity="strict",
            routes=("POST /api/session-control/close",),
        ),
        Tool(
            name="session_revive",
            description=(
                "Bring an ARCHIVED (history) session back into the live sidebar — the "
                "mirror of session_close. The conversation reopens as a live tab with "
                "its full transcript, the same thing as clicking it in the History tab; "
                "nothing runs until someone sends it a message. Use it when a closed "
                "session is the right home for new work (an investigation to continue, "
                "a session to tag or file), then address it with the returned key: "
                "session_send, session_read_message, chat_folder_move_session and "
                "chat_tag_assign all work on it afterwards. A session that is already "
                "open is refused with its live key — just use that. Only dashboard "
                "sessions in the caller's own workspace are addressable; a crew member "
                "or agent-created session may revive only a session it created itself, verified against "
                "the gateway's crew-log lineage (on by default; refused ownership_unverified "
                "when the log is off, unseeded, or predates the session)."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "The archived session: its slot key (``chat-7-...``), its "
                            "'dashboard:<slot>' session key, the transcript name "
                            "list_sessions reports, or its exact title when that title "
                            "is unique among archived sessions."
                        ),
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Sidebar folder to file the revived session into once it is live "
                            "— a folder id or a '/'-separated human path; missing path "
                            "segments are created (mkdir -p), like session_create's `folder`. "
                            "Filing is best-effort AFTER the revive has landed: the result's "
                            "`filed` says whether it happened, and a revive whose filing "
                            "failed still succeeds (the session is live, unfiled). Omit to "
                            "leave it where it was."
                        ),
                    },
                },
                "required": ["target"],
            },
            run=_run_session_revive,
            identity="strict",
            routes=(
                "POST /api/session-control/revive",
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "POST /api/chat/folders",
            ),
        ),
        Tool(
            name="session_send",
            description=(
                "Send a message into another session — the way to seed a session "
                "you just created with session_create, answer a question it "
                "raised, or correct it while it works. An idle target starts a "
                "turn on your message straight away. A BUSY target queues it for "
                "its next turn, unless you pass steer=true, which injects it into "
                "the turn already running so the target reads it mid-work; the "
                "result says which happened. The message lands in the target's "
                "transcript tagged as sent by your session, so the person reading "
                "it can tell it from their own typing. Use session_read_message "
                "afterwards to watch what the target did with it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The message to deliver. It becomes the target's next "
                            "user-role turn, so write it as you would type into "
                            "that session's composer."
                        ),
                    },
                    "steer": {
                        "type": "boolean",
                        "description": (
                            "Cut into the target's RUNNING turn instead of waiting "
                            "for it to end. Use it when waiting wastes the work in "
                            "flight — the target is heading the wrong way, or the "
                            "thing it is working on is already done. Ignored when "
                            "the target is idle (the message starts a turn either "
                            "way), and when mid-turn injection is unavailable the "
                            "message falls back to the queue rather than being "
                            "dropped. Default false."
                        ),
                    },
                },
                "required": ["target", "message"],
            },
            run=_run_session_send,
            identity="strict",
            routes=("POST /api/session-control/send",),
        ),
        Tool(
            name="session_broadcast",
            description=(
                "Send ONE message to several peer sessions at once — the fan-out "
                "counterpart of session_send, for when the thing you have to say "
                "is true of every worker rather than of one. Omit `targets` and it "
                "goes to every session YOU created, which is the usual case for a "
                "conductor; name them to reach a subset. `mode` picks the delivery "
                "and is required, because the two are different instructions: "
                "`queue` waits for each target's current turn to end (use it for "
                "'the base moved, rebase before you push'), while `steer` cuts "
                "into every running turn so each target reads it mid-work (use it "
                "for 'stop, that issue is already fixed'). Every target is checked "
                "the same way a single session_send is, so this can reach nothing "
                "a session_send could not. PARTIAL DELIVERY IS NORMAL and the "
                "result says so per target: a session that was closed, went "
                "incognito, or belongs to an app is one refused row and the rest "
                "still get the message — read the rows rather than assuming all or "
                "nothing. Poll the targets afterwards with session_read_message, "
                "or take the roster with session_status."
            ),
            schema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": (
                            "The message every target receives. It becomes each "
                            "one's next user-role turn and is tagged as a "
                            "broadcast in their transcripts, so a worker can tell "
                            "an instruction its siblings also got from one aimed "
                            "at it alone. Write it so it is true for all of them."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["queue", "steer"],
                        "description": (
                            "`queue`: each target runs the message when its "
                            "current turn ends (an idle one starts immediately). "
                            "`steer`: cut into the turn already running so the "
                            "target reads it mid-work; on an idle target it starts "
                            "a turn either way, and where mid-turn injection is "
                            "unavailable that target falls back to its queue "
                            "rather than being dropped. No default — say which."
                        ),
                    },
                    "targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Session keys or exact titles, as session_send takes "
                            "them. Omit to reach every session you created. A "
                            "repeated target is delivered to once."
                        ),
                    },
                },
                "required": ["message", "mode"],
            },
            run=_run_session_broadcast,
            identity="strict",
            routes=("POST /api/session-control/broadcast",),
        ),
        Tool(
            name="session_status",
            description=(
                "List the sessions YOU stood up and what each one is doing right "
                "now — the roster a conductor patrols. Each row is `working` (a "
                "turn is in flight, wait), `queued` (idle with messages waiting), "
                "`idle` (open and doing nothing — this is the one that needs a "
                "decision), `closed` (the crew log recorded a DELIBERATE tab close "
                "and the dashboard no longer holds it: a worker that FINISHED and "
                "closed its tab — ignore it, it carries `closed_at`), `lost` (the "
                "crew log remembers the session, the dashboard no longer holds it, "
                "and it did NOT close cleanly — a turn was still open, a process "
                "recycle or destroy tore it down, or its lifecycle could not be "
                "read: treat it as lost with the process that ran it and "
                "re-dispatch it), or `unknown` (birth metadata records that you "
                "created it, but neither a live session nor an attested crew-log "
                "edge exists — it was created and its fate is not recorded, so "
                "read it before you re-dispatch it). A row with "
                "`pending_approval: true` is parked on a tool approval a person "
                "must answer (`pending_approval_tool` names the tool when known): "
                "it still reads `working`, but waiting will not move it. `closed` "
                "and `lost` are the reason to use this instead of "
                "reading sessions one at a time: a worker that vanished is absent "
                "from any live list, so a live list cannot tell a worker that "
                "finished from one lost mid-task from one you never dispatched. "
                "Read both quality fields before "
                "trusting the count: `tree` describes the crew-log roster and "
                "`history` describes transcript birth metadata. For either one, "
                "`readable` means that source was read completely, `incomplete` "
                "means its rows may be missing, and `unreadable` means that "
                "durable source was unavailable. `roster_omitted` is separate "
                "again: it counts rows this reply dropped because the three "
                "sources' union exceeded the row bound, which no source's quality "
                "field describes. The result caveats each gap "
                "under its own source name. READ-only."
            ),
            schema={"type": "object", "properties": {}},
            run=_run_session_status,
            identity="strict",
            routes=("GET /api/session-control/status",),
        ),
        Tool(
            name="session_adopt",
            description=(
                "Take another session UNDER yours, so the sidebar shows it nested "
                "beneath this one and you are recorded as the session that holds "
                "it. Use it when you are taking over work someone else started: "
                "adopt each session you are now running, and the sessions THEY "
                "opened come with them, because the tree hangs on the session and "
                "not on a path. A session that already has a parent can be "
                "adopted — that is the takeover — and the parent it had is kept in "
                "the record. Refused if the target is already above you in the "
                "tree, which would make a loop. Nothing about the target's work "
                "changes: it keeps its own conversation, its turns and its tools. "
                "session_release undoes it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_adopt,
            identity="strict",
            routes=("POST /api/session-control/adopt",),
        ),
        Tool(
            name="session_release",
            description=(
                "Let a session out from under its parent, so it stands on its own "
                "in the sidebar again. The counterpart of session_adopt, and the "
                "only way to undo one. You may release a session you hold, and you "
                "may release YOURSELF from whoever holds you — so a session whose "
                "conductor has stopped running is not stuck under it. Refused for a "
                "session that has no parent, and for one that hangs under somebody "
                "else. Sessions the released one holds stay with it: it keeps its "
                "own subtree and only its own edge upward goes."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Session key from list_sessions, or its exact title. Your "
                            "own key releases you from your parent."
                        ),
                    },
                },
                "required": ["target"],
            },
            run=_run_session_release,
            identity="strict",
            routes=("POST /api/session-control/release",),
        ),
        Tool(
            name="session_read_message",
            description=(
                "Read the tail of another session's transcript, plus whether it is still "
                "working. Use it to watch a peer session's progress: `wait`, then read — "
                "pass the ``next_since`` from the previous read back as ``since`` "
                "and you get only what arrived in between, so a poll loop does not re-read "
                "the same messages. ``running: false`` with nothing new means the target "
                "finished and is idle, which is the difference between 'not done yet' and "
                "'done'. ``pending_approval: true`` means the target is parked on a "
                "tool approval a person must answer (``pending_approval_tool`` names "
                "the tool when known); it stays ``running`` but will not progress "
                "until someone decides. READ-only: it never sends anything or changes "
                "the target's state."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 20, max 100).",
                        "default": 20,
                    },
                    "since": {
                        "type": "integer",
                        "description": (
                            "Return messages from this index onward — pass the ``next_since`` from "
                            "your previous read to get only new ones. Omit for the newest tail."
                        ),
                    },
                },
                "required": ["target"],
            },
            run=_run_session_read_message,
            identity="strict",
            routes=("GET /api/session-control/read",),
        ),
        Tool(
            name="session_summary",
            description=(
                "Read another session's intent summary: the short digest the dashboard's "
                "summary panel shows (each goal with its status, progress and next steps, "
                "plus recurring project notes), and whether the session is still working. "
                "Use it on a patrol cycle to learn what a peer is doing without paging its "
                "transcript with session_read_message. It is a CACHED read and never spends "
                "a model call: the dashboard writes the summary at turn end, only when the "
                "operator has turned session summaries on, so it can be absent or `stale` "
                "(older than the latest turns). Fall back to session_read_message for "
                "anything newer or more exact. Authorized exactly as session_read_message "
                "is. READ-only."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_summary,
            identity="strict",
            routes=("GET /api/session-control/summary",),
        ),
        Tool(
            name="session_queue",
            description=(
                "See and manage the messages waiting in the queue of a session YOU created. "
                "session_send to a busy session queues the message (started: false); this is "
                "how you see where it sits, take it back when it has gone stale, or move a "
                "correction ahead of the older message it replaces. action=list returns the "
                "first 50 queued entries in run order (the rest are counted in `omitted`, not "
                "listed), each with its id, its position (0 runs next), a "
                "redacted excerpt and whether YOU queued it. action=cancel removes one entry; "
                "action=move puts one entry at `position`. cancel and move apply ONLY to "
                "entries you queued with session_send: a person's queued message, or another "
                "session's, is never cancellable or movable here. A move to an earlier "
                "position may pass only your own entries; a move later is always allowed. "
                "Entries queued before a gateway restart lose their sender stamp and list "
                "as not yours."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["list", "cancel", "move"],
                        "description": "list (default), cancel or move.",
                        "default": "list",
                    },
                    "entry": {
                        "type": "string",
                        "description": "Queue entry id from action=list. Required for cancel and move.",
                    },
                    "position": {
                        "type": "integer",
                        "description": (
                            "move only: the 0-based position to put the entry at (0 runs next). "
                            "A position past the end moves it to the back."
                        ),
                    },
                },
                "required": ["target"],
            },
            run=_run_session_queue,
            identity="strict",
            routes=("POST /api/session-control/queue",),
        ),
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


def _get_rows(client: DashboardClient, path: str) -> tuple[list[dict], str | None]:
    """GET a gateway endpoint whose success body is a JSON **array**.

    The routes this server reads as lists (``/api/chat/folders``,
    ``/api/chat/slots``) answer a refusal as an object, so the two shapes are
    split apart here. Returns ``(rows, None)`` on success and ``([], error)``
    otherwise. A body that is neither an array nor a refusal is reported as an
    error rather than read as empty: "the tree is empty" and "the endpoint is
    broken" must not render identically.
    """
    try:
        payload: object = client.get(path)
    except DashboardError as refused:
        return [], str(refused.error)
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)], None
    return [], f"unexpected response shape from {path}"


def _read_folder_sort_setting() -> object:
    """The raw ``dashboard.folder_sort`` value from the gateway's config on disk.

    Read the way the other MCP tools read a dashboard setting (the probe timeout
    in ``mcp_discovery``, the quarantine threshold in ``mcp_quarantine``):
    through the loader from ``config.json``, NOT through ``GET
    /api/config/kirocrew``. That route is cookie-authenticated only -- it is in
    neither internal-secret allowlist -- and admitting it to read one enum would
    open the whole config surface, PATCH included, to every secret-bearing
    caller. The sidebar menu's PATCH persists to the same file before it
    answers, so this read sees the menu's last choice; the loader has reduced
    the stored value to the known set, and the local overlay file is merged the
    same way for both readers.
    """
    return KiroCrewConfig.load().dashboard.folder_sort


def _chat_folder_sort_mode(
    read: Callable[[], object] = _read_folder_sort_setting,
) -> tuple[str, str | None]:
    """The person's sidebar folder sort mode, as ``read`` reports it.

    ``dashboard.folder_sort`` is the ONE stored copy of the preference: the
    sidebar menu writes it through the config PATCH allowlist, the sidebar reads
    it back through its config query, and the tree tool reads the same file, so
    the two cannot draw the tree in different orders. Returns ``(mode, None)``,
    or ``("custom", error)`` when the read itself failed -- ``custom`` being the
    stored-order listing every earlier build produced. A value outside the known
    set also reads as ``custom``, without an error: the loader has already
    reduced the stored value to a known one, so that can only be a build skew,
    not a broken read. The caller decides what to say about an error: the tree
    is still worth listing when only its ordering is in doubt.

    ``read`` is the config read; its default is the one the tree tool uses.
    """
    try:
        raw = read()
    except Exception as exc:  # noqa: BLE001 -- said in the header, not raised
        return FOLDER_SORT_DEFAULT, f"{type(exc).__name__}: {exc}"
    if isinstance(raw, str) and raw in FOLDER_SORT_MODES:
        return raw, None
    return FOLDER_SORT_DEFAULT, None


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


# The largest integer JavaScript represents exactly (``Number.MAX_SAFE_INTEGER``).
# The sidebar reads folder rows through ``JSON.parse``, so an ``order`` past this
# is not the number the store holds; both sides clamp to it so their comparisons
# agree on rows no sane writer produces but a hand-edited store can.
_CHAT_FOLDER_ORDER_LIMIT = 2**53 - 1

# A bare ``json.loads`` yields these for ``1e999`` / ``-1e999``; neither is a
# position, and neither survives the arithmetic a placement does with one.
_POS_INF = float("inf")
_NEG_INF = float("-inf")


def _chat_folder_order(folder: dict) -> int:
    """A folder's sidebar sort position. Anything that is not a finite number is 0.

    The endpoint stores whatever int a PATCH hands it and never renumbers, so a
    row can carry a duplicate order, a gap, or (from an older store) no ``order``
    key at all. The sidebar tolerates all three; so must every reader here.

    Only a real JSON number is accepted, and the frontend's counterpart accepts
    exactly the same set. That is deliberate: this value has to sort IDENTICALLY
    here and in the sidebar, and the two languages' conversions of everything else
    do not agree — ``int("0x10")`` and ``int("1e3")`` raise where ``Number`` reads
    16 and 1000, ``int([5])`` raises where ``Number`` reads 5. Rejecting the whole
    class costs nothing (the endpoint only ever writes an int) and removes the
    parity question instead of answering it per type.

    A float truncates toward zero, matching ``Math.trunc``, and ``bool`` is
    excluded even though it is an ``int`` subclass, because ``typeof true`` is not
    ``'number'`` on the other side. The result is clamped to the range JavaScript
    represents exactly: Python ints are unbounded, but the sidebar reads the same
    row through ``JSON.parse``, where ``2**53 + 1`` and ``2**53`` collapse to one
    value — so without the clamp those two rows compare as ordered here and as
    EQUAL there, and there the name tie-break decides the pair instead.

    This is a SORT KEY: an escaping exception would abort the whole sort and take
    down every folder tool rather than one row, so nothing here may raise.
    """
    value = folder.get("order")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value != value or value in (_POS_INF, _NEG_INF):  # NaN, ±Infinity
        return 0
    return max(-_CHAT_FOLDER_ORDER_LIMIT, min(_CHAT_FOLDER_ORDER_LIMIT, int(value)))


# A-Z to a-z and nothing else, written out rather than looked up. Every Unicode
# version ever published maps this range identically, so both sides can fold it
# without consulting a table — which is the whole reason the fold stops here.
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _chat_folder_name_key(folder: dict) -> bytes:
    """A folder name as the frontend's ``<`` would compare it.

    ``chat_folder_tree`` is where an agent picks a ``before``/``after`` anchor, so a
    sequence that differs from the rendered one aims the anchor at the wrong gap.
    That makes this key part of a contract with ``folderTree.bySidebarOrder``, and
    every operation in it has to mean the same thing in both languages.

    No Unicode-table fold. ``str.lower()`` reads the interpreter's tables and
    ``String.prototype.toLowerCase()`` reads the browser's, so a character whose case
    mapping was added or changed between the interpreter's version and the browser's
    folds differently on the two sides — a skew no code here can close, because
    neither side owns both tables.

    ``A``-``Z`` fold anyway, through the literal 26-entry table above. That range's
    case mapping is fixed for every Unicode version ever published and the frontend
    does the same fold by ARITHMETIC on the code unit, so it carries no version
    dependency while still ordering ``alpha`` before ``Beta``. The distinction is
    worth drawing because a store written before ``order`` existed has every sibling
    tied at 0 — the whole tie-break decides those sidebars, so a fold that only
    ordered by raw code unit would show them uppercase-first until the first drag.

    ``utf-16-be`` rather than the str itself: Python orders str by CODE POINT while
    JavaScript orders by UTF-16 CODE UNIT, and the two disagree above U+FFFF — an
    astral character's surrogates start at 0xD800, so Python sorts U+1F600 after
    U+FF21 and JavaScript sorts it before. Comparing the big-endian UTF-16 bytes is
    code-unit order. This is a fixed encoding, not a table lookup, so it carries no
    version dependency either.

    ``surrogatepass`` because a folder name is persisted JSON and can hold a LONE
    surrogate, which the strict codec refuses outright — and a raised encoder
    inside a sort key takes down every folder tool, not one row. Passing it through
    also keeps the byte-for-byte match: a JavaScript string holds that same lone
    unit and compares it as 0xD800, which is exactly what these bytes carry.

    A name that is not a ``str`` reads as empty rather than being stringified, and
    the frontend's counterpart does the same. Stringifying is where the two
    languages part company: ``str({"a": 1})`` is ``"{'a': 1}"`` where
    ``String({a: 1})`` is ``"[object Object]"``, and ``str(True)`` is ``"True"``
    where ``String(true)`` is ``"true"``. Reading the whole class as empty makes
    both sides agree by construction instead of per type.
    """
    name = folder.get("name")
    text = name if isinstance(name, str) else ""
    return text.translate(_ASCII_FOLD).encode("utf-16-be", "surrogatepass")


def _chat_folder_created(folder: dict) -> float | None:
    """A folder's creation stamp as the sidebar reads it, or ``None`` when it has none.

    ``created_at`` is epoch seconds written by every folder creator since the
    sidebar's ``created`` sort existed; a row from before that carries no key. The
    acceptance rule is ``_chat_folder_order``'s, for the same reason: this value
    orders the ``created`` mode on both sides, so only a real, finite JSON number
    counts and everything else reads as "no stamp" identically here and in the
    sidebar's ``folderCreated``. Both sides clamp to the range JavaScript holds
    exactly, and an unbounded int is clamped BEFORE the ``float`` call because
    ``float(10**400)`` raises -- and this is a sort key, so nothing here may.
    """
    value = folder.get("created_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        value = max(-_CHAT_FOLDER_ORDER_LIMIT, min(_CHAT_FOLDER_ORDER_LIMIT, value))
    stamp = float(value)
    if stamp != stamp or stamp in (_POS_INF, _NEG_INF):  # NaN, ±Infinity
        return None
    return max(-float(_CHAT_FOLDER_ORDER_LIMIT), min(float(_CHAT_FOLDER_ORDER_LIMIT), stamp))


_DIGIT_RUNS = _re.compile(r"[0-9]+|[^0-9]+")


def _chat_folder_natural_key(folder: dict) -> tuple[tuple[Any, ...], ...]:
    """The ``name`` sort mode's key: case-folded natural order, ``01.`` < ``02.`` < ``10.``.

    The folded name is cut into maximal runs of ASCII digits and of everything
    else, and the runs are compared position by position. A digit run compares by
    its integer value -- leading zeros stripped, then length, then the digits, so
    the value is compared without ever being converted to a number and an
    arbitrarily long run cannot overflow on either side. A text run compares as
    ``_chat_folder_name_key`` does (UTF-16 code units of the ``A``-``Z``-folded
    text). Where a digit run meets a text run, the digit run sorts first; where
    every compared run is equal, the shorter name sorts first.

    Only ``0``-``9`` is a digit, deliberately: ``str.isdigit`` reads the
    interpreter's Unicode tables where the sidebar's ``/[0-9]/`` does not, and a
    key that agrees with ``folderTree.naturalNameCompare`` by construction is the
    whole point -- ``chat_folder_tree`` in this mode must list what the sidebar
    draws. Ties (``01`` against ``1``) fall through to the ``custom`` key.
    """
    name = folder.get("name")
    text = (name if isinstance(name, str) else "").translate(_ASCII_FOLD)
    key: list[tuple[Any, ...]] = []
    for run in _DIGIT_RUNS.findall(text):
        if "0" <= run[0] <= "9":
            digits = run.lstrip("0")
            key.append((0, len(digits), digits.encode("ascii")))
        else:
            key.append((1, run.encode("utf-16-be", "surrogatepass")))
    return tuple(key)


def _chat_folder_sort_key(mode: str) -> Callable[[dict], tuple[Any, ...]]:
    """The sibling sort key for one folder sort mode.

    ``custom`` is the stored ``order`` then the folded name -- the only order that
    existed before the mode did, and the one every placement (``before``/``after``)
    is computed in. ``name`` and ``created`` are VIEW orders layered on top: each
    ends in the ``custom`` key so two folders the mode cannot separate keep the
    order the person arranged, and so the whole key stays total on both sides.
    ``created`` is newest first, the direction the sidebar's session list already
    reads in; a folder with no stamp sorts as older than every stamped one.

    An unknown mode reads as ``custom`` rather than raising, because this is
    called from a listing an agent depends on and the loader has already reduced
    the stored value to a known one.
    """

    def custom(folder: dict) -> tuple[Any, ...]:
        return (_chat_folder_order(folder), _chat_folder_name_key(folder))

    if mode == "name":
        return lambda f: (_chat_folder_natural_key(f), *custom(f))
    if mode == "created":

        def created(folder: dict) -> tuple[Any, ...]:
            stamp = _chat_folder_created(folder)
            return (1, 0.0, *custom(folder)) if stamp is None else (0, -stamp, *custom(folder))

        return created
    return custom


def _chat_folder_siblings(
    folders: list[dict], parent_id: str, mode: str = FOLDER_SORT_DEFAULT
) -> list[dict]:
    """Direct children of ``parent_id``, in the order the sidebar renders them.

    The comparator mirrors the sidebar's own (``folderTree.folderComparator``
    sorts each parent's children by the person's folder sort mode -- stored
    ``order`` then name in ``custom``), because a caller saying "put this after
    that" means after what the PERSON SEES. Sorting by ``order`` alone would
    disagree with the rendered list wherever two siblings share a number, which
    the store permits.

    ``mode`` defaults to ``custom`` because that is the order a POSITION lives in:
    the placement helpers call this to find the gap a ``before``/``after`` anchor
    names, and a gap only exists in the stored order. The tree LISTING passes the
    person's mode instead, so it shows what the sidebar shows.

    The name key is ``_chat_folder_name_key``, which folds ``A``-``Z`` and compares
    the UTF-16 encoding, so ``Apple`` sorts between ``alpha`` and ``apricot``.
    Nothing outside that range folds: a wider fold would read each runtime's own
    Unicode tables, and the sidebar's runtime is not this one — see that function
    for which mappings are left alone and why.
    """
    kids = [f for f in folders if f.get("id") and str(f.get("parent_id") or "") == parent_id]
    return sorted(kids, key=_chat_folder_sort_key(mode))


# ``ChatSidebar``'s ``renderFolderBlock`` returns nothing for ``depth > 10``, so a
# row nested deeper is not drawn. The store caps folder COUNT but not nesting, so
# such a tree is legal rather than corrupt. Only the reported DEPTH is clamped
# here, never the row: this listing is an agent's one view of the tree and it
# carries each folder's live sessions, so dropping a folder would hide those
# sessions to buy indentation parity on a row nobody can see anyway.
_SIDEBAR_MAX_DRAWN_DEPTH = 10


def _chat_folder_render_order(
    folders: list[dict], mode: str = FOLDER_SORT_DEFAULT
) -> list[tuple[str, int]]:
    """``(folder_id, depth)`` in sidebar render order — pre-order, siblings by ``mode``.

    Depth-first from the top level, so a child always follows its parent, which
    is the shape the sidebar draws. Siblings sort with ``_chat_folder_sort_key``
    for the person's folder sort mode, at every depth, because the sidebar applies
    its comparator to every parent's children alike. Two defences the sidebar also
    carries: a row whose ``parent_id`` names a folder absent from this list renders
    at the top level rather than being dropped, and a parent cycle can neither
    loop the walk nor swallow the folders caught in it (those are appended at the
    end).

    Depth is clamped to ``_SIDEBAR_MAX_DRAWN_DEPTH`` so the indentation cannot claim
    a level the sidebar does not draw. Every row is still listed at that depth: an
    anchor beside a row the person cannot see writes an ``order`` nobody reads,
    which is a smaller cost than hiding the folder and the live sessions filed in
    it from the only tree view an agent gets.
    """
    known = {str(f.get("id") or "") for f in folders if f.get("id")}
    by_parent: dict[str, list[dict]] = {}
    for folder in folders:
        if not folder.get("id"):
            continue
        parent = str(folder.get("parent_id") or "")
        by_parent.setdefault(parent if parent in known else "", []).append(folder)
    sort_key = _chat_folder_sort_key(mode)
    for kids in by_parent.values():
        kids.sort(key=sort_key)

    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    # Iterative walk: the store caps folder COUNT but not nesting depth, and a
    # recursive descent would answer a deep chain with a RecursionError.
    stack: list[tuple[str, int]] = [
        (str(f.get("id") or ""), 0) for f in reversed(by_parent.get("", []))
    ]
    while stack:
        fid, depth = stack.pop()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        out.append((fid, min(depth, _SIDEBAR_MAX_DRAWN_DEPTH)))
        stack.extend((str(f.get("id") or ""), depth + 1) for f in reversed(by_parent.get(fid, [])))
    for folder in folders:
        fid = str(folder.get("id") or "")
        if fid and fid not in seen:
            seen.add(fid)
            out.append((fid, 0))
    return out


def _free_slot_order(siblings: list[dict], index: int) -> int | None:
    """An unused ``order`` that lands a folder at ``index``, or ``None`` if none fits.

    Positioning a folder means writing several rows when the siblings have to be
    renumbered, and several writes cannot be made atomic from here — so the cheap
    case is worth taking whenever the store already has room: before the first
    sibling, after the last, or in a gap between two adjacent ones. Then the whole
    reposition is ONE write and cannot land half-applied.

    ``siblings`` is the destination's children WITHOUT the folder being placed, in
    render order. ``None`` means the neighbours are adjacent integers, which is
    what a sidebar drag leaves behind, and the caller must renumber instead.

    An edge slot at ``±_CHAT_FOLDER_ORDER_LIMIT`` is also not free. The neighbour's
    order is read back through the same clamp, so a value one past the bound
    returns as the bound itself — equal to the anchor rather than outside it, which
    hands the pair to the name tie-break and can place the folder on the wrong
    side. There is no representable slot there, so the caller renumbers.
    """
    if not siblings:
        return 0
    if index <= 0:
        first = _chat_folder_order(siblings[0])
        return None if first <= -_CHAT_FOLDER_ORDER_LIMIT else first - 1
    if index >= len(siblings):
        last = _chat_folder_order(siblings[-1])
        return None if last >= _CHAT_FOLDER_ORDER_LIMIT else last + 1
    low = _chat_folder_order(siblings[index - 1])
    high = _chat_folder_order(siblings[index])
    # Equal or inverted neighbours leave no room either: the pair is already
    # separated only by the name tie-break, which no order value can get between.
    if high - low >= 2:
        return low + (high - low) // 2
    return None


def _ambiguous_segment_error(seg: str, matches: list[dict]) -> str:
    """Refusal naming the duplicate folders, so the caller can pick one by id."""
    ids = ", ".join(str(m.get("id") or "?") for m in matches)
    return (
        f"{len(matches)} folders named {redact(seg)} share the same parent "
        f"({ids}) — pass the folder id instead of a path"
    )


def _resolve_chat_folder_ref(
    ref: str,
    folders: list[dict],
    *,
    create_missing: bool,
    client: DashboardClient | None = None,
    session_key: str | None = None,
    before_create: Callable[[str], str | None] | None = None,
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
        return (
            "",
            [],
            (
                f"{len(exact)} folders render the same path {redact(ref)} "
                f"({', '.join(exact)}) — pass the folder id instead of a path"
            ),
        )

    # Reading 3: walk the segments. When the exact reading already resolved, the
    # walk runs resolve-only — there is nothing to create, and creating before
    # the two readings are compared would mutate the tree on a reference we are
    # about to refuse.
    walked, created, walk_err = _walk_chat_folder_segments(
        ref,
        folders,
        create_missing=create_missing and not exact,
        client=client,
        session_key=session_key,
        before_create=before_create,
    )
    if walk_err:
        return "", created, walk_err

    if exact and walked and walked != exact[0]:
        return (
            "",
            [],
            (
                f"{redact(ref)} is ambiguous: it is both a folder's own name "
                f"({exact[0]}) and a nested path ({walked}) — pass the folder id"
            ),
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
    ref: str,
    folders: list[dict],
    *,
    create_missing: bool,
    client: DashboardClient | None = None,
    session_key: str | None = None,
    before_create: Callable[[str], str | None] | None = None,
) -> tuple[str, list[str], str | None]:
    """Walk a ``/``-separated path segment by segment. ONE walk, two modes.

    Returns ``(folder_id, created_names, error)``; ``folder_id`` is ``""`` when a
    segment is missing and ``create_missing`` is false. Duplicate siblings are
    refused in BOTH modes — resolving to the first match would act on an
    arbitrary folder, and creating under it would bury the new folder in
    whichever duplicate happened to come first.

    ``client`` makes the creates (and the re-read after a lost race), so the
    create mode needs one. ``folders`` is appended in place for each created row
    so a later path render sees it. Created names come back even alongside an error, so a partial
    mkdir -p is reported rather than silently left behind.

    Two checks run before the FIRST folder is created, so a walk that would
    stop part-way stops with nothing made. Every segment's length is tested up
    front. ``before_create`` is called once, with the id of the deepest folder
    that already exists, just before the first create; an error it returns
    ends the walk there. It is how ``session_create`` asks whether the create
    itself would be refused before any folder exists for it.
    """
    walked = ""
    parent = ""
    created: list[str] = []
    checked_before_create = before_create is None
    segments = [s.strip() for s in ref.split("/") if s.strip()]
    if create_missing:
        for raw in segments:
            seg = redact(raw)
            if len(seg) > _MAX_FOLDER_NAME:
                return (
                    "",
                    created,
                    f"folder name too long ({len(seg)} chars): "
                    f"`{seg[:40]}…` — keep each path segment to "
                    f"{_MAX_FOLDER_NAME} characters or fewer",
                )
    for raw in segments:
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
        if not checked_before_create and before_create is not None:
            checked_before_create = True
            refused = before_create(parent)
            if refused:
                return "", created, refused
        assert client is not None  # the create mode always carries one
        try:
            made = client.post(
                "/api/chat/folders",
                {"name": seg, "parent_id": parent},
                session_key=session_key,
            )
        except DashboardError as refused:
            made = refused.body
        # The lost race is read off the code before any error, as the endpoint's
        # refusal names it, so a reply carrying the code alone is that race too.
        if made.get("code") == "folder_name_exists":
            # The endpoint refuses an agent a same-name sibling under its lock.
            # Either a concurrent walk created this segment after our read, or
            # the folder exists but this caller's view of the tree omits it (a
            # crew member reads only its own folders). Re-read once: the first
            # case resolves to the winner's folder, the second is refused
            # rather than forked into a duplicate beside the one it cannot see.
            fresh, fresh_err = _get_rows(client, "/api/chat/folders")
            if fresh_err:
                return "", created, redact(str(fresh_err))
            folders[:] = fresh
            matches = _chat_folder_children(folders, parent, seg)
            if len(matches) > 1:
                return "", created, _ambiguous_segment_error(seg, matches)
            if matches:
                walked = str(matches[0].get("id") or "")
                parent = walked
                continue
            return (
                "",
                created,
                (
                    f"a folder named `{seg}` already exists there and is not one "
                    "this session can file into, so no duplicate was created — "
                    "use a path under a folder you created, or a different name"
                ),
            )
        if made.get("error"):
            return "", created, str(made["error"])
        if made.get("reused"):
            # The endpoint handed back this caller's own same-name folder, which
            # a concurrent walk created after our read. Nothing new exists.
            made = {k: v for k, v in made.items() if k != "reused"}
            if not any(str(f.get("id") or "") == str(made.get("id") or "") for f in folders):
                folders.append(made)
            walked = str(made.get("id") or "")
            parent = walked
            continue
        folders.append(made)
        created.append(str(made.get("name") or seg))
        walked = str(made.get("id") or "")
        parent = walked
    return walked, created, None


def _resolve_chat_folder_id(ref: str, folders: list[dict]) -> tuple[str, str | None]:
    """Resolve an EXISTING sidebar folder (id or human path) to its id."""
    fid, _created, err = _resolve_chat_folder_ref(ref, folders, create_missing=False)
    return fid, err


def _ensure_chat_folder_path(
    ref: str,
    folders: list[dict],
    *,
    client: DashboardClient,
    session_key: str,
    before_create: Callable[[str], str | None] | None = None,
) -> tuple[str, list[str], str | None]:
    """Resolve a parent-folder reference, creating missing segments (mkdir -p).

    ``session_key`` is REQUIRED because this variant writes: the intermediate
    segments are real folders, and each must be created under the identity the
    caller's gate verified rather than one the client re-derives.
    """
    return _resolve_chat_folder_ref(
        ref,
        folders,
        create_missing=True,
        client=client,
        session_key=session_key,
        before_create=before_create,
    )


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
            "for slot keys. An ARCHIVED session cannot be moved: bring it back "
            "with session_revive first"
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
        "keys. An ARCHIVED session cannot be moved: bring it back with session_revive first"
    )


def _resolve_chat_tag_ids(refs: list[str], tags: list[dict]) -> tuple[list[str], str | None]:
    """Resolve tag references (ids or exact names) to tag ids, in call order.

    Tag ids are minted as ``uuid.uuid4().hex[:12]`` (``chat_tags.create_tag_definition``).
    A reference is a tag id or a tag's exact name, matched case-insensitively
    and never as a substring — so a partial or unknown name fails loudly naming
    the vocabulary instead of tagging with the wrong label. Ids win over names
    when a tag is NAMED like another's id, since the id is the unambiguous form.
    Duplicates collapse; an unknown reference fails the whole call, so a delta
    is applied whole or not at all.
    """
    by_id = {str(t["id"]): t for t in tags if isinstance(t.get("id"), str) and t["id"]}
    by_name: dict[str, list[str]] = {}
    for t in tags:
        nm = str(t.get("name") or "").strip().lower()
        if nm and isinstance(t.get("id"), str) and t["id"]:
            by_name.setdefault(nm, []).append(str(t["id"]))
    out: list[str] = []
    for raw in refs:
        ref = str(raw or "").strip()
        if not ref:
            continue
        tid = ""
        if ref in by_id:
            tid = ref
        else:
            named = by_name.get(ref.lower(), [])
            if len(named) > 1:
                return [], (
                    f"{len(named)} tags share the name {redact(ref)} "
                    f"({', '.join(named)}) — pass the tag id instead"
                )
            if named:
                tid = named[0]
        if not tid:
            return [], (
                f"no tag matches {redact(ref)} — call chat_tag_list for the vocabulary, "
                "or chat_tag_create to add it"
            )
        if tid not in out:
            out.append(tid)
    return out, None


def _render_chat_tags(tags: list[dict]) -> str:
    """One line per tag: id, name, color, and the status marker."""
    if not tags:
        return "No tags defined yet — chat_tag_create makes one."
    lines = [f"\U0001f3f7\ufe0f Tag vocabulary — {len(tags)} tag{'' if len(tags) == 1 else 's'}"]
    # Same coercion as folder rows: ``tags.json`` is loaded verbatim, so a
    # hand-edited ``order`` must sort as 0 rather than end the tool call.
    for t in sorted(tags, key=_chat_folder_order):
        marker = "  [status]" if t.get("status") else ""
        lines.append(
            f"- `{t.get('name', '?')}`  id={t.get('id', '?')}  color={t.get('color', '?')}{marker}"
        )
    return "\n".join(lines)


def _chat_tag_column_label(col: dict, tag_names: dict[str, str]) -> str:
    """What a board column shows, in words: its tag filter or its live-state lane."""
    if col.get("source") == "state":
        return f"live state `{col.get('state_key') or '?'}`"
    # ``tag_columns.json`` is loaded verbatim, so a hand-edited ``tag_ids``
    # that is not a list must render as "no filter" rather than end the call.
    raw_ids = col.get("tag_ids")
    tag_ids = [str(t) for t in raw_ids if isinstance(t, str)] if isinstance(raw_ids, list) else []
    if not tag_ids:
        # An empty filter matches every session; ``include_untagged`` adds
        # nothing to that (``columnMatches`` in the board UI).
        return "all sessions"
    names = ", ".join(f"`{tag_names.get(t, t)}`" for t in tag_ids)
    mode = str(col.get("mode") or "any")
    label = f"tags {names} (match {mode})"
    if col.get("include_untagged"):
        label += " + untagged sessions"
    return label


def _render_chat_tag_columns(columns: list[dict], tags: list[dict]) -> str:
    """One line per board column, in board order (the endpoint returns it sorted)."""
    if not columns:
        return "The board has no columns yet — chat_tag_column_create adds one."
    tag_names = {str(t.get("id") or ""): str(t.get("name") or "?") for t in tags}
    lines = [
        f"\U0001f5c2\ufe0f Board columns — {len(columns)} column{'' if len(columns) == 1 else 's'}"
    ]
    for pos, col in enumerate(columns, start=1):
        name = str(col.get("name") or "").strip() or "(unnamed)"
        lines.append(
            f"{pos}. `{name}`  id={col.get('id', '?')}  shows "
            f"{_chat_tag_column_label(col, tag_names)}"
        )
    return "\n".join(lines)


def _resolve_chat_tag_column(ref: str, columns: list[dict]) -> tuple[str, str | None]:
    """Resolve a column reference (id or exact name) to a column id.

    Same rules as :func:`_resolve_chat_tag_ids`: the id wins, a name matches
    whole and case-insensitively, and a name two columns share is refused
    rather than guessed, since moving the wrong column rearranges the board.
    """
    ref = str(ref or "").strip()
    ids = [str(c.get("id")) for c in columns if isinstance(c.get("id"), str) and c["id"]]
    if ref in ids:
        return ref, None
    named = [
        str(c["id"])
        for c in columns
        if isinstance(c.get("id"), str)
        and c["id"]
        and str(c.get("name") or "").strip().lower() == ref.lower()
    ]
    if len(named) > 1:
        return "", (
            f"{len(named)} columns share the name {redact(ref)} "
            f"({', '.join(named)}) — pass the column id instead"
        )
    if not ref or not named:
        return "", (
            f"no board column matches {redact(ref)} — call chat_tag_column_list for "
            "the current columns"
        )
    return named[0], None


def _refuse_channel_board_write(name: str, caller_key: str) -> str | None:
    """Refuse a board write from a ``channel:`` caller, at dispatch.

    The ``CHANNEL_AGENT_BLOCKED_TOOLS`` name match runs at the permission
    prompt, which an auto-approved call never reaches, so the containment has to
    hold here too (the same split ``chat_session_pin`` uses). A channel agent
    acts on thread text other people wrote, and the board is the person's own
    sidebar layout.
    """
    if not caller_key.startswith("channel:"):
        return None
    try:
        sel().log_tool_invocation(
            session_key=caller_key,
            source="mcp",
            tool_name=name,
            tool_kind=SERVER_NAME,
            outcome="rejected_blocked_tool",
        )
    except Exception:
        # Stdio-silent: stderr would corrupt the JSON-RPC stream. The refusal
        # below holds either way.
        pass
    return (
        f"Error: {name} is not available to channel agents — the board is the "
        "person's own sidebar layout, and a channel agent acts on thread text "
        "other people wrote."
    )


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    return TABLE.validate(name, args)


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
    # A slot bound to a channel or cron session runs its turns under
    # ``linked_session_key`` ("when set, _run_chat uses this as session key" --
    # ``DashboardState``), so the caller presents THAT key and the match above
    # cannot find it. The rows serialize the field, so an app-owned linked slot
    # would otherwise read as unscoped here even though its owner is on record.
    #
    # POSITIVE matches only: a channel- or cron-born row is created with no
    # ``app``, so returning on an ownerless match would answer "" (the person)
    # for a delegated caller that the refusal below would otherwise fail closed
    # on -- weakening this layer instead of extending it.
    for r in rows:
        if str(r.get("linked_session_key") or "") == caller_key:
            linked_app = str(r.get("app") or "")
            if linked_app:
                return linked_app
            break
    if caller_key.startswith(_DELEGATED_CALLER_PREFIXES):
        return None
    if caller_key.startswith("dashboard:"):
        return None
    return ""


def _own_chat_slot(caller_key: str, rows: list[dict]) -> tuple[dict, str | None]:
    """The caller's OWN sidebar slot row, or why it has none to file.

    Resolved from the VERIFIED caller key only, never from an argument: this is
    what lets ``chat_folder_file_self`` be granted to an unattended conductor
    where ``chat_folder_move_session`` is withheld — the one placement it can
    write is its own.

    Only a ``dashboard:<slot>`` key qualifies, and it must name a row that is
    present. That key IS the slot: the slot key never changes for the life of
    the tab, so the row this resolves and the row the PATCH lands on are the
    same object (the endpoint re-checks identity under its lock). A slot bound
    to a channel or a schedule presents its ``linked_session_key`` instead, and
    that binding is REBOUND on live slots with no running gate — so a key that
    matched one slot at this read could name a different conversation by the
    time the write arrives. Filing on that match would file someone else's
    session; those callers are refused rather than raced.

    Two more refusals, both for what the placement would mean. A private
    (incognito / temporary) session is kept out of the sidebar record by the
    person's choice, and a folder placement is durable sidebar metadata. A crew
    member's pinned DM thread (``mode == "member"``) is one thread that spans
    every goal the member ever runs and lives on the Crew page, not in the
    sidebar tree — so a conductor running AS a member does not file itself; its
    workers still go under ``<goal>/<agent>`` via ``session_create``.
    """
    if not caller_key.startswith("dashboard:"):
        return {}, (
            "Error: this session has no sidebar slot to file — only a dashboard "
            "chat session can be placed in a folder."
        )
    slot_key = caller_key.split(":", 1)[1]
    row: dict | None = None
    for r in rows:
        if str(r.get("key") or "") == slot_key:
            row = r
            break
    if row is None:
        return {}, (
            "Error: this session has no sidebar slot to file — only a dashboard "
            "chat session can be placed in a folder."
        )
    if str(row.get("mode") or "") == "member":
        return {}, (
            "Error: a crew member's pinned DM thread lives on the Crew page and is "
            "not filed in a sidebar folder — it spans every goal the member runs. "
            "Skip filing yourself; create your workers with "
            "session_create's `folder` set to `<goal>/<agent>` instead."
        )
    if str(row.get("memory_mode") or "persistent") != "persistent":
        return {}, (
            "Error: a private (incognito or temporary) session is kept out of the "
            "sidebar record and cannot be filed."
        )
    return row, None


def _visible_chat_slots(ctx: ToolContext) -> tuple[list[dict], str | None]:
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
    rows, err = _get_rows(ctx.client, "/api/chat/slots")
    if err:
        return [], err
    live = [r for r in rows if str(r.get("memory_mode") or "persistent") == "persistent"]
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        "cannot verify which session is calling, so the session list is "
        "withheld — these tools scope what they show to the caller"
    )
    if not caller_key:
        return [], strict_err
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


def _refuse_tree_shaping_if_unverifiable(
    ctx: ToolContext, verb: str
) -> tuple[str, str, str | None]:
    """``(verified_caller_key, caller_app, error)`` — how to WRITE, or why not.

    Folders now carry an owner (``chat_folders._folder_owner_app``), so an app
    HAS a folder of its own to write to and the endpoint bounds each write to
    it: an app may create at the top level or inside its own folders, and may
    rename, reparent or delete only what it owns. The person keeps full
    authority over everything, and an app keeps read of the whole tree plus
    filing its OWN sessions into any folder.

    So this layer does not decide the policy — that would be a second copy of a
    rule the endpoint enforces under the store lock, and only the endpoint can
    see the authoritative tree. What stays here is the one question the endpoint
    cannot answer: whether the caller can be placed at all. An unverifiable or
    delegated caller has no scope to bound a write to, and a write to shared
    structure is not the place to assume the caller is the human.

    The verified key is RETURNED rather than left for the write helpers to
    re-derive. A request that names no key carries the frame's attribution key
    (``mcp_core._resolve_session_key``), whose ``/proc`` ancestor walk can
    resolve to a different slot than the strict check above —
    so re-resolving would check one identity and write under another, and for an
    app-owned session the walk landing on an ancestor makes the write arrive at
    the endpoint looking like the unconfined person. Every caller of this gate
    must pass what it returns straight to the write.

    ``caller_app`` (``""`` for the person) comes back for the same reason: it is
    the identity the endpoint will judge each write against, and a caller that
    must issue SEVERAL writes to keep one call atomic can only check them all up
    front by holding it. Re-deriving it would mean a second ``/api/chat/slots``
    fetch, against a roster that may have changed.
    """
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        f"Error: cannot verify which session is calling, so {verb} is "
        "refused — reshaping the shared folder tree requires a caller "
        "identity the gateway can vouch for."
    )
    if not caller_key:
        return "", "", strict_err
    rows, err = _get_rows(ctx.client, "/api/chat/slots")
    if err:
        return "", "", f"Error: {err}"
    scope = _caller_app_scope(caller_key, rows)
    if scope is None:
        return (
            "",
            "",
            (
                f"Error: cannot establish what this caller is allowed to change, so "
                f"{verb} is refused — a subagent or a scheduled job runs on behalf of "
                "whatever created it and cannot be granted more than that."
            ),
        )
    if scope and not caller_key.startswith("dashboard:"):
        # An app-owned LINKED session -- a channel- or cron-bound slot runs its
        # turns under ``linked_session_key`` -- is the one shape whose staleness
        # nothing downstream can notice. The endpoint re-derives the app from
        # this key and refuses a key that NAMES a slot which is gone, but
        # ``caller_names_a_missing_slot`` is ``dashboard:``-only BY DESIGN: for
        # any other shape absence is not evidence of a vanished slot, since a
        # Slack thread or a channel session legitimately never had one, and
        # refusing those would take these tools from callers no app could have
        # been attached to.
        #
        # So for a linked key the endpoint cannot tell "this app's slot just
        # closed" from "no app owns this caller", and would read the second and
        # apply the person's authority. THIS layer can tell, because it resolved
        # the scope positively a moment ago, so the refusal belongs here.
        return (
            "",
            "",
            (
                f"Error: {verb} is refused for a channel- or schedule-bound session "
                "owned by an app — its identity cannot be re-verified at the point of "
                "the write, so the folder rules could not be bounded to it. Run this "
                "from the app's own dashboard session."
            ),
        )
    return caller_key, scope, None


def _resolve_folder_for_new_session(
    ctx: ToolContext,
    folder_ref: str,
    verb: str,
    preflight: Callable[[str], str | None] | None = None,
) -> tuple[str, str, str, str | None]:
    """``(folder_id, folder_label, made_note, error)`` for filing a NEW session.

    Shared by ``session_create``, ``session_fork`` and ``session_revive``, which file a session the same
    way: the reference is resolved with ``chat_folder_create``'s `parent`
    semantics -- missing path segments are CREATED -- and creating folders is
    tree shaping, so the same gate applies rather than a second authorization
    path: a caller that could not reshape the tree by creating a folder must not
    reach the same write by naming the path here. The gate's verified key is what
    the segment creation writes under, per its own contract.

    An empty reference resolves to ``("", "", "", None)``: nothing to file.
    ``made_note`` names any path segments the mkdir -p walk created, and it is
    reported on BOTH outcomes -- those segments persist even when the create
    itself is then refused, the same partial-report posture chat_folder_create
    takes, since folder deletion is deliberately not a capability this server
    has.

    ``preflight`` closes that gap for the common case. The walk calls it just
    before its first create, with the deepest folder that already exists, and
    it returns an error string when the session create itself would be
    refused. That refusal ends the walk with nothing made, so a create that
    cannot succeed leaves no empty folder behind. Only a refusal that appears
    between the preflight and the create (a race, or a folder-create rate limit
    hit part-way down the path) can still strand segments.
    """
    if not folder_ref:
        return "", "", "", None
    gate_key, _gate_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, verb)
    if gate:
        return "", "", "", gate
    # An app-scoped caller may create folders, but it can NEVER complete a
    # session create or fork (the endpoints refuse `app_scoped_caller`), so
    # resolving the folder for it would only leave created path segments behind
    # for a call that cannot succeed. This is a side-effect guard, not a second
    # authorization home: the endpoint's refusal stays authoritative for the
    # create itself.
    scope_rows, scope_err = _get_rows(ctx.client, "/api/chat/slots")
    if scope_err:
        return "", "", "", redact(f"Error: {scope_err}")
    if _caller_app_scope(gate_key, scope_rows):
        return (
            "",
            "",
            "",
            (
                "Error: an app-scoped session cannot create sessions, so there is "
                "nothing to file — folder resolution is refused before it would "
                "create path segments for a create that cannot succeed."
            ),
        )
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return "", "", "", redact(f"Error: {folders_err}")
    fld_id, created_segments, fld_err = _ensure_chat_folder_path(
        folder_ref, chat_folders, client=ctx.client, session_key=gate_key, before_create=preflight
    )
    made_note = ""
    if created_segments:
        made_note = f" (created folder path: {'/'.join(created_segments)})"
    if fld_err:
        # Refuse the whole create: the caller asked for a session filed in this
        # folder, and "created but unfiled" would silently honor half of that.
        # No SESSION exists yet; path segments the mkdir -p walk already created
        # DO persist and are reported in `made_note`.
        return "", "", made_note, redact(f"Error: {fld_err}{made_note}")
    return fld_id, _chat_folder_paths(chat_folders).get(fld_id, fld_id), made_note, None


def _summary_time(value: object) -> str:
    """The sidecar's ``generated_at`` (a ``time.time()`` float) as UTC ISO-8601."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return "at an unknown time"


def _closed_at_display(value: object) -> str:
    """A gone row's ``closed_at`` (crew-log epoch MILLISECONDS) as UTC ISO-8601.

    The close edge stamps ``entry.time`` from ``now_ms()``, so the value is in
    milliseconds; render it human-readable rather than as a raw epoch number. Falls
    back to the value's own string form when it is not a finite millisecond count,
    so a malformed stamp still shows something rather than raising in the renderer.
    """
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value) / 1000.0))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)


def _render_session_summary(resp: dict[str, Any]) -> str:
    """Render a ``/api/session-control/summary`` body as compact text.

    The route has already bounded and redacted every field; this only formats
    it and says where the route left something out.
    """
    state_line = "still working" if resp.get("running") else "idle"
    head = f"\U0001f9ed `{resp.get('target', '')}` — {resp.get('title', '')} ({state_line})"
    if not resp.get("enabled"):
        return (
            f"{head}\nSession summaries are switched off on this gateway "
            "(session_summary.enabled), so there is no digest to read. Use "
            "session_read_message instead."
        )
    intents = [i for i in (resp.get("intents") or []) if isinstance(i, dict)]
    if not intents:
        return (
            f"{head}\nNo summary has been written for this session yet (one is written "
            "at turn end once the session has enough turns). Use session_read_message "
            "instead."
        )
    when = _summary_time(resp.get("generated_at"))
    stale = " — STALE: newer turns exist." if resp.get("stale") else "."
    lines = [head, f"Summary written {when}{stale}"]
    for intent in intents:
        lines.append(f"- [{intent.get('state') or '?'}] {intent.get('title', '')}")
        lines.extend(f"    progress: {p}" for p in intent.get("progress") or [])
        if intent.get("progress_omitted"):
            lines.append(f"    ({intent['progress_omitted']} earlier progress item(s) not shown)")
        lines.extend(f"    next: {st}" for st in intent.get("next_steps") or [])
        if intent.get("next_steps_omitted"):
            lines.append(f"    ({intent['next_steps_omitted']} more next step(s) not shown)")
    if resp.get("intents_omitted"):
        lines.append(f"({resp['intents_omitted']} older intent(s) not shown.)")
    notes = resp.get("constraints") or []
    if notes:
        lines.append("Project notes:")
        lines.extend(f"- {n}" for n in notes)
    if resp.get("constraints_omitted"):
        lines.append(f"({resp['constraints_omitted']} more project note(s) not shown.)")
    return "\n".join(lines)


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


def _run_session_create(args: dict[str, Any], ctx: ToolContext) -> str:
    payload: dict[str, Any] = {"title": args.get("title", ""), "agent": args.get("agent", "")}
    if args.get("model"):
        payload["model"] = args["model"]

    def _preflight_create(deepest_id: str) -> str | None:
        # The same create, as a dry run, against the folder the new path
        # segments would hang from. Its refusal is the real create's.
        probe = {**payload, "dry_run": True}
        if deepest_id:
            probe["folder_id"] = deepest_id
        try:
            ctx.client.post("/api/session-control/create", probe, session_key=ctx.caller_key)
        except DashboardError as refused:
            return redact(
                f"Error: could not create a session: {refused.error} " "(no folder was created)"
            )
        return None

    fld_id, folder_label, made_note, fld_err = _resolve_folder_for_new_session(
        ctx,
        str(args.get("folder") or ""),
        "filing a new session at creation",
        preflight=_preflight_create,
    )
    if fld_err:
        return fld_err
    if fld_id:
        payload["folder_id"] = fld_id
    try:
        resp = ctx.client.post(
            "/api/session-control/create",
            payload,
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: could not create a session: {refused.error}{made_note}")
    filed = f" filed in `{folder_label}`" if folder_label else ""
    on_model = f" on model `{resp['model']}`" if resp.get("model") else ""
    return redact(
        f"\U0001f195 Opened `{resp.get('target')}` ({resp.get('title')}){filed}{on_model}.{made_note} "
        "It is empty and waiting in the user's sidebar; watch it with "
        "session_read_message."
    )


def _run_session_fork(args: dict[str, Any], ctx: ToolContext) -> str:
    payload = {"source": args.get("source", ""), "title": args.get("title", "")}
    if args.get("at_message_index") is not None:
        payload["at_message_index"] = args["at_message_index"]
    fld_id, folder_label, made_note, fld_err = _resolve_folder_for_new_session(
        ctx, str(args.get("folder") or ""), "filing a forked session at creation"
    )
    if fld_err:
        return fld_err
    if fld_id:
        payload["folder_id"] = fld_id
    try:
        resp = ctx.client.post(
            "/api/session-control/fork",
            payload,
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: could not fork the session: {refused.error}{made_note}")
    filed = f" filed in `{folder_label}`" if folder_label else ""
    return redact(
        f"\U0001f500 Forked `{resp.get('source')}` into `{resp.get('target')}` "
        f"({resp.get('title')}) carrying {resp.get('messages')} message(s){filed}."
        f"{made_note} It is idle and waiting in the user's sidebar; seed it with "
        "session_send and watch it with session_read_message."
    )


def _run_session_stop(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/stop",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not stop that session: {refused.error}"
    target = resp.get("target", args["target"])
    info = resp.get("info")
    if info:
        # Two different facts share this reply and must not read alike. A
        # target that was never running has nothing to stop; one whose
        # cooperative cancel is still in flight IS stopping, and a re-sent
        # stop lands there routinely — telling that caller "nothing to
        # stop" would report the opposite of what happened and invite it to
        # act as though the target were still free-running.
        if resp.get("already_stopping"):
            return f"\u2139\ufe0f `{target}`: {info} — the earlier stop still stands."
        return f"\u2139\ufe0f `{target}`: {info} — nothing to stop."
    return f"\U0001f6d1 Stop sent to `{target}`. Its transcript now shows the stop card."


def _run_session_end_wait(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/end-wait",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not end that session's wait: {refused.error}"
    target = resp.get("target", args["target"])
    if not resp.get("ended"):
        info = resp.get("info") or "not sleeping in the wait tool"
        return f"\u2139\ufe0f `{target}`: {info}. Nothing to end."
    return (
        f"\u23f0 End-wait sent to `{target}`. Its wait returns on the next "
        "keepalive ping (within about 5s) and the turn continues."
    )


def _run_session_retry(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/retry",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not retry that session's turn: {refused.error}"
    target = resp.get("target", args["target"])
    return redact(
        f"\U0001f501 Retry started in `{target}`. Its transcript shows the resume row; "
        "read it with session_read_message."
    )


def _run_session_set_model(args: dict[str, Any], ctx: ToolContext) -> str:
    payload = {"target": args["target"]}
    for key in ("model", "reasoning_effort"):
        if key in args:
            payload[key] = args[key]
    try:
        resp = ctx.client.post(
            "/api/session-control/set-model",
            payload,
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not change that session's model: {refused.error}"
    target = resp.get("target", args["target"])
    effort = resp.get("reasoning_effort")
    effort_text = f"reasoning effort `{effort}`"
    if "model" in resp:
        change = f"`{resp.get('model') or 'auto'}`"
        if effort is not None:
            change += f" at {effort_text}"
    else:
        change = effort_text
    return redact(f"\U0001f501 `{target}` will switch to {change} when its next turn starts.")


def _run_session_reload(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/reload",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        resp = refused.body
    # Read off the code before any error: the process WAS torn down and only the
    # notice was skipped, so "could not reload" would tell the agent nothing
    # happened.
    if resp.get("code") == "target_changed_during_reload":
        return (
            "Warning: the target's agent process was reset and starts again on "
            f"its next message, but no reload notice was added: {resp.get('error', '')}"
        )
    if resp.get("error"):
        return f"Error: could not reload that session: {resp['error']}"
    target = resp.get("target", args["target"])
    if resp.get("warning"):
        return redact(
            f"\U0001f504 `{target}` is relaunching its agent process with the conversation "
            f"kept, but the old process's teardown reported an error ({resp['warning']})."
        )
    return redact(
        f"\U0001f504 `{target}` is relaunching its agent process with the conversation "
        "kept. Its transcript shows the reload notice."
    )


def _run_session_close(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/close",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not close that session: {refused.error}"
    target = resp.get("target", args["target"])
    return (
        f"\U0001f5d1\ufe0f Closed `{target}` — the tab is dismissed and the "
        "conversation archived to history (it can be reopened later)."
    )


def _run_session_revive(args: dict[str, Any], ctx: ToolContext) -> str:
    payload_r: dict[str, Any] = {"target": args["target"]}
    fld_id, folder_label, made_note, fld_error = _resolve_folder_for_new_session(
        ctx, str(args.get("folder") or ""), "filing a revived session"
    )
    if fld_error:
        return fld_error
    if fld_id:
        payload_r["folder_id"] = fld_id
    try:
        resp = ctx.client.post("/api/session-control/revive", payload_r, session_key=ctx.caller_key)
    except DashboardError as refused:
        return redact(f"Error: could not revive that session: {refused.error}{made_note}")
    target = resp.get("target", args["target"])
    filed = f" and filed in `{folder_label}`" if resp.get("filed") and folder_label else ""
    unfiled_note = (
        " (the folder could not be applied; the session keeps its previous placement)"
        if fld_id and not resp.get("filed") and resp.get("folder_id") != fld_id
        else ""
    )
    return redact(
        f"\u267b\ufe0f Revived `{target}` ({resp.get('title')}) with "
        f"{resp.get('messages', 0)} messages{filed}.{unfiled_note}{made_note} It is open and idle "
        "in the user's sidebar; session_send starts its next turn."
    )


def _run_session_send(args: dict[str, Any], ctx: ToolContext) -> str:
    steer = bool(args.get("steer"))
    try:
        resp = ctx.client.post(
            "/api/session-control/send",
            {"target": args["target"], "message": args["message"], "steer": steer},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not send to that session: {refused.error}"
    target = resp.get("target", args["target"])
    if resp.get("steered"):
        return (
            f"\U0001f4e8 Steered `{target}` — your message went into the turn it "
            "is running, so it reads it mid-work. Watch what it does with it "
            "with session_read_message."
        )
    if resp.get("started"):
        return (
            f"\U0001f4e8 Delivered to `{target}` — it started a turn on your message. "
            "Watch the result with session_read_message."
        )
    # A steer that could not be injected lands here, on the queue: say so, or
    # the caller reads "queued" as "the target was busy" and never learns its
    # steer did not cut anything.
    queued_note = (
        " Your steer could not go into the running turn, so it was queued instead." if steer else ""
    )
    return (
        f"\U0001f4e8 Queued for `{target}` — it is mid-turn, so your message runs "
        f"when the current turn ends.{queued_note} Poll with session_read_message."
    )


def _run_session_broadcast(args: dict[str, Any], ctx: ToolContext) -> str:
    payload = {"message": args["message"], "mode": args["mode"]}
    # The VALUE decides, not the key: the validator keeps an explicit JSON
    # `null` as the field's `None` default, so a presence test would send
    # `None` into `list()`. `None` and omission both select the default
    # audience, while an explicitly empty list is still forwarded for the
    # backend to refuse rather than widened to that audience here.
    if args.get("targets") is not None:
        payload["targets"] = list(args["targets"])
    try:
        resp = ctx.client.post(
            "/api/session-control/broadcast",
            payload,
            # The backend delivers SEQUENTIALLY, so one request covers up to
            # `MAX_BROADCAST_TARGETS` deliveries and the 30-second default would
            # expire mid-fleet: the client then reports a failure for a broadcast
            # the server went on to deliver, discarding the per-target report that
            # is this verb's whole contract, and the caller's natural retry
            # delivers the whole message twice.
            #
            # The same per-target allowance the backend ENFORCES per delivery, read
            # from one name so the two cannot drift. Multiplied by the cap because
            # the deliveries are sequential, then given the shared response margin:
            # the client budget must EXCEED the backend's worst-case delivery time,
            # never merely equal it, or the per-target report can still be lost.
            timeout=(
                MAX_BROADCAST_TARGETS * BROADCAST_TARGET_ALLOWANCE_SECS
                + BROADCAST_RESPONSE_MARGIN_SECS
            ),
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not broadcast: {refused.error}"
    rows = resp.get("results") or []
    requested = int(resp.get("requested", len(rows)) or 0)
    delivered = int(resp.get("delivered", 0) or 0)
    mode = str(resp.get("mode", args["mode"]))
    if resp.get("audience_empty"):
        # Not an error, and said plainly: a conductor before its first dispatch
        # is in this state, and "delivered to 0 of 0" reads like a failure.
        return (
            "\U0001f4e3 Nothing to broadcast to — you have not created any "
            "session that is still open. Name `targets` to reach a session you "
            "did not create, or open one with session_create."
        )
    verb = "Steered" if mode == "steer" else "Queued for"
    lines = [f"\U0001f4e3 {verb} {delivered}/{requested} session(s):"]
    for row in rows:
        target = str(row.get("target", ""))
        if not row.get("ok"):
            lines.append(
                f"  \u274c `{target}` — {row.get('error', 'refused')} "
                f"({row.get('code', 'unknown')})"
            )
        elif row.get("steered"):
            lines.append(f"  \u2705 `{target}` — cut into its running turn")
        elif row.get("started"):
            lines.append(f"  \u2705 `{target}` — started a turn on it")
        else:
            # A steer that could not be injected lands here, for the reason
            # session_send spells out: reporting a plain queue would leave the
            # caller believing that target was interrupted.
            fell_back = " (steer fell back to the queue)" if mode == "steer" else ""
            lines.append(f"  \u2705 `{target}` — queued until its turn ends{fell_back}")
    if delivered < requested:
        lines.append(
            "Some targets were not reached — the rows above say which and why. "
            "Nothing retries them for you."
        )
    return redact("\n".join(lines))


def _run_session_status(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.get("/api/session-control/status", session_key=ctx.caller_key)
    except DashboardError as refused:
        return f"Error: could not read your session roster: {refused.error}"
    rows = resp.get("sessions") or []
    tree = str(resp.get("tree", "unreadable"))
    history = str(resp.get("history", "readable"))
    quality_notes: list[str] = []
    if tree == "incomplete":
        quality_notes.append(
            "The crew-log roster read was INCOMPLETE, so this count is a floor: "
            "a session you created may be missing from it."
        )
    elif tree == "unreadable":
        quality_notes.append(
            "The crew-log roster was unreadable (crew log off, or not seeded "
            "yet), so this lists only sessions that are still open — a worker "
            "that was lost would not appear."
        )
    if history == "incomplete":
        quality_notes.append(
            "The transcript-metadata roster read was INCOMPLETE, so a session "
            "created before its first crew-log edge may be missing."
        )
    elif history == "unreadable":
        quality_notes.append(
            "The transcript-metadata roster was unreadable, so archived birth "
            "records could not complete this answer."
        )
    # The union cut, caveated under its OWN name. Three states, not two: the
    # field ABSENT (an older backend that does not compute it) says nothing and
    # must not claim a cut, zero says the union was retained whole, and a
    # published value that will not read as a number is a cut whose size is
    # unknown -- which is not the same as no cut and gets its own sentence.
    omitted_raw = resp.get("roster_omitted")
    if omitted_raw is not None:
        try:
            roster_cut = int(omitted_raw)
        except (TypeError, ValueError):
            roster_cut = -1
        if roster_cut > 0:
            quality_notes.append(
                f"{roster_cut} more session(s) you created are NOT listed: the "
                "three rosters' union exceeded this reply's row bound. Name a "
                "session directly to read it."
            )
        elif roster_cut < 0:
            quality_notes.append(
                "Sessions you created may be missing: this reply reported a "
                "roster overflow without a readable count."
            )
    if not rows:
        empty = "\U0001f4cb You have no sessions open or on record."
        if quality_notes:
            empty += " " + " ".join(quality_notes)
        return empty
    status_lines = [f"\U0001f4cb {len(rows)} session(s) you stood up:"]
    for row in rows:
        target = str(row.get("target", ""))
        status = str(row.get("status", ""))
        if status == "closed":
            closed_at = row.get("closed_at")
            when = f" at {_closed_at_display(closed_at)}" if closed_at else ""
            status_lines.append(
                f"  \u2705 `{target}` — closed (finished and closed its tab{when}; "
                "nothing to re-dispatch)"
            )
            continue
        if status == "lost":
            status_lines.append(
                f"  \U0001faa6 `{target}` — lost (the crew log has it, the "
                "dashboard does not, and it did not close cleanly — a turn was "
                "open, a recycle or destroy tore it down, or the lifecycle could "
                "not be read: re-dispatch it)"
            )
            continue
        title = str(row.get("title", ""))
        depth = int(row.get("queue_depth", 0) or 0)
        queued = f", {depth} queued" if depth else ""
        if status == "unknown":
            # Its OWN mark, not the glyph map's default. Falling through to that
            # default draws idle's sleep glyph -- "open and doing nothing", the
            # one status that means no decision is needed -- for a row whose
            # actual meaning is the opposite: nothing here knows whether this
            # session finished or was lost.
            status_lines.append(
                f"  \u2753 `{target}` ({title}) — unknown (you created it; "
                "neither a live session nor the crew log accounts for it)"
            )
            continue
        mark = {"working": "\U0001f503", "queued": "\u23f8\ufe0f"}.get(status, "\U0001f4a4")
        if row.get("pending_approval"):
            status_lines.append(
                f"  \u270b `{target}` ({title}) — {status}, "
                f"{_pending_approval_phrase(row)}{queued}"
            )
            continue
        status_lines.append(f"  {mark} `{target}` ({title}) — {status}{queued}")
    status_lines.extend(quality_notes)
    return redact("\n".join(status_lines))


def _run_session_adopt(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/adopt",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not adopt that session: {refused.error}"
    target = resp.get("target", args["target"])
    previous = resp.get("previous_parent") or ""
    took_over = (
        f" It was under `{previous}` before, and that is recorded."
        if previous
        else " It was a root before."
    )
    return (
        f"\U0001f91d Adopted `{target}` — the sidebar now nests it under this "
        f"session, along with anything it opened.{took_over}"
    )


def _run_session_release(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/release",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not release that session: {refused.error}"
    target = resp.get("target", args["target"])
    previous = resp.get("previous_parent") or ""
    return (
        f"\U0001f513 Released `{target}` from `{previous}` — it stands on its own "
        "in the sidebar again, keeping whatever it opened under itself."
    )


def _pending_approval_phrase(resp: dict[str, Any]) -> str:
    """The state words for a session parked on a tool approval."""
    tool = str(resp.get("pending_approval_tool") or "")
    return "waiting on a tool approval" + (f": {redact(tool)}" if tool else "")


def _run_session_read_message(args: dict[str, Any], ctx: ToolContext) -> str:
    query = f"target={quote(str(args['target']))}&limit={args.get('limit', 20)}"
    if args.get("since") is not None:
        query += f"&since={int(args['since'])}"
    try:
        resp = ctx.client.get(f"/api/session-control/read?{query}", session_key=ctx.caller_key)
    except DashboardError as refused:
        return f"Error: could not read that session: {refused.error}"
    msg_rows = resp.get("messages") or []
    state_line = "still working" if resp.get("running") else "idle"
    if resp.get("pending_approval"):
        state_line = _pending_approval_phrase(resp)
    queued = resp.get("queue_depth", 0)
    if queued:
        state_line += f", {queued} message(s) queued"
    if resp.get("model"):
        state_line += f", model {redact(str(resp['model']))}"
    if resp.get("pending_model"):
        state_line += f", pending model {redact(str(resp['pending_model']))} for its next turn"
    if resp.get("reasoning_effort"):
        state_line += f", reasoning effort {redact(str(resp['reasoning_effort']))}"
    if resp.get("pending_reasoning_effort"):
        pending_effort = redact(str(resp["pending_reasoning_effort"]))
        state_line += f", pending reasoning effort {pending_effort} for its next turn"
    head_line = (
        f"\U0001f4d6 `{resp.get('target', '')}` — {resp.get('title', '')} "
        f"({state_line}; total={resp.get('total', 0)})"
    )
    if not msg_rows:
        # The cursor rides along even with nothing to show. A poll loop's most
        # common answer is an empty window, and a caller left without a
        # position either re-reads without `since` -- taking the tail, which
        # silently skips everything older than the last `limit` rows once the
        # target answers in a burst -- or keeps reusing a cursor from before,
        # re-reading rows it has already seen. The server now returns
        # `next_since` on trimmed sessions too (positions are based on a
        # durable-only prefix count), so its absence is only a defensive
        # possibility here, not a live server state.
        empty_lines = [head_line, "No messages in that window yet."]
        if "next_since" in resp:
            empty_lines.append(
                f"Pass since={resp['next_since']} on your next read to resume from here."
            )
        return "\n".join(empty_lines)
    read_lines = [head_line]
    for row in msg_rows:
        text_body = str(row.get("content", ""))
        if row.get("truncated"):
            text_body += " …[truncated]"
        read_lines.append(f"[{row.get('index')}] {row.get('role')}: {text_body}")
    read_lines.append(
        (
            f"Pass since={resp['next_since']} on your next read to see only what "
            f"is new. (total={resp.get('total', 0)} is the backlog depth — when it "
            f"exceeds next_since there are older rows this window did not reach, so "
            f"read again immediately rather than waiting.)"
        )
        if "next_since" in resp
        else (
            "No cursor came back with this read. "
            "Read again without `since` to get the latest messages."
        )
    )
    return "\n".join(read_lines)


def _run_session_summary(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.get(
            f"/api/session-control/summary?target={quote(str(args['target']))}",
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not read that session's summary: {refused.error}"
    return _render_session_summary(resp)


_QUEUE_FROM_LABELS = {"yours": "yours ", "person": "person", "other": "other "}


def _render_session_queue(resp: dict[str, Any]) -> str:
    """Render a ``/api/session-control/queue`` body as compact text.

    The route has already redacted and cut every excerpt; this only formats the
    listing and leads with what the action did.
    """
    state_line = "still working" if resp.get("running") else "idle"
    count = int(resp.get("count") or 0)
    # The title is editable text too, so it is collapsed like the entry rows below.
    target_text = " ".join(str(resp.get("target") or "").split())
    title_text = " ".join(str(resp.get("title") or "").split())
    head = f"\U0001f4e5 `{target_text}` — {title_text} ({state_line}), {count} queued"
    lines: list[str] = []
    if resp.get("cancelled"):
        lines.append(f"Cancelled queued entry {resp['cancelled']}.")
    if resp.get("moved"):
        lines.append(f"Moved queued entry {resp['moved']} to position {resp.get('position')}.")
    lines.append(head)
    entries = [e for e in (resp.get("entries") or []) if isinstance(e, dict)]
    if not entries:
        lines.append("The queue is empty.")
    for e in entries:
        label = _QUEUE_FROM_LABELS.get(str(e.get("from")), "other ")
        # Every whitespace run, CR, CRLF and U+2028 included, becomes one space: an
        # entry's text is untrusted, and a line break in it would render as an extra
        # listing row with a forged position, id and owner label.
        excerpt = " ".join(str(e.get("excerpt") or "").split())
        entry_id = " ".join(str(e.get("id") or "").split())
        lines.append(f"{e.get('position')}  {entry_id}  {label}  \"{excerpt}\"")
    if resp.get("omitted"):
        lines.append(f"({resp['omitted']} more queued entries not shown.)")
    return "\n".join(lines)


def _run_session_queue(args: dict[str, Any], ctx: ToolContext) -> str:
    action = args.get("action") or "list"
    body: dict[str, Any] = {"target": args["target"], "action": action}
    if args.get("entry") is not None:
        body["entry"] = args["entry"]
    if args.get("position") is not None:
        body["position"] = args["position"]
    try:
        resp = ctx.client.post("/api/session-control/queue", body, session_key=ctx.caller_key)
    except DashboardError as refused:
        return f"Error: could not {action} that session's queue: {refused.error}"
    return _render_session_queue(resp)


def _run_chat_folder_tree(args: dict[str, Any], ctx: ToolContext) -> str:
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    chat_slots, slots_err = _visible_chat_slots(ctx)
    if slots_err:
        return f"Error: {slots_err}"
    # Read AFTER the two rows reads, which are the ones that can legitimately
    # refuse (a filtered caller, a gone slot). A failed mode read does not
    # abort the listing -- the header says the order is assumed instead -- so
    # the tree stays readable when only its ordering is in doubt.
    sort_mode, mode_err = _chat_folder_sort_mode()
    tree_paths = _chat_folder_paths(chat_folders)
    # Group live sessions by folder up front so an id with no folder row left
    # (a slot pointing at a deleted folder) still surfaces under
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
        f"{'' if len(chat_slots) == 1 else 's'} (folder order: {sort_mode}"
        f"{'' if not mode_err else ', assumed — could not read the dashboard settings: ' + mode_err}):"
    ]
    if sort_mode != FOLDER_SORT_DEFAULT:
        # The listing below is what the person sees, but a POSITION is a
        # stored-order concept: in a name or created sort the before/after
        # anchors chat_folder_move takes still write the stored (custom)
        # position, which this view does not display. Say so up front, or an
        # agent will "move A after B", re-read the tree, and see nothing move.
        tree_lines.append(
            f"Folders are sorted by {sort_mode}, so a before/after anchor passed "
            "to chat_folder_move sets the stored (custom) position without "
            "changing the order shown here; it becomes visible when the person "
            "switches the sidebar back to the custom folder order."
        )
    if sort_mode == "created":
        # A folder from before the ``created_at`` stamp existed has none, so
        # the created key puts it after every stamped row, in the stored order
        # -- the same rule the sidebar's ``folderCreated`` applies, and the
        # sidebar's menu says the same thing under its rows. An agent reading
        # a tree whose tail is in stored order must not take that tail for a
        # date order.
        unstamped = sum(1 for f in chat_folders if _chat_folder_created(f) is None)
        if unstamped:
            tree_lines.append(
                f"{unstamped} folder{'' if unstamped == 1 else 's'} with no created_at "
                "(made before the stamp existed) list last, in the stored (custom) "
                "order, not by date."
            )
    # Sidebar ORDER, not alphabetical (unless the person's mode IS by name).
    # This tool is how an agent reads the tree before repositioning a folder,
    # so listing it in any order the sidebar does not draw would show a
    # sequence the person never sees and make `before`/`after` a guess.
    for fid, depth in _chat_folder_render_order(chat_folders, sort_mode):
        fpath = tree_paths.get(fid, "?")
        row = next((f for f in chat_folders if str(f.get("id")) == fid), {})
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


def _run_chat_folder_create(args: dict[str, Any], ctx: ToolContext) -> str:
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "creating a folder")
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
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    # mkdir -p over the parent path: resolve as far as the tree already
    # goes, then create each missing segment.
    parent_id, created_segments, parent_err = _ensure_chat_folder_path(
        str(args.get("parent") or ""), chat_folders, client=ctx.client, session_key=caller_key
    )
    made_note = f" (created parent path: {'/'.join(created_segments)})" if created_segments else ""
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
    # The verified key is passed through unchanged: re-resolving inside the
    # client would let the write carry a different session's authority than
    # the one the gate checked, and the endpoint's ownership rule is only as
    # good as the identity that reaches it.
    try:
        d = ctx.client.post("/api/chat/folders", body, session_key=caller_key)
    except DashboardError as refused:
        return redact(f"Error: {refused.error}{made_note}")
    chat_folders.append(d)
    new_id = str(d.get("id") or "?")
    new_path = _chat_folder_paths(chat_folders).get(new_id) or str(d.get("name") or "?")
    return redact(f"Created folder `{new_path}` (id={new_id}).{made_note}")


def _run_chat_folder_move(args: dict[str, Any], ctx: ToolContext) -> str:
    caller_key, caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "moving a folder")
    if gate:
        return gate
    before_ref = str(args.get("before") or "").strip()
    after_ref = str(args.get("after") or "").strip()
    if before_ref and after_ref:
        return "Error: pass `before` or `after`, not both — one anchor names one position."
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    fld_id, fld_err = _resolve_chat_folder_id(args["folder"], chat_folders)
    if fld_err:
        return f"Error: {fld_err}"
    if not fld_id:
        return "Error: 'root' is not a folder — name the folder to move."
    anchor_ref = before_ref or after_ref
    anchor_id = ""
    if anchor_ref:
        anchor_id, anchor_err = _resolve_chat_folder_id(anchor_ref, chat_folders)
        if anchor_err:
            return f"Error: {anchor_err}"
        if not anchor_id:
            return (
                "Error: 'root' is not a folder — `before`/`after` names a "
                "SIBLING folder to sit next to."
            )
        if anchor_id == fld_id:
            return "Error: a folder cannot be positioned relative to itself."
    anchor_row = next((f for f in chat_folders if str(f.get("id")) == anchor_id), {})
    anchor_parent = str(anchor_row.get("parent_id") or "")
    current_parent = str(
        next((f for f in chat_folders if str(f.get("id")) == fld_id), {}).get("parent_id") or ""
    )
    if anchor_id and "new_parent" not in args:
        # An anchor already fixes the sibling set, so "put X after Y" needs no
        # second reference to the parent Y names — and demanding one would make
        # repositioning INSIDE a folder impossible to express, since an omitted
        # ``new_parent`` means the top level.
        dest_id = anchor_parent
    else:
        dest_id, dest_err = _resolve_chat_folder_id(args.get("new_parent") or "", chat_folders)
        if dest_err:
            return f"Error: {dest_err}"
        if anchor_id and anchor_parent != dest_id:
            return (
                "Error: the anchor is not in the destination — `before`/`after` "
                "names a SIBLING, so it must already sit directly under "
                "`new_parent`. Omit `new_parent` to let the anchor choose the "
                "parent."
            )

    # Placing a folder is ONE write whenever the store already has a free
    # integer slot at that position — before the first sibling, after the last,
    # or in a gap. Only when the two neighbours are adjacent integers, which is
    # what a sidebar drag leaves behind, do the siblings have to be renumbered;
    # that renumber is contiguous 0..n-1, the same shape a drag writes, so the
    # two paths leave one convention rather than two.
    #
    # The distinction is worth the branch because several writes cannot be made
    # atomic from here: the endpoint takes one row at a time. The one-write case
    # therefore cannot land half-applied at all, and the renumber is reserved
    # for the positions that genuinely need it.
    order_writes: list[tuple[str, int]] = []
    if anchor_id:
        siblings = [
            f for f in _chat_folder_siblings(chat_folders, dest_id) if str(f.get("id")) != fld_id
        ]
        slot = next((i for i, f in enumerate(siblings) if str(f.get("id")) == anchor_id), -1)
        if slot < 0:
            return "Error: the anchor folder is no longer where it was — re-read the tree."
        moving = next(f for f in chat_folders if str(f.get("id")) == fld_id)
        index = slot if before_ref else slot + 1
        free = _free_slot_order(siblings, index)
        if free is not None:
            # Skip a write that would store the value the row already carries.
            order_writes = [] if _chat_folder_order(moving) == free else [(fld_id, free)]
        else:
            placed = siblings[:index] + [moving] + siblings[index:]
            order_writes = [
                (str(f.get("id")), i) for i, f in enumerate(placed) if _chat_folder_order(f) != i
            ]
        # The moved folder itself must be owned -- the ONE ownership clause the
        # write paths cannot re-derive, so it stays in the tool. Positioning is
        # RELATIVE: renumbering the app's OWN siblings around a folder changes
        # where that folder renders WITHOUT writing to it (when its own order is
        # unchanged, `own_pos` is None and no PATCH names it). A caller could
        # then reposition a folder it does not own by writing only rows it does,
        # and every write the reorder endpoint sees would be legitimately owned,
        # leaving nothing for it to refuse. The sibling-row and subtree clauses
        # of the old predicate genuinely moved to the write paths (the reorder
        # endpoint re-authorizes each row AND its subtree under the lock); only
        # this moved-folder clause has no write to hang off, so it is checked
        # here, before any write, exactly as the base did. A plain reparent is
        # not gated here because it always writes to the moved folder, so the
        # PATCH endpoint's own ownership check refuses it.
        if caller_app:
            moving_owner = _folder_owner_app(
                next((f for f in chat_folders if str(f.get("id")) == fld_id), {})
            )
            if moving_owner != caller_app:
                return (
                    "Error: this app does not own the folder it is positioning, "
                    "so the position is refused. An app may reorder only its own "
                    "folders; ask the person to set the order of theirs."
                )
            # And its SUBTREE, but ONLY when no write names the moved folder.
            # Positioning takes the descendants with it, so an app repositioning
            # a folder it owns whose subtree holds the person's relocates theirs.
            # When a write DOES name the moved folder (a free-slot order PATCH, or
            # a reparent), the endpoint's own subtree guard fires on that write --
            # so the tool must not pre-empt it there. The uncovered case is the
            # relative renumber: the moved folder's own order is unchanged, so no
            # write names it, and neither write path sees a row to refuse. That is
            # the one the tool must catch, exactly as the base's moved-folder
            # subtree clause did.
            moved_is_written = any(sid == fld_id for sid, _pos in order_writes)
            if not moved_is_written and _subtree_holds_foreign_folder(
                chat_folders, root_id=fld_id, request_app=caller_app
            ):
                return (
                    "Error: this folder contains folders this app does not own, "
                    "and positioning it moves everything inside it, so the "
                    "position is refused. Ask the person to set the order."
                )

    # The moved folder's own position rides along with the reparent: one write
    # for the row this call is about, so the common case stays a single request.
    #
    # ``parent_id`` is omitted when the parent is NOT changing. The endpoint
    # treats its presence as a reparent and applies the reparent-only rule that
    # a subtree holding a folder the caller does not own cannot be moved — so
    # sending the current parent back would make an app's pure reposition fail
    # on a guard about a move that is not happening.
    move_body: dict[str, Any] = {}
    if current_parent != dest_id:
        move_body["parent_id"] = dest_id
    own_pos = next((pos for fid, pos in order_writes if fid == fld_id), None)
    # A renumber writes several sibling rows, and those cannot be made atomic
    # one PATCH at a time: a refusal partway would leave the person's sidebar
    # in an order nobody chose. So the moved folder's own position folds into
    # the reparent PATCH ONLY when it is the single row to write (a free slot
    # existed); when siblings must be renumbered too, the whole set -- moved
    # folder included -- goes through the atomic reorder endpoint below, and
    # the reparent PATCH carries parent_id alone.
    sibling_writes = [(sid, pos) for sid, pos in order_writes if sid != fld_id]
    if own_pos is not None and not sibling_writes:
        move_body["order"] = own_pos
    # A renumber goes through the atomic reorder endpoint, which refuses the
    # whole batch if any named row (or its subtree) is not the app's and leaves
    # the stored order untouched -- so a same-parent reposition needs no
    # tool-layer pre-check: the endpoint's atomic refusal is complete and no
    # write can strand.
    #
    # The one case that IS exposed is a cross-parent move whose renumber also
    # needs the reorder: the reparent PATCH commits first (parent_id below),
    # THEN the reorder can reject, leaving the folder reparented but
    # unpositioned. For that case only, preflight the endpoint's own per-row
    # predicate over the whole batch before the reparent commits, so a batch
    # that would be refused writes nothing at all. This adds no refusal a
    # legitimate call would not already hit at the endpoint; it only moves the
    # already-certain refusal ahead of the reparent. Reproduces the reorder
    # endpoint's check (`chat_folders.api_chat_folder_reorder._apply`): row
    # owned by the app AND its subtree holds no foreign folder.
    if caller_app and sibling_writes and "parent_id" in move_body:
        for sid, _pos in order_writes:
            row = next((f for f in chat_folders if str(f.get("id")) == sid), {})
            if _folder_owner_app(row) != caller_app:
                return (
                    "Error: this move would renumber a folder this app does not "
                    "own, so it is refused before anything is moved. An app may "
                    "reorder only its own folders; ask the person to set the "
                    "order of theirs."
                )
            if _subtree_holds_foreign_folder(chat_folders, root_id=sid, request_app=caller_app):
                return (
                    "Error: this move would reposition a folder whose subtree "
                    "holds folders this app does not own, so it is refused "
                    "before anything is moved. Ask the person to reorder theirs."
                )
    if move_body:
        try:
            d = ctx.client.patch(f"/api/chat/folders/{fld_id}", move_body, session_key=caller_key)
        except DashboardError as refused:
            # The endpoint owns the cycle guard (a folder cannot move into its
            # own descendant) — surface its verdict rather than re-deriving it.
            return f"Error: {refused.error}"
    else:
        # Nothing to write for this row: it is already in the destination and
        # already holds the position asked for.
        d = next((f for f in chat_folders if str(f.get("id")) == fld_id), {})
    moved: list[dict] = [f for f in chat_folders if str(f.get("id")) != fld_id]
    moved.append({**d, "id": fld_id})
    dest_path = _chat_folder_paths(moved).get(fld_id) or "(top level)"
    if sibling_writes:
        # The renumber, in ONE atomic request. The endpoint applies the whole
        # list under the folder-store lock, all-or-none, re-validating this
        # app's ownership of EVERY row inside that lock the way a single PATCH
        # does -- so ownership lives with the lock-holder rather than a
        # tool-layer pre-check here, and a refusal leaves the stored order
        # untouched instead of half-applied. The moved folder's own order
        # joins the batch here (it is not folded into the reparent PATCH
        # above), so its position relative to the renumbered siblings lands
        # in the same transaction.
        reorder_body = [{"id": sid, "order": pos} for sid, pos in order_writes]
        try:
            ctx.client.post(
                "/api/chat/folders/reorder",
                # Every row in the batch lives in the destination container by
                # this point (the reparent PATCH above has landed), and the
                # batch was computed from a snapshot -- so the claim lets the
                # endpoint refuse the renumber if a concurrent reparent moved a
                # sibling between that read and this write, instead of landing
                # an index computed for a container the row has left.
                {"orders": reorder_body, "expected_parent": dest_id},
                session_key=caller_key,
            )
        except DashboardError as refused:
            # The reparent (if any) landed and is not in doubt; the ordering
            # did not -- and, being atomic, left the stored order untouched
            # rather than partway. Say which half held so the caller can
            # re-run to finish, matching the success wording's move/reposition
            # split.
            landed = (
                f"Repositioned folder (id={fld_id})"
                if current_parent == dest_id
                else f"Moved folder (id={fld_id}) to `{dest_path}`"
            )
            return redact(
                f"{landed}, but ordering was refused: {refused.error}. "
                "The stored order is unchanged. Re-run the same call to finish "
                "positioning it."
            )
    if anchor_id:
        side = "before" if before_ref else "after"
        anchor_path = _chat_folder_paths(chat_folders).get(anchor_id) or anchor_id
        if current_parent == dest_id:
            # Nothing moved, so saying "moved to <the folder's own path>" would
            # describe a reparent that did not happen. Name what changed.
            parent_label = _chat_folder_paths(chat_folders).get(dest_id) or "(top level)"
            return redact(
                f"Repositioned folder (id={fld_id}) {side} `{anchor_path}` " f"in `{parent_label}`."
            )
        return redact(
            f"Moved folder (id={fld_id}) to `{dest_path}`, positioned {side} " f"`{anchor_path}`."
        )
    return redact(f"Moved folder (id={fld_id}) to `{dest_path}`.")


def _run_chat_folder_update(args: dict[str, Any], ctx: ToolContext) -> str:
    # The body is built from these three fields only, so no other folder field
    # can reach the PATCH even if the schema later grows one. The schema does
    # not list the keep-off fields, so validation refuses them as unknown.
    folder_changes: dict[str, Any] = {}
    # Test the VALUE, not the key: the schema keeps an explicit JSON null as
    # ``None``, and ``str(None)`` would rename the folder to "None".
    if args.get("name") is not None:
        # Same rules as chat_folder_create: no '/', redact before the write, and
        # check the stored (redacted) length, because the endpoint keeps
        # ``name[:100]`` and a truncated name no later path lookup matches.
        if "/" in str(args["name"]):
            return (
                "Error: a folder name cannot contain '/' — it would render "
                "identically to a nested path and become unaddressable by path."
            )
        safe_name = redact(str(args["name"])).strip()
        if not safe_name:
            return "Error: folder name must not be empty"
        if len(safe_name) > _MAX_FOLDER_NAME:
            return (
                f"Error: folder name too long after redaction ({len(safe_name)} "
                f"chars): `{safe_name[:40]}…` — keep it to {_MAX_FOLDER_NAME} "
                "characters or fewer"
            )
        folder_changes["name"] = safe_name
    # Icon and color pass through as given: '' clears, and the endpoint's
    # single-emoji check and palette allowlist are the authority for the rest.
    if args.get("icon") is not None:
        folder_changes["icon"] = str(args["icon"])
    if args.get("color") is not None:
        folder_changes["color"] = str(args["color"]).lower()
    if not folder_changes:
        return "Error: pass at least one of ``name``, ``icon`` or ``color``"
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "updating a folder")
    if gate:
        return gate
    # Channel callers are refused by the endpoint (403 ``channel_reachable_caller``),
    # which sees every channel-shaped key and the channel bindings of a dashboard
    # session; the CHANNEL_AGENT_BLOCKED_TOOLS entry hides the tool at the
    # permission prompt.
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    fld_id, fld_err = _resolve_chat_folder_id(args["folder"], chat_folders)
    if fld_err:
        return f"Error: {fld_err}"
    if not fld_id:
        return "Error: 'root' is not a folder — name the folder to update."
    before = next((f for f in chat_folders if str(f.get("id")) == fld_id), {})
    before_path = _chat_folder_paths(chat_folders).get(fld_id) or str(before.get("name") or fld_id)
    # Ownership (an app or crew member may update only a folder it created) and
    # the sibling-name rule are the endpoint's, decided under the folder-store
    # lock over the whole tree rather than the part this caller can read.
    try:
        ctx.client.patch(
            f"/api/chat/folders/{quote(fld_id, safe='')}", folder_changes, session_key=caller_key
        )
    except DashboardError as refused:
        if refused.code == "folder_name_exists":
            return (
                "Error: a sibling folder already has that name. Two folders "
                "with one name under the same parent cannot be told apart by "
                "path, so the rename is refused."
            )
        if refused.code == "folder_not_owned":
            return (
                "Error: this caller does not own that folder. An app agent or "
                "crew member may update only a folder it created; ask the "
                "person to rename or restyle theirs."
            )
        if refused.code == "channel_reachable_caller":
            # A channel conversation resumed into this dashboard session runs
            # under its ``dashboard:`` key, so only the endpoint's reachability
            # check can see it.
            return (
                "Error: this session is linked to a channel conversation, so its "
                "turns may come from thread text other people wrote and "
                "chat_folder_update is refused. Unlink the channel, or rename "
                "the folder in the sidebar."
            )
        return redact(f"Error: {refused.error}")
    # Report what was written, not the response row: the endpoint returns the
    # folder object it looked up before taking the store lock.
    parts = []
    if "name" in folder_changes:
        parts.append(f"renamed to `{folder_changes['name']}`")
    if "icon" in folder_changes:
        parts.append(f"icon {folder_changes['icon'] or '(default)'}")
    if "color" in folder_changes:
        parts.append(f"color {folder_changes['color'] or '(default)'}")
    return redact(f"Updated folder `{before_path}` (id={fld_id}): {'; '.join(parts)}.")


def _run_chat_folder_move_session(args: dict[str, Any], ctx: ToolContext) -> str:
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    fld_id, fld_err = _resolve_chat_folder_id(args.get("folder") or "", chat_folders)
    if fld_err:
        return f"Error: {fld_err}"
    chat_slots, slots_err = _visible_chat_slots(ctx)
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
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        "Error: cannot verify which session is calling, so this move is "
        "refused — filing another session requires a caller identity the "
        "gateway can vouch for."
    )
    if not caller_key:
        return strict_err
    # The verified key is passed through unchanged: re-resolving inside the
    # client would let the write carry a different session's authority than
    # the one checked here.
    try:
        ctx.client.patch(
            f"/api/chat/slots/{quote(slot_key, safe='')}/folder",
            {"folder_id": fld_id},
            session_key=caller_key,
        )
    except DashboardError as refused:
        return f"Error: {refused.error}"
    if not fld_id:
        return redact(f"Unfiled session `{slot_key}` to the top level.")
    folder_label = _chat_folder_paths(chat_folders).get(fld_id, fld_id)
    return redact(f"Moved session `{slot_key}` into `{folder_label}` (id={fld_id}).")


def _run_chat_folder_delete(args: dict[str, Any], ctx: ToolContext) -> str:
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "deleting a folder")
    if gate:
        return gate
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return f"Error: {folders_err}"
    fld_id, fld_err = _resolve_chat_folder_id(args["folder"], chat_folders)
    if fld_err:
        return redact(f"Error: {fld_err}")
    if not fld_id:
        return "Error: 'root' is not a folder — name the folder to delete."
    fld_path = _chat_folder_paths(chat_folders).get(fld_id) or fld_id
    # Empty-only, decided by the endpoint: ``if_empty`` makes it re-check
    # subfolders and live sessions under the folder-store lock in the same
    # step that removes the row, so nothing filed after this tool's read can
    # be unfiled by the delete. A pre-check here could not give that answer.
    # The refusal text names no session and carries no count.
    try:
        ctx.client.delete(
            f"/api/chat/folders/{quote(fld_id, safe='')}?if_empty=true",
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "folder_not_agent_owned":
            return redact(
                f"Error: folder `{fld_path}` is not deleted: this session did not "
                "create it, or the person has edited or used it since. Leave it "
                "for the person."
            )
        if refused.code == "folder_not_empty":
            return redact(
                f"Error: folder `{fld_path}` is not deleted: {refused.error}. Empty it "
                "first with chat_folder_move / chat_folder_move_session (an "
                "archived session needs session_revive before it can move)."
            )
        return redact(f"Error: {refused.error}")
    return redact(f"Deleted empty folder `{fld_path}` (id={fld_id}).")


def _run_chat_folder_file_self(args: dict[str, Any], ctx: ToolContext) -> str:
    # The destination may not exist yet (mkdir -p, like session_create's
    # ``folder``), and creating folders is tree shaping — so the same gate,
    # not a second authorization path. Its verified key is what every write
    # below carries, per the gate's own contract.
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "filing this session")
    if gate:
        return gate
    rows, rows_err = _get_rows(ctx.client, "/api/chat/slots")
    if rows_err:
        return redact(f"Error: {rows_err}")
    own_slot, own_err = _own_chat_slot(caller_key, rows)
    if own_err:
        return own_err
    own_key = str(own_slot.get("key") or "")
    # The row's ``created`` is the slot's birth stamp, minted once per slot
    # object. It rides along on the PATCH as a generation token so the
    # endpoint refuses (409 ``session_gone``) if this tab closed and its key
    # was recreated for another conversation between this read and the
    # write — the recreated slot shares the ``dashboard:<key>`` transcript
    # key, so the history pin alone would let that write through.
    own_created = str(own_slot.get("created") or "")
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return redact(f"Error: {folders_err}")
    folder_ref = str(args.get("folder") or "")
    made_note = ""
    fld_id = ""
    if folder_ref and folder_ref != "root":
        fld_id, created_segments, fld_err = _ensure_chat_folder_path(
            folder_ref, chat_folders, client=ctx.client, session_key=caller_key
        )
        if created_segments:
            made_note = f" (created folder path: {'/'.join(created_segments)})"
        if fld_err:
            # Segments the walk already created persist and are reported —
            # the same partial-report posture chat_folder_create and
            # session_create take, since folder deletion is deliberately not
            # a capability this server has.
            return redact(f"Error: {fld_err}{made_note}")
    # The target is the CALLER's own slot, resolved above from the verified
    # key — never from an argument — which is what makes this verb safe to
    # grant where chat_folder_move_session is withheld: it can write no
    # placement but its own. ``expected_created`` pins the write to the
    # slot generation resolved above; the endpoint checks it under its lock.
    patch_body: dict[str, str] = {"folder_id": fld_id}
    if own_created:
        patch_body["expected_created"] = own_created
    try:
        ctx.client.patch(
            f"/api/chat/slots/{quote(own_key, safe='')}/folder",
            patch_body,
            session_key=caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: {refused.error}{made_note}")
    if not fld_id:
        return redact(f"Unfiled this session (`{own_key}`) to the top level.{made_note}")
    folder_label = _chat_folder_paths(chat_folders).get(fld_id, fld_id)
    return redact(f"Filed this session (`{own_key}`) in `{folder_label}` (id={fld_id}).{made_note}")


def _run_chat_tag_list(args: dict[str, Any], ctx: ToolContext) -> str:
    # The vocabulary is one shared list of labels with no per-session or
    # per-app content in it — nothing here names a session — so it needs no
    # caller scoping, unlike the slot list every other read here goes through.
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    return redact(_render_chat_tags(tags))


def _run_chat_tag_create(args: dict[str, Any], ctx: ToolContext) -> str:
    # Same gate as the folder writes: it settles whether the caller can be
    # placed at all and returns the verified key the write must carry. The
    # app rule itself — an app-scoped caller may not coin a shared tag —
    # lives in the endpoint (``api_chat_tag_create``), which judges every
    # transport on the middleware's validated claim; restating it here
    # would be a second copy that can only drift.
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "creating a tag")
    if gate:
        return gate
    # Agent-authored name landing in durable, re-rendered state — redact
    # before the write, like folder names. The endpoint stores ``name[:60]``,
    # and redaction can lengthen a string (a credential becomes a marker), so
    # the length is checked on what would be stored: a truncated name is one
    # no later chat_tag_assign name lookup can match.
    safe_name = redact(str(args["name"])).strip()
    if not safe_name:
        return "Error: tag name must not be empty"
    if len(safe_name) > _MAX_TAG_NAME:
        return (
            f"Error: tag name too long after redaction ({len(safe_name)} chars): "
            f"`{safe_name[:40]}…` — keep it to {_MAX_TAG_NAME} characters or fewer"
        )
    tag_body: dict[str, Any] = {"name": safe_name, "status": bool(args.get("status", False))}
    if args.get("color"):
        tag_body["color"] = str(args["color"])
    # The verified key is passed through unchanged, per the gate's contract.
    try:
        d = ctx.client.post("/api/chat/tags", tag_body, session_key=caller_key)
    except DashboardError as refused:
        if refused.code == "app_forbidden":
            return (
                "Error: an app-owned session cannot create a tag — tags are one "
                "shared vocabulary with no per-app owner. Use the tags that already "
                "exist (chat_tag_list)."
            )
        return redact(f"Error: {refused.error}")
    tid = str(d.get("id") or "?")
    got_name = str(d.get("name") or safe_name)
    marker = " (status tag)" if d.get("status") else ""
    if got_name.lower() != safe_name.lower():
        # Cannot happen through the endpoint's own dedup (it matches on the
        # lowered name), but the response is the record: report what exists.
        return redact(f"Tag `{got_name}` (id={tid}){marker} already covers `{safe_name}`.")
    return redact(f"Tag `{got_name}` (id={tid}, color={d.get('color', '?')}){marker} is available.")


def _run_chat_tag_update(args: dict[str, Any], ctx: ToolContext) -> str:
    changes: dict[str, Any] = {}
    if args.get("name") is not None:
        # Agent-authored name landing in durable state — redact before the
        # write and check the stored length, exactly as chat_tag_create does.
        safe_name = redact(str(args["name"])).strip()
        if not safe_name:
            return "Error: tag name must not be empty"
        if len(safe_name) > _MAX_TAG_NAME:
            return (
                f"Error: tag name too long after redaction ({len(safe_name)} chars): "
                f"`{safe_name[:40]}…` — keep it to {_MAX_TAG_NAME} characters or fewer"
            )
        changes["name"] = safe_name
    if args.get("color"):
        changes["color"] = str(args["color"])
    if args.get("status") is not None:
        changes["status"] = bool(args["status"])
    if not changes:
        return "Error: pass at least one of ``name``, ``color`` or ``status``"
    # Same gate as the other vocabulary write: whether the caller can be
    # placed at all, and the verified key the write must carry. The app
    # rule lives in the endpoint.
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, "updating a tag")
    if gate:
        return gate
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    ids, ref_err = _resolve_chat_tag_ids([str(args["tag"])], tags)
    if ref_err:
        return redact(f"Error: {ref_err}")
    tid = ids[0]
    before = next((t for t in tags if str(t.get("id")) == tid), {})
    try:
        d = ctx.client.patch(
            f"/api/chat/tags/{quote(tid, safe='')}", changes, session_key=caller_key
        )
    except DashboardError as refused:
        if refused.code == "app_forbidden":
            return (
                "Error: an app-owned session cannot change a tag — tags are one "
                "shared vocabulary with no per-app owner."
            )
        return redact(f"Error: {refused.error}")
    parts = []
    if "name" in changes:
        parts.append(f"renamed `{before.get('name', '?')}` → `{d.get('name', changes['name'])}`")
    if "color" in changes:
        parts.append(f"color {before.get('color', '?')} → {d.get('color', changes['color'])}")
    if "status" in changes:
        parts.append(f"status tag: {'yes' if d.get('status') else 'no'}")
    return redact(f"Updated tag `{d.get('name', '?')}` (id={tid}): {'; '.join(parts)}.")


def _run_chat_tag_assign(args: dict[str, Any], ctx: ToolContext) -> str:
    add_refs = [str(x) for x in (args.get("add") or [])]
    remove_refs = [str(x) for x in (args.get("remove") or [])]
    if not add_refs and not remove_refs:
        return "Error: pass at least one tag in ``add`` or ``remove``"
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    add_ids, add_err = _resolve_chat_tag_ids(add_refs, tags)
    if add_err:
        return redact(f"Error: {add_err}")
    remove_ids, remove_err = _resolve_chat_tag_ids(remove_refs, tags)
    if remove_err:
        return redact(f"Error: {remove_err}")
    clash = [t for t in add_ids if t in remove_ids]
    if clash:
        return f"Error: {', '.join(clash)} named in both ``add`` and ``remove``"
    chat_slots, slots_err = _visible_chat_slots(ctx)
    if slots_err:
        return f"Error: {slots_err}"
    slot_key, slot_err = _resolve_chat_slot_key(args["session"], chat_slots)
    if slot_err:
        return redact(f"Error: {slot_err}")
    slot_row = next((s for s in chat_slots if str(s.get("key") or "") == slot_key), {})
    current = [str(t) for t in (slot_row.get("tags") or []) if isinstance(t, str)]
    new_tags = [t for t in current if t not in remove_ids]
    for tid in add_ids:
        if tid not in new_tags:
            new_tags.append(tid)
    # Like chat_folder_move_session, this writes to a session OTHER than the
    # caller's, so identity is resolved STRICTLY and the verified key rides
    # on the write unchanged — see that tool for why the lenient walk is
    # unsafe here.
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        "Error: cannot verify which session is calling, so this tag change is "
        "refused — tagging another session requires a caller identity the "
        "gateway can vouch for."
    )
    if not caller_key:
        return strict_err
    names_by_id = {str(t.get("id") or ""): str(t.get("name") or "?") for t in tags}
    if new_tags == current:
        shown = ", ".join(f"`{names_by_id.get(t, t)}`" for t in current) or "none"
        return redact(f"No change: session `{slot_key}` already carries {shown}.")
    # The revision the list above was composed on. The endpoint applies the
    # write compare-and-set against it, so a tag the person toggles between
    # this read and the PUT is not silently dropped by a wholesale replace —
    # the call fails 409 ``stale_base`` and is retried on the fresh list.
    put_body: dict[str, Any] = {"tags": new_tags}
    base_rev = str(slot_row.get("tags_revision") or "")
    if base_rev:
        put_body["base_tags_revision"] = base_rev
    try:
        d = ctx.client.put(
            f"/api/chat/slots/{quote(slot_key, safe='')}/tags",
            put_body,
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "stale_base":
            return redact(
                f"Error: the tags on `{slot_key}` changed while this call was "
                "composing its delta (someone else toggled a tag). Nothing was "
                "written — call chat_tag_assign again; it re-reads the current list."
            )
        return redact(f"Error: {refused.error}")
    final = [str(t) for t in (d.get("tags") or new_tags) if isinstance(t, str)]
    shown = ", ".join(f"`{names_by_id.get(t, t)}`" for t in final) or "none"
    added = ", ".join(f"`{names_by_id.get(t, t)}`" for t in add_ids if t not in current)
    removed = ", ".join(f"`{names_by_id.get(t, t)}`" for t in remove_ids if t in current)
    parts = []
    if added:
        parts.append(f"added {added}")
    if removed:
        parts.append(f"removed {removed}")
    return redact(f"Session `{slot_key}`: {'; '.join(parts)}. Tags now: {shown}.")


def _run_chat_session_pin(args: dict[str, Any], ctx: ToolContext) -> str:
    want = args["pinned"]
    # Like chat_folder_move_session, this writes to a session OTHER than the
    # caller's, so identity is resolved STRICTLY and the verified key rides
    # on the write unchanged — see that tool for why the lenient walk is
    # unsafe here.
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        "Error: cannot verify which session is calling, so this pin change is "
        "refused — pinning another session requires a caller identity the "
        "gateway can vouch for."
    )
    if not caller_key:
        return strict_err
    # Channel containment is enforced HERE, at dispatch, and not only by the
    # CHANNEL_AGENT_BLOCKED_TOOLS name match: that match runs at the
    # permission prompt, which an auto-approved call never reaches. A
    # channel agent acts on thread text other people wrote, and nothing at
    # the route refuses a ``channel:`` caller, so it is refused before any
    # session is listed.
    if caller_key.startswith("channel:"):
        try:
            sel().log_tool_invocation(
                session_key=caller_key,
                source="mcp",
                tool_name="chat_session_pin",
                tool_kind=SERVER_NAME,
                outcome="rejected_blocked_tool",
            )
        except Exception:
            # Stdio-silent: stderr would corrupt the JSON-RPC stream. The
            # refusal below holds either way.
            pass
        return (
            "Error: chat_session_pin is not available to channel agents — "
            "pinning rearranges the person's sidebar, and a channel agent acts "
            "on thread text other people wrote."
        )
    chat_slots, slots_err = _visible_chat_slots(ctx)
    if slots_err:
        return f"Error: {slots_err}"
    slot_key, slot_err = _resolve_chat_slot_key(args["session"], chat_slots)
    if slot_err:
        return redact(f"Error: {slot_err}")
    verb = "Pinned" if want else "Unpinned"
    slot_row = next((s for s in chat_slots if str(s.get("key") or "") == slot_key), {})
    # No client-side "already in that state" shortcut: the list read above
    # can be stale, so the route decides under its lock, after the
    # generation and ownership re-checks, and reports ``changed``.
    # ``expected_created`` pins the write to the slot generation resolved
    # above, the same token chat_folder_file_self sends; the endpoint
    # checks it under its lock, so a slot key recreated for a different
    # conversation in between is refused instead of pinned.
    pin_body: dict[str, Any] = {"pinned": want}
    slot_created = str(slot_row.get("created") or "")
    if slot_created:
        pin_body["expected_created"] = slot_created
    try:
        d = ctx.client.patch(
            f"/api/chat/slots/{quote(slot_key, safe='')}/pin",
            pin_body,
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "session_gone":
            return redact(
                f"Error: session `{slot_key}` closed or was replaced after it was "
                "resolved. Nothing was written — call chat_folder_tree to see "
                "the current sessions."
            )
        return redact(f"Error: {refused.error}")
    if d.get("changed") is False:
        state_word = "pinned" if want else "not pinned"
        return redact(f"No change: session `{slot_key}` is already {state_word}.")
    return redact(f"{verb} session `{slot_key}`.")


def _run_chat_tag_column_list(args: dict[str, Any], ctx: ToolContext) -> str:
    # Like the tag vocabulary, the board is one shared layout that names no
    # session, so the read needs no caller scoping.
    columns, cols_err = _get_rows(ctx.client, "/api/chat/tag-columns")
    if cols_err:
        return f"Error: {cols_err}"
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    return redact(_render_chat_tag_columns(columns, tags))


def _run_chat_tag_column_create(args: dict[str, Any], ctx: ToolContext) -> str:
    # Agent-authored heading landing in durable, re-rendered state: redact
    # before the write and check the stored length, as chat_tag_create does.
    safe_name = redact(str(args["name"])).strip()
    if not safe_name:
        return "Error: column name must not be empty"
    if len(safe_name) > _MAX_TAG_NAME:
        return (
            f"Error: column name too long after redaction ({len(safe_name)} chars): "
            f"`{safe_name[:40]}…` — keep it to {_MAX_TAG_NAME} characters or fewer"
        )
    # Same gate as the vocabulary writes. The app and crew-member rule lives
    # in the endpoint (``_refuse_vocabulary_write`` in the tag-columns
    # handlers), which judges every transport on the validated claim.
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(
        ctx, "creating a board column"
    )
    if gate:
        return gate
    channel_err = _refuse_channel_board_write("chat_tag_column_create", caller_key)
    if channel_err:
        return channel_err
    tags, tags_err = _get_rows(ctx.client, "/api/chat/tags")
    if tags_err:
        return f"Error: {tags_err}"
    ids, ref_err = _resolve_chat_tag_ids([str(args["tag"])], tags)
    if ref_err:
        return redact(f"Error: {ref_err}")
    tid = ids[0]
    tag_name = next((str(t.get("name") or "?") for t in tags if t.get("id") == tid), tid)
    columns, cols_err = _get_rows(ctx.client, "/api/chat/tag-columns")
    if cols_err:
        return f"Error: {cols_err}"
    # ``ensure`` makes the endpoint return an existing column with this
    # name and tag instead of appending a twin, decided under its write
    # lock, so a retried or racing call converges on one column.
    try:
        d = ctx.client.post(
            "/api/chat/tag-columns",
            {"name": safe_name, "tag_ids": [tid], "mode": "any", "ensure": True},
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "app_forbidden":
            return (
                "Error: an app agent or crew member cannot add a board column — the "
                "board is the person's own layout. Read it with chat_tag_column_list."
            )
        return redact(f"Error: {refused.error}")
    got_id = str(d.get("id") or "?")
    if any(str(c.get("id")) == got_id for c in columns):
        return redact(
            f"Column `{d.get('name', safe_name)}` (id={got_id}) already shows " f"tag `{tag_name}`."
        )
    return redact(
        f"Added column `{d.get('name', safe_name)}` (id={got_id}) showing "
        f"tag `{tag_name}`. Place it with chat_tag_column_move."
    )


def _run_chat_tag_column_move(args: dict[str, Any], ctx: ToolContext) -> str:
    # An empty string is "not given": the schema passes it through as "",
    # and treating it as a reference would resolve the literal "None".
    col_before = args.get("before") or None
    col_after = args.get("after") or None
    if (col_before is None) == (col_after is None):
        return "Error: pass exactly one of ``before`` or ``after``"
    caller_key, _caller_app, gate = _refuse_tree_shaping_if_unverifiable(
        ctx, "moving a board column"
    )
    if gate:
        return gate
    channel_err = _refuse_channel_board_write("chat_tag_column_move", caller_key)
    if channel_err:
        return channel_err
    columns, cols_err = _get_rows(ctx.client, "/api/chat/tag-columns")
    if cols_err:
        return f"Error: {cols_err}"
    move_id, col_err = _resolve_chat_tag_column(str(args["column"]), columns)
    if col_err:
        return redact(f"Error: {col_err}")
    anchor_id, col_err = _resolve_chat_tag_column(str(col_before or col_after), columns)
    if col_err:
        return redact(f"Error: {col_err}")
    if anchor_id == move_id:
        return "Error: a column cannot be placed next to itself"
    base_ids = [str(c.get("id")) for c in columns if isinstance(c.get("id"), str)]
    order = list(base_ids)
    order.remove(move_id)
    at = order.index(anchor_id) + (0 if col_before is not None else 1)
    order.insert(at, move_id)
    names = {str(c.get("id")): str(c.get("name") or "").strip() or "(unnamed)" for c in columns}
    side = "before" if col_before is not None else "after"
    if order == base_ids:
        return redact(
            f"No change: column `{names[move_id]}` is already {side} `{names[anchor_id]}`."
        )
    # ``base_ids`` is the order this call read. The endpoint compares it
    # under its lock and refuses with ``stale_base`` when the board changed
    # in between, so the person's own reorder is never overwritten.
    try:
        ctx.client.put(
            "/api/chat/tag-columns/order",
            {"ids": order, "base_ids": base_ids},
            session_key=caller_key,
        )
    except DashboardError as refused:
        if refused.code == "stale_base":
            return (
                "Error: the board's columns changed while this call was composing "
                "the move. Nothing was written — call chat_tag_column_move again; "
                "it re-reads the current order."
            )
        if refused.code == "app_forbidden":
            return (
                "Error: an app agent or crew member cannot reorder the board — it "
                "is the person's own layout."
            )
        return redact(f"Error: {refused.error}")
    return redact(f"Moved column `{names[move_id]}` {side} `{names[anchor_id]}`.")


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
#: Advertising is not cosmetic. ``mcp_gateway/backend.py`` strips any client-forged
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
