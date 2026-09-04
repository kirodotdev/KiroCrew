"""The the artifact library: save, iterate, comment, organize, deploy tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from kiro_crew import mcp_core
from kiro_crew.artifacts import _infer_kind
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import (
    ARTIFACT_AGENT_MARKER,
    ARTIFACT_DELETE_COMMENT_SCHEMA,
    ARTIFACT_DELETE_SCHEMA,
    ARTIFACT_FOLDER_CREATE_SCHEMA,
    ARTIFACT_FOLDER_DELETE_SCHEMA,
    ARTIFACT_FOLDER_LIST_SCHEMA,
    ARTIFACT_FOLDER_MOVE_SCHEMA,
    ARTIFACT_FOLDER_RENAME_SCHEMA,
    ARTIFACT_GET_COMMENTS_SCHEMA,
    ARTIFACT_GET_SCHEMA,
    ARTIFACT_LIST_SCHEMA,
    ARTIFACT_MARK_REVIEW_SCHEMA,
    ARTIFACT_MOVE_SCHEMA,
    ARTIFACT_POST_COMMENT_SCHEMA,
    ARTIFACT_REPLY_COMMENT_SCHEMA,
    ARTIFACT_REVERT_SCHEMA,
    ARTIFACT_SAVE_SCHEMA,
    ARTIFACT_UPDATE_SCHEMA,
    ARTIFACT_VERSIONS_SCHEMA,
    validate_tool_args,
)


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
                "Use this when the user asks to save a widget, when you create "
                "something worth keeping, or before iterating (use artifact_update "
                "for the iteration step itself)."
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
                        "enum": ["widget", "html", "markdown", "svg", "json", "text", "webapp"],
                        "description": (
                            "Artifact kind. Optional — inferred from the content "
                            "when omitted (HTML-ish body -> widget, markdown text "
                            "-> markdown). Pass explicitly to override; markdown "
                            "documents should set kind='markdown'."
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
                "the user can revert to any prior agent iteration via "
                "artifact_revert. Use after artifact_get when iterating "
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
                        "description": "Replacement tag list (optional).",
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
                        "enum": ["widget", "html", "markdown", "svg", "json", "text", "webapp"],
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
                "on an artifact before addressing them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to get comments for.",
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
                "List the artifact-library folder tree. Returns each folder's id, "
                "name, parent_id, human path, and direct item_count. Use to "
                "discover folder ids/paths before moving or organizing artifacts."
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
                "count to the user before doing so. ``folder`` = folder id or human path."
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
                "Final confirmation happens in the dashboard Artifact Deploy page. "
                "Restricted-session guard and SEL audit apply identically to the "
                "HTTP endpoint."
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
                            "Slug of a static artifact (widget/html/markdown) "
                            "to deploy — its content is rendered as a page. "
                            "kind=webapp artifacts are rejected (their content "
                            "is an app summary, not deployable HTML — deploy "
                            "the app's built directory via local_dir instead). "
                            "Mutually exclusive with local_dir."
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


def _theme_contrast_hint(flagged: object) -> str:
    """Phrase the MCP-side hint from the gateway's relayed verdict.

    ``flagged`` is the ``theme_contrast_warning`` field the save/update
    handlers stamp on their responses — computed there from the PERSISTED
    content (the request body can be stale for file-promoted saves), so
    this wrapper only converts the relayed boolean into hint text: the
    exact pattern ``slug_collided_with`` uses. Warning only, never a
    reject — same contract as the dedup and cost hints.
    """
    if not flagged:
        return ""
    return (
        "\n\n⚠️  Hardcoded colors, no theme variables: this content renders "
        "inside the dashboard's themed iframe, and literal colors (#hex / "
        "rgb() / hsl()) clash when the user switches between light, dark and "
        "custom themes -- worst when only one half of a foreground/background "
        "pair is set. Prefer the injected theme vars with fallbacks, e.g. "
        "`color:var(--text,#111); background:var(--bg,#fff)` -- the widgets "
        "skill carries the full variable table."
    )


def artifact_save(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)
    save_body: dict[str, Any] = {
        "name": args["name"],
        "content": args["content"],
    }
    for k in ("slug", "kind", "source", "description", "tags", "folder", "webapp_metadata"):
        if k in args and args[k] is not None:
            save_body[k] = args[k]
    # Attribute the save to the calling chat session. Without this the store
    # persists ``session_key=""`` for every agent-authored artifact, so the
    # in-session Artifacts tab — which scopes by session — could never show
    # the very artifacts the agent just created in that session. The header
    # this call already carries only feeds the ``source`` bucket, not the
    # session field, so it has to travel in the body. The handler
    # re-validates it against the session-key grammar, and an unresolvable
    # session (no caller context, no env, no pid file) sends nothing and
    # keeps today's unattributed behavior.
    _origin_sk = mcp_core._resolve_session_key()
    if _origin_sk:
        save_body["origin_session_key"] = _origin_sk
    # Pre-save dedup probe: when saving a chat-source widget, check for
    # an existing widget artifact with the same NFC-normalized name.
    # If one exists we still allow the save (the agent may have a real
    # reason to create a parallel artifact), but we attach a hint so
    # the agent can self-correct on the next turn — typically that
    # means deleting the just-created duplicate and using
    # ``artifact_update`` on the pre-existing slug instead. Without
    # this hint, the agent's only signal that a duplicate happened is
    # the user noticing in the library, which is exactly the failure
    # mode observed in session logs (agent created
    # ``rules-of-fight-club`` even though ``a07ece9a8c3309aa`` named
    # "The Rules of Fight Club" already existed).
    # Resolve the kind the same way the store will (kind inference):
    # an explicit kind wins, else infer from the inline content. The MCP
    # save path never forwards a source_path, so content sniff is the only
    # signal. This keeps the widget-only duplicate probe below from firing
    # on a markdown/text deliverable that merely shares a name with a widget.
    kind_for_dedup = args.get("kind") or _infer_kind(args.get("content", ""), "", None)
    source_for_dedup = args.get("source", "chat")
    explicit_slug = args.get("slug")
    target_name = args.get("name", "")
    dedup_hint = ""
    if (
        kind_for_dedup == "widget"
        and source_for_dedup == "chat"
        and not explicit_slug
        and isinstance(target_name, str)
        and target_name
        and target_name.lower() != "widget"
    ):
        try:
            qs = urlencode(
                {
                    "kind": "widget",
                    "source": "chat",
                    "q": target_name,
                }
            )
            listing = mcp_core._get(f"/api/artifacts?{qs}")
            if listing.get("error"):
                raise ValueError(listing["error"])
            candidates = listing.get("artifacts") or []
            target_norm = unicodedata.normalize("NFC", target_name).lower()
            conflicts = [
                a
                for a in candidates
                if isinstance(a, dict)
                and isinstance(a.get("name"), str)
                and isinstance(a.get("slug"), str)
                and unicodedata.normalize("NFC", a["name"]).lower() == target_norm
            ]
            if conflicts:
                # Sort newest first, mirror frontend dedup.
                conflicts.sort(
                    key=lambda a: a.get("updated_at") or "",
                    reverse=True,
                )
                existing_slug = conflicts[0]["slug"]
                if len(conflicts) > 1:
                    dedup_hint = (
                        "\n\n⚠️  Possible duplicate: a widget artifact named "
                        f'"{target_name}" already exists at '
                        f"slug={existing_slug!r} (and {len(conflicts) - 1} "
                        "other same-named match(es))."
                    )
                else:
                    dedup_hint = (
                        "\n\n⚠️  Possible duplicate: a widget artifact named "
                        f'"{target_name}" already exists at '
                        f"slug={existing_slug!r}."
                    )
                dedup_hint += (
                    " If you intended to capture a new version of that "
                    "artifact, delete the duplicate just created and "
                    "call `artifact_update` on the existing slug "
                    "instead. If both artifacts are genuinely needed, "
                    "rename one to disambiguate."
                )
        except Exception:
            # Probe failure is non-fatal — proceed with the save and
            # skip the hint. Don't let a transient list failure block
            # legitimate save calls. We deliberately swallow without
            # logging because mcp_core.py runs as a stdio MCP server
            # — any stdout/stderr writes corrupt the JSON-RPC stream.
            pass
    d = mcp_core._post("/api/artifacts", save_body)
    if d.get("error"):
        return f"Error: {d['error']}"
    slug = d.get("slug", "?")
    version = d.get("version", 1)
    name = d.get("name", args.get("name", ""))
    kind = d.get("kind", args.get("kind", "widget"))
    # The artifact-deploy skill requires webapp producers to fill
    # projected cost estimates at save time, but nothing enforced it —
    # field-tested agents skipped it and the card's cost area rendered
    # blank until deploy. Attach a soft warning hint (never a hard
    # reject: existing flows must keep working) so the agent
    # self-corrects on the next turn.
    cost_hint = ""
    wm = args.get("webapp_metadata")
    if kind == "webapp" and isinstance(wm, dict):
        cost = wm.get("cost") or {}
        if not (isinstance(cost, dict) and cost.get("estimates")):
            cost_hint = (
                "\n\n⚠️  webapp_metadata.cost.estimates is empty — the "
                "artifact card's cost area will render blank. Call "
                "`artifact_update` with projected what-if estimates "
                "(e.g. views buckets with usd amounts) per the "
                "artifact-deploy skill contract."
            )
    # Widgets re-surface via the re-emit tag; only non-widgets need the link.
    ref_link = "" if kind == "widget" else f"{mcp_core._artifact_ref_link(slug, name)}\n\n"
    # The store had to suffix the derived slug, so this is a new artifact rather
    # than a new version of the one already at that slug. Reported by the store,
    # so it holds for every kind — the dedup probe above is a name-based
    # pre-check scoped to chat widgets, and stays silent for a markdown or text
    # deliverable saved under a name that is already in use. Suppressed when
    # that probe already fired, so a colliding widget gets one warning.
    collision_hint = ""
    taken = d.get("slug_collided_with")
    if taken and not dedup_hint:
        collision_hint = (
            f"\n\n⚠️  slug {taken!r} is already taken, so this is a NEW artifact "
            f"at slug={slug!r} — not a new version of {taken!r}. If you meant to "
            f"revise that artifact, delete this one and call `artifact_update` on "
            f"{taken!r}. If both are genuinely needed, rename one to disambiguate."
        )
    return (
        f"Saved artifact: slug={slug} version={version}\n\n"
        f"{ref_link}"
        f"{mcp_core._artifact_reemit_hint(slug, name, kind)}"
        f"{dedup_hint}"
        f"{cost_hint}"
        f"{collision_hint}"
        f"{_theme_contrast_hint(d.get('theme_contrast_warning'))}"
    )


def artifact_get(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_GET_SCHEMA)
    slug = args["slug"]
    version = args.get("version")
    path = f"/api/artifacts/{slug}"
    if version:
        path = f"/api/artifacts/{slug}/versions/{int(version)}"
    d = mcp_core._get(path)
    if d.get("error"):
        return f"Error: {d['error']}"

    content = d.get("content") or ""
    content, _ = redact_exfiltration_urls(content)
    content, _ = redact_credentials(content)
    meta_lines = [
        f"slug: {d.get('slug', '?')}",
        f"name: {d.get('name', '?')}",
        f"kind: {d.get('kind', '?')}",
        f"version: {d.get('version', '?')}",
        f"updated_at: {d.get('updated_at', '?')}",
    ]
    if d.get("description"):
        meta_lines.append(f"description: {d['description']}")
    if d.get("tags"):
        meta_lines.append(f"tags: {', '.join(d['tags'])}")
    out_body = "\n".join(meta_lines) + "\n\n--- content ---\n" + content
    # Append a re-emit hint for widgets so the agent has the exact tag
    # string it should use when surfacing the artifact in chat. Without
    # this the slug rule from the artifacts skill is easy to overlook
    # at emission time even though it's right there at the top of this
    # response — verified by session logs where the LLM had
    # the slug in front of it twice and still emitted without it.
    kind = d.get("kind", "widget")
    if kind == "widget":
        out_body += "\n\n" + mcp_core._artifact_reemit_hint(d.get("slug", "?"), d.get("name", ""), kind)
    else:
        out_body += "\n\n" + mcp_core._artifact_ref_link(d.get("slug", "?"), d.get("name", ""))
    return out_body


def artifact_update(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_UPDATE_SCHEMA)
    slug = args["slug"]
    update_body = {k: v for k, v in args.items() if k != "slug" and v is not None}
    if not update_body:
        return "Error: nothing to update (provide content/name/description/tags)"
    # Note: 'actor' is no longer set in the body — the API handler infers
    # it from the X-Internal-Secret header presence (MCP=agent,
    # dashboard=user). This is more secure than trusting a body field
    # and saves the agent from having to remember to set it.
    # ``_patch`` is the PATCH verb helper (it did not exist when this was
    # written, which is why the request used to be hand-rolled). Going through
    # it buys the refusal-invalidate-re-resolve-replay recovery every
    # other verb has, the ``X-Internal-Caller`` audit attribution, the
    # latin-1 session-key guard, and redaction of the gateway's error body.
    d = mcp_core._patch(f"/api/artifacts/{slug}", update_body)
    if d.get("error"):
        return f"Error: {d['error']}"
    out = [f"Updated artifact: slug={d.get('slug', slug)} version={d.get('version', '?')}"]
    # Surface source_path so the agent can emit unified-diff headers
    # when summarising the change in chat (powers the dashboard's
    # Open file affordance on diff blocks). See artifacts skill for
    # the exact format.
    sp = d.get("source_path") or ""
    if sp:
        out.append(f"source_path: {sp}")
    # Re-emit hint for widget-kind updates — same rationale as in
    # artifact_get above. Iterate flow especially needs this because
    # the agent's next step is almost always re-emitting the updated
    # widget in chat, and forgetting the slug at that point is the
    # single largest source of duplicate-artifact creation.
    if d.get("kind", "widget") == "widget":
        out.append("")
        out.append(mcp_core._artifact_reemit_hint(d.get("slug", slug), d.get("name", ""), "widget"))
    else:
        out.append("")
        out.append(mcp_core._artifact_ref_link(d.get("slug", slug), d.get("name", "")))
    # Same soft nudge as artifact_save: an iterate pass that writes a
    # hardcoded palette into a themed iframe should hear about it now,
    # not when the user flips the dashboard theme. The verdict is the
    # gateway's relayed ``theme_contrast_warning`` (False for
    # metadata-only updates, which have nothing to lint).
    theme_hint = _theme_contrast_hint(d.get("theme_contrast_warning"))
    if theme_hint:
        out.append("")
        out.append(theme_hint.lstrip("\n"))
    return "\n".join(out)


def artifact_revert(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_REVERT_SCHEMA)
    slug = args["slug"]
    target_version = int(args["target_version"])
    # Step 1: read the target version's content. Using the API endpoint
    # so the actor / session_id inference from the PATCH stays consistent
    # — we don't bypass the auth-aware handler.
    target = mcp_core._get(f"/api/artifacts/{slug}/versions/{target_version}")
    if target.get("error"):
        return f"Error: cannot fetch version {target_version}: {target['error']}"
    target_content = target.get("content") or ""
    # Step 2: PATCH the artifact with the target's content + reverted
    # event metadata. Snapshot is forced True for reverted updates by
    # the handler — this becomes a new version pinned to the timeline.
    body = {
        "content": target_content,
        "event_type": "reverted",
        "from_version": target_version,
    }
    d = mcp_core._patch(f"/api/artifacts/{slug}", body)
    if d.get("error"):
        return f"Error: {d['error']}"
    # Surface source_path on the response so the calling agent can build
    # a proper unified-diff header (--- <path>\n+++ <path>) when
    # summarising the revert in chat. The dashboard's diff renderer
    # reads those headers to show the "Open file" button — without
    # them, the user sees a diff with no way to drop into the file
    # in the side panel.
    live_version = d.get("version", "?")
    source_path = d.get("source_path") or ""
    out_lines = [
        f"Reverted {slug} to v{target_version}'s content. "
        f"Live state is now v{live_version} (snapshot of v{target_version}).",
    ]
    if source_path:
        out_lines.append(f"source_path: {source_path}")
        out_lines.append(
            "When summarising in chat, emit a ```diff fenced block "
            f"with `--- {source_path}` and `+++ {source_path}` "
            "headers so the dashboard's Open file button is operable."
        )
    return "\n".join(out_lines)


def artifact_list(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_LIST_SCHEMA)
    params: dict[str, str] = {}
    for k in ("tag", "kind", "q"):
        v = args.get(k)
        if v:
            params[k] = v
    path = "/api/artifacts"
    if params:
        path = f"{path}?{urlencode(params)}"
    d = mcp_core._get(path)
    if d.get("error"):
        return f"Error: {d['error']}"
    items = d.get("artifacts", [])
    if not items:
        return "No artifacts saved."
    lines = []
    for a in items:
        tags = f"  [{', '.join(a.get('tags', []))}]" if a.get("tags") else ""
        lines.append(
            f"{a.get('slug', '?')}  v{a.get('version', '?')}  "
            f"{a.get('kind', '?')}{tags}  {a.get('name', '?')}"
        )
    return "\n".join(lines)


def artifact_versions(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_VERSIONS_SCHEMA)
    slug = args["slug"]
    d = mcp_core._get(f"/api/artifacts/{slug}/versions")
    if d.get("error"):
        return f"Error: {d['error']}"
    versions = d.get("versions", [])
    if not versions:
        return f"No versions found for {slug}."
    return f"{slug}: versions {', '.join(f'v{v}' for v in versions)}"


def artifact_delete(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_DELETE_SCHEMA)
    slug = args["slug"]
    d = mcp_core._delete(f"/api/artifacts/{slug}")
    if d.get("error"):
        return f"Error: {d['error']}"
    return f"Deleted artifact: {slug}"


def artifact_get_comments(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_GET_COMMENTS_SCHEMA)
    slug = args["slug"]
    d = mcp_core._get(f"/api/artifacts/{slug}/comments")
    if d.get("error"):
        return f"Error: {d['error']}"
    comments = d.get("comments", [])
    if not comments:
        return f"No comments on artifact `{slug}`."
    lines = []
    for c in comments:
        # Agent provenance rides on the structured is_agent field, not the
        # persisted body — prefix a plain-text marker on this CLI/text surface
        # (the dashboard shows a lucide Bot icon from the same field).
        prefix = ARTIFACT_AGENT_MARKER if c.get("is_agent") else ""
        comment_body = str(c.get("body", ""))
        anchor = ""
        if c.get("anchor") and c["anchor"].get("quote"):
            anchor = mcp_core._format_anchor(c["anchor"])
        indent = "  ↳ " if c.get("parent_id") else "• "
        # Surface the comment id: it is the handle the agent must pass to
        # artifact_mark_review / artifact_delete_comment, so omitting it left
        # those follow-up tools uncallable from a get_comments result.
        cid = c.get("id")
        id_tag = f" (id={cid})" if cid else ""
        lines.append(
            f"{indent}{prefix}{c.get('author', '?')}: {comment_body}"
            f"{anchor} [{c.get('status', 'open')}]{id_tag}"
        )
    result_str = f"Comments on `{slug}` ({len(comments)}):\n" + "\n".join(lines)
    # Route verbatim comment egress through the canonical context-aware shim
    # (not the raw redact_credentials/redact_exfiltration_urls pair) so a
    # companion's extra credential patterns apply, matching the chat-history
    # egress in this same file.
    return redact(result_str)


def artifact_post_comment(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_POST_COMMENT_SCHEMA)
    slug = args["slug"]
    text = args["text"]
    scope = args.get("scope") or "private"
    # Never trust LLM output — redact before posting to the dashboard. Route
    # through the canonical context-aware shim so a companion's extra
    # credential patterns apply on this egress path too. (The SEL audit log
    # is redacted centrally in call_tool_with_logging, so the raw text can't
    # leak into the audit resources either.)
    text = redact(text)
    d = mcp_core._post(
        f"/api/artifacts/{slug}/comments",
        {
            # Store the body verbatim; agent provenance is the structured
            # is_agent flag (no emoji persisted into the body — AGENTS.md).
            "text": text,
            "scope": scope,
            "is_agent": True,
            "author": "agent",
        },
    )
    if d.get("error"):
        return f"Error: {d['error']}"
    cmt = d.get("comment", {})
    return f"Comment posted (id={cmt.get('id', '?')}, sync={cmt.get('sync_state', '?')})"


def artifact_reply_comment(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_REPLY_COMMENT_SCHEMA)
    slug = args["slug"]
    parent_id = args["parent_id"]
    text = args["text"]
    # Never trust LLM output — redact before posting to the dashboard. Route
    # through the canonical context-aware shim so a companion's extra
    # credential patterns apply on this egress path too.
    text = redact(text)
    d = mcp_core._post(
        f"/api/artifacts/{slug}/comments/{parent_id}/reply",
        {
            # Store the body verbatim; agent provenance is the structured
            # is_agent flag (no emoji persisted into the body — AGENTS.md).
            "text": text,
            "is_agent": True,
            "author": "agent",
        },
    )
    if d.get("error"):
        return f"Error: {d['error']}"
    cmt = d.get("comment", {})
    return f"Reply posted (id={cmt.get('id', '?')}, sync={cmt.get('sync_state', '?')})"


def artifact_mark_review(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_MARK_REVIEW_SCHEMA)
    slug = args["slug"]
    comment_id = args["comment_id"]
    d = mcp_core._post(f"/api/artifacts/{slug}/comments/{comment_id}/review", {})
    if d.get("error"):
        return f"Error: {d['error']}"
    return f"Comment {comment_id} advanced to REVIEW status."


def artifact_delete_comment(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_DELETE_COMMENT_SCHEMA)
    slug = args["slug"]
    comment_id = args["comment_id"]
    reason = args["reason"]
    # Never trust LLM output — the reason lands in the activity feed, so
    # redact before sending. Route through the canonical context-aware shim
    # so a companion's extra credential patterns apply. (The SEL audit log
    # is redacted centrally in call_tool_with_logging.)
    reason = redact(reason)
    d = mcp_core._delete(
        f"/api/artifacts/{slug}/comments/{comment_id}",
        {"reason": reason},
    )
    if d.get("error"):
        return f"Error: {d['error']}"
    return f"Comment {comment_id} deleted (reason recorded in activity feed)."


def artifact_folder_list(name: str, args: dict[str, Any]) -> str:
    validate_tool_args(args, ARTIFACT_FOLDER_LIST_SCHEMA)
    d = mcp_core._get("/api/artifact-folders")
    if d.get("error"):
        return f"Error: {d['error']}"
    folder_rows = d.get("folders", [])
    if not folder_rows:
        return "No artifact folders."
    # Present as a path-sorted tree so the agent can pick an id or path.
    folder_rows.sort(key=lambda fld: str(fld.get("path") or fld.get("name", "")).lower())
    out_lines = []
    for fld in folder_rows:
        fld_path = fld.get("path") or fld.get("name", "?")
        count = fld.get("item_count", 0)
        out_lines.append(
            f"{fld.get('id', '?')}  {fld_path}  ({count} item{'' if count == 1 else 's'})"
        )
    return "\n".join(out_lines)


def artifact_folder_create(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_FOLDER_CREATE_SCHEMA)
    create_body = {"name": args["name"]}
    if args.get("parent"):
        create_body["parent"] = args["parent"]
    d = mcp_core._post("/api/artifact-folders", create_body)
    if d.get("error"):
        return f"Error: {d['error']}"
    return f"Created folder `{d.get('path') or d.get('name', '?')}` (id={d.get('id', '?')})."


def artifact_folder_rename(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_FOLDER_RENAME_SCHEMA)
    fld_id, fld_err = mcp_core._resolve_artifact_folder_id(args["folder"])
    if fld_err:
        return f"Error: {fld_err}"
    if not fld_id:
        return "Error: cannot rename the library root."
    d = mcp_core._patch(f"/api/artifact-folders/{fld_id}", {"name": args["name"]})
    if d.get("error"):
        return f"Error: {d['error']}"
    return f"Renamed folder to `{d.get('path') or d.get('name', '?')}` (id={fld_id})."


def artifact_folder_move(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_FOLDER_MOVE_SCHEMA)
    fld_id, fld_err = mcp_core._resolve_artifact_folder_id(args["folder"])
    if fld_err:
        return f"Error: {fld_err}"
    if not fld_id:
        return "Error: cannot move the library root."
    parent_fid, parent_err = mcp_core._resolve_artifact_folder_id(args.get("new_parent") or "")
    if parent_err:
        return f"Error: {parent_err}"
    d = mcp_core._patch(f"/api/artifact-folders/{fld_id}", {"parent_id": parent_fid})
    if d.get("error"):
        return f"Error: {d['error']}"
    move_dest = d.get("path") or "(root)"
    return f"Moved folder (id={fld_id}) to `{move_dest}`."


def artifact_folder_delete(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_FOLDER_DELETE_SCHEMA)
    fld_id, fld_err = mcp_core._resolve_artifact_folder_id(args["folder"])
    if fld_err:
        return f"Error: {fld_err}"
    if not fld_id:
        return "Error: cannot delete the library root."
    cascade = bool(args.get("delete_contents"))
    del_qs = "?delete_contents=true" if cascade else ""
    d = mcp_core._delete(f"/api/artifact-folders/{fld_id}{del_qs}")
    if d.get("error"):
        return f"Error: {d['error']}"
    if cascade:
        n_del = len(d.get("deleted_artifact_slugs", []))
        n_folders = len(d.get("deleted_folder_ids", []))
        return (
            f"Deleted folder (id={fld_id}) and its entire subtree "
            f"({n_folders} folders, {n_del} artifacts)."
        )
    n_kept = len(d.get("reparented_artifact_slugs", []))
    return (
        f"Deleted folder (id={fld_id}); kept {n_kept} artifact"
        f"{'' if n_kept == 1 else 's'} (re-parented to the folder's parent)."
    )


def artifact_move(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ARTIFACT_MOVE_SCHEMA)
    slug = args["slug"]
    d = mcp_core._patch(f"/api/artifacts/{slug}/folder", {"folder": args.get("folder") or ""})
    if d.get("error"):
        return f"Error: {d['error']}"
    moved_fid = d.get("folder_id", "")
    return f"Moved artifact `{slug}` to " + (
        f"folder id={moved_fid}." if moved_fid else "the library root (unfiled)."
    )


def deploy_artifact(name: str, args: dict[str, Any]) -> str:
    has_slug = bool(args.get("artifact_slug"))
    has_dir = bool(args.get("local_dir"))
    if has_slug and has_dir:
        return "Error: provide exactly one of artifact_slug or local_dir"
    if not has_slug and not has_dir:
        return "Error: provide artifact_slug or local_dir"
    deploy_body: dict[str, Any] = {"site_id": args["site_id"]}
    if args.get("artifact_slug"):
        deploy_body["artifact_slug"] = args["artifact_slug"]
    if args.get("local_dir"):
        deploy_body["local_dir"] = args["local_dir"]
    if args.get("profile"):
        deploy_body["profile"] = args["profile"]
    if args.get("ttl_hours") is not None:
        deploy_body["ttl_hours"] = args["ttl_hours"]
    d = mcp_core._post("/api/deploy/deploy", deploy_body)
    # Everything textual returned to the LLM goes through the
    # canonical credential redaction -- error/scan/message fields can
    # carry file content.
    from kiro_crew.deploy.handlers import _redact_text as _deploy_redact
    if d.get("error"):
        return f"Error: {_deploy_redact(str(d['error']))}"
    if d.get("blocked"):
        findings = _deploy_redact(str(d.get("findings", "")))
        if d.get("credential"):
            # Credential-class findings are a HARD block — never pending.
            return (f"Deploy BLOCKED by scan ({d.get('count', '?')} finding(s)):\n"
                    f"{findings}")
        # Non-credential findings are documented as human-overridable.
        # Persist a pending entry flagged override_scan_required so the
        # dashboard can present the explicit "deploy anyway" action for
        # these previews.
        from kiro_crew.deploy.pending import add_pending
        add_pending({
            "site_id": args["site_id"],
            "artifact_slug": args.get("artifact_slug", ""),
            "local_dir": args.get("local_dir", ""),
            "profile": d.get("profile", args.get("profile", "")),
            "region": d.get("region", ""),
            "ttl_hours": args.get("ttl_hours", 72),
            "scan_summary": findings,
            "content_digest": d.get("content_digest", ""),
            "override_scan_required": True,
        })
        return (
            f"Deploy blocked by scan ({d.get('count', '?')} non-credential "
            f"finding(s)):\n{findings}\n\n"
            f"These findings are overridable by a HUMAN: the deploy now "
            f"appears under \"Pending confirmations\" on the Artifact "
            f"Deploy page, where the user can review the findings and "
            f"explicitly deploy anyway (or dismiss).\n"
            f"\nWARNING: Anyone with the published link can view this content. "
            f"It is served on the public internet with no authentication. Relay "
            f"this warning to the user before they confirm."
        )
    # Preview response (requires_confirm is always true for the tool path)
    # Persist as a pending confirmation so the dashboard UI can execute it.
    from kiro_crew.deploy.pending import add_pending
    pending_params = {
        "site_id": args["site_id"],
        "artifact_slug": args.get("artifact_slug", ""),
        "local_dir": args.get("local_dir", ""),
        "profile": d.get("profile", args.get("profile", "")),
        "region": d.get("region", deploy_body.get("region", "")),
        "ttl_hours": args.get("ttl_hours", 72),
        "scan_summary": d.get("scan", "clean"),
        "content_digest": d.get("content_digest", ""),
    }
    add_pending(pending_params)
    # The relay instruction below is best-effort: the ENFORCED gate is the
    # dashboard's "Pending confirmations" card, which renders the same warning
    # next to its own Confirm Deploy button. Keep both — an edit that drops the
    # card's warning must not assume this line covers it.
    return (
        f"Deploy preview for site '{args['site_id']}':\n"
        f"  Public: {d.get('public', True)}\n"
        f"  Size: {d.get('bytes', '?')} bytes\n"
        f"  Scan: {_deploy_redact(str(d.get('scan', 'clean')))}\n"
        f"  TTL: {args.get('ttl_hours', 72)} hours\n"
        f"\nWARNING: Anyone with the published link can view this content. "
        f"It is served on the public internet with no authentication. Relay "
        f"this warning to the user before they confirm.\n"
        f"\nThis deploy now appears under \"Pending confirmations\" on the "
        f"Artifact Deploy page in the dashboard. Open it to confirm or dismiss."
    )


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "artifact_save": artifact_save,
    "artifact_get": artifact_get,
    "artifact_update": artifact_update,
    "artifact_revert": artifact_revert,
    "artifact_list": artifact_list,
    "artifact_versions": artifact_versions,
    "artifact_delete": artifact_delete,
    "artifact_get_comments": artifact_get_comments,
    "artifact_post_comment": artifact_post_comment,
    "artifact_reply_comment": artifact_reply_comment,
    "artifact_mark_review": artifact_mark_review,
    "artifact_delete_comment": artifact_delete_comment,
    "artifact_folder_list": artifact_folder_list,
    "artifact_folder_create": artifact_folder_create,
    "artifact_folder_rename": artifact_folder_rename,
    "artifact_folder_move": artifact_folder_move,
    "artifact_folder_delete": artifact_folder_delete,
    "artifact_move": artifact_move,
    "deploy_artifact": deploy_artifact,
}
