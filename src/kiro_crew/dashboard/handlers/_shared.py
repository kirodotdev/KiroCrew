"""Shared helpers used across handler submodules."""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the helpers the handler modules share: every name is reached
# through it as ``kiro_crew.dashboard.handlers._shared.<name>``. A read is served by the
# owner that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` --
# reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import
# this module, and it keeps no code of its own. New work goes to the owner of its
# responsibility:
#   ``_shared_scrub`` -- redacting memory fields, JSON transports and search rows
#   ``_shared_request_body`` -- bounded request-body and response reads
#   ``_shared_access`` -- the owner check, capability admission and caller bounds
#   ``_shared_session_memory`` -- a session's memory mode and restricted sessions
#   ``_shared_member_scope`` -- the member scope and the private-memory session check
#   ``_shared_route_guards`` -- shared route refusals and the owner-surface guard
#   ``_shared_workspace`` -- the caller's memory, lessons, skills and active project
#   ``_shared_skill_roots`` -- the trusted skill roots and skill-path containment
#   ``_shared_skill_listing`` -- the installed-skills list and the agents loading each
#   ``_shared_skill_catalog`` -- the skill catalog, agent skill mappings, skill files
#   ``_shared_memory_stores`` -- the memory store a request addresses and its tiers
#   ``_shared_config_followups`` -- the agent-spec rebuild and optional-extra commands
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers._shared_scrub",
    "kiro_crew.dashboard.handlers._shared_request_body",
    "kiro_crew.dashboard.handlers._shared_access",
    "kiro_crew.dashboard.handlers._shared_session_memory",
    "kiro_crew.dashboard.handlers._shared_member_scope",
    "kiro_crew.dashboard.handlers._shared_route_guards",
    "kiro_crew.dashboard.handlers._shared_workspace",
    "kiro_crew.dashboard.handlers._shared_skill_roots",
    "kiro_crew.dashboard.handlers._shared_skill_listing",
    "kiro_crew.dashboard.handlers._shared_skill_catalog",
    "kiro_crew.dashboard.handlers._shared_memory_stores",
    "kiro_crew.dashboard.handlers._shared_config_followups",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers._shared_access import (  # noqa: F401
        _admits,
        _audit_admission,
        _caller_bounds,
        _capability_manager,
        _owner_denial_response,
        admits_cloud_deployment,
        admits_registry,
        require_owner_dashboard_request,
        require_owner_for_dashboard_caller,
    )
    from kiro_crew.dashboard.handlers._shared_config_followups import (  # noqa: F401
        AGENT_SPEC_NOT_REWRITTEN,
        _pip_install_channel_available,
        logger,
        pip_extra_install_command,
        rebuild_agent_spec,
    )
    from kiro_crew.dashboard.handlers._shared_member_scope import (  # noqa: F401
        _MEMBER_SCOPE_KEY,
        MemberScope,
        _audit_private_memory_denial,
        _cron_execution_from_registry,
        internal_memory_scope,
        member_request_scope,
        require_private_memory_session,
        spawn_parent_claim,
    )
    from kiro_crew.dashboard.handlers._shared_memory_stores import (  # noqa: F401
        MEMORY_STORE_PARAM,
        _admin_store,
        _audit,
        _session_memory_store,
        _store_name,
        _store_tier_lock,
        _store_unavailable,
        lesson_store_owner_only,
        markdown_memory_for_store,
        memory_startup_refusal,
        release_markdown_memory_store,
        resolve_lesson_memory_store,
        resolve_requested_memory_store,
        vector_memory_for_store,
    )
    from kiro_crew.dashboard.handlers._shared_request_body import (  # noqa: F401
        _MAX_BODY_BYTES,
        _RESPONSE_READ_CHUNK_BYTES,
        _declares_json,
        _json_content_type_error,
        body_not_object_error,
        bounded_json_error_as_exception,
        bounded_json_error_code,
        read_bounded_json,
        read_capped_response,
    )
    from kiro_crew.dashboard.handlers._shared_route_guards import (  # noqa: F401
        _CRON_SESSION_CONTROL_PATHS,
        _MEMBER_CHAT_FOLDER_ID_METHODS,
        _MEMBER_CHAT_FOLDER_REORDER_METHODS,
        _MEMBER_CHAT_FOLDERS_METHODS,
        _MEMBER_CHAT_SLOT_DROP_METHODS,
        _MEMBER_CHAT_SLOT_FOLDER_METHODS,
        _MEMBER_CHAT_SLOT_PIN_METHODS,
        _MEMBER_CHAT_SLOT_TAGS_METHODS,
        _MEMBER_CHAT_SLOTS_METHODS,
        _MEMBER_CHAT_TAG_COLUMNS_METHODS,
        _MEMBER_CHAT_TAGS_METHODS,
        _SLOT_SCOPED_TRUST_MODES,
        RouteGuard,
        _admitted_chat_route_methods,
        _audit_cron_chat_denial,
        _cron_session_control_refusal,
        _member_admitted_to_chat_folder_tag_route,
        _single_id_segment,
        cron_creator_admission,
        cron_mode_refusal,
        cron_slot_creator,
        dashboard_owner_surface_guard,
        guard_owner_surface_routes,
        guarded_route,
        member_scope_denied_refusal,
        owner_surface_guard,
        owner_surface_route,
        private_chat_route_refusal,
        private_owner_surface_refusal,
    )
    from kiro_crew.dashboard.handlers._shared_scrub import (  # noqa: F401
        _MAX_JSON_SCRUB_DEPTH,
        _UNSCANNABLE_JSON,
        SESSION_SEARCH_TEXT_FIELDS,
        _decode_unique,
        _is_json_document,
        _redact_memory_field,
        _scrub_decoded,
        _scrub_json_transport,
        _scrub_text,
        _scrubbed_document_or,
        _UnscannableJSON,
    )
    from kiro_crew.dashboard.handlers._shared_session_memory import (  # noqa: F401
        _METADATA_HEAD_MAX_BYTES,
        _blocks_reads_session,
        _is_restricted_session,
        _persisted_session_memory_mode,
        _persisted_session_path,
        _persisted_session_paths,
        _probe_persisted_session,
        _read_memory_mode,
        _read_session_key,
        _session_has_persisted_history,
        inherited_session_memory_mode,
        is_restricted_session_key,
        live_session_memory_mode,
        require_live_session_memory_mode,
        resolve_session_memory_mode,
    )
    from kiro_crew.dashboard.handlers._shared_skill_catalog import (  # noqa: F401
        _CATALOG_SETTLE_NS,
        _GLOB_CHARS,
        _SKILL_NEST_DEPTH,
        MAX_AGENT_SKILLS,
        SKILL_FILE_MAX_BYTES,
        SKILL_TREE_MAX_ENTRIES,
        SkillCatalogSnapshot,
        _collect_skills_under,
        _dir_mtime_ns,
        _skill_key_roots,
        agent_skill_keys,
        agent_skill_views,
        agent_unmanaged_skill_uris,
        apply_skill_mapping,
        enumerate_skill_catalog,
        list_skill_tree,
        read_skill_file,
        skill_key_for_uri,
        skill_uri_for_key,
        walk_skill_catalog,
    )
    from kiro_crew.dashboard.handlers._shared_skill_listing import (  # noqa: F401
        _KIRO_SKILL_FRONTMATTER_BYTES,
        _agent_dirs,
        _agent_loads_skill,
        _agents_loading_skill,
        _expand_agent_globs,
        _expand_resource_uri,
        _kiro_skill_roots,
        _load_parsed_agents,
        _one_row_per_skill_file,
        _parse_skill_description,
        _resolve_loaded_by_agents,
        _row_is_editable,
        _row_rank,
        _skill_file_identity,
        _warn_skills_outside_roots,
        annotate_skills_with_agents,
        collect_skills_blocking,
        list_kiro_skills,
        skill_rows_for_globs,
    )
    from kiro_crew.dashboard.handlers._shared_skill_roots import (  # noqa: F401
        LEAF_SYMLINK_PREFIX,
        _canonical_skill_roots,
        _declared_app_skill_dirs,
        _dedupe_resolved,
        _edition_package_roots,
        _edition_skill_roots,
        _leaf_is_contained,
        _path_at_or_under,
        _resolve_package_skill_path,
        _resolve_skill_root,
        _resolved_set,
    )
    from kiro_crew.dashboard.handlers._shared_workspace import (  # noqa: F401
        _get_active_workspace,
        _get_lessons,
        _get_memory,
        _get_skills,
        _lesson_caller_slot_key,
        _resolve_active_project,
        _slot_project,
        active_project_dir,
        active_project_state,
        requesting_slot_project,
        session_folder_steering_dirs,
        session_project_for_spawn_gate,
    )
if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import extras  # noqa: F401
    from kiro_crew.agent_discovery import SKILL_URI_PREFIX  # noqa: F401
    from kiro_crew.agent_discovery import expand_skill_uri  # noqa: F401
    from kiro_crew.agent_discovery import parsed_agent_specs  # noqa: F401
    from kiro_crew.agent_discovery import skill_resource_uris  # noqa: F401
    from kiro_crew.agent_discovery import skill_uri_for_path  # noqa: F401
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401
    from kiro_crew.config.loader import config_dir  # noqa: F401
    from kiro_crew.config.paths import kiro_agents_dir  # noqa: F401
    from kiro_crew.dashboard.state import VALID_MEMORY_MODES  # noqa: F401
    from kiro_crew.dashboard.state import DashboardState  # noqa: F401
    from kiro_crew.dashboard.state import _normalize_slot_key  # noqa: F401
    from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS  # noqa: F401
    from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY  # noqa: F401
    from kiro_crew.dashboard.token_auth import _b64url_decode  # noqa: F401
    from kiro_crew.dashboard.token_auth import required_peer_key_unverified  # noqa: F401
    from kiro_crew.frontmatter import SKILL_LOADER  # noqa: F401
    from kiro_crew.frontmatter import parse_frontmatter  # noqa: F401
    from kiro_crew.history import TRANSCRIPT_HEADER_MAX_BYTES  # noqa: F401
    from kiro_crew.history import memory_mode_from_header_line  # noqa: F401
    from kiro_crew.hooks import FileTooLargeError  # noqa: F401
    from kiro_crew.hooks import safe_read_file_bytes_nolink  # noqa: F401
    from kiro_crew.loop_lock import LoopBoundLock  # noqa: F401
    from kiro_crew.messaging.link import is_channel_session_key  # noqa: F401
    from kiro_crew.messaging.privacy_mode import hydrate as _hydrate_conv_flags  # noqa: F401
    from kiro_crew.messaging.privacy_mode import is_incognito as is_thread_incognito  # noqa: F401
    from kiro_crew.messaging.privacy_mode import is_temporary as is_thread_temporary  # noqa: F401
    from kiro_crew.security import is_sensitive_path  # noqa: F401
    from kiro_crew.security import redact_credentials  # noqa: F401
    from kiro_crew.security import redact_exfiltration_urls  # noqa: F401
    from kiro_crew.skill_trust import is_project_trusted as _is_project_trusted  # noqa: F401
    from kiro_crew.skills import _matches_any  # noqa: F401
    from kiro_crew.skills import _trusted_skill_roots  # noqa: F401
    from kiro_crew.skills import _with_canonical_globs  # noqa: F401
    from kiro_crew.skills import skills_dir  # noqa: F401
    from kiro_crew.terminal_safe import normalize_for_scanning  # noqa: F401
    from kiro_crew.terminal_safe import strip_control_characters  # noqa: F401
