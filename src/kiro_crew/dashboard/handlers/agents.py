"""Agent administration HTTP handlers: the facade over :mod:`kiro_crew.dashboard.agent_admin`.

Routes, the handlers package, sibling handlers and tests import and patch every
agents handler through this module. The handler families live in the
``agent_admin`` owners -- ``app_mcp_ownership``, ``agent_config``,
``default_agent``, ``capabilities``, ``template_lineage``, ``fork_publish``,
``agent_detail``, ``roster``, ``installed_agents``, ``crew_records``,
``crew_update``, ``crew_removal`` and ``avatars`` -- and ``agent_admin.compose``
runs them on this module's globals, so a patch of ``agents.<name>`` reaches them.

This module keeps the imports, constants and seams the owners read (``_err500``,
``_sel``, ``_require_owner``, the ``_get_config_lock`` every config writer in the
dashboard takes), the config schema route, the model and picker reads
(``GET /api/models`` with its catalog cache and entitlement narrowing, effort
levels, slash commands) with the crew model-pin checks, and the two routes
repository guards read here by path: ``GET /api/agents`` and the avatar ``GET``.
New work goes to the owner of its family;
``docs/system-specs/modules/learn-cron-dashboard.md`` records the map.
"""

from __future__ import annotations

import asyncio
import contextlib  # noqa: F401
import copy  # noqa: F401
import dataclasses
import functools
import hashlib  # noqa: F401
import json
import logging
import os
import re
import stat  # noqa: F401
import subprocess
import time
import uuid
from collections.abc import Sequence  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, cast  # noqa: F401  (cast: crew_records reads it)

from aiohttp import BodyPartReader, web  # noqa: F401

from kiro_crew import agent_state  # noqa: F401
from kiro_crew import crew_teams as teams_mod  # noqa: F401
from kiro_crew import model_registry, model_scope
from kiro_crew.acp.client import advertised_model_ids, model_is_unusable
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    model_registry_namespace,
    selectable_backend_values,
)
from kiro_crew.agent import (  # noqa: F401
    AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
    SharedAgentHomeRefused,
    _atomic_json_write,
    _declined_foreign_spec_write,
    _is_confirmed_managed_dashboard_author,
    _refresh_forked_templates,
    _spec_path_is_safe,
    agents_spec_lock,
    clear_model_pin,
    emission_eligible_mcp_servers,
    get_shipped_tools,
    install_agent,
    kiro_agents_dir_path,
)
from kiro_crew.agent_capabilities import CapabilityError, require_unmanaged_template  # noqa: F401
from kiro_crew.agent_discovery import (  # noqa: F401
    SCOPE_GLOBAL,
    AgentInfo,
    _read_agent_spec,
    clear_list_agents_cache,
    list_agents,
    project_agent_names,
    spec_model,
    spec_str,
)
from kiro_crew.agent_files import (  # noqa: F401
    DASHBOARD_AUTHOR_AGENT_FILENAME,
    KAS_RESERVED_AGENT_IDS,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for, capabilities_of
from kiro_crew.agent_sdk.drivers.acp import (
    EntitlementRevalidating,
    catalog_row_would_drop,
    resolve_kiro_bin_for_spawn,
    resolve_pin_spelling,
    resolve_ssh_auth_sock,
)
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.agent_spec_format import (  # noqa: F401
    agent_spec_candidates,
    is_markdown_spec,
    iter_agent_spec_files,
)
from kiro_crew.apps.bridges import _mcp_lock as _agent_file_lock  # noqa: F401
from kiro_crew.apps.bridges import _registration_source  # noqa: F401
from kiro_crew.apps.manager import (  # noqa: F401
    INSTALLED_META_FILENAME,
    app_dir,
    app_enabled_state,
    apps_dir,
)
from kiro_crew.atomic_write import replace_with_retry  # noqa: F401
from kiro_crew.config.loader import (  # noqa: F401
    ConfigReadError,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    _safe_color,
    coerce_dict_section,
    coerce_effort,
    config_local_path,
    config_path,
    dispatch_kiro_agent,
    inject_kiro_cli_api_key,
    normalize_agent_model,
    read_config_text,
    resolve_agent_config_path,
    resolve_agent_identity,
    resolve_effective_model,
    update_config_locked,
    write_config_atomically,
)
from kiro_crew.config.paths import data_home  # noqa: F401
from kiro_crew.config.schema import SCHEMA_REGISTRY, config_entry_to_dict
from kiro_crew.config.sections import (  # noqa: F401
    _AVATAR_FILE_PIN_RE,
    _AVATAR_GHOST_BOOL_TRAITS,
    _AVATAR_GHOST_STR_TRAITS,
)
from kiro_crew.config.sections import _AVATAR_IMAGE_EXTS as _LOADER_AVATAR_IMAGE_EXTS
from kiro_crew.config.sections import (  # noqa: F401
    _safe_avatar,
)
from kiro_crew.dashboard import agent_admin as _agent_admin
from kiro_crew.dashboard.agent_admin import agent_config as _owner_agent_config
from kiro_crew.dashboard.agent_admin import agent_detail as _owner_agent_detail
from kiro_crew.dashboard.agent_admin import app_mcp_ownership as _owner_app_mcp_ownership
from kiro_crew.dashboard.agent_admin import avatars as _owner_avatars
from kiro_crew.dashboard.agent_admin import capabilities as _owner_capabilities
from kiro_crew.dashboard.agent_admin import crew_records as _owner_crew_records
from kiro_crew.dashboard.agent_admin import crew_removal as _owner_crew_removal
from kiro_crew.dashboard.agent_admin import crew_update as _owner_crew_update
from kiro_crew.dashboard.agent_admin import default_agent as _owner_default_agent
from kiro_crew.dashboard.agent_admin import fork_publish as _owner_fork_publish
from kiro_crew.dashboard.agent_admin import installed_agents as _owner_installed_agents
from kiro_crew.dashboard.agent_admin import roster as _owner_roster
from kiro_crew.dashboard.agent_admin import template_lineage as _owner_template_lineage
from kiro_crew.dashboard.agent_admin.agent_config import (  # noqa: F401
    _commit_agent_config,
    _commit_agent_config_locked,
    _find_agent_config,
    _installed_agent_config,
    _write_installed_config,
    api_agent_config,
)
from kiro_crew.dashboard.agent_admin.agent_detail import (  # noqa: F401
    _agent_detail_candidates,
    _merge_resources_delta,
    _reorder_named,
    api_agent_detail,
)
from kiro_crew.dashboard.agent_admin.app_mcp_ownership import (  # noqa: F401
    AppOwnershipUnreadable,
    _app_declared_server_names,
    _app_or_host_owned,
    _drop_unbacked_app_entries,
    _merge_unowned_servers,
    _on_disk_mcp_servers,
    _require_present_shape,
)
from kiro_crew.dashboard.agent_admin.avatars import (  # noqa: F401
    _avatar_stem,
    _avatar_variant_paths,
    _avatars_dir,
    _carry_motions_through_motionless_save,
    _carry_pack_through_faceless_save,
    _commit_promoted_avatar,
    _discard_pending_avatar,
    _image_body_complete,
    _is_ghost_shaped,
    _live_avatar_file,
    _pending_avatar_path,
    _promote_pending_avatar,
    _read_avatar_file,
    _remove_avatar_files,
    _rollback_promoted_avatar,
    _sniff_image_ext,
    _staging_token,
    api_kirocrew_agent_avatar_upload,
)
from kiro_crew.dashboard.agent_admin.capabilities import (  # noqa: F401
    _audit_capability,
    _is_valid_capability_package,
    _mutate_agent_package,
    api_capability_agents_install,
    api_capability_agents_list,
    api_capability_agents_uninstall,
    api_capability_mcp_install,
    api_capability_mcp_list,
    api_capability_mcp_registry,
    api_capability_mcp_uninstall,
    api_capability_plugins_list,
    api_capability_plugins_sync,
    api_capability_skills_install,
    api_capability_skills_list,
    api_capability_skills_uninstall,
)
from kiro_crew.dashboard.agent_admin.crew_records import (  # noqa: F401
    _api_kirocrew_agents_create,
    _crew_effort_rejected,
    _crew_memory_store_rejected,
    _refresh_session_defaults,
    api_kirocrew_agent_resolved_model,
    api_kirocrew_agents_create,
)
from kiro_crew.dashboard.agent_admin.crew_removal import (  # noqa: F401
    _member_slug_is_claimed,
    _prune_private_copy_of_deleted_crew,
    _reclaim_deleted_member_crew_log,
    api_kirocrew_agent_delete,
)
from kiro_crew.dashboard.agent_admin.crew_update import (  # noqa: F401
    _apply_member_approval_to_live_thread,
    _effort_inputs,
    api_kirocrew_agent_update,
)
from kiro_crew.dashboard.agent_admin.default_agent import (  # noqa: F401
    _alias_binding_template,
    _AmbiguousDefaultTarget,
    _AppRegisteredTemplate,
    _binds_template,
    _installed_template_alias,
    _is_app_registered,
    api_default_agent,
)
from kiro_crew.dashboard.agent_admin.fork_publish import (  # noqa: F401
    api_agent_fork,
    api_agent_publish,
    api_agent_reset,
)
from kiro_crew.dashboard.agent_admin.installed_agents import (  # noqa: F401
    _do_agents_sync,
    _namespaced_agent_file_exists,
    api_agents_installed,
    api_kirocrew_agents_sync,
)
from kiro_crew.dashboard.agent_admin.roster import (  # noqa: F401
    _agent_roster_row,
    _carries_mask,
    _name_would_be_masked,
    _roster_avatar,
    _roster_mask,
)
from kiro_crew.dashboard.agent_admin.template_lineage import (  # noqa: F401
    _AmbiguousTemplateName,
    _foreign_private_copy_owner,
    _ForeignPrivateCopy,
    _ForkBookkeepingFailed,
    _is_reserved_basename,
    _lineage_unverifiable_error,
    _load_template_specs,
    _PublishNameBound,
    _rebind_crew_locked,
    _reserved_binding_names,
    _sidecar_unverifiable,
    _spec_stem_on_disk,
    _StaleBinding,
    _unlink_copy_unless_referenced,
    _UnverifiableLineage,
    _write_spec_file,
)
from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_ordered
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    _BLOCKED_SLASH_COMMANDS,
    _SLASH_COMMANDS,
    SLASH_COMMAND_DESCRIPTIONS,
    _history_key_for,
    drained_to_thread,
    is_deprecated_model,
    run_config_write,
)
from kiro_crew.dashboard.conditional_get import conditional_response, strong_content_etag
from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
    MAX_AGENT_SKILLS,
    SkillCatalogSnapshot,
    _capability_manager,
    _read_session_key,
    active_project_dir,
    agent_skill_keys,
    agent_skill_views,
    apply_skill_mapping,
    enumerate_skill_catalog,
    read_bounded_json,
)
from kiro_crew.dashboard.handlers.agent_templates import (  # noqa: F401
    TEMPLATE_DEFINITION_KEYS,
    apply_definition_patch,
    read_only_reason_for_path,
    validate_definition_patch,
)
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.effort import EFFORT_LEVELS, EFFORT_VALUES  # noqa: F401
from kiro_crew.executors import discovery_executor, maintenance_executor, subprocess_executor
from kiro_crew.external_text import redact_external_text as _redact_external  # noqa: F401
from kiro_crew.kiro_prerequisite import spawn_supervised_oneshot
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.members import MemberNameError, key_new_crew, validate_member_name  # noqa: F401
from kiro_crew.memory_stores import (  # noqa: F401
    DEFAULT_MEMORY_STORE,
    MemberAlreadyExists,
    UnknownMemoryStore,
    memory_store_binding_defect,
    memory_store_namespace_lock,
    persist_member_config,
    provision_member_memory,
    retire_unpublished_allocation,
)
from kiro_crew.platform.governance import sanitize_agent_config_governance  # noqa: F401
from kiro_crew.platform_compat import is_link_or_junction, kill_and_reap  # noqa: F401
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    configured_sandbox_mode,
    scrub_agent_subprocess_env,
    wrap_argv,
)
from kiro_crew.user_json import loads_user_json  # noqa: F401
from kiro_crew.validation import TEMPLATE_NAME_RE  # noqa: F401

_MODEL_LIST_STDERR_TAIL_CHARS = 1000
# How long one GET /api/models waits for a catalog before answering the degraded
# 503. Unchanged: a remote hub still budgets its whole cold path against this
# bound (see api_models), so raising it would move
# DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS with it.
_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS: float = 10.0
# How long the SHARED background catalog fetch may run, decoupled from any one
# request. On a host where a cold `kiro-cli --list-models` spawn always exceeds
# the request bound, killing it at that bound meant no call ever finished, so
# every 8s poll started another doomed cold spawn and the picker never left
# `auto`. The fetch now outlives the request that started it: the request still
# answers 503 after _LIST_MODELS_SUBPROCESS_TIMEOUT_SECS, but the spawn keeps
# running under this ceiling and the next poll is served from its result.
_LIST_MODELS_BACKGROUND_TIMEOUT_SECS: float = 90.0
# A cached catalog younger than this is served straight away; an older one is
# served straight away too and refreshed behind the reply (stale-while-
# revalidate). The catalog changes rarely, so a few minutes stale costs nothing
# the per-request entitlement narrowing does not already correct.
_LIST_MODELS_CATALOG_TTL_SECS: float = 300.0
# The cache is the one place ``--list-models`` output is held past the request,
# so retention is bounded before it is stored. Row count and identifying-field
# length reuse the repo's ONE shared admission bound
# (``model_registry.ADVERTISED_MODELS_MAX_IDS`` /
# ``ADVERTISED_MODEL_ID_MAX_CHARS``) so the picker cannot serve an id the window
# / advertised stores refused — an over-long id is REFUSED (the whole row
# skipped), never truncated, matching ``admit_catalog_rows``. This cap bounds
# only the non-identifying extra fields a row may carry (the shared admission
# does not speak to those): each is clamped to the id-length bound and a row
# keeps at most this many.
_CATALOG_CACHE_MAX_FIELDS_PER_ROW = 64

logger = logging.getLogger(__name__)


def _err500(exc: BaseException) -> web.Response:
    """Return a generic 500 with a correlation id; log the detail server-side.

    Browser-facing 5xx bodies must not echo raw backend exception text
    (CWE-209). The short correlation id ties the sanitized client response to
    the full server-side log line (which retains the traceback).
    """
    corr = uuid.uuid4().hex[:12]
    logger.error("agents handler error [%s]", corr, exc_info=exc)
    return web.json_response({"error": "internal error", "id": corr}, status=500)


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


async def _require_owner(request: web.Request, operation: str) -> web.Response | None:
    """Owner gate shared by every mutating agents handler, here and in the owners.

    ``~/.kiro/agents`` and ``cfg.agents`` are machine-global: a write there
    installs tool grants and MCP server commands that later sessions execute,
    so mutations are owner-only — the same server-side boundary
    ``mcp_apps.api_mcp_apps_call`` enforces. The caller identity comes from
    the token-auth middleware (``request["user"]`` / ``request["app"]``),
    never from a client-set header. Returns the 403 to send, or ``None`` when
    the caller is the owner.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


# ── Config Schema ──


_CONFIG_SCHEMA_ACP_BACKEND = "agent.acp_backend"


def _supply_live_enum(entry: dict) -> None:
    """In place: give ``agent.acp_backend`` the values this build can actually serve.

    The field carries no static ``enum`` on purpose (see ``AgentConfig``): an
    edition registers its backends at boot, strictly after ``SCHEMA_REGISTRY`` is
    built, so a frozen list could only be wrong — it would omit a registered
    backend from the dashboard while the PATCH allowlist accepted it.

    Resolved from the same owner as the PATCH allowlist and the config load path,
    so the three cannot disagree. One binding today, so it is spelled once rather
    than made a registry; turn it into a path -> callable map when a second
    dynamic enum appears.
    """
    if entry.get("path") == _CONFIG_SCHEMA_ACP_BACKEND:
        entry["enumValues"] = selectable_backend_values()


async def api_config_schema(request: web.Request) -> web.Response:
    """GET /api/config/schema — return config schema entries."""
    entries = SCHEMA_REGISTRY

    # Filter by tags (comma-separated, intersection)
    tags_param = request.query.get("tags", "").strip()
    if tags_param:
        requested_tags = {t.strip() for t in tags_param.split(",") if t.strip()}
        entries = [e for e in entries if set(e.tags) & requested_tags]

    # Filter out deprecated entries when deprecated=false
    dep_param = request.query.get("deprecated", "").strip().lower()
    if dep_param == "false":
        entries = [e for e in entries if not e.deprecated]

    # Serialize, masking sensitive defaultValues and converting dataclass
    # defaults to None (they aren't JSON-serializable).
    result = []
    for entry in entries:
        d = config_entry_to_dict(entry)
        if entry.sensitive or dataclasses.is_dataclass(d.get("defaultValue")):
            d["defaultValue"] = None
        _supply_live_enum(d)
        result.append(d)

    return web.json_response({"entries": result})


_CAPABILITY_UNAVAILABLE = "capability manager not available"

#: Upper bound on a capability package name. Generous for a real package id, but
#: it stops an unbounded string from reaching an edition's argv or a path join.
_MAX_CAPABILITY_PACKAGE_LEN = 200
#: Package-name charset. Deliberately permissive enough for the real shapes
#: (scoped npm ids, ``Pkg-1.0``, ``package/skill`` paths) while excluding
#: whitespace and every shell metacharacter.
#:
#: A leading ``@`` is allowed so a bare scoped npm id (``@scope/pkg``) is accepted,
#: but it must be FOLLOWED by an alphanumeric: what excluding ``-`` at position 0
#: buys is that a flag-shaped value can never be read as an option, and ``@-evil``
#: would hand ``-evil`` to an installer that strips the scope prefix.
_VALID_CAPABILITY_PACKAGE_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._@:/-]*$")


def _normalize_model_key(name: str) -> str:
    """Canonical key for de-duping CC model ids across spelling variants.

    Mirrors ``normalizeModelKey`` in ``website/src/lib/model.ts``: both route a
    model id through the shared canonical registry (``model_registry.json``) so
    "same model?" has ONE definition across the dashboard (dropdown dedup, slot
    display, and the subagent downgrade flag).

    Resolution order:
    1. ``auto``/``default``/unset -> the ``auto`` sentinel (both mean "let the
       backend pick"); an empty id stays ``""`` (no pin, distinct from Auto).
    2. Registry canonical key: a canonical key, a registry alias, or a
       claude_code provider id -- with or without a region/vendor routing prefix
       (``us.anthropic.…``, ``global.anthropic.…``) -- folds to its canonical
       key. This makes an alias and its provider-prefixed canonical id equal
       (``us.anthropic.claude-opus-4-8[1m]`` == ``claude-opus-4.8`` ->
       ``opus-4.8-1m``) while keeping DISTINCT registry entries distinct -- the
       advertised dashed ``claude-opus-4-8`` (200K, ``opus-4.8``) does NOT fold
       onto dotted ``claude-opus-4.8`` (1M, ``opus-4.8-1m``); a bare
       ``.``->``-`` fold conflates those two different-window models.
    3. Fallback for an id the registry does not list (GPT/DeepSeek/Qwen, future
       models, operator-typed ids): a lossless fold -- lowercase,
       ``.``->``-`` -- so behavior is identity-preserving off the registered set,
       matching ``from_provider_id``'s pass-through contract.
    """
    string_fold = (name or "").strip().lower().replace(".", "-")
    if not string_fold:
        return ""
    if string_fold in ("default", "auto"):
        return "auto"
    # Registry lookups are exact and its keys/aliases/provider-ids are all
    # lowercase, so resolve on the lowercased id. canonical_key resolves
    # acp-first then claude_code AND peels a known routing prefix, so it covers
    # both spelling halves above; a miss returns None.
    resolved = model_registry.canonical_key((name or "").strip().lower())
    if resolved is not None:
        return resolved
    return string_fold


def _advertised_cc_models(request: web.Request, namespace: str) -> list[dict]:
    """Map a live provider's advertised models to the API shape, per namespace.

    ``model_name`` is the advertised id verbatim: it is the wire value sent back
    on selection, and the adapter only accepts ids it advertised. Returns ``[]``
    when no session of that namespace has initialized or the backend advertised
    nothing.

    Two filters, and both are load-bearing. The CAPABILITY gate
    (``SessionCapabilities.resolves_model_from_advertised_list``) is the property
    this list depends on: a backend whose served list is the only source of ids it
    accepts back is exactly the backend whose advertised list has to be read. The
    NAMESPACE gate (``model_id_namespace``) is whose ids these are. Harnesses
    can advertise different served ids, so a retained claude session would
    otherwise answer the codex picker with claude ids --
    every one of which codex refuses.

    Newest matching session first, like :func:`_entitled_kiro_models`: forward
    order is creation order, so the most recently started session carries the most
    recent snapshot of what the account is served.
    """
    try:
        state: DashboardState = request.app["state"]
        providers = state.sessions.active_providers()
    except (KeyError, AttributeError):
        return []
    for provider in reversed(providers):
        # Read each field straight off ``capabilities_of(provider)``: binding it to a
        # local would be a second spelling of the question, which the one-spelling
        # ratchet in test_agent_sdk_capabilities.py exists to keep greppable.
        if not capabilities_of(provider).resolves_model_from_advertised_list:
            continue
        if capabilities_of(provider).model_id_namespace != namespace:
            continue
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            advertised = getter()
        except Exception:
            continue
        if advertised:
            return [
                {
                    "model_name": m.get("modelId", ""),
                    "display_name": m.get("name", "") or m.get("modelId", ""),
                    "description": m.get("description", ""),
                }
                for m in advertised
                if m.get("modelId")
            ]
    return []


async def _entitled_kiro_models(request: web.Request, models: list[dict]) -> list[dict]:
    """Narrow the ``--list-models`` catalog to what a live session advertises.

    ``kiro chat --list-models`` is a CATALOG, not an entitlement: it returns the
    same rows whatever the account's tier, so after a downgrade it still offers
    (and still SHOWS as selected) a model no turn can run. The per-session
    ``session/new`` ``availableModels`` list is the tier-aware one — the same
    signal ``model_is_unusable`` pre-flights against before the wire — so when a
    live session has one, it wins here too. Same rule as the claude_code branch
    in :func:`_cc_models`: advertised is authoritative when present.

    The keep/drop decision delegates to ``model_is_unusable`` rather than
    comparing ids here, so the picker cannot disagree with the wire about what
    "this account can run" means. A local comparison would be a second spelling
    of that question — the exact drift that predicate exists to prevent — and any
    difference in how the two fold spelling variants shows up as a row the picker
    offers and the wire then withholds. A literal miss is retried through
    ``resolve_pin_spelling``, the same namespace fold the wire sites apply
    before withholding — but the row is REWRITTEN to the advertised spelling
    the fold answers with (and dropped when another row already offers that
    spelling): the selection sinks (the agent/crew pin validator and
    ``set_model``'s pre-flight) compare the picked value literally, so a row
    must carry an id they accept verbatim, not the catalog's stale
    ``<namespace>::<bare-id>`` qualifier it resolved from.

    The ``auto`` sentinel is never filtered: it means "inherit whatever the
    session already resolved", so it stays selectable even on a backend that does
    not advertise it by name.

    Only a session whose ids live in the catalog's own namespace can narrow it
    (``capabilities_of(provider).model_id_namespace``, the same gate
    :func:`_advertised_cc_models` applies). A live claude session advertises
    ``global.anthropic.…[1m]`` ids; ``resolve_pin_spelling`` folds those onto the
    catalog's bare ids because they name the same models, so without the gate a
    claude list would rewrite kiro's picker rows into claude's spelling and narrow
    them to claude's entitlements. Namespace is the question here, not spelling.

    Fails open in every unknowable case — no live session in this namespace, a
    backend that advertises nothing, or an advertised set that does not intersect
    the catalog at all under any spelling. Filtering on any of those would empty
    the picker, which is worse than listing one model too many.
    """
    try:
        state: DashboardState = request.app["state"]
        providers = state.sessions.active_providers()
    except (KeyError, AttributeError):
        return models
    advertised: list[str] = []
    catalog_ids = [m.get("model_name", "") for m in models]
    # Newest session first. `active_providers()` walks a dict of live sessions, so
    # forward order is creation order — and a session that started BEFORE a plan
    # change still holds the advertised list it captured at its own session/new.
    # Reading the oldest one would narrow the catalog to pre-downgrade
    # entitlements, i.e. keep offering exactly the models this narrowing exists to
    # hide. The most recently started session carries the most recent snapshot.
    for provider in reversed(providers):
        if capabilities_of(provider).model_id_namespace != model_registry_namespace(
            ACP_BACKEND_KIRO
        ):
            continue
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            ids = advertised_model_ids(getter())
        except Exception:
            continue
        if not ids:
            continue
        # The snapshot for the session that will narrow the picker gets a chance
        # to prove itself first. When it would drop a catalog model, the read
        # path has no explicit-pick refusal to trigger the refresh-before-refuse
        # heal, so an unconfirmed startup-race snapshot would silently hide
        # entitled models here. ``maybe_refresh_available_models`` is declared on
        # the provider ABC (default: return the current snapshot), owns the
        # staleness heuristic and the single-flight, fail-open probe; a probe
        # that fails or agrees leaves ``ids`` exactly as they were.
        try:
            refreshed = advertised_model_ids(
                await provider.maybe_refresh_available_models(catalog_ids)
            )
            if refreshed:
                ids = refreshed
        except EntitlementRevalidating:
            # The probe is in flight past the deadline. Propagate so the endpoint
            # returns its degraded response and the frontend polls again rather
            # than caching the un-revalidated snapshot; the next read serves the
            # landed result. Never swallowed as a fail-open.
            raise
        except Exception:
            pass
        advertised = ids
        break
    if not advertised:
        return models
    advertises_auto = any(_normalize_model_key(i) == "auto" for i in advertised)
    offered: set[str] = {
        _normalize_model_key(m.get("model_name", ""))
        for m in models
        if _normalize_model_key(m.get("model_name", "")) == "auto"
        or not model_is_unusable(m.get("model_name", ""), advertised)
    }
    kept: list[dict] = []
    for m in models:
        name = m.get("model_name", "")
        # The per-row keep/drop verdict is shared with the read-path
        # revalidation, so the probe decision and this filter agree on which
        # rows a snapshot hides.
        if catalog_row_would_drop(name, advertised):
            continue
        if _normalize_model_key(name) == "auto" or not model_is_unusable(name, advertised):
            kept.append(m)
            continue
        resolved = resolve_pin_spelling(name, advertised)
        if not resolved:
            continue
        key = _normalize_model_key(resolved)
        # Another row (literal or already-rewritten) offers this advertised
        # spelling: emitting a second one would show duplicate rows for one
        # model, so the qualified duplicate drops.
        if key in offered:
            continue
        offered.add(key)
        kept.append({**m, "model_name": resolved})
    # Tell "not comparable" apart from "entitled to almost nothing". A backend
    # that advertises `auto` shares a namespace with the catalog by definition, so
    # `auto` alone is a real answer — the most restricted tier there is — and must
    # narrow the picker to it. Only when nothing at all lines up, `auto` included,
    # is this a namespace mismatch (bare vs prefixed provider ids) where showing
    # the whole catalog beats emptying the picker. `auto` is always kept, so it can
    # never serve as the evidence that the two sides are comparable. A row
    # rewritten through the ``resolve_pin_spelling`` retry IS such evidence: a
    # peeled match proves the two vocabularies line up once the qualifier is
    # removed.
    if not advertises_auto and not any(
        _normalize_model_key(m.get("model_name", "")) != "auto" for m in kept
    ):
        return models
    return kept


def _cc_models(request: web.Request, configured_default: str = "") -> list[dict]:
    """Assemble the CC model dropdown, scoped to what the account can actually use.

    The live backend's advertised set is AUTHORITATIVE when present. It is the
    only source that reflects entitlement: claude-agent-acp captures it at session
    init from what the signed-in account is actually served. The registry is a
    static catalog of everything KiroCrew knows how to name, so a free-tier user
    shown it unfiltered is offered the full flagship list and discovers the truth
    only when a prompt fails.

    So when anything is advertised, registry rows are FILTERED DOWN to it (keeping
    the registry's cleaner display names for the survivors), and advertised models
    the registry does not list are appended for forward-compat.

    When NOTHING is advertised the registry is shown unfiltered. That is not a
    preference for the unfiltered list -- an empty advertised set means
    "no session has initialized yet", which is indistinguishable from "this account
    gets nothing", and showing an empty picker on a cold dashboard would be worse
    than showing a superset.

    ``auto`` is always present and always FIRST. It is the configured default
    (``config.agent.model``) and a sentinel rather than a real model, so it is
    never filtered by entitlement. It leads the list because the registry's own
    ``default: true`` flag sorts the current flagship to the top, which would
    present a specific paid model as the default in the picker.
    """
    advertised = _advertised_cc_models(request, "claude_code")
    registry_rows = model_registry.display_list("claude_code")

    if advertised:
        advertised_keys = {
            _normalize_model_key(e.get("model_name", ""))
            for e in advertised
            if _normalize_model_key(e.get("model_name", ""))
        }
        # Keep registry rows only when the backend also advertises them; "auto" is
        # a sentinel, not an entitlement, so it survives regardless.
        registry_rows = [
            e
            for e in registry_rows
            if _normalize_model_key(e.get("model_name", "")) in advertised_keys
            or _normalize_model_key(e.get("model_name", "")) == "auto"
        ]

    merged: list[dict] = []
    seen: dict[str, int] = {}
    for entry in (*registry_rows, *advertised):
        name = entry.get("model_name", "")
        key = _normalize_model_key(name)
        if not key:
            continue
        if key in seen:
            # Collision: registry keeps display, advertised id keeps the wire value.
            if key != "auto":
                merged[seen[key]] = {**merged[seen[key]], "model_name": name}
            continue
        seen[key] = len(merged)
        merged.append(entry)
    # "auto" leads. It may be absent entirely if a future registry drops the row,
    # so synthesize it rather than assuming the filter above preserved one.
    merged = [e for e in merged if _normalize_model_key(e.get("model_name", "")) == "auto"] + [
        e for e in merged if _normalize_model_key(e.get("model_name", "")) != "auto"
    ]
    if not any(_normalize_model_key(e.get("model_name", "")) == "auto" for e in merged):
        merged.insert(0, {"model_name": "auto", "display_name": "Auto", "description": ""})
        seen = {_normalize_model_key(e.get("model_name", "")): i for i, e in enumerate(merged)}
    # Guarantee the configured default is present (e.g. a custom cc_model the
    # backend doesn't advertise) so the selected model never vanishes. Resolve it
    # to its canonical key first (it may be stored as a provider id or alias) so a
    # default that already maps to a registry row does NOT produce a duplicate.
    if configured_default:
        canonical_default = model_registry.from_provider_id(
            model_registry.to_provider_id(configured_default, "claude_code"), "claude_code"
        )
        # Skip a blank canonical key: cc_model="auto" round-trips to "" (auto's
        # provider id is empty), and _normalize_model_key("")=="" is never in
        # `seen` (which holds "auto"), so without the `if key` guard — the same
        # one the merge loop above uses — a blank-named row would be inserted as
        # the first/selected dropdown option. The "auto" registry row already
        # covers this case.
        key = _normalize_model_key(canonical_default)
        # Only resurrect the configured default when entitlement cannot contradict
        # it: either nothing was advertised (unknown, so trust config) or it WAS
        # advertised but the registry lacked a row. Force-including a model the
        # backend did not advertise would reintroduce exactly the unusable option
        # this filter removes -- a stale config pick outliving the entitlement.
        may_include = not advertised or key in {
            _normalize_model_key(e.get("model_name", "")) for e in advertised
        }
        if key and key not in seen and may_include:
            # After "auto", never before it: "auto" is the configured default in
            # the general case and leads the list.
            merged.insert(
                (
                    1
                    if merged and _normalize_model_key(merged[0].get("model_name", "")) == "auto"
                    else 0
                ),
                {
                    "model_name": canonical_default,
                    "display_name": canonical_default,
                    "description": "Configured default",
                },
            )
    # Enrich every row with a context_window via the central authority so the CC
    # dropdown carries the same field the kiro branch does (the frontend picker
    # + tooltip read it uniformly). None -> reference (never a silent 200k).
    for entry in merged:
        if "context_window" not in entry:
            name = entry.get("model_name", "")
            entry["context_window"] = (
                model_registry.model_window(name) or model_registry.REFERENCE_WINDOW_TOKENS
            )
    return merged


def _advertised_backend_models(
    request: web.Request, backend: str, configured_default: str = ""
) -> list[dict]:
    """Assemble model choices from one ACP backend's advertised namespace.

    A live session wins over the cross-session cache. Both sources retain the
    adapter's exact model ids, which may be provider/model pairs for Pi. A cold
    cache offers ``auto`` and, when set, the configured default until the first
    session advertises its choices.
    """
    namespace = model_registry_namespace(backend)
    advertised = _advertised_cc_models(request, namespace)
    if not advertised:
        cached = model_registry.advertised_models(namespace)
        advertised = [{"model_name": m, "display_name": m, "description": ""} for m in cached]

    rows: list[dict] = [
        {"model_name": "auto", "display_name": "Auto", "description": "Backend default"}
    ]
    seen: set[str] = {"auto"}
    for entry in advertised:
        name = str(entry.get("model_name", "") or "").strip()
        if not name or _normalize_model_key(name) == "auto" or name in seen:
            continue
        seen.add(name)
        rows.append(
            {
                "model_name": name,
                "display_name": entry.get("display_name") or name,
                "description": entry.get("description", ""),
            }
        )
    default = (configured_default or "").strip()
    if (
        default
        and _normalize_model_key(default) != "auto"
        and default not in seen
        and not advertised
    ):
        rows.insert(
            1, {"model_name": default, "display_name": default, "description": "Configured default"}
        )
    for entry in rows:
        entry["context_window"] = (
            model_registry.model_window(entry["model_name"])
            or model_registry.REFERENCE_WINDOW_TOKENS
        )
    return rows


def _wrap_list_models_argv(argv: list[str]) -> tuple[list[str], str | None]:
    """Sandbox-wrap the ``--list-models`` argv at the configured tier.

    Runs in an executor, never on the loop: :func:`configured_sandbox_mode` stats
    (and on a cache miss re-reads and revalidates) ``config.json``, and
    ``wrap_argv`` -> ``detect_backend`` can cold-probe the sandbox backend with a
    synchronous ``subprocess.run(..., timeout=5)``. Resolving the mode here rather
    than passing it in keeps BOTH blocking reads in the worker thread.

    ``is_kiro_cli=True`` is explicit because ``_spawns_kiro_cli``'s basename test
    only matches a literal ``kiro-cli``: a Windows ``kiro-cli.exe``, a wrapper
    shim, or a ``KIROCREW_KIRO_BIN`` pointing at a nonstandard launch path all
    read as "not kiro-cli". The positive classification is also the security gate
    for default Windows delegation to Kiro's internal sandbox; basename inference
    cannot grant it. Both ACP spawn paths pass this flag for the same reason.
    """
    return wrap_argv(argv, mode=configured_sandbox_mode(), is_kiro_cli=True)


def _scoped_default(cfg: Any, backend: str) -> str:
    """The stored pin, but only when *backend*'s own harness can claim it.

    A pin chosen in another harness is not a default here: marking it as one puts
    an id the returned list does not even contain into the picker's selected slot,
    while every turn runs the harness default. Same rule the provider factory
    sends by, so the marker and the wire agree.

    Called from the two branches that read the pin rather than once above them:
    the kiro branch does not consult ``agent.model`` at all, and pulling the read
    up would make it pay for a value it never uses (harness-parity H13 -- the
    test is whether the kiro path changed, not whether it still works).
    """
    return model_scope.scoped_pin(
        getattr(cfg.agent, "model", "") or "",
        capabilities_for(backend).model_id_namespace,
        source="api_models",
        log_level=logging.DEBUG,
    )


class _CatalogUnavailable(Exception):
    """A degraded catalog fetch outcome that maps to one 503 body.

    The fetch runs in a shared background task that cannot return an HTTP
    response, so each degraded outcome (binary unresolved, timeout, non-zero
    exit, empty / invalid output) raises this carrying its error message, and
    the request path renders a dict-literal 503 body from it — one 503 per
    outcome. The message is carried, not a response dict, so the single render
    site stays a dict literal the error-code ratchet can scan.
    """

    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


def _bounded_catalog(models: list[dict]) -> list[dict]:
    """Clamp a parsed catalog to what the cache may safely retain.

    The retention point (:data:`_catalog_cache`) holds the list past the request,
    so it is bounded here before it is stored, through the SAME admission the
    window / advertised stores use — ``model_registry.ADVERTISED_MODELS_MAX_IDS``
    rows, each identifying field (``model_name`` / ``model_id``) at most
    ``ADVERTISED_MODEL_ID_MAX_CHARS`` — so the cache cannot serve an id those
    stores refused. An over-long id is REFUSED (its whole row skipped), never
    truncated, matching :func:`model_registry.admit_catalog_rows`'s own
    invariant; the picker reads the name, so a sliced name would name a different
    model or none.

    Each remaining scalar field (str / int / float / bool / None) is kept, with
    strings clamped to the id-length bound and a row holding at most
    :data:`_CATALOG_CACHE_MAX_FIELDS_PER_ROW`; a non-scalar field (nested object
    or array) is dropped — no reader of the cached list consumes one, and it is
    the unbounded edge the retention bound exists to close. Row ORDER is
    preserved (readers treat catalog order as meaningful). Rows and fields
    discarded for any reason are counted and logged once per snapshot.
    """
    max_rows = model_registry.ADVERTISED_MODELS_MAX_IDS
    max_chars = model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
    bounded: list[dict] = []
    dropped_rows = 0
    dropped_fields = 0
    for row in models:
        if len(bounded) >= max_rows:
            dropped_rows += 1
            continue
        if not isinstance(row, dict):
            dropped_rows += 1
            continue
        # Refuse (skip) a row whose identifying field is over-long, rather than
        # slicing it into an id no store retained.
        ident = row.get("model_name") or row.get("model_id")
        if isinstance(ident, str) and len(ident) > max_chars:
            dropped_rows += 1
            continue
        kept: dict = {}
        for key, value in row.items():
            if len(kept) >= _CATALOG_CACHE_MAX_FIELDS_PER_ROW:
                dropped_fields += 1
                continue
            # The KEY is a retained string too: refuse (skip) an over-long one
            # rather than cache it, so no retained string is unbounded.
            if isinstance(key, str) and len(key) > max_chars:
                dropped_fields += 1
                continue
            if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
                kept[key] = value
            elif isinstance(value, str):
                kept[key] = value[:max_chars]
            else:
                # Non-scalar (nested dict/list): dropped — unbounded and unread.
                dropped_fields += 1
        bounded.append(kept)
    if dropped_rows or dropped_fields:
        logger.warning(
            "api_models: catalog cache dropped %d row(s) and %d field(s) over the "
            "retention bound (max_rows=%d, max_id_chars=%d)",
            dropped_rows,
            dropped_fields,
            max_rows,
            max_chars,
        )
    return bounded


@dataclasses.dataclass
class _CatalogCache:
    """The last good ``--list-models`` catalog, kept in memory only.

    One slow success is enough: once ``models`` is set, every request is served
    from it (after per-request entitlement narrowing) while a stale entry is
    refreshed behind the reply. A gateway restart drops it, so the first request
    after a restart pays the cold start again — the catalog is not persisted,
    which keeps a downgraded account's stale rows from surviving a restart.

    ``task`` single-flights the fetch: concurrent polls (and the 8s self-heal
    loop) share one spawn rather than each starting its own cold start.
    """

    models: list[dict] | None = None
    fetched_at: float = 0.0
    task: "asyncio.Task[list[dict]] | None" = None


_catalog_cache = _CatalogCache()


async def _fetch_kiro_catalog() -> list[dict]:
    """Spawn ``kiro-cli --list-models`` once and return the catalog rows.

    Calls the module-level ``resolve_kiro_bin_for_spawn`` /
    ``resolve_ssh_auth_sock`` wrappers directly. They are imported from
    ``kiro_crew.agent_sdk.drivers.acp`` — the agent-SDK surface, not a forbidden
    root — so this background worker reaches the spawn helpers without adding an
    ACP-layer edge (see ``scripts/check_agent_sdk_boundary.py``).

    Returns the deprecated-stripped list, each row under the ``model_id`` its
    session advertises (see ``wire_row`` below), and seeds the window/advertised caches —
    the UNFILTERED-then-deprecated-stripped list, i.e. the response body before
    per-request entitlement narrowing. Raises :class:`_CatalogUnavailable` for a
    degraded outcome (binary unresolved, timeout, non-zero exit, empty / invalid
    output) and lets :class:`SandboxUnavailableError` propagate; the request path
    renders each as its own 503.

    Bounded by :data:`_LIST_MODELS_BACKGROUND_TIMEOUT_SECS`, NOT the request wait,
    so a cold start that outlasts one poll still finishes and warms the cache for
    the next one.
    """
    from kiro_crew.env import augmented_path  # noqa: F811

    kiro_bin = await resolve_kiro_bin_for_spawn()
    if not kiro_bin:
        # Degraded (binary not resolved yet), NOT a genuine "zero models" result.
        # 503 so the client retries instead of caching an empty list — a cached
        # [] renders an empty picker that only a manual page refresh recovers from.
        raise _CatalogUnavailable("kiro binary not resolved")
    argv = [kiro_bin, "chat", "--list-models", "--format", "json", "--no-interactive"]
    # Mirror AcpClient._spawn() sandbox: wrap_argv + env + process isolation.
    # Note: AcpClient._spawn() is for interactive ACP sessions (stdin/stdout
    # pipes); this is a one-shot read-only command, so we replicate the
    # sandbox setup directly.  See the security-controls rule.
    #
    # The configured tier is passed EXPLICITLY rather than left to
    # wrap_argv's "auto" parameter default, so this endpoint can never ask
    # for stricter isolation than the chat spawn of the same binary. It
    # matters wherever the operator set agent.sandbox="off" (deferring
    # isolation to kiro-cli's own internal sandbox): the one-shot and chat
    # path must have one posture. The explicit Kiro classification also
    # makes the shipped "auto" tier work on Windows via Kiro's built-in
    # sandbox instead of answering 503 on every 8s poll.
    #
    # OFF the loop: `configured_sandbox_mode()` stats (and on a cache miss
    # re-reads + revalidates) config.json, and `wrap_argv` -> `detect_backend`
    # can cold-probe the backend with a synchronous
    # `subprocess.run(..., timeout=5)`. This runs behind the request, but a
    # blocking call on the event loop still stalls chat, cron and the liveness
    # heartbeat on exactly the host where the probe is slowest. Both reads run
    # in the worker, so the mode is resolved there too rather than passed in.
    #
    # A remote hub proxying this endpoint budgets its WHOLE cold path (the
    # sandbox detection, the list-models subprocess, and the up to
    # _READ_PATH_PROBE_DEADLINE_SECS the read-path entitlement revalidation
    # waits in _entitled_kiro_models) via
    # DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS in
    # kiro_crew/instances/constants.py — 5 + 10 + 3 < 20. The per-request wait
    # (_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS) is what that budget sizes against,
    # NOT the longer background ceiling; growing the request wait means moving
    # that constant with it.
    argv, cleanup = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), _wrap_list_models_argv, argv
    )
    argv = cgroup_scope_argv(argv)  # cgroup DoS ceiling
    try:
        env = {**os.environ}
        env["PATH"] = augmented_path(env.get("PATH", ""))
        # OFF the loop: the resolver globs /tmp/ssh-*/agent.* and stats
        # every hit, so its latency scales with the /tmp entry count. Its
        # sibling wrapper's contract states it must never run on the event loop.
        await asyncio.to_thread(resolve_ssh_auth_sock, env)
        # The Docker entrypoint removes credentials from the long-lived
        # gateway environment.  This fixed-argv child is the official
        # kiro-cli and KIRO_API_KEY is its own model credential, so settle
        # the same single key the interactive ACP spawn receives.  Keep
        # the protected .env read off the gateway loop.
        await asyncio.to_thread(inject_kiro_cli_api_key, env)
        env = scrub_agent_subprocess_env(env)
        # Supervised so the call ends whatever it leaves behind. A kiro-cli
        # launcher wrapper can start a ~140-thread credential helper for each
        # call and leave it running; the self-heal loop re-polls every 8s while
        # degraded, so on a gateway on that path every poll leaked one until
        # the agent cgroup ran out of pids.
        proc = await spawn_supervised_oneshot(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_LIST_MODELS_BACKGROUND_TIMEOUT_SECS
            )
        except asyncio.TimeoutError:
            # The whole group, while the supervisor still leads it: killing
            # only the leader would leave the command and its helpers running.
            await kill_and_reap(proc)
            # A cold CLI spawn exceeded even the background ceiling. 503 so the
            # client keeps its last-good list and polls again rather than caching
            # a successful empty result.
            logger.warning("api_models: --list-models timed out; returning 503")
            raise _CatalogUnavailable("model list timed out")
    finally:
        if cleanup and callable(cleanup):
            cleanup()

    if proc.returncode != 0:
        from kiro_crew.platform import redact_via_context  # noqa: F811

        stderr_tail = stderr.decode(errors="replace").strip()
        stderr_tail = redact_via_context(stderr_tail)[-_MODEL_LIST_STDERR_TAIL_CHARS:]
        logger.warning(
            "api_models: --list-models exited %s: %s; returning 503",
            proc.returncode,
            stderr_tail or "<no stderr>",
        )
        raise _CatalogUnavailable("model list command failed")

    if not stdout.strip():
        logger.warning("api_models: --list-models returned empty output; returning 503")
        raise _CatalogUnavailable("model list returned empty output")

    try:
        data = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        logger.warning(
            "api_models: --list-models returned invalid JSON (%s); returning 503",
            exc,
        )
        raise _CatalogUnavailable("model list returned invalid JSON")
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        logger.warning("api_models: --list-models returned an invalid payload; returning 503")
        raise _CatalogUnavailable("model list returned an invalid payload")
    models = data["models"]
    # Seed the central window authority from kiro's authoritative structured
    # 'context_window_tokens' field (keyed by model_id/model_name). This is
    # the ONE place these rows enter the system; every other consumer (the
    # ACP backfill, the context-budget scaler, the live meter) then resolves
    # through model_registry.model_window() rather than re-reading kiro. The
    # in-memory update is synchronous (cheap dict mutation); only the disk
    # persist is offloaded to an executor so the event loop never blocks on
    # filesystem I/O (no blocking call on the event loop).
    #
    # This fork keeps kiro's bare-dotted ids as the picker WIRE FORMAT
    # (guarded by _model_rejected_reason / api_chat_slot_model, which rejects
    # canonical registry keys the ACP CLI can't accept). The upstream
    # registry-key canonicalization is deliberately NOT ported — it is
    # incompatible with this fork's _model_rejected_reason guard. The window
    # seeding above uses kiro's authoritative context_window_tokens to give
    # the backfill real GPT/DeepSeek/Qwen windows, independent of the
    # wire-format choice.
    #
    # The same rows also warm the ``acp`` advertised-model cache, kiro's
    # VOCABULARY: which ids are kiro's own, so model_scope can tell a pin
    # chosen for another harness from one chosen here before any session
    # exists (the chip and the provider factory judge from the cache; the
    # wire holds the live list). ONE admission feeds both caches
    # (refresh_kiro_catalog), so an id the bound refuses gets no row in
    # either. Fed from the UNFILTERED catalog on purpose: a deprecated or
    # unentitled row is still a kiro id, and dropping it here would make
    # model_scope call a native pin foreign. Entitlement stays with the
    # live ``session/new`` list downstream (_entitled_kiro_models,
    # model_is_unusable) -- ``--list-models`` is a catalog and no reader
    # of this cache treats it as more. Sourced here rather than from any
    # ``session/new`` payload because the registry attributes that payload
    # to claude-agent-acp and a kiro session's list is scoped to the agent
    # that session started. In-memory updates on the loop, disk persists
    # off it.
    windows_changed, advertised_changed = model_registry.refresh_kiro_catalog(
        models, model_registry_namespace(ACP_BACKEND_KIRO)
    )
    if windows_changed:
        await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), model_registry.persist_kiro_windows
        )
    if advertised_changed:
        await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), model_registry.persist_advertised_models
        )

    def wire_row(row: dict) -> dict:
        # Every reader downstream treats a row's ``model_name`` as the model id:
        # the entitlement narrowing judges it against the live ``session/new``
        # ``availableModels`` ids, the pin validators compare it literally, and
        # the picker sends it to ``session/set_model``. kiro-cli prints a
        # ``model_id`` beside the name, and a model can carry a display name that
        # differs from its id. ``session/new`` advertises the ``model_id``, so a
        # row left under its printed name is never found among the advertised ids
        # and the picker hides a model the account can run. Such a row is served
        # under the ``model_id``, with the printed name kept as ``display_name``;
        # a row with no usable ``model_id`` is returned unchanged. Nested rather
        # than module-level: the facade's definitions are pinned by
        # ``test/test_dashboard_agents_composition_contract.py``.
        model_id = row.get("model_id")
        name = row.get("model_name")
        if not isinstance(model_id, str) or not model_id.strip() or model_id == name:
            return row
        label = name if isinstance(name, str) and name.strip() else model_id
        return {**row, "model_name": model_id, "display_name": row.get("display_name") or label}

    return [wire_row(m) for m in models if not is_deprecated_model(m.get("model_name", ""))]


def _shared_catalog_fetch() -> "asyncio.Task[list[dict]]":
    """Return the in-flight catalog fetch, starting one if none is running.

    Single-flight: concurrent polls share one spawn. The task caches its result
    (and clears itself) when it finishes, so the next poll after a slow success
    is served straight from the cache rather than starting another cold start.
    """
    existing = _catalog_cache.task
    if existing is not None and not existing.done():
        return existing

    async def _run() -> list[dict]:
        try:
            models = await _fetch_kiro_catalog()
        finally:
            # Release the single-flight slot the moment this attempt ends,
            # win or lose, so a failed fetch does not wedge every later poll.
            _catalog_cache.task = None
        # Bound the retained list before it enters the one store that outlives
        # the request (see _bounded_catalog).
        bounded = _bounded_catalog(models)
        _catalog_cache.models = bounded
        _catalog_cache.fetched_at = time.monotonic()
        return bounded

    task = asyncio.ensure_future(_run())
    _catalog_cache.task = task
    # A stale-while-revalidate refresh is fire-and-forget: the request path
    # returns the cached list without awaiting this task, so a degraded refresh
    # (expired auth, background-ceiling timeout, SandboxUnavailableError) would
    # leave an unretrieved exception that crash_guard's asyncio handler logs as a
    # full crash record at the 8s poll cadence. Consume it here: a failed refresh
    # keeps the last-good cache and is a debug line, not a crash.
    task.add_done_callback(_consume_refresh_exception)
    return task


def _consume_refresh_exception(task: "asyncio.Task[list[dict]]") -> None:
    """Retrieve a finished refresh task's exception so it is never 'unhandled'.

    A cached catalog means a degraded background refresh is non-fatal — the
    request path already served the last-good list — so its exception is logged
    at debug and swallowed rather than left for ``Task.__del__`` to escalate.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("api_models: background catalog refresh failed (serving cached)", exc_info=exc)


async def api_models(request: web.Request) -> web.Response:
    """GET /api/models — the model list for the configured backend.

    kiro-family backends read kiro-cli's ``--list-models`` catalog (narrowed to a
    live session's entitlement); advertised-selection backends read their own
    adapter's namespace, because they do not accept ids from that catalog.
    """
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    backend = getattr(cfg.agent, "acp_backend", "")
    if backend == ACP_BACKEND_CLAUDE:
        return web.json_response(
            _cc_models(request, configured_default=_scoped_default(cfg, backend))
        )
    if capabilities_for(backend).resolves_model_from_advertised_list:
        return web.json_response(
            _advertised_backend_models(
                request, backend, configured_default=_scoped_default(cfg, backend)
            )
        )
    # Signed-out gateways must never reach the spawn below. kiro-cli auto-opens
    # an interactive browser login for ANY subcommand run unauthenticated
    # (--no-interactive does not suppress it, and there is no opt-out env var),
    # and the frontend polls this endpoint every 8s while the model list is
    # degraded — which is exactly the signed-out state. Ungated, that pairing
    # opened a browser window every 8s indefinitely. The 503 is the same
    # degraded response the timeout/unresolved branches already return, so the
    # client contract is unchanged; only the subprocess is skipped.
    #
    # Keep the tight destructive bound here: this endpoint polls only while the
    # model list is a degraded fallback and stops the instant a live fetch wins
    # (website/src/providers/modelListHealth.ts), so it is not polled at all when
    # healthy and gains no latency from a wider window. It has no server-side
    # cooldown on its kiro-cli spawn, so a wide stale-authorize window would let a
    # stale ready=True latch spawn a browser-opening login on most degraded ticks.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    try:
        cached = _catalog_cache.models
        if cached is not None:
            # Serve the last good catalog straight away; a stale one is refreshed
            # behind the reply (stale-while-revalidate). Entitlement narrowing
            # still runs on every request, so a plan change is reflected now even
            # when the catalog rows themselves are a few minutes old.
            if time.monotonic() - _catalog_cache.fetched_at >= _LIST_MODELS_CATALOG_TTL_SECS:
                _shared_catalog_fetch()
            models = await _entitled_kiro_models(request, list(cached))
            return web.json_response(models)

        # No catalog yet. Join (or start) the one shared background fetch and wait
        # at most the per-request bound for it — the SAME 10s a request waited
        # before. On timeout we answer the same degraded 503, but the fetch is NOT
        # cancelled: it runs on under its own longer ceiling and warms the cache,
        # so the next 8s poll is served from its result instead of starting
        # another doomed cold start.
        task = _shared_catalog_fetch()
        try:
            models = await asyncio.wait_for(
                asyncio.shield(task), timeout=_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS
            )
        except asyncio.TimeoutError:
            # The cold spawn outran one request. Keep the shared fetch running
            # (shield already detached it from this await) so the next poll lands
            # on the warmed cache rather than re-spawning.
            logger.warning("api_models: --list-models slower than the request bound; returning 503")
            return web.json_response({"error": "model list timed out"}, status=503)
        models = await _entitled_kiro_models(request, list(models))
        return web.json_response(models)
    except _CatalogUnavailable as exc:
        # A degraded fetch outcome (binary unresolved, timeout, non-zero exit,
        # empty / invalid output). The carried body is the 503 for that outcome.
        return web.json_response({"error": exc.error}, status=503)
    except EntitlementRevalidating:
        # An entitlement revalidation is in flight past the read deadline. The
        # picker snapshot might narrow the catalog on an un-revalidated answer,
        # and the frontend caches any non-empty 200 with no refetch — so serve
        # the degraded 503 contract instead: the frontend keeps its last-good
        # list and polls again in 8s, and the next read (once the probe has
        # landed, whether it corrected the list or failed open) returns 200.
        logger.info("api_models: entitlement revalidation in flight; returning 503 to re-poll")
        return web.json_response(
            {"error": "model list revalidating", "code": "model_list_revalidating"},
            status=503,
        )
    except SandboxUnavailableError as exc:
        # Narrower than the generic clause below, and BEFORE it: this is the one
        # degraded cause that no amount of retrying fixes, so it must not be
        # reported as an anonymous "model list unavailable". Reached only when the
        # tier resolved to "auto" (the shipped default) on a host with no
        # backend, where a configured "off" passes through
        # configured_sandbox_mode() above and never lands here.
        #
        # Still a 503: the client contract for "degraded, keep the last-good list
        # and poll" is what keeps the picker from caching an empty result, and a
        # 4xx here would make the frontend treat a host-capability problem as a
        # bad request. The `code` is what lets the UI tell this apart from a
        # timeout, and the log carries the sandbox layer's own remedy text (which
        # names the agent.sandbox_allow_unsandboxed_exec opt-in).
        logger.warning(
            "api_models: sandbox refused the --list-models spawn (kind=%s, detail=%s); "
            "returning 503. Retrying will not clear this — %s",
            exc.kind,
            exc.detail,
            exc,
        )
        return web.json_response(
            {"error": "model list unavailable", "code": "model_list_sandbox_unavailable"},
            status=503,
        )
    except Exception:
        # Spawn failure, JSON parse error, etc. — degraded, not "zero models".
        # 503 so the client retries instead of caching an empty picker.
        logger.warning("api_models failed; returning 503 for client retry", exc_info=True)
        return web.json_response({"error": "model list unavailable"}, status=503)


async def api_effort_levels(request: web.Request) -> web.Response:
    """GET /api/effort-levels — list available reasoning effort levels.

    Per-slot: when a ``?slot=`` query param resolves to a live ACP provider,
    return the levels that slot's CURRENT model reported (ACP escalation order),
    so concurrent slots on different models/backends each see their own set and
    a model switch is reflected immediately. Falls back to the process-global
    ordered list (cold start / no live provider / provider without the getter).
    """
    slot = request.query.get("slot")
    if slot:
        try:
            state: DashboardState = request.app["state"]
            provider = state.sessions.get_provider(_history_key_for(slot))
            getter = getattr(provider, "get_valid_effort_levels", None) if provider else None
            if callable(getter):
                levels = getter()
                if levels:
                    return web.json_response(levels)
        except (KeyError, AttributeError):
            pass
    return web.json_response(get_reasoning_effort_ordered())


async def api_slash_commands(request: web.Request) -> web.Response:
    """GET /api/slash-commands — list available slash commands (provider-aware)."""
    cfg = KiroCrewConfig.load()
    # This global endpoint has no session/member key; the configured default
    # backend is its capability authority, independent of the provider label.
    blocked_commands = _BLOCKED_SLASH_COMMANDS
    if capabilities_for(cfg.agent.acp_backend).supports_native_todos:
        blocked_commands = blocked_commands - {"/todos"}
    if is_claude_code(cfg.agent.provider):
        state: DashboardState = request.app["state"]
        cc_commands: list[str] = []
        for provider in state.sessions.active_providers():
            cmds = getattr(provider, "_slash_commands", [])
            if cmds:
                cc_commands = cmds
                break
        if not cc_commands:
            cc_commands = [
                "compact",
                "clear",
                "context",
                "help",
                "init",
                "review",
                "security-review",
                "usage",
            ]
        result = [
            {"name": f"/{c}", "description": SLASH_COMMAND_DESCRIPTIONS.get(f"/{c}", "")}
            for c in cc_commands
            if f"/{c}" not in blocked_commands
        ]
        for command in ("/side", "/workflow"):
            if not any(item["name"] == command for item in result):
                result.append(
                    {"name": command, "description": SLASH_COMMAND_DESCRIPTIONS.get(command, "")}
                )
        return web.json_response(result)

    # Blocked commands stay in _SLASH_COMMANDS (typing one still gets the
    # explicit "not available in the dashboard" rejection in chat_runner), but
    # the suggestion payload must not advertise them: a menu entry that only
    # ever produces a warning is an inert affordance.
    return web.json_response(
        [
            {"name": c, "description": SLASH_COMMAND_DESCRIPTIONS.get(c, "")}
            for c in sorted(_SLASH_COMMANDS - blocked_commands)
        ]
    )


# Windows reserves these basenames (before the first dot, any extension) at the
# filesystem level: creating CON.json raises, and some transports mangle them.
# Checked wherever a template filename is chosen — user-supplied publish names
# are refused, generated fork names are suffixed past them.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


# ── Kiro Crew Agent CRUD API ──


async def api_kirocrew_agents(request: web.Request) -> web.Response:
    """GET /api/agents — list all Kiro Crew agent definitions, most-used first.

    Also surfaces the requesting session's project-scope agents
    (``<project>/.kiro/agents``, resolved via ``X-Session-Key``) tagged
    ``scope="project"`` — these dispatch from that slot because kiro-cli runs
    with the slot's project as cwd, so the picker must offer them. A config
    alias of the same name is listed once, as the alias:
    dispatch resolves aliases first, so the alias is what would answer.
    """
    cfg = KiroCrewConfig.load()
    # Caller class, resolved once for the whole response. It decides only VALUE
    # treatment, never the key set -- see ``_roster_mask``.
    #
    # The OWNER predicate, not an app-token check. `request.get("app", "")` alone
    # asks "is this an app?", and a non-owner DASHBOARD session answers no: an
    # allow-listed messaging user running `!dashboard` holds a dashboard token
    # with `app == ""` and would have sailed through, which is the same
    # caller-class hole that keeps reappearing when this question is hand-rolled
    # per class instead of delegated to the one predicate that already answers
    # it. `is_owner_dashboard_request` is what `_require_owner` resolves to for
    # the mutating agents routes, so the read and write sides now agree
    # on who the owner is.
    #
    # Fails CLOSED -- treated as NOT the owner, so masked -- when the app carries
    # no state to resolve an owner against. The predicate subscripts
    # `app["state"]`, and for a disclosure control "unknown caller" must mean
    # "mask", not "show".
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    redact = request.app.get("state") is None or not is_owner_dashboard_request(request)
    agents = [
        _agent_roster_row(name, "global", agent_cfg, redact=redact)
        for name, agent_cfg in cfg.agents.items()
    ]

    state: DashboardState | None = request.app.get("state")

    # Project rows come from a directory scan, so it runs on the discovery
    # pool — same rule as every other agent listing: no filesystem I/O on the
    # event loop. Failure costs only the project rows, never the roster.
    project_dir = active_project_dir(state, _read_session_key(request)) if state else ""
    if project_dir:
        try:
            project_names = await asyncio.get_running_loop().run_in_executor(
                discovery_executor(),
                functools.partial(
                    project_agent_names,
                    project_dir,
                    operation="api_kirocrew_agents",
                    source="dashboard",
                ),
            )
        except Exception:
            logger.warning("Failed to list project agents for %s", project_dir, exc_info=True)
            project_names = frozenset()
        # One shared default record for every project row — they carry no
        # per-agent config of their own (nothing on disk to read without a
        # second scan), so the row is the default record under a project tag.
        project_default = KiroCrewAgentConfig()
        agents.extend(
            _agent_roster_row(name, "project", project_default, redact=redact)
            for name in sorted(project_names - set(cfg.agents.keys()))
        )

    # Reorder by usage frequency (most-used first). Derived read-only from chat
    # history; degrade to config-insertion order on any failure so the dropdown
    # never breaks or drops agents when history is unreadable.
    conversation_log = state.conversation_log if state else None
    if conversation_log:
        try:
            usage = await asyncio.to_thread(conversation_log.agent_usage)
            # Default missing agents to (0, 0) — keeps the sort key total and
            # deterministic (never negates None); never-used agents collapse to
            # their config-insertion index and form a stable bottom block.
            sorted_agents = sorted(
                enumerate(agents),
                # ``str(...)`` because the row's value type widened to ``object``
                # for ``avatar`` (the one structured field); ``name`` is always a
                # ``str`` -- masked or verbatim, ``_roster_mask`` returns one.
                key=lambda item: (
                    -usage.get(str(item[1]["name"]), (0, 0.0))[0],
                    -usage.get(str(item[1]["name"]), (0, 0.0))[1],
                    item[0],
                ),
            )
            agents = [a for _, a in sorted_agents]
        except Exception:
            logger.warning("Failed to sort agents by usage; using config order", exc_info=True)

    return web.json_response(
        {
            "agents": agents,
            "default_agent": cfg.default_agent,
        }
    )


_config_lock = LoopBoundLock()


def _get_config_lock() -> LoopBoundLock:
    """Return the config lock (loop-bound; rebinds when the running loop changes)."""
    return _config_lock


def _pin_entitlement_backend(cfg: Any) -> str:
    """The harness whose live catalog may judge a crew's model pin.

    Every agent the crew create and update handlers write is a Crew Member whose DM slot
    (``member-<slug>``) routes through ``agent.member_acp_backend`` — not
    through the configured default harness ``agent.acp_backend``. When the two
    share a model-registry namespace, the default backend scopes the
    entitlement evidence correctly (kiro, including the empty default backend,
    and kas share ``acp``). When they do not, the default's catalog cannot
    establish whether the pin the DM thread will actually run is usable — a
    live kiro session's catalog would deterministically reject a
    claude-advertised id — so the evidence must come from the harness the DM
    thread will ACTUALLY run on, which is ``member_backend``. Returning it (not
    ``None``) keeps the scope on the member's own namespace: a provider from an
    unrelated harness can neither admit nor reject the pin, and when no member
    -namespace provider is live the catalog is simply unknown (fail-open) rather
    than judged by the wrong backend's advertised ids.
    """
    default_backend = getattr(cfg.agent, "acp_backend", "")
    member_backend = getattr(cfg.agent, "member_acp_backend", "")
    if (
        capabilities_for(member_backend).model_id_namespace
        != capabilities_for(default_backend).model_id_namespace
    ):
        return member_backend
    return default_backend


async def _revalidate_crew_pin(model: str, request: web.Request) -> str | None:
    """Revalidate the snapshot a crew's model pin is about to be judged by.

    Awaited BEFORE the handlers take the config lock: :func:`_model_pin_rejected`
    runs synchronously inside it and cannot probe, and a probe must not hold the
    lock for its read deadline. Resolves the same entitlement backend the locked
    check will use and hands off to the role-pin revalidation, which heals the
    live snapshot in place. Returns a denial reason only while that probe is still
    in flight past its deadline; ``None`` otherwise.

    Skips the cases the locked check answers without the live list -- inherit,
    the retained claude_code seam and a known wrong-flavour spelling -- so no probe
    is spent on a value the snapshot does not decide.
    """
    if not model or model == "auto" or model_registry.acp_id_correction(model):
        return None
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if is_claude_code(cfg.agent.provider):
        return None
    # circular import: see _model_pin_rejected.
    from kiro_crew.dashboard.handlers.core import _revalidate_role_pin_evidence

    return await _revalidate_role_pin_evidence(
        model, request, backend=_pin_entitlement_backend(cfg)
    )


def _model_pin_rejected(
    model: str, request: web.Request, provider: str, *, backend: str | None = None
) -> str | None:
    """Reason a crew's model pin is unusable, or ``None`` to allow it.

    An agent's ``model`` is read by kiro-cli when the child starts, so a pin the
    account cannot serve kills every session and subagent using that agent
    seconds after spawn, before anything can inspect it. Rejecting it here — at
    the one moment a human is looking at the value — turns that into a single
    message on the surface that authored it.

    *provider* is passed in rather than resolved here so this whole path adds no
    config read of its own: every caller already holds a loaded config, and
    ``KiroCrewConfig.load()`` deep-copies the validated dict even on a cache
    hit — work that must not land on the event loop while the config lock is
    held. It is forwarded to the validator for the same reason.

    A known wrong-flavour registry spelling is reported before entitlement: a
    live advertised set would otherwise replace the actionable ACP-id mapping
    with a generic "not available" error. All other values delegate to the
    per-role validator so the crew form, the role pins and the session-init
    withhold apply one predicate. ``""``/``"auto"`` mean inherit and always
    pass; an unknown advertised set means entitlement is unknowable, and the
    validator accepts rather than accusing on no evidence.
    """
    # The retained claude_code seam accepts canonical and registered Bedrock
    # wire ids that the ACP correction and advertised-id comparison below
    # intentionally map away from. Its entitlement guard lives in its own
    # provider path, where full configured ids and bare advertised ids can be
    # canonicalized before comparison.
    if is_claude_code(provider):
        return None

    # The registry knows each model under several spellings and only one is what
    # kiro-cli serves; the others reach the child verbatim and kill it at startup.
    # Check this before live entitlement because a wrong-flavour id is naturally
    # absent from that set and would otherwise produce a less actionable error.
    correction = model_registry.acp_id_correction(model)
    if correction:
        # Deliberately NOT prescriptive. Upstream naming does not line up across
        # providers — Bedrock's ``claude-opus-4-8`` is the registry's
        # ``claude-opus-4.5``, while ``claude-opus-4-8[1m]`` is ``claude-opus-4.8``
        # — so a user who typed the Bedrock spelling meaning "Opus 4.8" may not
        # want the id this maps to. Telling them to adopt it would steer a
        # plausible-intent user into a quieter capability change than the one
        # they asked for. Report the mapping, show what is actually served, and
        # let them choose.
        served = ", ".join(model_registry.available_models("acp")[:8]) or "auto"
        return (
            f"{model!r} is not a model kiro-cli serves. The registry maps that "
            f"spelling to {correction!r} — confirm that is the model you want, or "
            f"pick one of: {served}, or 'auto'."
        )
    # circular import: handlers.core resolves _get_config_lock from this module,
    # so importing it at module scope would close the cycle.
    from kiro_crew.dashboard.handlers.core import _validate_role_model

    return _validate_role_model(model, request, provider=provider, backend=backend)


# ── Per-crew uploaded avatars ────────────────────────────────────────
#
# The "image" tier of per-crew custom avatars: the picture lives as a file
# under the data home, the config field only records `{"kind": "image"}`
# (see config.sections._safe_avatar). Serving goes through the authenticated API —
# never a raw filesystem path — so remote dashboards work unchanged.

#: Accepted image formats, sniffed from magic bytes — the client-sent
#: Content-Type is attacker-controlled and is deliberately ignored. The set
#: itself lives in the config loader, which validates the ``ext`` pin the
#: committed config carries against the same vocabulary.
_AVATAR_IMAGE_EXTS = _LOADER_AVATAR_IMAGE_EXTS
_AVATAR_CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
#: Upload ceiling. The client downscales to 512px before upload, so a
#: compliant upload is tens of KB; 1 MB tolerates a generous margin while
#: keeping a hostile body from ballooning memory (parts accumulate in RAM).
_AVATAR_MAX_BYTES = 1024 * 1024


# ``asyncio.to_thread`` that a cancellation cannot abandon mid-mutation --
# moved to chat_utils so the files handler's staging copy can share the one
# implementation; the local name is kept for its callers.
_drained_to_thread = drained_to_thread


async def api_kirocrew_agent_avatar_get(request: web.Request) -> web.Response:
    """GET /api/agents/{name}/avatar — serve the crew's uploaded picture.

    Owner-gated and SEL-audited like its POST/DELETE peers. ``ETag`` derives
    from the bytes served, so a replaced picture invalidates even when size
    and second-granularity mtime coincide.
    """
    denied = await _require_owner(request, "agent.avatar_get")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    # The file is served only while the crew's config actually selects it —
    # a leftover file after an out-of-band config edit or a failed cleanup
    # must not remain silently retrievable. The file pin narrows that further:
    # only the exact committed file is served, so an uncommitted install left
    # by a mid-save crash cannot impersonate the saved picture.
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    agent = cfg.agents.get(name)
    if agent is None or agent.avatar.get("kind") != "image":
        return web.json_response(
            {"error": "no uploaded avatar", "code": "avatar_not_found"}, status=404
        )
    path = await asyncio.to_thread(_live_avatar_file, name, agent.avatar.get("file"))
    if path is None:
        return web.json_response(
            {"error": "no uploaded avatar", "code": "avatar_not_found"}, status=404
        )
    data = await asyncio.to_thread(_read_avatar_file, path)
    if data is None:
        return web.json_response(
            {"error": "no uploaded avatar", "code": "avatar_not_found"}, status=404
        )
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.avatar_get",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return conditional_response(
        request,
        data,
        _AVATAR_CONTENT_TYPES[path.suffix.lstrip(".")],
        etag=strong_content_etag(data),
        cache_control="private, max-age=0, must-revalidate",
    )


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.dashboard.handlers.agents.<name>`` reaches it wherever it lives;
# see ``kiro_crew.dashboard.agent_admin``. Run once, after this body has bound every
# name.
_agent_admin.compose(
    globals(),
    (
        _owner_app_mcp_ownership,
        _owner_agent_config,
        _owner_default_agent,
        _owner_capabilities,
        _owner_template_lineage,
        _owner_fork_publish,
        _owner_agent_detail,
        _owner_roster,
        _owner_installed_agents,
        _owner_crew_records,
        _owner_crew_update,
        _owner_crew_removal,
        _owner_avatars,
    ),
)
