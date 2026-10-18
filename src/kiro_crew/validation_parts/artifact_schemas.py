"""Tool schemas for artifacts and for organising artifacts and chats into folders and tags.

The ``artifact_*`` tools and their comment, folder, move and deploy companions, plus
the chat-sidebar folder, tag and column tools, which share the folder reference
bounds.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import json
import re
from pathlib import PureWindowsPath

# The artifact tag rule lives with the store's other field grammar and is read
# here so the tool gate and the store cannot disagree about a tag. Import-safe:
# ``artifact_store.rules`` loads the record dataclasses and the slug hash
# fallback, never ``kiro_crew.artifacts`` (the service module whose import from
# here would close the ``artifacts -> hooks -> webhooks -> validation`` cycle).
from kiro_crew.artifact_store.rules import ALLOWED_KINDS as _ARTIFACT_KINDS
from kiro_crew.artifact_store.rules import MAX_TAG_LEN as ARTIFACT_TAG_MAX
from kiro_crew.artifact_store.rules import normalize_tag as _normalize_artifact_tag
from kiro_crew.constants import ARTIFACT_MAX_CONTENT_BYTES, AWS_PROFILE_NAME_RE, MAX_SHORT_STRING
from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError

# Artifact tools — slug pattern matches kiro_crew.artifacts._SLUG_RE. The tag
# rule is NOT a pattern: a tag is Unicode letters, marks and digits, which are
# general categories ``re`` cannot spell, so the artifact schemas below carry
# only the tag count and length caps as fields and check each tag's characters
# in ``_validate_artifact_tags`` through the store's own ``normalize_tag``.
_ARTIFACT_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
# BUILT FROM THE STORE'S OWN SET, not spelled again. This pattern decides only
# whether a kind REACHES the store, so a name the store accepts and this list
# omits is a kind advertised nowhere and refused "invalid format" with no rule
# behind it. Sorted so the pattern is stable whatever order the frozenset
# iterates in, and ``re.escape`` because the alternation is built rather than
# written: a kind carrying a regex metacharacter would otherwise change what
# this matches instead of being rejected by the store's own name rules.
#
# What the content has to be stays the store's question. ``kind="dashboard"``
# is a layout package, and ``ArtifactStore.create`` / ``.update`` run every one
# of those writes through ``artifact_store.dashboard_package``, which is the
# single gate on it; spelling any of those rules here would make a second
# vocabulary that could drift from the gate that holds.
_ARTIFACT_KIND_RE = re.compile(
    "^(" + "|".join(re.escape(k) for k in sorted(_ARTIFACT_KINDS)) + ")$"
)


_ARTIFACT_SOURCE_RE = re.compile(r"^(chat|cron|subagent|manual|import)$")
# Single source of truth: the MCP save/update field cap MUST equal the store's
# own content cap, else the tool path rejects content the store would accept
# (or vice-versa). Both read ``constants.ARTIFACT_MAX_CONTENT_BYTES`` -- a leaf,
# so this module never imports ``artifacts`` (that edge closed the cycle
# ``artifacts -> hooks -> webhooks -> validation -> artifacts``).
ARTIFACT_CONTENT_MAX = ARTIFACT_MAX_CONTENT_BYTES

ARTIFACT_WEBAPP_METADATA_MAX_BYTES = 16_384


def _validate_artifact_tags(cleaned: dict) -> None:
    """Hold every tag argument to the store's tag rule and answer with its reason.

    Runs after the field pass, so each item is already NFC-normalized, stripped
    of hidden characters and bounded by the schema's caps; what is left to check
    is the character rule, which is a category test no ``FieldSpec`` pattern can
    express. The store applies the same rule on write; checking here means the
    tool reports the offending tag and why, instead of a store error.
    """
    tags = cleaned.get("tags")
    if isinstance(tags, list):
        for i, item in enumerate(tags):
            try:
                _normalize_artifact_tag(item)
            except ValueError as exc:
                raise ValidationError("tags", f"item[{i}]: {exc}") from None
    tag = cleaned.get("tag")
    if isinstance(tag, str) and tag:
        try:
            _normalize_artifact_tag(tag)
        except ValueError as exc:
            raise ValidationError("tag", str(exc)) from None


def _validate_artifact_save(cleaned: dict) -> None:
    """Reject a malformed tag, then an oversized or structurally invalid webapp_metadata blob."""
    _validate_artifact_tags(cleaned)
    am = cleaned.get("webapp_metadata")
    if am is None:
        return
    if not isinstance(am, dict):
        raise ValidationError("webapp_metadata", "must be a dict")
    try:
        size = len(json.dumps(am, default=str))
    except (TypeError, ValueError) as exc:
        raise ValidationError("webapp_metadata", "is not JSON-serializable") from exc
    if size > ARTIFACT_WEBAPP_METADATA_MAX_BYTES:
        raise ValidationError(
            "webapp_metadata",
            f"serialized webapp_metadata exceeds {ARTIFACT_WEBAPP_METADATA_MAX_BYTES} bytes (got {size})",
        )
    # ── Bounded structural validation (input shape at MCP/HTTP boundary) ──
    _validate_webapp_metadata_shape(am)


# Shared slug pattern (matches _ARTIFACT_SLUG_RE + deploy slug validation).
_WM_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# constants.AWS_PROFILE_NAME_RE — the single source of truth: '+'
# admitted for IAM Identity Center derived profiles
# ("<account>+<permission-set>"); first char excludes '-' so a stored
# profile is never option-shaped when it later reaches `--profile <value>`
# argv. \Z is load-bearing here: this path matches the raw value WITHOUT a
# strip, so a $ anchor would let a trailing-newline value through.
_WM_PROFILE_RE = AWS_PROFILE_NAME_RE
_WM_URL_RE = re.compile(r"^https?://.{1,2048}$")
_WM_LIFECYCLE_STATUSES = {"draft", "deploying", "live", "error", "expired"}
_WM_LIST_CAP = 50


def _validate_webapp_metadata_shape(am: dict) -> None:
    """Validate webapp_metadata nested structure — tolerant for absent fields."""
    # app_dir — LLM-controlled filesystem path consumed by the local preview
    # channel. Reject control characters (NUL crashes
    # Path.resolve with ValueError) and relative paths at WRITE time; the
    # serve path re-validates against the allow-listed roots regardless.
    app_dir = am.get("app_dir")
    if app_dir is not None:
        if not isinstance(app_dir, str) or len(app_dir) > 4096:
            raise ValidationError("webapp_metadata.app_dir", "must be a string (max 4096 chars)")
        if app_dir != "":
            if any(ord(c) < 0x20 for c in app_dir):
                raise ValidationError(
                    "webapp_metadata.app_dir", "must not contain control characters"
                )
            posix_abs = app_dir.startswith("/") or app_dir.startswith("~/") or app_dir == "~"
            if not posix_abs and not PureWindowsPath(app_dir).is_absolute():
                raise ValidationError(
                    "webapp_metadata.app_dir", "must be an absolute path (or ~/-prefixed)"
                )
    # deploy_target.public_url
    dt = am.get("deploy_target")
    if dt is not None:
        if not isinstance(dt, dict):
            raise ValidationError("webapp_metadata.deploy_target", "must be a dict")
        pub_url = dt.get("public_url")
        if pub_url is not None:
            if not isinstance(pub_url, str):
                raise ValidationError(
                    "webapp_metadata.deploy_target.public_url",
                    "must be a valid http(s) URL (max 2048 chars)",
                )
            # Empty string is allowed (documented draft state — URL not yet assigned).
            if pub_url != "" and not _WM_URL_RE.match(pub_url):
                raise ValidationError(
                    "webapp_metadata.deploy_target.public_url",
                    "must be a valid http(s) URL (max 2048 chars)",
                )
            # Reject Basic-auth userinfo (https://user:pass@host) —
            # the regex above accepts it, and a credential-bearing URL would
            # be surfaced/linked by the dashboard, transmitting the embedded
            # credentials to the host when opened.
            if pub_url != "":
                from urllib.parse import urlsplit

                try:
                    parts = urlsplit(pub_url)
                except ValueError:
                    raise ValidationError(
                        "webapp_metadata.deploy_target.public_url",
                        "must be a valid http(s) URL (max 2048 chars)",
                    )
                if parts.username or parts.password:
                    raise ValidationError(
                        "webapp_metadata.deploy_target.public_url",
                        "must not contain userinfo (user:password@) credentials",
                    )
        # profile/region/slug — string-typed with existing regex patterns.
        for key, pattern, label in (
            ("profile", _WM_PROFILE_RE, "profile"),
            # Mirrors deploy/profiles.py _REGION_RE so a region the deploy path
            # accepted (incl. GovCloud us-gov-west-1) is never rejected here.
            ("region", re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$"), "region"),
            ("slug", _WM_SLUG_RE, "slug"),
        ):
            val = dt.get(key)
            if val is not None:
                if not isinstance(val, str) or len(val) > 128:
                    raise ValidationError(
                        f"webapp_metadata.deploy_target.{key}", "must be a string (max 128 chars)"
                    )
                if val and not pattern.match(val):
                    raise ValidationError(
                        f"webapp_metadata.deploy_target.{key}", f"invalid {label} format"
                    )

    # lifecycle.status enum
    lc = am.get("lifecycle")
    if lc is not None:
        if not isinstance(lc, dict):
            raise ValidationError("webapp_metadata.lifecycle", "must be a dict")
        status = lc.get("status")
        # Require a string BEFORE the enum membership test — an
        # unhashable value (list/dict) raises TypeError inside `in` and
        # turns artifact save/update into a 500.
        if status is not None and not isinstance(status, str):
            raise ValidationError("webapp_metadata.lifecycle.status", "must be a string")
        if status is not None and status not in _WM_LIFECYCLE_STATUSES:
            raise ValidationError(
                "webapp_metadata.lifecycle.status",
                f"must be one of {sorted(_WM_LIFECYCLE_STATUSES)}",
            )
        # persistent: strict bool — the string "false" is truthy and would
        # silently flip expiry/teardown behavior downstream.
        pers = lc.get("persistent")
        if pers is not None and not isinstance(pers, bool):
            raise ValidationError("webapp_metadata.lifecycle.persistent", "must be a boolean")
        # ttl_hours: strict int (bool excluded — bool subclasses int), 0-8760.
        ttl = lc.get("ttl_hours")
        if ttl is not None:
            if isinstance(ttl, bool) or not isinstance(ttl, int):
                raise ValidationError("webapp_metadata.lifecycle.ttl_hours", "must be an integer")
            if not (0 <= ttl <= 8760):
                raise ValidationError("webapp_metadata.lifecycle.ttl_hours", "must be 0-8760")
        # expires_at ISO-8601 parseable
        exp = lc.get("expires_at")
        if exp is not None and exp != "":
            if not isinstance(exp, str):
                raise ValidationError("webapp_metadata.lifecycle.expires_at", "must be a string")
            from datetime import datetime, timezone

            try:
                datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                raise ValidationError(
                    "webapp_metadata.lifecycle.expires_at",
                    "must be ISO-8601 UTC (YYYY-MM-DDTHH:MM:SSZ)",
                )

    # cost and architecture lists capped
    for key in ("cost", "architecture"):
        val = am.get(key)
        if val is not None:
            if isinstance(val, dict):
                # architecture.resources or cost.items may be lists inside
                for sub_key, sub_val in val.items():
                    if isinstance(sub_val, list) and len(sub_val) > _WM_LIST_CAP:
                        raise ValidationError(
                            f"webapp_metadata.{key}.{sub_key}",
                            f"list exceeds {_WM_LIST_CAP} entries",
                        )
            elif isinstance(val, list) and len(val) > _WM_LIST_CAP:
                raise ValidationError(
                    f"webapp_metadata.{key}", f"list exceeds {_WM_LIST_CAP} entries"
                )


ARTIFACT_SAVE_SCHEMA = ToolSchema(
    custom_validator=_validate_artifact_save,
    tool_name="artifact_save",
    fields=[
        FieldSpec("name", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("content", str, required=True, max_len=ARTIFACT_CONTENT_MAX),
        FieldSpec("slug", str, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("kind", str, max_len=20, pattern=_ARTIFACT_KIND_RE),
        FieldSpec("source", str, max_len=20, pattern=_ARTIFACT_SOURCE_RE),
        FieldSpec("description", str, max_len=2_000),
        FieldSpec(
            "tags",
            list,
            item_type=str,
            item_max_len=ARTIFACT_TAG_MAX,
            max_items=16,
        ),
        FieldSpec("folder", str, max_len=4096),
        FieldSpec("webapp_metadata", dict),
    ],
)

ARTIFACT_GET_SCHEMA = ToolSchema(
    tool_name="artifact_get",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("version", int, min_val=1, max_val=10_000),
    ],
)

ARTIFACT_UPDATE_SCHEMA = ToolSchema(
    custom_validator=_validate_artifact_save,
    tool_name="artifact_update",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("content", str, max_len=ARTIFACT_CONTENT_MAX),
        FieldSpec("name", str, max_len=200),
        FieldSpec("description", str, max_len=2_000),
        FieldSpec(
            "tags",
            list,
            item_type=str,
            item_max_len=ARTIFACT_TAG_MAX,
            max_items=16,
        ),
        FieldSpec("clear_tags", bool),
        FieldSpec("webapp_metadata", dict),
    ],
)

ARTIFACT_DELETE_SCHEMA = ToolSchema(
    tool_name="artifact_delete",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
    ],
)

ARTIFACT_LIST_SCHEMA = ToolSchema(
    custom_validator=_validate_artifact_tags,
    tool_name="artifact_list",
    fields=[
        FieldSpec("tag", str, max_len=ARTIFACT_TAG_MAX),
        FieldSpec("kind", str, max_len=20, pattern=_ARTIFACT_KIND_RE),
        FieldSpec("q", str, max_len=200),
    ],
)

ARTIFACT_VERSIONS_SCHEMA = ToolSchema(
    tool_name="artifact_versions",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
    ],
)

ARTIFACT_REVERT_SCHEMA = ToolSchema(
    tool_name="artifact_revert",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("target_version", int, required=True, min_val=1, max_val=10_000),
    ],
)

# Artifact comments. Comment ids are local UUIDs or provider-origin
# ids (e.g. a remote provider's "<ts>-<uuid>"); allow alphanumerics + - . : _ .
_ARTIFACT_COMMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
_ARTIFACT_SCOPE_RE = re.compile(r"^(private|shared)$")
ARTIFACT_COMMENT_TEXT_MAX = 10_000

#: Plain-text marker for agent-authored comments on CLI/text surfaces that lack
#: a structured ``is_agent`` flag (e.g. the ``artifact_get_comments`` MCP text
#: rendering). NOT persisted into the comment body and NOT an emoji: agent
#: provenance is carried by the structured ``is_agent`` field, which the
#: dashboard renders as a lucide ``Bot`` icon (no emoji in the UI, per
#: AGENTS.md). This constant is only for prefixing plain-text output.
ARTIFACT_AGENT_MARKER = "[agent] "

ARTIFACT_GET_COMMENTS_SCHEMA = ToolSchema(
    tool_name="artifact_get_comments",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("exclude_resolved", bool),
    ],
)

ARTIFACT_POST_COMMENT_SCHEMA = ToolSchema(
    tool_name="artifact_post_comment",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        # The body is stored verbatim (no watermark prepended), so the full
        # ARTIFACT_COMMENT_TEXT_MAX budget is available; agent provenance rides
        # on the structured is_agent field, not the text.
        FieldSpec("text", str, required=True, max_len=ARTIFACT_COMMENT_TEXT_MAX),
        FieldSpec("scope", str, max_len=10, pattern=_ARTIFACT_SCOPE_RE),
    ],
)

ARTIFACT_REPLY_COMMENT_SCHEMA = ToolSchema(
    tool_name="artifact_reply_comment",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("parent_id", str, required=True, max_len=128, pattern=_ARTIFACT_COMMENT_ID_RE),
        FieldSpec("text", str, required=True, max_len=ARTIFACT_COMMENT_TEXT_MAX),
    ],
)

ARTIFACT_MARK_REVIEW_SCHEMA = ToolSchema(
    tool_name="artifact_mark_review",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("comment_id", str, required=True, max_len=128, pattern=_ARTIFACT_COMMENT_ID_RE),
    ],
)

#: Cap on the one-line justification an agent must record when deleting a
#: comment it has applied (surfaced in the SEL audit + activity timeline).
ARTIFACT_DELETE_COMMENT_REASON_MAX = 500

ARTIFACT_DELETE_COMMENT_SCHEMA = ToolSchema(
    tool_name="artifact_delete_comment",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("comment_id", str, required=True, max_len=128, pattern=_ARTIFACT_COMMENT_ID_RE),
        FieldSpec("reason", str, required=True, max_len=ARTIFACT_DELETE_COMMENT_REASON_MAX),
    ],
)

# Artifact folders. A folder reference is a folder id OR a
# ``/``-separated human path, so it can't share the slug regex — only bound
# the length. Folder names cap at 100 chars (matches ArtifactFolderStore).
_ARTIFACT_FOLDER_NAME_MAX = 100
_ARTIFACT_FOLDER_REF_MAX = 4096

ARTIFACT_FOLDER_LIST_SCHEMA = ToolSchema(
    tool_name="artifact_folder_list",
    fields=[],
)

ARTIFACT_FOLDER_CREATE_SCHEMA = ToolSchema(
    tool_name="artifact_folder_create",
    fields=[
        FieldSpec("name", str, required=True, max_len=_ARTIFACT_FOLDER_NAME_MAX),
        FieldSpec("parent", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

ARTIFACT_FOLDER_RENAME_SCHEMA = ToolSchema(
    tool_name="artifact_folder_rename",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("name", str, required=True, max_len=_ARTIFACT_FOLDER_NAME_MAX),
    ],
)

ARTIFACT_FOLDER_MOVE_SCHEMA = ToolSchema(
    tool_name="artifact_folder_move",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("new_parent", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

ARTIFACT_FOLDER_DELETE_SCHEMA = ToolSchema(
    tool_name="artifact_folder_delete",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("delete_contents", bool),
    ],
)

# Chat (sidebar) folders. Same reference model as the artifact folders above —
# a folder is addressed by id OR by a ``/``-separated human path — so the two
# bounds are shared. Folder names cap at 100 chars server-side
# (chat_folders.api_chat_folder_create truncates); reject longer input here
# instead of silently filing the session under a truncated name.
CHAT_FOLDER_TREE_SCHEMA = ToolSchema(
    tool_name="chat_folder_tree",
    fields=[],
)

CHAT_FOLDER_CREATE_SCHEMA = ToolSchema(
    tool_name="chat_folder_create",
    fields=[
        FieldSpec("name", str, required=True, max_len=_ARTIFACT_FOLDER_NAME_MAX),
        FieldSpec("parent", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

CHAT_FOLDER_MOVE_SCHEMA = ToolSchema(
    tool_name="chat_folder_move",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("new_parent", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
        # Sibling anchors for the folder's POSITION among its siblings. Mutually
        # exclusive, and refused unless the anchor already sits directly under
        # the destination -- the handler owns both rules, since neither is
        # expressible as a field constraint.
        FieldSpec("before", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("after", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

CHAT_FOLDER_UPDATE_SCHEMA = ToolSchema(
    tool_name="chat_folder_update",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("name", str, max_len=_ARTIFACT_FOLDER_NAME_MAX),
        # One emoji grapheme or "" to clear; the endpoint's ``_is_single_emoji``
        # is the authority, so only the length is bounded here (it caps at 16).
        FieldSpec("icon", str, max_len=16),
        # A palette value or "" to clear; the endpoint's allowlist decides.
        FieldSpec("color", str, max_len=16),
    ],
)

CHAT_FOLDER_MOVE_SESSION_SCHEMA = ToolSchema(
    tool_name="chat_folder_move_session",
    fields=[
        # A session reference is a slot key, a ``dashboard:`` session key, or an
        # exact session title — none share a charset, so only bound the length.
        FieldSpec("session", str, required=True, max_len=512),
        FieldSpec("folder", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

CHAT_FOLDER_DELETE_SCHEMA = ToolSchema(
    tool_name="chat_folder_delete",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

CHAT_SESSION_PIN_MOVE_SCHEMA = ToolSchema(
    tool_name="chat_session_pin_move",
    fields=[
        # Session references share chat_folder_move_session's shape: a slot key,
        # a ``dashboard:`` session key, or an exact title -- only bound the length.
        # Exactly one of ``before`` / ``after`` is required; the handler owns that
        # rule, since it is not expressible as a field constraint.
        FieldSpec("session", str, required=True, max_len=512),
        FieldSpec("before", str, max_len=512),
        FieldSpec("after", str, max_len=512),
    ],
)

CHAT_FOLDER_FILE_SELF_SCHEMA = ToolSchema(
    tool_name="chat_folder_file_self",
    fields=[
        # No ``session`` field on purpose: the target is the caller's own slot,
        # resolved server-side from the verified identity. The folder reference
        # takes the same id-or-path shape as ``chat_folder_move_session.folder``.
        FieldSpec("folder", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

# The tag endpoints store ``name[:60]`` (``chat_tags._NAME_MAX``); the cap is
# mirrored here so the server refuses an overlong name instead of writing one
# that no later ``chat_tag_assign`` name lookup can match.
_CHAT_TAG_NAME_MAX = 60
#: A tag reference is a 12-hex id or the tag's exact name; the two share no
#: charset, so only the length is bounded — the handler resolves the shape.
_CHAT_TAG_REF_MAX = 64
#: Sidebar tag lists are short by construction (a strip of columns); the cap
#: bounds one call, not the vocabulary.
_CHAT_TAG_MAX_ITEMS = 32

CHAT_TAG_LIST_SCHEMA = ToolSchema(tool_name="chat_tag_list", fields=[])

CHAT_TAG_CREATE_SCHEMA = ToolSchema(
    tool_name="chat_tag_create",
    fields=[
        FieldSpec("name", str, required=True, max_len=_CHAT_TAG_NAME_MAX),
        FieldSpec("color", str, pattern=re.compile(r"^#[0-9a-fA-F]{6}$")),
        FieldSpec("status", bool),
    ],
)

CHAT_TAG_UPDATE_SCHEMA = ToolSchema(
    tool_name="chat_tag_update",
    fields=[
        # The tag to change, by id or exact name; the fields to change are all
        # optional and the handler requires at least one.
        FieldSpec("tag", str, required=True, max_len=_CHAT_TAG_REF_MAX),
        FieldSpec("name", str, max_len=_CHAT_TAG_NAME_MAX),
        FieldSpec("color", str, pattern=re.compile(r"^#[0-9a-fA-F]{6}$")),
        FieldSpec("status", bool),
    ],
)

CHAT_TAG_ASSIGN_SCHEMA = ToolSchema(
    tool_name="chat_tag_assign",
    fields=[
        # Same session-reference shape as ``chat_folder_move_session.session``.
        FieldSpec("session", str, required=True, max_len=512),
        FieldSpec(
            "add",
            list,
            item_type=str,
            item_max_len=_CHAT_TAG_REF_MAX,
            max_items=_CHAT_TAG_MAX_ITEMS,
        ),
        FieldSpec(
            "remove",
            list,
            item_type=str,
            item_max_len=_CHAT_TAG_REF_MAX,
            max_items=_CHAT_TAG_MAX_ITEMS,
        ),
    ],
)

CHAT_SESSION_PIN_SCHEMA = ToolSchema(
    tool_name="chat_session_pin",
    fields=[
        # Same session-reference shape as ``chat_folder_move_session.session``.
        FieldSpec("session", str, required=True, max_len=512),
        # A real JSON boolean: the string "false" is truthy, so a coerced value
        # would pin a session the caller asked to unpin.
        FieldSpec("pinned", bool, required=True),
    ],
)

SESSION_MOVE_TO_COLUMN_SCHEMA = ToolSchema(
    tool_name="session_move_to_column",
    fields=[
        # Same session-reference shape as ``chat_session_pin.session``, and the
        # column-reference bound the ``chat_tag_column_*`` schemas use.
        FieldSpec("session", str, required=True, max_len=512),
        FieldSpec("column", str, required=True, max_len=_CHAT_TAG_REF_MAX),
    ],
)

# Board columns (``/api/chat/tag-columns``). A column name is stored as
# ``name[:60]`` (``chat_tags._NAME_MAX``), the same cap as a tag name, and a
# column reference is a 12-hex id or the column's exact name.
CHAT_TAG_COLUMN_LIST_SCHEMA = ToolSchema(tool_name="chat_tag_column_list", fields=[])

CHAT_TAG_COLUMN_CREATE_SCHEMA = ToolSchema(
    tool_name="chat_tag_column_create",
    fields=[
        FieldSpec("name", str, required=True, max_len=_CHAT_TAG_NAME_MAX),
        FieldSpec("tag", str, required=True, max_len=_CHAT_TAG_REF_MAX),
    ],
)

CHAT_TAG_COLUMN_MOVE_SCHEMA = ToolSchema(
    tool_name="chat_tag_column_move",
    fields=[
        # The handler requires exactly one of ``before`` / ``after``.
        FieldSpec("column", str, required=True, max_len=_CHAT_TAG_REF_MAX),
        FieldSpec("before", str, max_len=_CHAT_TAG_REF_MAX),
        FieldSpec("after", str, max_len=_CHAT_TAG_REF_MAX),
    ],
)

ARTIFACT_MOVE_SCHEMA = ToolSchema(
    tool_name="artifact_move",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("folder", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

DEPLOY_ARTIFACT_SCHEMA = ToolSchema(
    tool_name="deploy_artifact",
    fields=[
        FieldSpec("site_id", str, required=True, max_len=64),
        FieldSpec("artifact_slug", str, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("local_dir", str, max_len=4096),
        FieldSpec("profile", str, max_len=64),
        FieldSpec("ttl_hours", int, min_val=0, max_val=8760),
    ],
)
