"""Tool declarations of the ``artifacts`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.artifacts`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the artifacts tools."""
    return [
        {
            "name": "artifact_save",
            "description": (
                "Save a chat-rendered artifact (typically the HTML body of an "
                "<mcwidget>) so the user can find, view, and iterate on it later. "
                "Returns the slug — a stable handle the user (and you) can "
                "reference in future sessions ('iterate on artifact <slug>'). "
                "Every <mcwidget> you emit in chat is ALREADY auto-registered as an "
                "artifact (except in an incognito or temporary session), so do not "
                "call this on emitted widget content (it makes a duplicate); the "
                "user's star pins it. Use this for content you never emitted as a "
                "widget. Iterate with artifact_get + artifact_update. An incognito "
                "or temporary session also refuses artifact writes, so there is "
                "nothing to save there."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable name (e.g. 'CR Queue Dashboard'). Used to derive the slug if omitted.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Artifact content. For widgets, the inner HTML of the <mcwidget> tag (NOT the surrounding tag itself).",
                    },
                    "slug": {
                        "type": "string",
                        "description": (
                            "Optional explicit slug (lowercase, digits, hyphens). "
                            "A taken or malformed slug is REFUSED, never renamed — "
                            "call artifact_update on the existing slug to version "
                            "it in place. Omit to derive one from name and let a "
                            "collision resolve by suffixing (-2, -3, ...)."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": [
                            "widget",
                            "html",
                            "markdown",
                            "svg",
                            "json",
                            "text",
                            "webapp",
                            "dashboard",
                        ],
                        "description": (
                            "Artifact kind. Optional — inferred from the content "
                            "when omitted (HTML-ish body -> widget, markdown text "
                            "-> markdown). Pass explicitly to override; markdown "
                            "documents should set kind='markdown'. kind='dashboard' "
                            "is a dynamic dashboard's LAYOUT PACKAGE: content is a "
                            "JSON object carrying exactly these five keys -- kind "
                            "(the string 'dashboard'), bound_to, model, view, theme "
                            "-- and no values anywhere in it, because values stay in "
                            "the crew log and reach the page through the fold. The "
                            "store refuses anything else, naming the rule it broke. "
                            "To put your own page LIVE, use the dashboard_save tool "
                            "instead: it writes this package for you and makes it "
                            "the page your Dashboard tab draws, as a version a "
                            "rollback can undo."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "enum": ["chat", "cron", "subagent", "manual", "import"],
                        "description": "Provenance marker. Default: chat.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description of what the artifact shows or does.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for filtering in the library (max 16).",
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Optional folder to file the artifact in — a folder id "
                            "OR a '/'-separated human path (e.g. 'Reports/Q3'). "
                            "Missing path segments are auto-created (mkdir -p). "
                            "Omit or pass 'root' to leave it unfiled."
                        ),
                    },
                    "webapp_metadata": {
                        "type": "object",
                        "description": (
                            "For kind='webapp' only — metadata for the app-artifact "
                            "control card. Shape: {slug, origin_session, "
                            "deploy_target:{provider,account,region,public_url}, "
                            "architecture, lifecycle, cost, teardown}. "
                            "For draft apps: set lifecycle.status='draft'"
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["name", "content"],
            },
        },
        {
            "name": "artifact_get",
            "description": (
                "Load an artifact by slug. Returns the metadata and content. "
                "Use this before artifact_update to read the current HTML when "
                "the user asks to iterate on an existing artifact."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug (lowercase, digits, hyphens).",
                    },
                    "version": {
                        "type": "integer",
                        "description": "Specific version to read. Omit for current.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_update",
            "description": (
                "Update an artifact's live state. Each agent edit "
                "automatically creates a new version (like a git commit) — "
                "the user can revert to any retained version via "
                "artifact_revert (the newest 50 are kept; see "
                "artifact_versions). Use after artifact_get when iterating "
                "on an existing artifact at the user's request."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to update.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "New content. Each call records a new version "
                            "automatically when invoked via MCP."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "New name (optional rename).",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Replacement tag list (optional). To remove every "
                            "tag, pass clear_tags: true instead of an empty list."
                        ),
                    },
                    "clear_tags": {
                        "type": "boolean",
                        "description": (
                            "true removes every tag. Use it rather than tags: [], "
                            "which some clients drop before it reaches the "
                            "server. Refused beside a non-empty tags."
                        ),
                    },
                    "webapp_metadata": {
                        "type": "object",
                        "description": (
                            "Webapp deployment metadata (optional). Used to "
                            "transition an artifact between draft and live "
                            "deployment states."
                        ),
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_revert",
            "description": (
                "Revert an artifact's live state to a prior version. Reads "
                "version N's content and writes it as the new live state, "
                "creating a fresh snapshot tagged 'reverted' so the activity "
                "timeline shows the rollback. Use this instead of "
                "artifact_update when the user asks to undo recent changes "
                "or restore an earlier state — it avoids the agent having "
                "to manually fetch the old content first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to revert.",
                    },
                    "target_version": {
                        "type": "integer",
                        "description": (
                            "Version number to restore. Use artifact_versions "
                            "first to list available versions."
                        ),
                        "minimum": 1,
                    },
                },
                "required": ["slug", "target_version"],
            },
        },
        {
            "name": "artifact_list",
            "description": (
                "List saved artifacts. Optionally filter by tag, kind, or "
                "name substring. Use this to discover what artifacts exist "
                "before iterating, or when the user asks 'what have we saved?'"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Filter by tag."},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "widget",
                            "html",
                            "markdown",
                            "svg",
                            "json",
                            "text",
                            "webapp",
                            "dashboard",
                        ],
                        "description": "Filter by kind.",
                    },
                    "q": {
                        "type": "string",
                        "description": "Case-insensitive substring filter on artifact name.",
                    },
                },
            },
        },
        {
            "name": "artifact_versions",
            "description": (
                "List the version numbers stored for an artifact. Use this "
                "before artifact_get with an explicit version to figure out "
                "what's available."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_delete",
            "description": (
                "Permanently delete an artifact and all its versions. Use only "
                "when the user explicitly asks to remove an artifact."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to delete.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_get_comments",
            "description": (
                "Get all comments on an artifact (local + provider-synced). "
                "Use to read feedback, review comments, or discussion threads "
                "on an artifact before addressing them. Pass "
                "exclude_resolved=true to skip threads that are already "
                "resolved, so a mid-review read does not hand you back feedback "
                "you have addressed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to get comments for.",
                    },
                    "exclude_resolved": {
                        "type": "boolean",
                        "description": "Omit threads whose root is resolved (default false).",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_post_comment",
            "description": (
                "Post a comment on an artifact. Agent comments are flagged "
                "(is_agent) and SEL-audited. Use scope='shared' to sync to the "
                "provider."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Comment body text.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "private (local only) or shared (syncs to provider).",
                    },
                },
                "required": ["slug", "text"],
            },
        },
        {
            "name": "artifact_reply_comment",
            "description": (
                "Reply to an existing comment thread on an artifact. "
                "If the parent is provider-origin, the reply posts back."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "parent_id": {
                        "type": "string",
                        "description": "ID of the comment to reply to.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Reply body text.",
                    },
                },
                "required": ["slug", "parent_id", "text"],
            },
        },
        {
            "name": "artifact_mark_review",
            "description": (
                "Advance a comment thread to REVIEW status, signaling "
                "the issue is addressed and awaiting human verification. "
                "Agent can mark_review but NEVER resolve."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "comment_id": {
                        "type": "string",
                        "description": "ID of the root comment to advance.",
                    },
                },
                "required": ["slug", "comment_id"],
            },
        },
        {
            "name": "artifact_delete_comment",
            "description": (
                "Delete a comment thread you have demonstrably applied — an "
                "unambiguous directive ('delete this', 'fix typo') that was "
                "fully executed. Root deletes cascade to replies. For "
                "judgment calls the human may want to verify, use "
                "artifact_mark_review instead. Provider-synced comments "
                "cannot be deleted by agents (the tool refuses) — mark those "
                "REVIEW. Deletion is SEL-audited and recorded in the "
                "artifact's activity feed with your reason."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "comment_id": {
                        "type": "string",
                        "description": "ID of the comment to delete (root deletes its replies too).",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One-line justification recorded in the audit log and "
                            "activity feed, e.g. 'applied in v12: deleted the "
                            "flagged paragraph'."
                        ),
                    },
                },
                "required": ["slug", "comment_id", "reason"],
            },
        },
        {
            "name": "artifact_folder_list",
            "description": (
                "List the artifact-library folder tree. Returns one line per "
                "folder with its id, human path and direct item count, sorted by "
                "path. Use to discover folder ids/paths before moving or "
                "organizing artifacts."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "artifact_folder_create",
            "description": (
                "Create an artifact-library folder. ``parent`` accepts a folder id "
                "OR a '/'-separated human path; missing segments are auto-created "
                "(mkdir -p). Omit ``parent`` (or pass 'root') to create at the top "
                "level. Returns the new folder id and canonical path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Folder name (max 100 chars)."},
                    "parent": {
                        "type": "string",
                        "description": "Parent folder id or human path. Omit / 'root' for top level.",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "artifact_folder_rename",
            "description": "Rename an artifact-library folder. ``folder`` = folder id or human path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder id or human path."},
                    "name": {"type": "string", "description": "New name (max 100 chars)."},
                },
                "required": ["folder", "name"],
            },
        },
        {
            "name": "artifact_folder_move",
            "description": (
                "Reparent an artifact-library folder (nest it under another, or move "
                "to the top level). Cycle-guarded — a folder cannot become its own "
                "descendant. ``folder`` and ``new_parent`` are each a folder id or "
                "human path; omit ``new_parent`` (or pass 'root') to move to top level."
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
            "name": "artifact_folder_delete",
            "description": (
                "Delete an artifact-library folder. By default (delete_contents=false) "
                "this is SAFE: the folder's direct child folders and artifacts are "
                "re-parented up to the folder's parent, and only the folder itself is "
                "removed. Pass delete_contents=true to permanently delete the entire "
                "subtree, INCLUDING every descendant artifact — echo the affected "
                "count to the user and get their consent to the permanent deletion "
                "before doing so. ``folder`` = folder id or human path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder id or human path."},
                    "delete_contents": {
                        "type": "boolean",
                        "description": (
                            "false (default) = keep artifacts, re-parent to the folder's "
                            "parent. true = permanently delete the whole subtree."
                        ),
                    },
                },
                "required": ["folder"],
            },
        },
        {
            "name": "artifact_move",
            "description": (
                "Move an existing artifact into a folder (or unfile it). ``folder`` = "
                "a folder id, a '/'-separated human path (missing segments auto-created), "
                "or ''/'root' to unfile. Metadata-only — does not change the artifact's "
                "content or version."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Artifact slug to move."},
                    "folder": {
                        "type": "string",
                        "description": "Destination folder id or human path; ''/'root' to unfile.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "deploy_artifact",
            "description": (
                "Preview a deploy of a webapp artifact or local directory to a "
                "public URL on the user's AWS account. This tool is PREVIEW-ONLY: "
                "it returns scan status and deploy details but never executes. "
                "Final confirmation happens in the dashboard on the Artifact "
                "Deploy page (/deploy) -> Pending confirmations; the Artifacts "
                "page shows its Artifact Deploy button only when the Artifact "
                "Deploy Feature Preview is on, and /deploy works either way. "
                "A pending preview expires 1 hour after it is made; tell the "
                "user the confirm-by time from the reply. "
                "Restricted-session guard and SEL audit apply identically to the "
                "HTTP endpoint. Load the artifact-deploy skill first. A preview "
                "never proves a deployment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "string",
                        "description": "Deploy slot name (e.g. 'my-app').",
                    },
                    "artifact_slug": {
                        "type": "string",
                        "description": (
                            "Slug of an artifact to deploy. A static artifact "
                            "(widget/html/markdown) has its content rendered "
                            "as a page; a kind=webapp artifact deploys its "
                            "app's static root through the same checks as "
                            "local_dir (refused with webapp_root_unavailable "
                            "when that root cannot be resolved). Mutually "
                            "exclusive with local_dir."
                        ),
                    },
                    "local_dir": {
                        "type": "string",
                        "description": (
                            "Validated absolute path to a static directory "
                            "(e.g. fullstack app's public/ root). Mutually "
                            "exclusive with artifact_slug."
                        ),
                    },
                    "profile": {
                        "type": "string",
                        "description": "AWS profile override (default: registry default).",
                    },
                    "ttl_hours": {
                        "type": "integer",
                        "description": "Hours until auto-cleanup, 0-8760 (default: 72; 0 = persistent).",
                    },
                },
                "required": ["site_id"],
            },
        },
    ]
