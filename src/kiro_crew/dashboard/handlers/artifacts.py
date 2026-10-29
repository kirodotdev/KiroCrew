"""Artifacts HTTP handlers — REST endpoints over :class:`ArtifactStore`.

Endpoints
---------
- ``GET    /api/artifacts``                    list (filter by ?tag, ?kind, ?q)
- ``POST   /api/artifacts``                    create (JSON body)
- ``GET    /api/artifacts/{slug}``             read current version
- ``PATCH  /api/artifacts/{slug}``             update (content/name/description/tags)
- ``DELETE /api/artifacts/{slug}``             delete
- ``GET    /api/artifacts/{slug}/versions``    list version numbers
- ``GET    /api/artifacts/{slug}/versions/{n}``  read a specific version
- ``GET    /api/artifacts/{slug}/events``      lifecycle event log

Authorization
~~~~~~~~~~~~~
Standard dashboard auth (token middleware). Restricted sessions cannot mutate
artifacts; reads are allowed so the agent can iterate from a hook callback.

The HTTP layer is the single source of truth for SEL audit events on artifact
mutations — MCP tools and the CLI both go through here.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# --------------------------------------------------------------------------- #
# The facade over ``artifacts_parts``
# --------------------------------------------------------------------------- #
# ``kiro_crew.dashboard.handlers.artifacts`` is the import path and the patch seam; the
# code it re-exports lives in the owner modules below, one responsibility each. The owners
# import each other's names, lowest layer first, so a name a test patches here is read in
# more than one module. ``_facade_mirror`` keeps that patch whole: a write or delete of a
# name through this module lands in every owner that binds it, and a name this module does
# not bind is read from the first owner that does. No owner imports this module.
#: Owner modules, lowest layer first: the first one binding a name answers a read of it.
_OWNERS = (
    "kiro_crew.dashboard.handlers.artifacts_parts.record_serialization",
    "kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing",
    "kiro_crew.dashboard.handlers.artifacts_parts.library_reads",
    "kiro_crew.dashboard.handlers.artifacts_parts.session_documents",
    "kiro_crew.dashboard.handlers.artifacts_parts.dashboard_package_gate",
    "kiro_crew.dashboard.handlers.artifacts_parts.create_update",
    "kiro_crew.dashboard.handlers.artifacts_parts.item_lifecycle",
    "kiro_crew.dashboard.handlers.artifacts_parts.publish_sharing",
    "kiro_crew.dashboard.handlers.artifacts_parts.artifact_folders",
    "kiro_crew.dashboard.handlers.artifacts_parts.artifact_comments",
    "kiro_crew.dashboard.handlers.artifacts_parts.remote_artifacts",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Owner-bound name this module does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# Derived, not listed: what ``from handlers.artifacts import *`` binds is every public name
# of the family, as it was when the family was one module.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name this module forwards, for type checkers
    from kiro_crew.dashboard.handlers.artifacts_parts.artifact_comments import (  # noqa: F401
        api_artifact_comments,
        api_artifact_delete_comment,
        api_artifact_edit_comment,
        api_artifact_mark_review,
        api_artifact_post_comment,
        api_artifact_reopen_comment,
        api_artifact_reply_comment,
        api_artifact_resolve_comment,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.artifact_folders import (  # noqa: F401
        _ARTIFACT_FOLDER_ICON_TASKS,
        _serialize_folder,
        _set_pinned_and_reload,
        _spawn_artifact_folder_icon_task,
        _withdraw_subtree_publications,
        api_artifact_folder_create,
        api_artifact_folder_delete,
        api_artifact_folder_update,
        api_artifact_folders,
        api_artifact_set_folder,
        api_artifact_set_pinned,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.create_update import (  # noqa: F401
        _validate_inbound_webapp_metadata,
        api_artifact_update,
        api_artifacts_create,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.dashboard_package_gate import (  # noqa: F401
        _SNAPSHOT_ABSENT,
        _SNAPSHOT_FOUND,
        _SNAPSHOT_UNREADABLE,
        _authoritative_promote_content,
        _binding_refusal,
        _crewmate_binding_refusal,
        _gate_must_classify,
        _is_dashboard_package,
        _package_binding,
        _package_write_refusal,
        _promote_verdict,
        _refuse_binding,
        _session_binding_refusal,
        _stored_snapshot_for,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.item_lifecycle import (  # noqa: F401
        api_artifact_delete,
        api_artifact_record_event,
        api_artifact_relocate,
        api_artifact_settle_blank,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.library_reads import (  # noqa: F401
        _CONTENT_CACHE_MAX_ITEM_BYTES,
        _CONTENT_CACHE_MAX_TOTAL_BYTES,
        _CONTEXT_LINE_LEN,
        _CONTEXT_MAX_LINES,
        _MD_BLOCKQUOTE_RE,
        _MD_EMPHASIS_RE,
        _MD_FENCE_RE,
        _MD_HEADING_RE,
        _MD_LINK_RE,
        _MD_LIST_RE,
        _SEARCH_QUERY_MAX_CHARS,
        _SNIPPET_CACHE_MAX_ENTRIES,
        _SNIPPET_MAX_LEN,
        _STRIP_TAGS_RE,
        _cache_entry_bytes,
        _cached_snippet,
        _chat_folder_session_names,
        _clean_markdown,
        _clean_pinned_filter,
        _content_cache,
        _content_cache_bytes,
        _content_cache_lock,
        _context_snippet,
        _load_content,
        _scan_artifacts,
        _snippet_cache,
        _snippet_from,
        _strip_content,
        api_artifact_asset,
        api_artifact_detail,
        api_artifact_events,
        api_artifact_version_detail,
        api_artifact_versions,
        api_artifacts_list,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.publish_sharing import (  # noqa: F401
        _ARTIFACT_PROVIDER_RE,
        _VALID_VISIBILITY,
        _sharing_model_dict,
        _sync_error_response,
        _validate_sharing_body,
        api_artifact_overwrite_remote,
        api_artifact_publish,
        api_artifact_publish_providers,
        api_artifact_pull_latest,
        api_artifact_refresh_sharing,
        api_artifact_reprobe_notice,
        api_artifact_unpublish,
        api_artifact_update_sharing,
        api_artifact_upstream_status,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import (  # noqa: F401
        _DELETED_SESSION_LABEL,
        _MAX_REDACT_DEPTH,
        _REMOTE_ID_CRED_TAG,
        _REMOTE_ID_KEYS,
        _SERIALIZE_REDACTED_KEYS,
        _id_embeds_hard_credential,
        _redact_audit_metadata,
        _redact_remote_response,
        _redact_text,
        _redact_webapp_metadata,
        _resolve_session_title,
        _serialize,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.remote_artifacts import (  # noqa: F401
        _REMOTE_COMMENT_CACHE_MAX,
        _REMOTE_COMMENT_TTL_SECS,
        _annotate_local_slugs,
        _audit_remote_denied,
        _invalidate_remote_comment_cache,
        _remote_cache_put,
        _remote_cache_sweep,
        _remote_comment_cache,
        _serialize_remote_comment,
        api_remote_artifact_comments,
        api_remote_artifact_delete_comment,
        api_remote_artifact_get,
        api_remote_artifact_mark_review,
        api_remote_artifact_post_comment,
        api_remote_artifact_reply_comment,
        api_remote_artifacts_browse,
        api_remote_artifacts_clone,
        api_remote_artifacts_fork,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (  # noqa: F401
        _MAX_BODY_BYTES,
        _REMOTE_PROVIDER_TIMEOUT_S,
        _SESSION_KEY_RE,
        _artifact_source_for_request,
        _audit,
        _clean_origin_session_key,
        _err,
        _event_session_id,
        _json_response,
        _non_owner_refusal,
        _notify_artifact_update,
        _publish_governance_denied,
        _read_json_body,
        _resolve_folder_ref,
        _resolve_folder_ref_off_loop,
        _run_off_loop,
        _session_key,
        _set_folder_and_reload,
        logger,
        sel,
    )
    from kiro_crew.dashboard.handlers.artifacts_parts.session_documents import (  # noqa: F401
        _collect_session_docs,
        _collision_report,
        _materialize_and_pin,
        _recorded_doc_identities,
        _scan_session_docs,
        api_artifact_materialize,
        api_artifact_session_docs,
    )


if TYPE_CHECKING:  # names this module re-exported from other modules
    from kiro_crew import hooks  # noqa: F401
    from kiro_crew import publish_sync  # noqa: F401
    from kiro_crew import sel as _sel_mod  # noqa: F401
    from kiro_crew.artifact_source import LINK  # noqa: F401
    from kiro_crew.artifact_source import classify_source  # noqa: F401
    from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND  # noqa: F401
    from kiro_crew.artifacts import EXPECT_ABSENT  # noqa: F401
    from kiro_crew.artifacts import MAX_CONTENT_BYTES  # noqa: F401
    from kiro_crew.artifacts import USER_SELECTABLE_KINDS  # noqa: F401
    from kiro_crew.artifacts import ArtifactAlreadyExistsError  # noqa: F401
    from kiro_crew.artifacts import ArtifactComment  # noqa: F401
    from kiro_crew.artifacts import ArtifactConflictError  # noqa: F401
    from kiro_crew.artifacts import ArtifactError  # noqa: F401
    from kiro_crew.artifacts import ArtifactNotFoundError  # noqa: F401
    from kiro_crew.artifacts import ArtifactReplacedError  # noqa: F401
    from kiro_crew.artifacts import ArtifactStillPublishedError  # noqa: F401
    from kiro_crew.artifacts import ArtifactValidationError  # noqa: F401
    from kiro_crew.artifacts import filter_comments_for_forward  # noqa: F401
    from kiro_crew.artifacts import get_default_folder_store  # noqa: F401
    from kiro_crew.artifacts import get_default_store  # noqa: F401
    from kiro_crew.artifacts import has_unthemed_hardcoded_colors  # noqa: F401
    from kiro_crew.artifacts import is_document_path  # noqa: F401
    from kiro_crew.artifacts import slug_is_well_formed  # noqa: F401
    from kiro_crew.artifacts import webapp_metadata_from_dict  # noqa: F401
    from kiro_crew.constants import md_link_destination  # noqa: F401
    from kiro_crew.dashboard.body_numbers import body_int  # noqa: F401
    from kiro_crew.dashboard.chat_folders import archived_session_names_in_folders  # noqa: F401
    from kiro_crew.dashboard.chat_folders import chat_folder_subtree_ids  # noqa: F401
    from kiro_crew.dashboard.chat_folders import generate_emoji_for_name  # noqa: F401
    from kiro_crew.dashboard.chat_folders import live_slot_names_in_folders  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import _is_restricted_session  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import bounded_json_error_as_exception  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import bounded_json_error_code  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import read_bounded_json  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request  # noqa: F401
    from kiro_crew.dashboard.state import _normalize_slot_key  # noqa: F401
    from kiro_crew.executors import subprocess_executor  # noqa: F401
    from kiro_crew.hooks import FileTooLargeError  # noqa: F401
    from kiro_crew.hooks import safe_read_file_bytes_with_identity  # noqa: F401
    from kiro_crew.hooks import stat_identity  # noqa: F401
    from kiro_crew.messaging.link import is_channel_session_key  # noqa: F401
    from kiro_crew.publish_governance import publish_denied_reason  # noqa: F401
    from kiro_crew.publish_provider import DEFAULT_PROVIDER  # noqa: F401
    from kiro_crew.publish_provider import Capability  # noqa: F401
    from kiro_crew.publish_provider import CommentAnchor  # noqa: F401
    from kiro_crew.publish_provider import KindSupport  # noqa: F401
    from kiro_crew.publish_provider import NotPublishedError  # noqa: F401
    from kiro_crew.publish_provider import PublishConflictError  # noqa: F401
    from kiro_crew.publish_provider import PublishError  # noqa: F401
    from kiro_crew.publish_provider import PublishUnavailableError  # noqa: F401
    from kiro_crew.publish_provider import get_provider  # noqa: F401
    from kiro_crew.publish_provider import is_registered  # noqa: F401
    from kiro_crew.publish_provider import list_providers  # noqa: F401
    from kiro_crew.security import _B64_CHUNK_RE  # noqa: F401
    from kiro_crew.security import _HARD_CREDENTIAL_RE  # noqa: F401
    from kiro_crew.security import is_sensitive_path  # noqa: F401
    from kiro_crew.security import redact_credentials  # noqa: F401
    from kiro_crew.security import redact_exfiltration_urls  # noqa: F401
    from kiro_crew.validation import infer_use_case  # noqa: F401
